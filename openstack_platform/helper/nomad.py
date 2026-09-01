"""Nomad Variable observations and owned-key compare-and-set updates."""

from __future__ import annotations

import json
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..validation import ValidationError, env_key, slug

OWNERS = frozenset({"platform", "staff"})
_STORAGE_OWNER = re.compile(r"storage\.(?:postgres|mongo|s3)\.[a-z][a-z0-9-]{0,39}")


class NomadError(RuntimeError):
    """A safe Nomad API failure."""


class CasConflict(NomadError):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        _req: object,
        _fp: object,
        _code: int,
        _msg: str,
        _headers: object,
        _newurl: str,
    ) -> None:
        return None


class SecretItems(Mapping[str, str]):
    """An in-memory mapping whose repr never renders values."""

    __slots__ = ("__items",)

    def __init__(self, values: Mapping[str, str]) -> None:
        self.__items = dict(values)

    def __getitem__(self, key: str) -> str:
        return self.__items[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.__items)

    def __len__(self) -> int:
        return len(self.__items)

    def __repr__(self) -> str:
        return f"<SecretItems keys={sorted(self.__items)!r}>"


@dataclass(frozen=True, slots=True)
class VariableSnapshot:
    path: str
    modify_index: int
    items: SecretItems = field(repr=False)

    @property
    def key_names(self) -> tuple[str, ...]:
        return tuple(sorted(self.items))


@dataclass(frozen=True, slots=True)
class VariableUpdate:
    path: str
    modify_index: int
    key_names: tuple[str, ...]


class VariableClient(Protocol):
    def read_variable(self, path: str) -> VariableSnapshot: ...

    def compare_and_set(self, path: str, expected_index: int, items: Mapping[str, str]) -> int: ...


def variable_path(application_slug: str) -> str:
    return f"nomad/jobs/{slug(application_slug)}"


def _validate_owner(owner: str) -> str:
    if owner not in OWNERS and _STORAGE_OWNER.fullmatch(owner) is None:
        raise ValidationError(f"unknown environment key owner {owner!r}")
    return owner


def merge_owned_items(
    current: Mapping[str, str],
    ownership: Mapping[str, str],
    *,
    owner: str,
    updates: Mapping[str, str] | None = None,
    removals: Sequence[str] = (),
    maximum_keys: int = 128,
    maximum_value_bytes: int = 65_536,
) -> SecretItems:
    """Merge one owner's keys while preserving every other value.

    Existing unowned values are never overwritten or removed.  The returned
    mapping redacts values from ``repr`` and should be passed directly to CAS.
    """
    owner = _validate_owner(owner)
    if maximum_keys <= 0 or maximum_value_bytes <= 0:
        raise ValueError("Nomad Variable limits must be positive")
    for key, key_owner in ownership.items():
        env_key(key)
        _validate_owner(key_owner)

    merged = dict(current)
    for key, value in merged.items():
        env_key(key)
        if not isinstance(value, str) or "\x00" in value:
            raise ValidationError(f"Nomad Variable key {key!r} has an invalid value")
        if len(value.encode()) > maximum_value_bytes:
            raise ValidationError(f"Nomad Variable value for {key!r} exceeds its size limit")

    removal_set = {env_key(key) for key in removals}
    set_values: dict[str, str] = {}
    for key, value in (updates or {}).items():
        key = env_key(key)
        if not isinstance(value, str) or "\x00" in value:
            raise ValidationError(f"environment value for {key!r} must be NUL-free text")
        if len(value.encode()) > maximum_value_bytes:
            raise ValidationError(f"environment value for {key!r} exceeds its size limit")
        set_values[key] = value
    overlap = removal_set & set_values.keys()
    if overlap:
        raise ValidationError(f"keys cannot be both set and removed: {', '.join(sorted(overlap))}")

    for key in removal_set | set_values.keys():
        recorded_owner = ownership.get(key)
        if key in current and recorded_owner is None:
            raise ValidationError(f"refusing to change existing unowned key {key!r}")
        if recorded_owner is not None and recorded_owner != owner:
            raise ValidationError(f"key {key!r} is owned by {recorded_owner}, not {owner}")
    for key in removal_set:
        merged.pop(key, None)
    merged.update(set_values)
    if len(merged) > maximum_keys:
        raise ValidationError(f"Nomad Variable would exceed its {maximum_keys}-key limit")
    return SecretItems(merged)


def update_owned_items(
    client: VariableClient,
    path: str,
    ownership: Mapping[str, str],
    *,
    owner: str,
    updates: Mapping[str, str] | None = None,
    removals: Sequence[str] = (),
    maximum_keys: int = 128,
    maximum_value_bytes: int = 65_536,
    attempts: int = 3,
) -> VariableUpdate:
    """Read/merge/write with ModifyIndex CAS and bounded conflict retries."""
    if attempts < 1 or attempts > 10:
        raise ValueError("CAS attempts must be from 1 through 10")
    for attempt in range(attempts):
        snapshot = client.read_variable(path)
        merged = merge_owned_items(
            snapshot.items,
            ownership,
            owner=owner,
            updates=updates,
            removals=removals,
            maximum_keys=maximum_keys,
            maximum_value_bytes=maximum_value_bytes,
        )
        try:
            index = client.compare_and_set(path, snapshot.modify_index, merged)
        except CasConflict:
            if attempt + 1 == attempts:
                raise
            continue
        return VariableUpdate(path=path, modify_index=index, key_names=tuple(sorted(merged)))
    raise AssertionError("bounded CAS loop did not return")


