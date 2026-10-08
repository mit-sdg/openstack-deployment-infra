"""Closed size projections and optional controller sizing routes."""

from __future__ import annotations

import threading
import time
from typing import Any

from ...controller.http import HttpError
from ...validation import flavor_reference
from .client import ControllerUnavailable, ProjectClient


def result(client: ProjectClient, path: str) -> dict[str, Any]:
    status, value = client.request("GET", path, timeout_seconds=35)
    error = value.get("error")
    if status == 404 and isinstance(error, dict) and error.get("code") == "NOT_FOUND":
        raise HttpError(
            503,
            "SIZING_UNAVAILABLE",
            "Sizes are not available yet. Ask an admin to update the platform.",
        )
    if status != 200:
        raise ControllerUnavailable("size observation unavailable")
    return value


def flavor(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "flavor_id",
        "name",
        "vcpus",
        "ram_mib",
        "disk_gib",
    }:
        raise ControllerUnavailable("invalid flavor projection")
    try:
        flavor_reference(value["flavor_id"])
        flavor_reference(value["name"])
    except ValueError:
        raise ControllerUnavailable("invalid flavor identity") from None
    if (
        any(
            type(value[key]) is not int or not 0 <= value[key] <= 2**31 - 1
            for key in ("vcpus", "ram_mib", "disk_gib")
        )
        or value["vcpus"] < 1
    ):
        raise ControllerUnavailable("invalid flavor capacity")
    return value


def flavors(client: ProjectClient) -> list[dict[str, Any]]:
    value = result(client, "/v1/flavors")
    items = value.get("items")
    if not isinstance(items, list) or len(items) > 4096:
        raise ControllerUnavailable("invalid flavor list")
    return [flavor(item) for item in items]


def builder_body(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"flavor", "expectedFlavor"}:
        raise HttpError(400, "INVALID_REQUEST", "Supply a builder size and its current selection.")
    return {key: None if item is None else flavor_reference(item) for key, item in value.items()}


class FlavorCache:
    """Broker-wide, single-flight capacity reads with a ten-minute lifetime."""

    def __init__(self, client: ProjectClient) -> None:
        self.client = client
        self.lock = threading.Lock()
        self.items: list[dict[str, Any]] = []
        self.expires = 0.0
        self.error: ControllerUnavailable | HttpError | None = None

    def get(self) -> list[dict[str, Any]]:
        with self.lock:
            if time.monotonic() >= self.expires:
                try:
                    self.items = flavors(self.client)
                    self.error = None
                    self.expires = time.monotonic() + 600
                except (ControllerUnavailable, HttpError) as error:
                    self.items = []
                    self.error = error
                    # Failed reads also share a short cooldown.
                    self.expires = time.monotonic() + 30
            if self.error is not None:
                raise self.error
            return self.items

    def enrich(self, value: object) -> dict[str, Any] | None:
        if not isinstance(value, dict):
            return None
        enriched = dict(value)
        try:
            match = next(
                (
                    item
                    for item in self.get()
                    if value.get("workerFlavor") in {item["name"], item["flavor_id"]}
                ),
                None,
            )
        except (ControllerUnavailable, HttpError):
            return enriched
        if match is not None:
            enriched.update(vcpus=match["vcpus"], ram_mib=match["ram_mib"])
        return enriched
