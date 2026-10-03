"""Owner resource contracts through the real, unprivileged project router."""

from __future__ import annotations

import contextlib
import copy
import hashlib
import hmac
import http.client
import io
import sqlite3
import threading
import uuid
from unittest import mock

from openstack_platform import management_release
from openstack_platform.controller import database as controller_db
from openstack_platform.controller.http import HttpError
from openstack_platform.management import activation
from openstack_platform.management.backup import restore_database
from openstack_platform.management.broker.api import DEFAULT_CONFIGURATION, Broker
from openstack_platform.management.broker.client import ControllerUnavailable
from openstack_platform.management.common import canonical
from tests import test_management as management
from tests import test_management_contract as contracts


class OwnerResourceContractTests(contracts.RealProjectContractTests):
    def setUp(self) -> None:
        super().setUp()
        self.real.reject = False
        self.values: dict[str, str] = {}
        original = self.real.fixture.api.helper_caller

        def helper(config, action, values, **kwargs):
            if action.startswith("app.env."):
                self.values.update(values.get("updates", {}))
                for name in values.get("keys", []):
                    self.values.pop(name, None)
                return {
                    "keys": list(self.values),
                    "modifyIndex": 1,
                    "restarted": True,
                    "schedulerHealthy": True,
                    "publicHealthy": True,
                }
            if action.startswith("storage.") and action.endswith((".verify", ".rotate")):
                return {
                    "providerId": values["providerId"],
                    "providerName": values["providerName"],
                    "credentialName": "fixture-role",
                    "verified": True,
                    "evidenceAccepted": True,
                    "retired": True,
                    "modifyIndex": 2,
                }
            return original(config, action, values, **kwargs)

        self.real.fixture.api.helper_caller = helper
        self.app_id = self.create(slug="resource-owner")
        self.real.application = controller_db.get_application(self.real.connection, self.app_id)

    def finish(self, intent):
        self.real.fixture.api.wait_for_operations()
        self.broker.journal.dispatch(intent["intentId"])
        return self.call("GET", f"/v1/intents/{intent['intentId']}", owner="alice").body["data"]

    def storage_create(self, resource_type="postgres", key=None):
        result = self.call(
            "POST", f"/v1/apps/{self.app_id}/storage", {"type": resource_type}, "alice", key
        ).body["data"]
        self.assertEqual(self.finish(result)["state"], "succeeded")
        return self.call("GET", f"/v1/apps/{self.app_id}/storage", owner="alice").body["data"][
            "items"
        ][-1]

    def save_bindings(self, bindings):
        configuration = copy.deepcopy(DEFAULT_CONFIGURATION)
        configuration["storageBindings"] = bindings
        return self.call(
            "PUT",
            f"/v1/apps/{self.app_id}/configuration",
            {
                "expectedRevision": 0,
                "repository": "https://github.com/example/app",
                "branch": "main",
                "configuration": configuration,
            },
            "alice",
        )

    def test_project_storage_and_env_are_owner_scoped_and_no_delete_exists(self):
        self.login("bob")
        for method, suffix, body in (
            ("GET", "environment", None),
            ("PUT", "environment/TOKEN", {"value": "private"}),
            ("DELETE", "environment/TOKEN", {}),
            ("GET", "storage", None),
            ("POST", "storage", {"type": "postgres"}),
            ("POST", f"storage/{uuid.uuid4()}/verify", {}),
            ("POST", f"storage/{uuid.uuid4()}/rotate", {}),
        ):
            with self.subTest(suffix=suffix, method=method):
                self.assert_error(
                    "NOT_FOUND",
                    lambda method=method, suffix=suffix, body=body: self.call(
                        method,
                        f"/v1/apps/{self.app_id}/{suffix}",
                        body,
                        "bob",
                        headers={"idempotency-key": str(uuid.uuid4())},
                    ),
                )
        resource = self.storage_create()
        self.assert_error(
            "NOT_FOUND",
            lambda: self.call(
                "DELETE", f"/v1/apps/{self.app_id}/storage/{resource['resourceId']}", {}, "alice"
            ),
        )
        self.assert_error(
            "NOT_FOUND",
            lambda: self.call("DELETE", f"/v1/storage/{resource['resourceId']}", {}, "alice"),
        )
        status, _body, _headers = self.wire(
            self.real_socket,
            "DELETE",
            f"/v1/storage/{resource['resourceId']}",
            {"confirmation": "default"},
            str(uuid.uuid4()),
        )
        self.assertEqual(status, 405)

    def test_one_per_type_replay_and_rotation_verification_projections(self):
        key = str(uuid.uuid4())
        resource = self.storage_create(key=key)
        self.assertEqual(resource["status"], "ready")
        self.assertEqual(resource["defaultBindings"]["url"], "DATABASE_URL")
        self.assertEqual(
            set(resource),
            {"resourceId", "type", "label", "status", "createdAt", "verifiedAt", "defaultBindings"},
        )
        self.assert_error(
            "STORAGE_TYPE_EXISTS",
            lambda: self.call(
                "POST", f"/v1/apps/{self.app_id}/storage", {"type": "postgres"}, "alice"
            ),
        )
        replay = self.call(
            "POST", f"/v1/apps/{self.app_id}/storage", {"type": "postgres"}, "alice", key
        ).body["data"]
        self.assertEqual(replay["state"], "succeeded")
        for action in ("verify", "rotate"):
            key = str(uuid.uuid4())
            result = self.call(
                "POST",
                f"/v1/apps/{self.app_id}/storage/{resource['resourceId']}/{action}",
                {},
                "alice",
                key,
            ).body["data"]
            self.assertEqual(self.finish(result)["state"], "succeeded")
            self.assertEqual(
                self.call(
                    "POST",
                    f"/v1/apps/{self.app_id}/storage/{resource['resourceId']}/{action}",
                    {},
                    "alice",
                    key,
                ).body["data"]["intentId"],
                result["intentId"],
            )
        for kind in ("mongo", "s3"):
            self.storage_create(kind)
        self.assertEqual(
            len(
                self.call("GET", f"/v1/apps/{self.app_id}/storage", owner="alice").body["data"][
                    "items"
                ]
            ),
            3,
        )

    def test_binding_validation_outputs_names_collisions_foreign_and_injectivity(self):
        resource = self.storage_create()
        for outputs, expected in (
            ({"unknown": "TOKEN"}, "INVALID_BINDING"),
            ({"url": "lower"}, "INVALID_CONFIGURATION"),
            ({"url": "PORT"}, "INVALID_CONFIGURATION"),
            ({"url": "STORAGE__VALUE"}, "INVALID_CONFIGURATION"),
            ({"url": "DATABASE_URL", "host": "DATABASE_URL"}, "INVALID_CONFIGURATION"),
        ):
            with self.subTest(outputs=outputs):
                self.assert_error(
                    expected,
                    lambda outputs=outputs: self.save_bindings(
                        [{"resourceId": resource["resourceId"], "outputs": outputs}]
                    ),
                )
        self.assert_error(
            "INVALID_BINDING",
            lambda: self.save_bindings(
                [{"resourceId": str(uuid.uuid4()), "outputs": {"url": "CUSTOM_DB"}}]
            ),
        )
        self.login("bob")
        foreign_app = self.create("bob", "foreign-storage")
        self.real.application = controller_db.get_application(self.real.connection, foreign_app)
        intent = self.call(
            "POST", f"/v1/apps/{foreign_app}/storage", {"type": "postgres"}, "bob"
        ).body["data"]
        self.real.fixture.api.wait_for_operations()
        self.broker.journal.dispatch(intent["intentId"])
        foreign = self.call("GET", f"/v1/apps/{foreign_app}/storage", owner="bob").body["data"][
            "items"
        ][0]
        self.assert_error(
            "INVALID_BINDING",
            lambda: self.save_bindings(
                [{"resourceId": foreign["resourceId"], "outputs": {"url": "CUSTOM_DB"}}]
            ),
        )
        env = self.call(
            "PUT", f"/v1/apps/{self.app_id}/environment/CUSTOM_DB", {"value": "private"}, "alice"
        ).body["data"]
        self.assertEqual(self.finish(env)["state"], "succeeded")
        self.assert_error(
            "ENV_BINDING_CONFLICT",
            lambda: self.save_bindings(
                [{"resourceId": resource["resourceId"], "outputs": {"url": "CUSTOM_DB"}}]
            ),
        )
        self.save_bindings(
            [{"resourceId": resource["resourceId"], "outputs": {"url": "APP_DATABASE"}}]
        )
        self.assert_error(
            "ENV_BINDING_CONFLICT",
            lambda: self.call(
                "PUT",
                f"/v1/apps/{self.app_id}/environment/APP_DATABASE",
                {"value": "private"},
                "alice",
            ),
        )

    def test_environment_values_absent_in_responses_database_audit_and_logs(self):
        sentinel = "OWNER_SECRET_SENTINEL_b8f142"
        responses = []
        logs = io.StringIO()
        with contextlib.redirect_stderr(logs):
            key = str(uuid.uuid4())
            intent = self.call(
                "PUT",
                f"/v1/apps/{self.app_id}/environment/API_TOKEN",
                {"value": sentinel},
                "alice",
                key,
            ).body["data"]
            responses += [intent, self.finish(intent)]
            responses.append(
                self.call(
                    "PUT",
                    f"/v1/apps/{self.app_id}/environment/API_TOKEN",
                    {"value": sentinel},
                    "alice",
                    key,
                ).body
            )
            self.assert_error(
                "IDEMPOTENCY_CONFLICT",
                lambda: self.call(
                    "PUT",
                    f"/v1/apps/{self.app_id}/environment/API_TOKEN",
                    {"value": sentinel + "x"},
                    "alice",
                    key,
                ),
            )
            responses += [
                self.call("GET", f"/v1/apps/{self.app_id}/environment", owner="alice").body,
                self.call("GET", "/v1/intents", owner="alice").body,
            ]
            deleted = self.call(
                "DELETE",
                f"/v1/apps/{self.app_id}/environment/API_TOKEN",
                {},
                "alice",
                headers={"idempotency-key": str(uuid.uuid4())},
            ).body["data"]
            responses.append(self.finish(deleted))
            self.assertEqual(
                self.call("GET", f"/v1/apps/{self.app_id}/environment", owner="alice").body["data"][
                    "items"
                ],
                [],
            )
        material = canonical(
            {
                "method": "PUT",
                "path": f"/v1/apps/{self.app_id}/environment/API_TOKEN",
                "body": {"value": sentinel},
            }
        )
        subkey = hmac.digest(
            self.broker.auth.anonymous.key, b"owner-portal/env-fingerprint/v1", hashlib.sha256
        )
        expected = hmac.new(subkey, material.encode(), hashlib.sha256).hexdigest()
        with self.broker.database.connect() as db:
            dump = "\n".join(db.iterdump())
            intents = [dict(row) for row in db.execute("SELECT * FROM intents")]
            audit = [dict(row) for row in db.execute("SELECT * FROM audit")]
        stored = next(row for row in intents if row["id"] == intent["intentId"])
        self.assertEqual(stored["fingerprint"], expected)
        deleted_row = next(row for row in intents if row["id"] == deleted["intentId"])
        # Deletion carries no value and keeps its existing unkeyed fingerprint.
        self.assertEqual(
            deleted_row["fingerprint"],
            hashlib.sha256(
                canonical(
                    {
                        "method": "DELETE",
                        "path": f"/v1/apps/{self.app_id}/environment/API_TOKEN",
                        "body": {},
                    }
                ).encode()
            ).hexdigest(),
        )
        stored_text = (
            canonical(responses) + dump + canonical(intents) + canonical(audit) + logs.getvalue()
        )
        self.assertNotIn(sentinel, stored_text)
        database_bytes = b"".join(
            path.read_bytes()
            for path in self.broker.database.path.parent.glob("management.sqlite3*")
        )
        self.assertNotIn(sentinel.encode(), database_bytes)
        for raw in (sentinel, canonical({"value": sentinel}), material):
            plain = hashlib.sha256(raw.encode())
            self.assertNotIn(plain.hexdigest(), stored_text)
            self.assertNotIn(plain.hexdigest().encode(), database_bytes)
            self.assertNotIn(plain.digest(), database_bytes)
        self.assertIn("env_set:API_TOKEN", dump)
        for name in ("PORT", "STORAGE__TOKEN", "lowercase", "A" * 129):
            self.assert_error(
                "RESERVED_ENV_NAME" if name in ("PORT", "STORAGE__TOKEN") else "INVALID_ENV_NAME",
                lambda name=name: self.call(
                    "PUT",
                    f"/v1/apps/{self.app_id}/environment/{name}",
                    {"value": sentinel},
                    "alice",
                ),
            )
        for value in ("x" * 65537, "\x00", "é" * 32769, 4):
            self.assert_error(
                "INVALID_ENV_VALUE",
                lambda value=value: self.call(
                    "PUT", f"/v1/apps/{self.app_id}/environment/TOKEN", {"value": value}, "alice"
                ),
            )

    def test_storage_intent_recovers_lost_admission_after_broker_restart(self):
        original = self.broker.client.request
        keys = []

        def lost(method, path, body=None, key=None, headers=None):
            result = original(method, path, body, key, headers)
            if method == "POST":
                keys.append(key)
                raise ControllerUnavailable("lost response")
            return result

        with mock.patch.object(self.broker.client, "request", side_effect=lost):
            result = self.call(
                "POST", f"/v1/apps/{self.app_id}/storage", {"type": "postgres"}, "alice"
            ).body["data"]
        self.assertEqual(result["state"], "unknown")
        self.assert_error(
            "STORAGE_TYPE_EXISTS",
            lambda: self.call(
                "POST", f"/v1/apps/{self.app_id}/storage", {"type": "postgres"}, "alice"
            ),
        )
        self.real.fixture.api.wait_for_operations()
        recovered = Broker(self.config)
        recovered.client.path = self.real_socket
        recovered.journal.dispatch(result["intentId"])
        recovered.journal.dispatch(result["intentId"])
        with recovered.database.connect() as db:
            row = db.execute("SELECT * FROM intents WHERE id=?", (result["intentId"],)).fetchone()
            self.assertEqual(row["state"], "succeeded")
            self.assertEqual(row["controller_key"], keys[0])
        self.assertEqual(
            len(
                controller_db.list_managed_resources(
                    self.real.connection, application_id=self.app_id
                )
            ),
            1,
        )

    def test_secret_unknown_admission_requires_same_value_resubmission(self):
        sentinel = "SECRET_UNKNOWN_6f948b"
        key = str(uuid.uuid4())
        with (
            mock.patch.object(
                self.broker.client, "request", side_effect=ControllerUnavailable(sentinel)
            ),
            contextlib.redirect_stderr(io.StringIO()) as log,
        ):
            intent = self.call(
                "PUT",
                f"/v1/apps/{self.app_id}/environment/TOKEN",
                {"value": sentinel},
                "alice",
                key,
            ).body["data"]
            self.broker.journal.reconcile()
            self.assertNotIn(sentinel, log.getvalue())
        self.assertTrue(intent["requiresResubmit"])
        pending = self.call("GET", f"/v1/apps/{self.app_id}/environment", owner="alice").body[
            "data"
        ]["intents"][0]
        self.assertEqual(pending["retryKey"], key)
        self.assertEqual(pending["names"], ["TOKEN"])
        self.assertNotIn(sentinel, canonical(pending))
        self.assert_error(
            "ENV_RESUBMIT_REQUIRED",
            lambda: self.call("POST", f"/v1/intents/{intent['intentId']}/resume", {}, "alice"),
        )
        self.assert_error(
            "IDEMPOTENCY_CONFLICT",
            lambda: self.call(
                "PUT",
                f"/v1/apps/{self.app_id}/environment/TOKEN",
                {"value": "different"},
                "alice",
                key,
            ),
        )
        retry = self.call(
            "PUT", f"/v1/apps/{self.app_id}/environment/TOKEN", {"value": sentinel}, "alice", key
        ).body["data"]
        self.assertEqual(retry["intentId"], intent["intentId"])
        self.assertEqual(self.finish(retry)["state"], "succeeded")

    def restart_broker(self):
        self.broker.journal.close()
        self.broker = Broker(self.config)
        self.broker.client.path = self.real_socket
        self.broker.client.timeout = 5
        self.router = self.broker.router()

    def test_environment_mac_replays_after_restart_and_conflicts_on_changed_value(self):
        key = str(uuid.uuid4())
        path = f"/v1/apps/{self.app_id}/environment/TOKEN"
        intent = self.call("PUT", path, {"value": "low-entropy"}, "alice", key).body["data"]
        self.assertEqual(self.finish(intent)["state"], "succeeded")
        self.restart_broker()
        with mock.patch.object(
            self.broker.client, "request", wraps=self.broker.client.request
        ) as request:
            replay = self.call("PUT", path, {"value": "low-entropy"}, "alice", key).body["data"]
            self.assertEqual(replay["intentId"], intent["intentId"])
            self.assertEqual(replay["state"], "succeeded")
            request.assert_not_called()
        self.assert_error(
            "IDEMPOTENCY_CONFLICT",
            lambda: self.call("PUT", path, {"value": "changed"}, "alice", key),
        )

    def test_restore_key_change_conflicts_with_inflight_environment_retry(self):
        key = str(uuid.uuid4())
        path = f"/v1/apps/{self.app_id}/environment/TOKEN"
        with mock.patch.object(
            self.broker.client, "request", side_effect=ControllerUnavailable("lost admission")
        ):
            intent = self.call("PUT", path, {"value": "low-entropy"}, "alice", key).body["data"]
        self.assertEqual(intent["state"], "unknown")
        with self.broker.database.connect() as db:
            fingerprint = db.execute(
                "SELECT fingerprint FROM intents WHERE id=?", (intent["intentId"],)
            ).fetchone()[0]
            with sqlite3.connect(self.root / "restore.sqlite3") as backup:
                db.backup(backup)
        source = self.root / "restore.sqlite3"
        source.chmod(0o600)
        self.broker.journal.close()
        restore_database(source, self.broker.database.path)
        self.assertFalse((self.config.state_directory / "anonymous.key").exists())
        self.restart_broker()
        self.login()
        with self.assertRaises(HttpError) as caught:
            self.call("PUT", path, {"value": "low-entropy"}, "alice", key)
        self.assertEqual(caught.exception.code, "IDEMPOTENCY_CONFLICT")
        self.assertIn("after restore", caught.exception.summary)
        self.assertIn("new request key", caught.exception.summary)
        with self.broker.database.connect() as db:
            row = db.execute("SELECT * FROM intents WHERE id=?", (intent["intentId"],)).fetchone()
            self.assertEqual(row["fingerprint"], fingerprint)
            self.assertEqual(row["state"], "unknown")
        # Restore does not silently release an unresolved application scope.
        self.assert_error(
            "APP_BUSY", lambda: self.call("PUT", path, {"value": "low-entropy"}, "alice")
        )

    def test_storage_failed_provisioning_reports_progress_and_same_key_recovery(self):
        self.real.reject = True
        intent = self.call(
            "POST", f"/v1/apps/{self.app_id}/storage", {"type": "postgres"}, "alice"
        ).body["data"]
        self.assertEqual(self.finish(intent)["state"], "blocked")
        listing = self.call("GET", f"/v1/apps/{self.app_id}/storage", owner="alice").body["data"]
        self.assertEqual(listing["items"][0]["status"], "failed")
        self.assert_error(
            "STORAGE_TYPE_EXISTS",
            lambda: self.call(
                "POST", f"/v1/apps/{self.app_id}/storage", {"type": "postgres"}, "alice"
            ),
        )
        self.real.reject = False
        retry = self.call("POST", f"/v1/intents/{intent['intentId']}/resume", {}, "alice").body[
            "data"
        ]
        self.assertEqual(self.finish(retry)["state"], "succeeded")

    def test_resource_mutations_require_origin_csrf_and_idempotency(self):
        for path, body in (
            (f"/v1/apps/{self.app_id}/storage", {"type": "postgres"}),
            (f"/v1/apps/{self.app_id}/environment/TOKEN", {"value": "private"}),
        ):
            method = "POST" if path.endswith("storage") else "PUT"
            self.assert_error(
                "ORIGIN_REJECTED",
                lambda method=method, path=path, body=body: self.call(
                    method, path, body, "alice", headers={"origin": "https://evil.example"}
                ),
            )
            self.assert_error(
                "CSRF_REJECTED",
                lambda method=method, path=path, body=body: self.call(
                    method, path, body, "alice", headers={"x-csrf-token": "invalid"}
                ),
            )
            with self.assertRaises(HttpError):
                self.call(method, path, body, "alice", headers={"idempotency-key": "invalid"})

    def test_environment_write_rate_limit_does_not_retain_values(self):
        with self.broker.database.connect(write=True) as db:
            for _ in range(30):
                identifier = self.broker.record(
                    db,
                    self.login_id(),
                    self.app_id,
                    "env_set",
                    str(uuid.uuid4()),
                    "fingerprint",
                    "PUT",
                    "/unused",
                    {"names": ["TOKEN"]},
                    str(uuid.uuid4()),
                )
                db.execute("UPDATE intents SET state='failed' WHERE id=?", (identifier,))
        self.assert_error(
            "RATE_LIMITED",
            lambda: self.call(
                "PUT", f"/v1/apps/{self.app_id}/environment/TOKEN", {"value": "private"}, "alice"
            ),
        )

    def login_id(self):
        with self.broker.database.connect() as db:
            return db.execute("SELECT user_id FROM apps WHERE id=?", (self.app_id,)).fetchone()[0]


