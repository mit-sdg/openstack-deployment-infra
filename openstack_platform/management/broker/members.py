"""Team apps: an app keeps one owner and can have members who work on it.

Members pass the same app gate as the owner (api.ACCESS), so they can change
settings, deploy, manage variables and storage and read logs. Only the owner or
an admin adds or removes people; a member can leave. The app counts against
the owner's quota only.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from ...controller.http import HttpError, Request, Response
from ...validation import ValidationError
from ...validation import uuid as checked_uuid
from ..common import utc
from .accounts import audit
from .journal import intent_model

if TYPE_CHECKING:
    from .api import Broker

MAXIMUM_MEMBERS = 10


class Members:
    def __init__(self, broker: Broker) -> None:
        self.broker = broker

    def routes(self, root: str) -> list[tuple[str, str, Any]]:
        return [
            ("GET", root + "/{app}/members", self.listing),
            ("POST", root + "/{app}/members", self.add),
            ("DELETE", root + "/{app}/members/{user}", self.remove),
        ]

    def people(self, app: dict[str, Any]) -> list[dict[str, Any]]:
        with self.broker.database.connect() as db:
            owner = db.execute(
                "SELECT id,username,display_name,issuer FROM users WHERE id=?", (app["user_id"],)
            ).fetchone()
            members = db.execute(
                "SELECT u.id,u.username,u.display_name,u.issuer,m.created FROM app_members m"
                " JOIN users u ON u.id=m.user_id WHERE m.app_id=? ORDER BY m.created,u.id",
                (app["id"],),
            ).fetchall()
        return [
            {
                "userId": row["id"],
                "username": row["username"],
                "displayName": row["display_name"],
                "method": "local" if row["issuer"] == "local" else "provider",
                "role": role,
                "addedAt": utc(row["created"]) if role == "member" else None,
            }
            for role, row in [("owner", owner), *[("member", row) for row in members]]
            if row is not None
        ]

    def listing(self, request: Request) -> Response:
        user, app = self.broker.own(request)
        return Response(
            200,
            {"data": {"items": self.people(app), "you": user["id"], "access": app["access"]}},
        )

    def add(self, request: Request) -> Response:
        user, app = self.broker.own(request, mutation=True)
        if app["access"] == "member":
            raise HttpError(403, "OWNER_ONLY", "Only the app’s owner can add people.")
        body = request.body
        if not isinstance(body, dict) or set(body) != {"username"}:
            raise HttpError(400, "INVALID_REQUEST", "Send the username to add.")
        name = body["username"]
        if not isinstance(name, str) or not 1 <= len(name) <= 64 or name != name.strip():
            raise HttpError(400, "INVALID_FIELD", "Enter a username.")
        now = time.time()
        with self.broker.database.connect(write=True) as db:
            # Usernames are unique per sign-in method; class accounts come first.
            candidates = db.execute(
                "SELECT id FROM users WHERE enabled=1 AND status='active' AND"
                " (username=? OR (issuer='local' AND username=? COLLATE NOCASE))"
                " ORDER BY issuer='local',id",
                (name, name),
            ).fetchall()
            if not candidates:
                raise HttpError(
                    404,
                    "ACCOUNT_NOT_REGISTERED",
                    f"{name} isn't registered yet. Ask them to sign in to the portal once, then add them.",
                )
            target = candidates[0]["id"]
            if target == app["user_id"]:
                raise HttpError(409, "ALREADY_OWNER", "This person owns the app.")
            existing = db.execute(
                "SELECT 1 FROM app_members WHERE app_id=? AND user_id=?", (app["id"], target)
            ).fetchone()
            if existing is None:
                count = db.execute(
                    "SELECT COUNT(*) FROM app_members WHERE app_id=?", (app["id"],)
                ).fetchone()[0]
                if count >= MAXIMUM_MEMBERS:
                    raise HttpError(
                        409, "TEAM_FULL", f"An app can have at most {MAXIMUM_MEMBERS} members."
                    )
                db.execute(
                    "INSERT INTO app_members(app_id,user_id,added_by,created) VALUES(?,?,?,?)",
                    (app["id"], target, user["id"], now),
                )
                self.record(db, request, user["id"], app, "member_add", target, now)
        return Response(200, {"data": {"items": self.people(app)}})

    def remove(self, request: Request) -> Response:
        user, app = self.broker.own(request, mutation=True)
        try:
            target = checked_uuid(request.path_parameters["user"])
        except ValidationError:
            raise HttpError(404, "NOT_FOUND", "This person isn’t on the team.") from None
        if app["access"] == "member" and target != user["id"]:
            raise HttpError(403, "OWNER_ONLY", "Only the app’s owner can remove people.")
        now = time.time()
        with self.broker.database.connect(write=True) as db:
            removed = db.execute(
                "DELETE FROM app_members WHERE app_id=? AND user_id=?", (app["id"], target)
            ).rowcount
            if not removed:
                raise HttpError(404, "NOT_FOUND", "This person isn’t on the team.")
            self.record(db, request, user["id"], app, "member_remove", target, now)
        left = target == user["id"]
        return Response(200, {"data": {"items": [] if left else self.people(app), "left": left}})

    @staticmethod
    def record(
        db: Any,
        request: Request,
        actor: str,
        app: dict[str, Any],
        action: str,
        target: str,
        now: float,
    ) -> None:
        db.execute(
            "INSERT INTO audit(user_id,app_id,intent_id,action,created) VALUES(?,?,?,?,?)",
            (actor, app["id"], None, action, now),
        )
        if request.path.startswith("/v1/admin-apps/"):
            audit(
                db,
                actor,
                app["user_id"],
                "app_" + action,
                {"applicationId": app["id"], "memberId": target},
                now,
            )


def activity(broker: Broker, request: Request) -> Response:
    """Recent changes to one app by anyone on its team, newest first."""
    user, app = broker.own(request)
    raw = request.query.get("limit", ("10",))
    if len(raw) != 1 or not raw[0].isdigit() or not 1 <= int(raw[0]) <= 50:
        raise HttpError(400, "INVALID_REQUEST", "Invalid activity limit.")
    with broker.database.connect() as db:
        rows = db.execute(
            "SELECT i.*,u.display_name AS actor_name FROM intents i"
            " LEFT JOIN users u ON u.id=i.user_id WHERE i.app_id=?"
            " ORDER BY i.created DESC,i.id DESC LIMIT ?",
            (app["id"], int(raw[0])),
        ).fetchall()
    items = [
        {
            **intent_model(row, diagnostic=user["role"] in {"staff", "admin"}, viewer=user["id"]),
            "appSlug": app["slug"],
            "actor": {"displayName": row["actor_name"], "you": row["user_id"] == user["id"]},
        }
        for row in rows
    ]
    return Response(200, {"data": {"items": items}})
