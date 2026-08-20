from __future__ import annotations

import unittest

from platform_cli.helper.nomad import (
    CasConflict,
    SecretItems,
    VariableSnapshot,
    merge_owned_items,
    update_owned_items,
    variable_path,
)
from platform_cli.validation import ValidationError

SENTINEL = "sentinel-database-password"


class OwnedKeyMergeTests(unittest.TestCase):
    def test_merge_preserves_other_owners_and_redacts_repr(self) -> None:
        current = SecretItems(
            {
                "PORT": "3000",
                "STAFF_SETTING": "keep",
                "DATABASE_URL": "old",
            }
        )
        ownership = {
            "PORT": "platform",
            "STAFF_SETTING": "staff",
            "DATABASE_URL": "storage.postgres.default",
        }
        merged = merge_owned_items(
            current,
            ownership,
            owner="storage.postgres.default",
            updates={"DATABASE_URL": SENTINEL},
        )
        self.assertEqual(merged["PORT"], "3000")
        self.assertEqual(merged["STAFF_SETTING"], "keep")
        self.assertEqual(merged["DATABASE_URL"], SENTINEL)
        self.assertNotIn(SENTINEL, repr(merged))
        snapshot = VariableSnapshot("nomad/jobs/demo-app", 4, merged)
        self.assertNotIn(SENTINEL, repr(snapshot))
        self.assertEqual(snapshot.key_names, ("DATABASE_URL", "PORT", "STAFF_SETTING"))

    def test_owner_cannot_change_other_or_unowned_existing_keys(self) -> None:
        with self.assertRaisesRegex(ValidationError, "owned by platform"):
            merge_owned_items(
                {"PORT": "3000"},
                {"PORT": "platform"},
                owner="staff",
                updates={"PORT": "4000"},
            )
        with self.assertRaisesRegex(ValidationError, "unowned"):
            merge_owned_items(
                {"LEGACY_KEY": "value"},
                {},
                owner="staff",
                updates={"LEGACY_KEY": "replacement"},
            )
        with self.assertRaisesRegex(ValidationError, "both set and removed"):
            merge_owned_items(
                {},
                {},
                owner="staff",
                updates={"NEW_KEY": "value"},
                removals=["NEW_KEY"],
            )

    def test_limits_are_enforced_before_cas(self) -> None:
        with self.assertRaisesRegex(ValidationError, "key limit"):
            merge_owned_items(
                {"ONE": "1"},
                {"ONE": "staff"},
                owner="staff",
                updates={"TWO": "2"},
                maximum_keys=1,
            )
        with self.assertRaisesRegex(ValidationError, "size limit"):
            merge_owned_items(
                {},
                {},
                owner="staff",
                updates={"LARGE": "1234"},
                maximum_value_bytes=3,
            )

    def test_variable_path_is_constrained_to_application_namespace(self) -> None:
        self.assertEqual(variable_path("demo-app"), "nomad/jobs/demo-app")
        with self.assertRaises(ValidationError):
            variable_path("../system")


class FakeClient:
    def __init__(self) -> None:
        self.index = 5
        self.items = SecretItems({"PORT": "3000", "STAFF_SETTING": "keep"})
        self.conflicts = 1
        self.written: SecretItems | None = None

    def read_variable(self, path: str) -> VariableSnapshot:
        return VariableSnapshot(path, self.index, self.items)

    def compare_and_set(self, path: str, expected_index: int, items: object) -> int:
        if self.conflicts:
            self.conflicts -= 1
            self.index += 1
            raise CasConflict("changed")
        self.assert_expected(expected_index)
        self.written = SecretItems(dict(items))  # type: ignore[arg-type]
        self.index += 1
        return self.index

    def assert_expected(self, expected_index: int) -> None:
        if expected_index != self.index:
            raise AssertionError(f"expected CAS index {self.index}, got {expected_index}")


class CasTests(unittest.TestCase):
    def test_conflict_is_reobserved_and_values_do_not_escape_result(self) -> None:
        client = FakeClient()
        result = update_owned_items(
            client,
            "nomad/jobs/demo-app",
            {"PORT": "platform", "STAFF_SETTING": "staff"},
            owner="storage.postgres.default",
            updates={"DATABASE_URL": SENTINEL},
            attempts=3,
        )
        self.assertEqual(client.conflicts, 0)
        assert client.written is not None
        self.assertEqual(client.written["DATABASE_URL"], SENTINEL)
        self.assertEqual(client.written["STAFF_SETTING"], "keep")
        self.assertNotIn(SENTINEL, repr(result))
        self.assertEqual(result.key_names, ("DATABASE_URL", "PORT", "STAFF_SETTING"))

    def test_conflicts_are_bounded(self) -> None:
        client = FakeClient()
        client.conflicts = 3
        with self.assertRaises(CasConflict):
            update_owned_items(
                client,
                "nomad/jobs/demo-app",
                {"PORT": "platform", "STAFF_SETTING": "staff"},
                owner="staff",
                updates={"NEW_SETTING": "value"},
                attempts=2,
            )


if __name__ == "__main__":
    unittest.main()
