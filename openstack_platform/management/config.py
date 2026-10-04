"""Closed non-secret management configuration; development remains loopback-only."""

from __future__ import annotations

import ipaddress
import json
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from .. import contracts
from ..config import load_platform
from .common import strict_json, text


def development_socket_path(path: Path) -> bool:
    """Allow worktree sockets or the harness's private, short temporary directory."""
    if path.resolve().is_relative_to((Path.cwd() / ".tmp").resolve()):
        return True
    parent = path.parent
    if (
        parent.parent != Path("/tmp")
        or not parent.name.startswith(f"owner-portal-sockets-{os.geteuid()}-")
        or parent.resolve() != parent
    ):
        return False
    try:
        metadata = parent.lstat()
    except FileNotFoundError:
        return False
    return (
        stat.S_ISDIR(metadata.st_mode)
        and metadata.st_uid == os.geteuid()
        and stat.S_IMODE(metadata.st_mode) == 0o700
    )


def socket_path_length(path: Path, name: str) -> None:
    length = len(os.fsencode(path))
    if length > 107:
        raise ValueError(f"{name} Unix socket path is {length} bytes; maximum is 107: {path}")


def management_peer(account: str) -> tuple[int, int]:
    value = json.loads(contracts._contract_bytes())["accounts"][account]
    return int(value["uid"]), int(value["gid"])


def management_web_peer() -> tuple[int, int]:
    return management_peer("managementWeb")


def origin(value: object, *, development: bool, allow_http: bool = True) -> str:
    checked = text(value, 256)
    parsed = urlsplit(checked)
    if (
        parsed.username
        or parsed.password
        or parsed.path
        or parsed.query
        or parsed.fragment
        or not parsed.hostname
    ):
        raise ValueError("origin must contain only scheme and authority")
    loopback = parsed.hostname == "localhost"
    try:
        loopback = loopback or ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        pass
    if parsed.scheme != "https" and not (
        allow_http and development and loopback and parsed.scheme == "http"
    ):
        raise ValueError("HTTPS origin required")
    if development and not loopback:
        raise ValueError("development origins must be loopback")
    if not development and loopback:
        raise ValueError("production must not use a development origin")
    if parsed.port is not None and not 1 <= parsed.port <= 65535:
        raise ValueError("invalid origin port")
    return checked


