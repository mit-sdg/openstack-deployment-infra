"""The Node.js or Bun version an app's repository asks for.

An app asks in its root package.json: ``engines.node``, ``engines.bun``, or
``"packageManager": "bun@x.y.z"``. Without one, a root ``.nvmrc`` or
``.node-version`` (Node.js) or ``.bun-version`` (Bun) file is read. Ranges use
a subset of npm's semver syntax: exact and partial versions, x-ranges, ``^``,
``~``, comparators, hyphen ranges, a space for AND and ``||`` for OR. Only
releases are ever chosen; a pre-release tag in a range only moves its bound.

The helper resolves a request to an exact release and image when it builds
(helper/runtime_images.py); an app that asks for nothing keeps the policy's
pinned image. The owner portal's deploy check parses requests the same way
(frontend/owner-portal/src/utils/runtimeVersions.ts), and both test suites run
the shared runtime-version-cases.json.

The management release ships this module with deployment_config.py, so it
imports nothing but validation.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from .config import RuntimeImages
from .validation import ValidationError, oci_digest_pin

RUNTIME_NAMES = {"node": "Node.js", "bun": "Bun"}
# Builds use official images only, as their -slim variant pinned by digest.
IMAGE_REPOSITORIES = {"node": "docker.io/library/node", "bun": "docker.io/oven/bun"}
IMAGE_VARIANT = "slim"
# The oldest release line a request may name, as (major, minor). Node.js lines
# before 20 are end-of-life; a request for one is refused rather than built.
OLDEST_LINES = {"node": (20, 0), "bun": (1, 1)}
VERSION_FILES = {"node": (".nvmrc", ".node-version"), "bun": (".bun-version",)}
VERSION_FILE_BYTES = 1_024
MAXIMUM_REQUEST_LENGTH = 256
DEFAULT_SOURCE = "default"
_EXAMPLES = {"node": ">=22", "bun": "^1.3"}


class RuntimeVersionError(ValidationError):
    """A version request the build can't honour; the message is for the owner."""


# A version as (major, minor, patch, release). release is 0 for a pre-release,
# which sorts just below the release it precedes, and 1 for the release itself.
_Key = tuple[int, int, int, int]
# Larger than any accepted component, which has at most nine digits.
_UNBOUNDED = 1_000_000_000
_NUMBER = r"0|[1-9][0-9]{0,8}"
_PART = rf"{_NUMBER}|[xX*]"
_IDENTIFIERS = r"[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*"
_PARTIAL = re.compile(
    rf"v?({_PART})(?:\.({_PART})(?:\.({_PART})(-{_IDENTIFIERS})?(?:\+{_IDENTIFIERS})?)?)?"
)
_EXACT = re.compile(rf"({_NUMBER})\.({_NUMBER})\.({_NUMBER})")
_COMPARATOR = re.compile(r"(\^|~>?|>=|<=|>|<|=)?(.+)")
_OPERATOR_SPACE = re.compile(r"(\^|~>?|[<>]=?|=) +")
_HYPHEN = re.compile(r"([^ ]+) - ([^ ]+)")
_RANGE_CHARACTERS = re.compile(r"[0-9A-Za-z.+*~^<>=| -]*")
_ALIAS = re.compile(r"[A-Za-z][A-Za-z0-9/*_.-]*")
_QUOTABLE = re.compile(r"[ !#-\[\]-~]{1,64}")


@dataclass(frozen=True, slots=True)
class _Bound:
    key: _Key
    inclusive: bool


@dataclass(frozen=True, slots=True)
class _Interval:
    """One comparator set: every npm set is a single interval of versions."""

    low: _Bound | None = None
    high: _Bound | None = None

    def admits(self, key: _Key) -> bool:
        low, high = self.low, self.high
        return (low is None or key > low.key or (low.inclusive and key == low.key)) and (
            high is None or key < high.key or (high.inclusive and key == high.key)
        )

    def intersect(self, other: _Interval) -> _Interval:
        lows = [bound for bound in (self.low, other.low) if bound is not None]
        highs = [bound for bound in (self.high, other.high) if bound is not None]
        # At an equal key, an exclusive bound is the tighter one.
        return _Interval(
            max(lows, key=lambda bound: (bound.key, not bound.inclusive)) if lows else None,
            min(highs, key=lambda bound: (bound.key, bound.inclusive)) if highs else None,
        )

    def lowest(self) -> _Key | None:
        """The lowest release in this interval, if it has any."""
        if self.low is None:
            key = (0, 0, 0, 1)
        else:
            major, minor, patch, release = self.low.key
            bump = 1 if release and not self.low.inclusive else 0
            key = (major, minor, patch + bump, 1)
        return key if self.admits(key) else None

    def highest(self) -> _Key:
        """The highest release in a non-empty interval; parts may be unbounded."""
        if self.high is None:
            return (_UNBOUNDED, _UNBOUNDED, _UNBOUNDED, 1)
        major, minor, patch, release = self.high.key
        if self.high.inclusive and release:
            return (major, minor, patch, 1)
        if patch:
            return (major, minor, patch - 1, 1)
        if minor:
            return (major, minor - 1, _UNBOUNDED, 1)
        return (major - 1, _UNBOUNDED, _UNBOUNDED, 1)


