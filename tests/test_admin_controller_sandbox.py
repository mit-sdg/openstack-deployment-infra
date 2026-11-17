"""The controller service may write every state directory its helper creates."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ControllerSandboxTests(unittest.TestCase):
    def test_helper_state_directories_are_writable_and_created_at_boot(self) -> None:
        helper = (ROOT / "openstack_platform/helper/production.py").read_text()
        admin = (ROOT / "nix/roles/admin.nix").read_text()
        names = set(re.findall(r'controller / "([a-z-]+)"', helper))
        names |= set(re.findall(r'"controller/([a-z-]+)"', helper))
        self.assertTrue({"build-logs", "source-keys", "staff-exports"} <= names)
        service = admin.split('systemd.services."${namespace}-controller" = {', 1)[1]
        writable = service.split("ReadWritePaths = [", 1)[1].split("];", 1)[0]
        for name in sorted(names):
            with self.subTest(directory=name):
                self.assertIn(f'"${{controllerRoot}}/{name}"', writable)
                self.assertIn(f'"d ${{controllerRoot}}/{name} 0700 ', admin)


if __name__ == "__main__":
    unittest.main()