@dataclass(frozen=True)
class Config:
    portal_origin: str
    commons_origin: str
    state_directory: Path
    broker_socket: Path
    controller_socket: Path
    identity_socket: Path
    development: bool = False
    app_limit: int = 2
    concurrency_limit: int = 1
    absolute_seconds: int = 28800
    idle_seconds: int = 1800
    web_peer: tuple[int, int] = field(default_factory=management_web_peer)
    controller_timeout: float = 5.0
    trusted_ingress_peers: tuple[str, ...] = ()
    client_address_header: str | None = None
    anonymous_options_per_minute: int = 600
    anonymous_starts_per_minute: int = 400
    class_label: str = "class account"
    # Shown as the portal brand. Production uses the inventory's
    # ownerPortal.portalName, or "<displayName> Apps" without one.
    platform_name: str = "App platform"

    @property
    def issuer(self) -> str:
        return self.commons_origin

    @property
    def session_cookie(self) -> str:
        return (
            "portal-dev-session"
            if self.portal_origin.startswith("http:")
            else "__Host-portal-session"
        )

    @property
    def login_cookie(self) -> str:
        return (
            "portal-dev-anonymous"
            if self.portal_origin.startswith("http:")
            else "__Host-portal-anonymous"
        )

    @property
    def device_cookie(self) -> str:
        return (
            "portal-dev-device"
            if self.portal_origin.startswith("http:")
            else "__Host-portal-device"
        )

    @classmethod
    def load(cls, path: Path) -> Config:
        if path.is_symlink() or path.stat().st_size > 65536:
            raise ValueError("invalid management configuration file")
        value = strict_json(path.read_bytes())
        required = {
            "portalOrigin",
            "commonsOrigin",
            "stateDirectory",
            "brokerSocket",
            "controllerSocket",
            "identitySocket",
            "development",
        }
        optional = {
            "appLimit",
            "concurrencyLimit",
            "absoluteSeconds",
            "idleSeconds",
            "controllerTimeout",
            "trustedIngressPeers",
            "clientAddressHeader",
            "platformConfig",
            "anonymousOptionsPerMinute",
            "anonymousStartsPerMinute",
            "classLabel",
            "platformName",
        }
        if (
            not isinstance(value, dict)
            or not required <= set(value)
            or set(value) - required - optional
            or type(value["development"]) is not bool
        ):
            raise ValueError("invalid management configuration fields")
        development = value["development"]
        portal = origin(value["portalOrigin"], development=development)
        commons = origin(value["commonsOrigin"], development=development, allow_http=False)
        paths = []
        for name in ("stateDirectory", "brokerSocket", "controllerSocket", "identitySocket"):
            candidate = Path(value[name])
            if (
                not candidate.is_absolute()
                or str(candidate) != os.path.normpath(candidate)
                or candidate.is_symlink()
            ):
                raise ValueError("canonical absolute management paths required")
            if name != "stateDirectory":
                socket_path_length(candidate, name)
            if development:
                if name == "stateDirectory":
                    if not candidate.resolve().is_relative_to((Path.cwd() / ".tmp").resolve()):
                        raise ValueError("development state must be in this worktree .tmp")
                elif not development_socket_path(candidate):
                    raise ValueError(
                        "development sockets require .tmp or a private harness directory"
                    )
            paths.append(candidate)
        limits = []
        for name, default in (
            ("appLimit", 2),
            ("concurrencyLimit", 1),
            ("absoluteSeconds", 28800),
            ("idleSeconds", 1800),
            ("anonymousOptionsPerMinute", 600),
            ("anonymousStartsPerMinute", 400),
        ):
            v = value.get(name, default)
            if type(v) is not int or not 1 <= v <= 100000:
                raise ValueError("invalid management limit")
            limits.append(v)
        if limits[3] > limits[2] or limits[2] > 86400:
            raise ValueError("invalid session lifetimes")
        timeout = value.get("controllerTimeout", 5.0)
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (float, int))
            or not 0.05 <= timeout <= 120
        ):
            raise ValueError("invalid controller timeout")
        peers = value.get("trustedIngressPeers", [])
        if not isinstance(peers, list) or not all(isinstance(peer, str) for peer in peers):
            raise ValueError("invalid ingress peers")
        peers = [str(ipaddress.ip_address(peer)) for peer in peers]
        header = value.get("clientAddressHeader")
        platform_name = text(value.get("platformName", "App platform"), 120)
        if development:
            if header is not None:
                raise ValueError("development uses the actual loopback peer")
        else:
            if not isinstance(value.get("platformConfig"), str) or header != "cf-connecting-ip":
                raise ValueError("production requires platformConfig and explicit cf-connecting-ip")
            platform_path = Path(value["platformConfig"])
            if not platform_path.is_absolute():
                if platform_path.as_posix() != "platform.json":
                    raise ValueError("relative platform configuration must be platform.json")
                platform_path = path.parent / platform_path
            platform = load_platform(platform_path)
            from .settings import configuration

            rendered = configuration(platform, platform_path.absolute())
            if any(
                value.get(name, rendered[name]) != rendered[name]
                for name in rendered
                if name != "platformConfig"
            ):
                raise ValueError("management configuration differs from platform inventory")
            from ..owner_portal_config import validate as owner_portal

            portal_settings = owner_portal(platform.document.get("ownerPortal", {"enabled": False}))
            platform_name = portal_settings.get("portalName") or text(
                f"{text(platform.get('displayName'), 120)} Apps", 128
            )
            if value.get("platformName", platform_name) != platform_name:
                raise ValueError("platform name must match platform inventory")
            peer = str(ipaddress.ip_address(platform.get("addresses.ingress")))
            if peers and peers != [peer]:
                raise ValueError("trusted ingress must match platform inventory")
            peers = [peer]
        return cls(
            portal,
            commons,
            paths[0],
            paths[1],
            paths[2],
            paths[3],
            development=development,
            app_limit=limits[0],
            concurrency_limit=limits[1],
            absolute_seconds=limits[2],
            idle_seconds=limits[3],
            web_peer=(os.geteuid(), os.getegid()) if development else management_web_peer(),
            controller_timeout=float(timeout),
            trusted_ingress_peers=tuple(peers),
            client_address_header=header,
            anonymous_options_per_minute=limits[4],
            anonymous_starts_per_minute=limits[5],
            class_label=text(value.get("classLabel", "class account"), 80),
            platform_name=platform_name,
        )
