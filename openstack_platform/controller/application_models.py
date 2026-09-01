"""Immutable application configuration and build value objects."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class StorageBinding:
    name: str
    resource_type: str
    environment: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class Manifest:
    runtime: str
    packages: tuple[str, ...]
    build_script: str | None
    start_script: str
    port: int
    health_path: str
    storage_bindings: tuple[StorageBinding, ...] = ()


@dataclass(frozen=True, slots=True)
class Recipe:
    dockerfile: bytes = field(repr=False)
    sha256: str