_ANY = _Interval()
_NOTHING = _Interval(high=_Bound((0, 0, 0, 0), False))


@dataclass(frozen=True, slots=True)
class VersionRange:
    """An npm-style range: any of its comparator sets may match."""

    alternatives: tuple[_Interval, ...]

    def admits(self, version: tuple[int, int, int]) -> bool:
        key = (*version, 1)
        return any(item.admits(key) for item in self.alternatives)


def parse_version(value: str) -> tuple[int, int, int] | None:
    """An exact release version such as 22.11.0, without a prefix or tag."""
    match = _EXACT.fullmatch(value)
    if match is None:
        return None
    return int(match[1]), int(match[2]), int(match[3])


def _partial(value: str) -> tuple[tuple[int, ...], bool] | None:
    """Leading numeric parts and whether a pre-release tag follows them."""
    match = _PARTIAL.fullmatch(value)
    if match is None:
        return None
    parts: list[int] = []
    wildcard = False
    for item in (match[1], match[2], match[3]):
        if item is None:
            break
        if item in {"x", "X", "*"}:
            wildcard = True
        elif wildcard:
            return None  # 1.x.3 names no version range.
        else:
            parts.append(int(item))
    prerelease = match[4] is not None
    if prerelease and len(parts) < 3:
        return None
    return tuple(parts), prerelease


def _start(parts: tuple[int, ...], prerelease: bool) -> _Key:
    """The first version a partial names: 1.2 is 1.2.0-0, 1.2.3 is itself."""
    if len(parts) == 3:
        return (parts[0], parts[1], parts[2], 0 if prerelease else 1)
    return (parts[0], parts[1] if len(parts) > 1 else 0, 0, 0)


def _after(parts: tuple[int, ...]) -> _Key:
    """The first version after a partial: 1 is 2.0.0-0, 1.2 is 1.3.0-0."""
    if len(parts) == 1:
        return (parts[0] + 1, 0, 0, 0)
    if len(parts) == 2:
        return (parts[0], parts[1] + 1, 0, 0)
    return (parts[0], parts[1], parts[2] + 1, 0)


def _comparator(token: str) -> _Interval | None:
    match = _COMPARATOR.fullmatch(token)
    parsed = None if match is None else _partial(match[2])
    if match is None or parsed is None:
        return None
    operator = match[1] or "="
    parts, prerelease = parsed
    if not parts:
        return _NOTHING if operator in {"<", ">"} else _ANY
    start = _start(parts, prerelease)
    exact = len(parts) == 3
    major = parts[0]
    if operator == "=":
        if exact:
            return _Interval(_Bound(start, True), _Bound(start, True))
        return _Interval(_Bound(start, True), _Bound(_after(parts), False))
    if operator == ">=":
        return _Interval(low=_Bound(start, True))
    if operator == ">":
        return _Interval(low=_Bound(start, False) if exact else _Bound(_after(parts), True))
    if operator == "<":
        return _Interval(high=_Bound(start, False))
    if operator == "<=":
        return _Interval(high=_Bound(start, True) if exact else _Bound(_after(parts), False))
    if operator in {"~", "~>"}:
        return _Interval(_Bound(start, True), _Bound(_after(parts[:2]), False))
    # Caret: the first nonzero part, or the last given one, may not change.
    if major or len(parts) == 1:
        high = (major + 1, 0, 0, 0)
    elif parts[1] or len(parts) == 2:
        high = (0, parts[1] + 1, 0, 0)
    else:
        high = (0, 0, parts[2] + 1, 0)
    return _Interval(_Bound(start, True), _Bound(high, False))


def _hyphen(first: str, last: str) -> _Interval | None:
    low, high = _partial(first), _partial(last)
    if low is None or high is None:
        return None
    return _Interval(
        _Bound(_start(*low), True) if low[0] else None,
        None
        if not high[0]
        else _Bound(_start(*high), True)
        if len(high[0]) == 3
        else _Bound(_after(high[0]), False),
    )