class ResourceWebTransportTests(management.ManagementCase):
    def test_http_delete_dispatches_only_the_environment_route(self):
        from urllib.parse import urlsplit

        from openstack_platform.management.web.server import WebServer

        web = WebServer(("127.0.0.1", 0), self.config, self.root)
        thread = threading.Thread(
            target=web.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        thread.start()
        self.addCleanup(web.server_close)
        self.addCleanup(web.shutdown)
        app = str(uuid.uuid4())
        connection = http.client.HTTPConnection("127.0.0.1", web.server_port)
        self.addCleanup(connection.close)
        with mock.patch.object(
            web.broker, "request", return_value=(202, {"data": {"state": "accepted"}})
        ) as request:
            connection.request(
                "DELETE",
                f"/api/v1/apps/{app}/environment/API_TOKEN",
                "{}",
                {
                    "Host": urlsplit(self.config.portal_origin).netloc,
                    "Content-Type": "application/json",
                },
            )
            response = connection.getresponse()
            self.assertEqual(response.status, 202)
            response.read()
            self.assertEqual(
                request.call_args.args[:2], ("DELETE", f"/v1/apps/{app}/environment/API_TOKEN")
            )
            count = request.call_count
            connection.request(
                "DELETE",
                f"/api/v1/apps/{app}/storage/{uuid.uuid4()}",
                "{}",
                {
                    "Host": urlsplit(self.config.portal_origin).netloc,
                    "Content-Type": "application/json",
                },
            )
            response = connection.getresponse()
            self.assertEqual(response.status, 404)
            response.read()
            self.assertEqual(request.call_count, count)

    def test_exact_resource_paths_forward_and_neighbors_fail_closed(self):
        from openstack_platform.management.web.server import WebServer

        web = WebServer(("127.0.0.1", 0), self.config, self.root)
        self.addCleanup(web.server_close)
        app, resource = str(uuid.uuid4()), str(uuid.uuid4())
        paths = [
            ("GET", f"/api/v1/apps/{app}/environment"),
            ("PUT", f"/api/v1/apps/{app}/environment/API_TOKEN"),
            ("DELETE", f"/api/v1/apps/{app}/environment/API_TOKEN"),
            ("GET", f"/api/v1/apps/{app}/storage"),
            ("POST", f"/api/v1/apps/{app}/storage"),
            ("POST", f"/api/v1/apps/{app}/storage/{resource}/verify"),
            ("POST", f"/api/v1/apps/{app}/storage/{resource}/rotate"),
        ]
        with mock.patch.object(web.broker, "request", return_value=(200, {"data": {}})) as request:
            for method, path in paths:
                self.assertEqual(web.forward(method, path, "", {}, b"").status, 200)
                self.assertEqual(request.call_args.args[:2], (method, path.removeprefix("/api")))
            count = request.call_count
            for suffix in (
                f"/storage/{resource}",
                f"/storage/{resource}/delete",
                "/environment/import",
                "/environment/lower",
                "/storage/extra",
                "/environment/TOKEN/extra",
            ):
                self.assertEqual(
                    web.forward("POST", f"/api/v1/apps/{app}{suffix}", "", {}, b"").status, 404
                )
            self.assertEqual(request.call_count, count)


class ReleaseOnlyCompatibilityTests(contracts.ManagementCase):
    def test_image_activation_and_release_compatibility_match_main_baseline(self):
        # Main's immutable image activation contract: release-only additions must
        # not bump these values or require a broker database migration.
        expected = {
            "brokerProtocolVersion": 2,
            "webProtocolVersion": 2,
            "authProtocolVersion": 2,
            "brokerSchemaVersion": 2,
            "controllerApiVersion": 1,
        }
        self.assertEqual(activation.COMPATIBILITY, expected)
        self.assertEqual(management_release.COMPATIBILITY, expected)
        with self.broker.database.connect() as db:
            self.assertEqual(db.execute("SELECT version FROM metadata").fetchone()[0], 2)
            self.assertEqual(
                [
                    row[0]
                    for row in db.execute("SELECT version FROM schema_migrations ORDER BY version")
                ],
                [1, 2],
            )
