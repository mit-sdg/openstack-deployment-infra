"""Immutable application configuration and build value objects."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..validation import ValidationError

_RUNTIME_FILE_PATH = re.compile(r"[A-Za-z0-9._@+/-]{1,1024}")


def runtime_file_paths(value: object) -> tuple[str, ...]:
    """Validate a bounded, disjoint set of literal paths in the built /app tree.

    Accept tuples for direct Manifest callers as well as parsed JSON arrays.
    Do not resolve against the checkout: these paths may be produced by a build.
    Docker COPY expands variables and globs even in JSON form, so only a small
    literal ASCII alphabet is allowed. In particular, no path component may be
    empty, '.' or '..', except for the explicit whole-tree selection '.'.
    """
    if not isinstance(value, (list, tuple)) or not 1 <= len(value) <= 32:
        raise ValidationError("build runtimeFiles must contain from 1 through 32 paths")
    for path in value:
        if not isinstance(path, str) or not _RUNTIME_FILE_PATH.fullmatch(path):
            raise ValidationError("runtime file path must be 1-1024 literal ASCII path characters")
        if path != "." and any(part in {"", ".", ".."} for part in path.split("/")):
            raise ValidationError("runtime file path must be normalized and relative to /app")
    paths = tuple(sorted(value))
    if len(paths) != len(set(paths)):
        raise ValidationError("runtime file paths must be unique")
    for path in paths:
        if (path == "." and len(paths) > 1) or any(other.startswith(path + "/") for other in paths):
            raise ValidationError("runtime file paths must not overlap")
    return paths


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
    runtime_files: tuple[str, ...] | None = None


@dataclass(frozen=True, slots=True)
class Recipe:
    dockerfile: bytes = field(repr=False)
    sha256: str