def parse_range(value: str) -> VersionRange | None:
    """Parse the supported subset of npm's range syntax; None if it isn't one."""
    if len(value) > MAXIMUM_REQUEST_LENGTH or not _RANGE_CHARACTERS.fullmatch(value):
        return None
    alternatives: list[_Interval] = []
    for alternative in value.split("||"):
        words = " ".join(word for word in alternative.split(" ") if word)
        hyphen = _HYPHEN.fullmatch(words)
        if hyphen is not None:
            interval = _hyphen(hyphen[1], hyphen[2])
        else:
            interval = _ANY
            for token in _OPERATOR_SPACE.sub(r"\1", words).split(" "):
                if not token:
                    continue
                comparator = _comparator(token)
                if comparator is None:
                    return None
                interval = interval.intersect(comparator)
        if interval is None:
            return None
        alternatives.append(interval)
    return VersionRange(tuple(alternatives))


def _quoted(value: str) -> str:
    """The value in quotes after a space, when it is short plain text."""
    return f' "{value}"' if _QUOTABLE.fullmatch(value) else ""


def _line(runtime: str, key: tuple[int, ...]) -> str:
    """A release line: Node.js by major version, Bun by minor version."""
    if runtime == "node":
        return f"Node.js {key[0]}"
    return f"Bun {key[0]}.{'x' if key[1] == _UNBOUNDED else key[1]}"


def oldest_supported(runtime: str) -> str:
    major, minor = OLDEST_LINES[runtime]
    return str(major) if runtime == "node" else f"{major}.{minor}"


@dataclass(frozen=True, slots=True)
class RuntimeRequest:
    """A checked request: it names a version no older than the oldest line."""

    runtime: str
    # Where the request is: engines.node, packageManager, .nvmrc, ...
    field: str
    # As written there, such as ">=22 <23", "bun@1.3.4" or "lts/*".
    value: str
    # The version part of value: "1.3.4" for "bun@1.3.4".
    wanted: str
    # None asks for the newest long-term support release (lts/*).
    versions: VersionRange | None

    @property
    def source(self) -> str:
        return f"{self.field} {self.value}"

    def describe(self) -> str:
        return f"{RUNTIME_NAMES[self.runtime]} {self.wanted} from {self.field}"


def _checked(request: RuntimeRequest) -> RuntimeRequest:
    assert request.versions is not None
    alternatives = request.versions.alternatives
    lowest = [key for item in alternatives if (key := item.lowest()) is not None]
    name = RUNTIME_NAMES[request.runtime]
    if not lowest:
        raise RuntimeVersionError(
            f"{request.field}{_quoted(request.value)} doesn't match any {name} version."
        )
    floor = _Interval(low=_Bound((*OLDEST_LINES[request.runtime], 0, 0), True))
    if any(item.intersect(floor).lowest() is not None for item in alternatives):
        return request
    newest = _line(
        request.runtime, max(item.highest() for item in alternatives if item.lowest() is not None)
    )
    asks = newest if _line(request.runtime, min(lowest)) == newest else f"{newest} or earlier"
    raise RuntimeVersionError(
        f"{request.field}{_quoted(request.value)} asks for {asks}, older than the oldest "
        f"supported version ({oldest_supported(request.runtime)})."
    )


def unmatched(request: RuntimeRequest) -> RuntimeVersionError:
    """No published release satisfies a request; only a lookup can tell."""
    return RuntimeVersionError(
        f"No {RUNTIME_NAMES[request.runtime]} release matches "
        f"{request.field}{_quoted(request.value)}."
    )


def _range_request(runtime: str, field: str, value: str) -> RuntimeRequest:
    if len(value) > MAXIMUM_REQUEST_LENGTH:
        raise RuntimeVersionError(f"{field} is longer than {MAXIMUM_REQUEST_LENGTH} characters.")
    versions = parse_range(value)
    if versions is not None:
        return _checked(RuntimeRequest(runtime, field, value, value, versions))
    if runtime == "node" and field in VERSION_FILES["node"] and _ALIAS.fullmatch(value):
        raise RuntimeVersionError(
            f"{field} asks for{_quoted(value)}. Use a version number or range, or lts/*."
        )
    raise RuntimeVersionError(f"{field}{_quoted(value)} isn't a valid version range.")


def version_file_request(text: str) -> str:
    """A version file's request: its first line that isn't blank or a comment."""
    for line in text.removeprefix("﻿").split("\n"):
        value = line.split("#", 1)[0].strip(" \t\r\v\f")
        if value:
            return value
    return ""