class NomadClient:
    """Small TLS Nomad Variable client.  Tokens and Items never appear in repr."""

    __slots__ = ("base_url", "__token", "ssl_context", "timeout_seconds", "response_limit")

    def __init__(
        self,
        base_url: str,
        *,
        token: str,
        ssl_context: ssl.SSLContext,
        timeout_seconds: float = 20,
        response_limit: int = 1_048_576,
    ) -> None:
        parsed = urllib.parse.urlsplit(base_url)
        if parsed.scheme != "https" or parsed.query or parsed.fragment:
            raise ValueError("Nomad base URL must be HTTPS without query or fragment")
        if not token or "\x00" in token:
            raise ValueError("Nomad token is missing or malformed")
        if timeout_seconds <= 0 or response_limit < 1:
            raise ValueError("Nomad bounds must be positive")
        self.base_url = base_url.rstrip("/")
        self.__token = token
        self.ssl_context = ssl_context
        self.timeout_seconds = timeout_seconds
        self.response_limit = response_limit

    def __repr__(self) -> str:
        return f"NomadClient(base_url={self.base_url!r})"

    def _request(
        self,
        method: str,
        path: str,
        *,
        expected_index: int | None = None,
        items: Mapping[str, str] | None = None,
    ) -> dict[str, Any] | None:
        if (
            not path.startswith("nomad/jobs/")
            or variable_path(path.removeprefix("nomad/jobs/")) != path
        ):
            raise ValidationError(
                "Nomad Variable path must belong to one validated application slug"
            )
        encoded_path = urllib.parse.quote(path, safe="/")
        url = f"{self.base_url}/v1/var/{encoded_path}"
        if expected_index is not None:
            url += "?" + urllib.parse.urlencode({"cas": expected_index})
        body = None
        if items is not None:
            try:
                body = json.dumps(
                    {"Path": path, "Items": dict(items)},
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode()
            except (TypeError, ValueError) as error:
                raise ValidationError("Nomad Variable contains non-JSON values") from error
        request = urllib.request.Request(
            url,
            data=body,
            method=method,
            headers={
                "Content-Type": "application/json",
                "X-Nomad-Token": self.__token,
            },
        )
        opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=self.ssl_context),
            _NoRedirect(),
        )
        try:
            with opener.open(request, timeout=self.timeout_seconds) as response:
                raw = response.read(self.response_limit + 1)
        except urllib.error.HTTPError as error:
            error.read(min(self.response_limit, 65_536))
            if error.code == 404 and method == "GET":
                return None
            if error.code == 409 and expected_index is not None:
                raise CasConflict("Nomad Variable changed concurrently") from None
            raise NomadError(f"Nomad Variable request failed with status {error.code}") from None
        except (urllib.error.URLError, TimeoutError) as error:
            raise NomadError(f"Nomad Variable request failed: {error.__class__.__name__}") from None
        if len(raw) > self.response_limit:
            raise NomadError("Nomad Variable response exceeded its size limit")
        try:
            result = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise NomadError("Nomad Variable response was malformed") from error
        if not isinstance(result, dict):
            raise NomadError("Nomad Variable response was not an object")
        return result

    def read_variable(self, path: str) -> VariableSnapshot:
        result = self._request("GET", path)
        if result is None:
            return VariableSnapshot(path=path, modify_index=0, items=SecretItems({}))
        if result.get("Path") != path:
            raise NomadError("Nomad returned an unexpected Variable path")
        index = result.get("ModifyIndex")
        items = result.get("Items")
        if (
            isinstance(index, bool)
            or not isinstance(index, int)
            or index < 0
            or not isinstance(items, dict)
        ):
            raise NomadError("Nomad Variable response omitted required metadata")
        validated: dict[str, str] = {}
        for key, value in items.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise NomadError("Nomad Variable Items were malformed")
            validated[key] = value
        return VariableSnapshot(path=path, modify_index=index, items=SecretItems(validated))

    def compare_and_set(self, path: str, expected_index: int, items: Mapping[str, str]) -> int:
        result = self._request("PUT", path, expected_index=expected_index, items=items)
        assert result is not None
        if result.get("Path") != path:
            raise NomadError("Nomad returned an unexpected Variable path")
        index = result.get("ModifyIndex")
        if isinstance(index, bool) or not isinstance(index, int) or index <= expected_index:
            raise NomadError("Nomad returned an invalid Variable ModifyIndex")
        return index
