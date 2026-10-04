"""Deploy keys for private repositories: owners and admins see the public half.

The helper creates and keeps each app's private key; it never leaves the
admin host. An access check runs `git ls-remote` with the key, so checks are
shared for a few seconds per app and run one at a time.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from ...controller.http import HttpError, Request, Response
from ...validation import ValidationError, commit
from ..common import digest
from .accounts import audit
from .client import ControllerUnavailable

if TYPE_CHECKING:
    from .api import Broker

SHARE_SECONDS = 10.0
WAIT_SECONDS = 4.0
PROBLEMS = {None, "key-refused", "not-found", "branch-missing", "unavailable"}


class SourceKeys:
    def __init__(self, broker: Broker, clock: Callable[[], float] = time.monotonic) -> None:
        self.broker = broker
        self.clock = clock
        self.lock = threading.Lock()
        self.checking = threading.BoundedSemaphore(1)
        self.checked: dict[str, tuple[float, str, dict[str, Any]]] = {}

    def routes(self, root: str) -> list[tuple[str, str, Any]]:
        return [
            ("GET", root + "/{app}/source-key", self.read),
            ("POST", root + "/{app}/source-key", self.create),
            ("POST", root + "/{app}/source-key/check", self.check),
            ("POST", root + "/{app}/source/commits", self.commits),
            ("POST", root + "/{app}/source/check", self.preflight),
        ]

    def controller(self, method: str, path: str, body: object = None) -> dict[str, Any]:
        status, result = self.broker.client.request(method, path, body)
        detail = result.get("error")
        if status == 404 and isinstance(detail, dict) and detail.get("code") == "NOT_FOUND":
            # A controller from before deploy keys has no such route.
            raise HttpError(
                409, "SOURCE_KEYS_UNAVAILABLE", "Private repositories aren't available yet."
            )
        if status != 200:
            raise ControllerUnavailable("deploy key request failed")
        return result

    @staticmethod
    def key_model(result: dict[str, Any]) -> dict[str, Any]:
        if result.get("present") is not True:
            return {"present": False}
        public = result.get("publicKey")
        fingerprint = result.get("fingerprint")
        if (
            not isinstance(public, str)
            or not public.startswith("ssh-ed25519 ")
            or len(public) > 256
            or not isinstance(fingerprint, str)
            or not fingerprint.startswith("SHA256:")
            or not isinstance(result.get("createdAt"), str)
        ):
            raise ControllerUnavailable("invalid deploy key")
        return {
            "present": True,
            "publicKey": public,
            "fingerprint": fingerprint,
            "createdAt": result["createdAt"],
        }

    def read(self, request: Request) -> Response:
        _user, app = self.broker.own(request)
        result = self.controller("GET", f"/v1/applications/{app['id']}/source-key")
        return Response(200, {"data": self.key_model(result)})

    def create(self, request: Request) -> Response:
        user, app = self.broker.own(request, mutation=True)
        body = {} if request.body is None else request.body
        if not isinstance(body, dict) or set(body) - {"replace"}:
            raise HttpError(400, "INVALID_REQUEST", "Unexpected deploy key fields.")
        replace = body.get("replace", False)
        if not isinstance(replace, bool):
            raise HttpError(400, "INVALID_FIELD", "Replace must be true or false.")
        result = self.controller(
            "POST", f"/v1/applications/{app['id']}/source-key", {"replace": replace}
        )
        now = self.broker.auth.clock()
        with self.broker.database.connect(write=True) as db:
            db.execute(
                "INSERT INTO audit(user_id,app_id,intent_id,action,created) VALUES(?,?,?,?,?)",
                (
                    user["id"],
                    app["id"],
                    None,
                    "source_key_replace" if replace else "source_key",
                    now,
                ),
            )
            if request.path.startswith("/v1/admin-apps/"):
                audit(
                    db,
                    user["id"],
                    app["user_id"],
                    "app_source_key",
                    {"applicationId": app["id"], "replace": replace},
                    now,
                )
        with self.lock:
            self.checked.pop(app["id"], None)
        return Response(200, {"data": self.key_model(result)})

    def check(self, request: Request) -> Response:
        """Whether GitHub accepts the key for the saved repository and branch."""
        _user, app = self.broker.own(request, mutation=True)
        if request.body not in (None, {}):
            raise HttpError(400, "INVALID_REQUEST", "Unexpected access check fields.")
        with self.broker.database.connect() as db:
            settings = db.execute(
                "SELECT repository,branch FROM configurations WHERE app_id=? ORDER BY revision DESC LIMIT 1",
                (app["id"],),
            ).fetchone()
        if settings is None:
            raise HttpError(409, "SETTINGS_REQUIRED", "Save the repository in settings first.")
        target = f"{settings['repository']}#{settings['branch']}"
        shared = self.recent(app["id"], target)
        if shared is not None:
            return Response(200, {"data": shared})
        if not self.checking.acquire(timeout=WAIT_SECONDS):
            raise HttpError(
                503,
                "CHECK_BUSY",
                "Another access check is running. Try again in a few seconds.",
                retryable=True,
            )
        try:
            shared = self.recent(app["id"], target)
            if shared is None:
                result = self.controller(
                    "POST",
                    f"/v1/applications/{app['id']}/source-key/check",
                    {"repository": settings["repository"], "branch": settings["branch"]},
                )
                shared = self.check_model(result, settings["branch"])
                with self.lock:
                    self.checked[app["id"]] = (self.clock(), target, shared)
            return Response(200, {"data": shared})
        finally:
            self.checking.release()

    def commits(self, request: Request) -> Response:
        return self.source_read(request, preflight=False)

    def preflight(self, request: Request) -> Response:
        return self.source_read(request, preflight=True)

    def source_read(self, request: Request, *, preflight: bool) -> Response:
        _user, app = self.broker.own(request, mutation=True)
        body = {} if request.body is None else request.body
        fields = {"commit", "configurationRevision"} if preflight else set()
        if not isinstance(body, dict) or set(body) != fields:
            raise HttpError(400, "INVALID_REQUEST", "Unexpected repository check fields.")
        with self.broker.database.connect() as db:
            settings = db.execute(
                "SELECT * FROM configurations WHERE app_id=? ORDER BY revision DESC LIMIT 1",
                (app["id"],),
            ).fetchone()
        if settings is None:
            raise HttpError(409, "SETTINGS_REQUIRED", "Save the repository in settings first.")
        values = {"repository": settings["repository"]}
        if preflight:
            if (
                type(body["configurationRevision"]) is not int
                or body["configurationRevision"] != settings["revision"]
            ):
                raise HttpError(
                    409,
                    "REVISION_CONFLICT",
                    "Settings changed. Reload before checking this commit.",
                )
            values.update(commit=commit(body["commit"]), configuration=settings["configuration"])
        else:
            values["branch"] = settings["branch"]
        mode = "check" if preflight else "commits"
        target = mode + ":" + digest(str(settings["revision"]) + str(values))
        if not self.checking.acquire(timeout=WAIT_SECONDS):
            raise HttpError(
                503,
                "CHECK_BUSY",
                "Another repository check is running. Try again in a few seconds.",
                retryable=True,
            )
        try:
            shared = self.recent(app["id"], target)
            if shared is None:
                status, result = self.broker.client.request(
                    "POST",
                    f"/v1/applications/{app['id']}/source/{mode}",
                    values,
                    timeout_seconds=30,
                )
                if status == 404:
                    raise HttpError(
                        409,
                        "SOURCE_READS_UNAVAILABLE",
                        "Private repository checks aren't available yet. The build checks your commit when you deploy.",
                    )
                if status != 200:
                    raise HttpError(
                        503,
                        "SOURCE_UNAVAILABLE",
                        "Couldn’t read this repository. Check deploy key access in Settings, then try again.",
                        retryable=True,
                    )
                if (
                    result.get("applicationId") != app["id"]
                    or type(result.get("keyPresent")) is not bool
                ):
                    raise ControllerUnavailable("invalid source identity")
                items = result.get("items", [])
                if not isinstance(items, list) or len(items) > (102 if preflight else 5):
                    raise ControllerUnavailable("invalid source items")
                allowed = (
                    {"id", "label", "state", "problem"}
                    if preflight
                    else {"sha", "message", "author", "date"}
                )
                shared = {
                    "keyPresent": result["keyPresent"],
                    "items": [
                        {key: value for key, value in item.items() if key in allowed}
                        for item in items
                        if isinstance(item, dict)
                    ],
                }
                with self.lock:
                    self.checked[app["id"]] = (self.clock(), target, shared)
            return Response(200, {"data": shared})
        finally:
            self.checking.release()

    def recent(self, app: str, target: str) -> dict[str, Any] | None:
        with self.lock:
            now = self.clock()
            for old in [
                key for key, (at, *_rest) in self.checked.items() if now - at >= SHARE_SECONDS
            ]:
                del self.checked[old]
            entry = self.checked.get(app)
            return entry[2] if entry is not None and entry[1] == target else None

    @staticmethod
    def check_model(result: dict[str, Any], branch: str) -> dict[str, Any]:
        if result.get("keyPresent") is False:
            return {"keyPresent": False}
        head = result.get("head")
        problem = result.get("problem")
        if (
            result.get("keyPresent") is not True
            or not isinstance(result.get("reachable"), bool)
            or problem not in PROBLEMS
            or not (head is None or isinstance(head, str))
        ):
            raise ControllerUnavailable("invalid access check")
        try:
            head = None if head is None else commit(head)
        except ValidationError:
            raise ControllerUnavailable("invalid commit in access check") from None
        return {
            "keyPresent": True,
            "reachable": result["reachable"],
            "head": head,
            "branch": branch,
            "problem": problem,
        }