def runtime_request(
    runtime: str,
    package: Mapping[str, Any],
    read_file: Callable[[str], str | None],
) -> RuntimeRequest | None:
    """The version a checkout asks for, or None to use the platform default.

    package is the root package.json. read_file returns a root version file's
    text, or None when there is no such file; it raises RuntimeVersionError
    when the file can't be used. Requests that can never be built raise it too.
    """
    if runtime not in RUNTIME_NAMES:
        raise ValidationError("runtime must be node or bun")
    manager = package.get("packageManager")
    # packageManager naming npm, pnpm or yarn is about installs, not the runtime.
    if runtime == "bun" and isinstance(manager, str) and manager.startswith("bun@"):
        wanted = manager.removeprefix("bun@").split("+", 1)[0]
        version = parse_version(wanted)
        if version is None:
            raise RuntimeVersionError(
                f"packageManager{_quoted(manager)} must name an exact Bun version, like bun@1.3.4."
            )
        exact = _Interval(_Bound((*version, 1), True), _Bound((*version, 1), True))
        return _checked(
            RuntimeRequest(
                runtime, "packageManager", f"bun@{wanted}", wanted, VersionRange((exact,))
            )
        )
    engines = package.get("engines")
    field = f"engines.{runtime}"
    if isinstance(engines, dict) and runtime in engines:
        value = engines[runtime]
        if not isinstance(value, str):
            raise RuntimeVersionError(
                f'{field} must be a version range in quotes, like "{_EXAMPLES[runtime]}".'
            )
        return _range_request(runtime, field, value)
    for name in VERSION_FILES[runtime]:
        text = read_file(name)
        value = "" if text is None else version_file_request(text)
        if not value:
            continue
        if runtime == "node" and value == "lts/*":
            return RuntimeRequest(runtime, name, value, value, None)
        return _range_request(runtime, name, value)
    return None


@dataclass(frozen=True, slots=True)
class ResolvedRuntime:
    """The runtime one build used: an exact release, or the policy default."""

    runtime: str
    # None for the policy default, whose release isn't recorded in policy.
    version: str | None
    image: str
    source: str

    def evidence(self) -> dict[str, str | None]:
        return {
            "runtime": self.runtime,
            "version": self.version,
            "image": self.image,
            "source": self.source,
        }

    def runtime_images(self, defaults: RuntimeImages) -> RuntimeImages:
        """The policy pins with this build's runtime image in its place."""
        return replace(defaults, **{self.runtime: self.image})

    def describe(self) -> str:
        name = RUNTIME_NAMES[self.runtime]
        if self.version is None:
            return f"Using the platform's default {name} image ({self.image})."
        return f"Using {name} {self.version} ({self.image}) from {self.source}."


def default_runtime(runtime: str, defaults: RuntimeImages) -> ResolvedRuntime:
    if runtime not in RUNTIME_NAMES:
        raise ValidationError("runtime must be node or bun")
    return ResolvedRuntime(runtime, None, getattr(defaults, runtime), DEFAULT_SOURCE)


_SOURCE_FIELDS = {
    "node": ("engines.node", *VERSION_FILES["node"]),
    "bun": ("packageManager", "engines.bun", *VERSION_FILES["bun"]),
}


def resolved_runtime(value: object, *, runtime: str, defaults: RuntimeImages) -> ResolvedRuntime:
    """Check a build's reported runtime against what it could have resolved.

    The default must be exactly the policy pin. Anything else must be an exact
    supported release, pinned by digest in the official repository, and must
    satisfy the request its source names.
    """
    if not isinstance(value, dict) or set(value) != {"runtime", "version", "image", "source"}:
        raise ValidationError("resolved runtime must name runtime, version, image and source")
    if value["runtime"] != runtime:
        raise ValidationError("resolved runtime differs from the configured runtime")
    default = default_runtime(runtime, defaults)
    if value["source"] == DEFAULT_SOURCE:
        if value["version"] is not None or value["image"] != default.image:
            raise ValidationError("default runtime must be the policy image")
        return default
    version_text, image, source = value["version"], value["image"], value["source"]
    version = parse_version(version_text) if isinstance(version_text, str) else None
    if version is None or version[:2] < OLDEST_LINES[runtime]:
        raise ValidationError("resolved runtime version is not a supported release")
    repository = IMAGE_REPOSITORIES[runtime]
    if not oci_digest_pin(image, field="resolved runtime image").startswith(f"{repository}@"):
        raise ValidationError("resolved runtime image is outside its official repository")
    if not isinstance(source, str) or len(source) > MAXIMUM_REQUEST_LENGTH + 32:
        raise ValidationError("resolved runtime source is malformed")
    field, _, requested = source.partition(" ")
    if field not in _SOURCE_FIELDS[runtime] or not requested:
        raise ValidationError("resolved runtime source is malformed")
    if requested != "lts/*" or field not in VERSION_FILES["node"]:
        package: dict[str, Any] = {}
        files: dict[str, str] = {}
        if field == "packageManager":
            package["packageManager"] = requested
        elif field.startswith("engines."):
            package["engines"] = {runtime: requested}
        else:
            files[field] = requested
        request = runtime_request(runtime, package, files.get)
        if (
            request is None
            or request.source != source
            or request.versions is None
            or not request.versions.admits(version)
        ):
            raise ValidationError("resolved runtime version does not satisfy its source")
    return ResolvedRuntime(runtime, version_text, image, source)
