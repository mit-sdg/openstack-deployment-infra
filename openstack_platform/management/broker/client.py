"""Bounded fixed Unix project-socket client with no network fallback."""

from __future__ import annotations

import http.client
import socket
import threading
from pathlib import Path
from typing import Any

from ..common import canonical, strict_json


class ControllerUnavailable(RuntimeError):
    """An unknown controller outcome; the original intent remains journaled."""


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: Path, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self.path = path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(str(self.path))


class ProjectClient:
    def __init__(self, path: Path, timeout: float = 5.0, capacity: int = 4) -> None:
        self.path, self.timeout = path, timeout
        if not 1 <= capacity <= 128:
            raise ValueError("invalid local connection capacity")
        self.capacity = threading.BoundedSemaphore(capacity)

    def request(
        self,
        method: str,
        path: str,
        body: object = None,
        key: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        if (
            not path.startswith("/v1/")
            or path.startswith("/v1/admin/")
            or "\r" in path
            or "\n" in path
        ):
            raise ValueError("invalid project request")
        if not self.capacity.acquire(timeout=self.timeout):
            raise ControllerUnavailable("local connection capacity")
        connection = UnixConnection(self.path, self.timeout)
        try:
            supplied = {"Content-Type": "application/json", **(headers or {})}
            if key:
                supplied["Idempotency-Key"] = key
            connection.request(
                method,
                path,
                body=None if body is None else canonical(body).encode(),
                headers=supplied,
            )
            response = connection.getresponse()
            length = response.getheader("Content-Length")
            if (
                response.getheader("Transfer-Encoding")
                or length is None
                or not length.isdigit()
                or int(length) > 1_048_576
                or response.getheader("Content-Type") != "application/json"
            ):
                raise ControllerUnavailable("invalid controller framing")
            raw = response.read(int(length) + 1)
            if len(raw) != int(length):
                raise ControllerUnavailable("incomplete controller response")
            value = strict_json(raw)
            if not isinstance(value, dict):
                raise ControllerUnavailable("invalid controller result")
            return response.status, value
        except (OSError, ValueError, http.client.HTTPException) as error:
            raise ControllerUnavailable("controller request outcome unknown") from error
        finally:
            connection.close()
            self.capacity.release()
