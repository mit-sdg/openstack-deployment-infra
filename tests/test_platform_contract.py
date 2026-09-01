from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path

from infra.lib.platform_contract import CONTRACT_PATH, ContractError, load_contract
from openstack_platform import contracts

ROOT = Path(__file__).resolve().parents[1]


class PlatformContractTests(unittest.TestCase):
    def test_checked_in_contract_is_the_only_source_for_shared_values(self) -> None:
        document = load_contract()
        self.assertEqual(tuple(document["roles"]["all"]), contracts.IMAGE_ROLES)
        self.assertEqual(document["ports"]["application"], contracts.APPLICATION_HOST_PORT)
        self.assertEqual(
            tuple(document["inventory"]["requiredPaths"]),
            contracts.INVENTORY_REQUIRED_PATHS,
        )
        self.assertEqual(CONTRACT_PATH, ROOT / "infra/lib/platform_contract.json")

    def test_contract_rejects_duplicates_invalid_ports_and_role_drift(self) -> None:
        base = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
        cases: list[str] = []
        cases.append('{"version":1,"version":1}')

        invalid_port = json.loads(json.dumps(base))
        invalid_port["ports"]["ssh"] = 70000
        cases.append(json.dumps(invalid_port))

        invalid_roles = json.loads(json.dumps(base))
        invalid_roles["roles"]["persistent"] = ["admin", "unknown"]
        cases.append(json.dumps(invalid_roles))

        duplicate_uid = json.loads(json.dumps(base))
        duplicate_uid["accounts"]["controller"]["uid"] = duplicate_uid["accounts"]["operator"][
            "uid"
        ]
        cases.append(json.dumps(duplicate_uid))

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for index, payload in enumerate(cases):
                path = root / f"contract-{index}.json"
                path.write_text(payload, encoding="utf-8")
                with self.subTest(index=index), self.assertRaises(ContractError):
                    load_contract(path)

    def test_role_modules_do_not_repeat_contract_port_numbers(self) -> None:
        contract = load_contract()
        port_tokens = {str(port) for port in contract["ports"].values()}
        for directory in (ROOT / "nix/modules", ROOT / "nix/roles"):
            for path in directory.glob("*.nix"):
                numbers = set(re.findall(r"\b[0-9]+\b", path.read_text(encoding="utf-8")))
                with self.subTest(path=path.relative_to(ROOT)):
                    self.assertTrue(port_tokens.isdisjoint(numbers))


if __name__ == "__main__":
    unittest.main()
