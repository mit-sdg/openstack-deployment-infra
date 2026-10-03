"""Release-local isolated entry; smoke checks metadata without network or live DB."""

from __future__ import annotations

import argparse
import dataclasses
import importlib.util
import json
import sys
import tempfile
from pathlib import Path

# Direct -I execution has no package context. Bind relative imports to this
# release only, without exposing or importing the public package name.
if not __package__:
    root = Path(__file__).resolve().parents[1]
    namespace = "_management_runtime"
    spec = importlib.util.spec_from_file_location(
        namespace, root / "__init__.py", submodule_search_locations=[str(root)]
    )
    if spec is None or spec.loader is None:
        raise ValueError("management release package is missing")
    package = importlib.util.module_from_spec(spec)
    sys.modules[namespace] = package
    spec.loader.exec_module(package)
    __package__ = namespace + ".management"


def main() -> None:
    from ..config import load_platform
    from .config import Config

    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("broker", "web", "identity", "staff-admin"))
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--assets", type=Path)
    parser.add_argument("--requirements", type=Path, required=True)
    parser.add_argument("--smoke", action="store_true")
    args, remaining = parser.parse_known_args()
    if remaining and args.mode != "staff-admin":
        parser.error("unexpected arguments")
    if args.mode == "staff-admin" and args.smoke:
        parser.error("staff recovery is not a service smoke mode")
    requirement = json.loads(args.requirements.read_text())
    if requirement != {
        "python": "3.14",
        "identityTls": "system-ca",
        "schemaVersion": 3,
    } or sys.version_info[:2] != (3, 14):
        raise ValueError("management runtime or schema requirements differ")
    if args.mode == "identity":
        from .identity.client import CommonsClient, IdentityConfig

        identity = IdentityConfig.load(args.config)
        if identity.development:
            raise ValueError("release cannot enable development trust")
        CommonsClient(identity)  # CA/configuration only; no network call.
        if not args.smoke:
            from .identity.main import main as run

            sys.argv = ["management-identity", "--config", str(args.config)]
            run()
    else:
        config = Config.load(args.config)
        if config.development:
            raise ValueError("release cannot enable development mode")
        if args.mode == "staff-admin":
            from .broker.staff_admin import run as staff_admin

            staff_admin(config, remaining)
            return
        if args.smoke:
            if args.mode == "broker":
                from .broker.database import Database

                with tempfile.TemporaryDirectory(prefix="management-smoke-") as directory:
                    database = Database(
                        dataclasses.replace(config, state_directory=Path(directory))
                    )
                    with database.connect() as connection:
                        if connection.execute("SELECT version FROM metadata").fetchone()[0] != 3:
                            raise ValueError("management schema differs")
            elif args.assets is None or not (args.assets / "index.html").is_file():
                raise ValueError("web assets are missing")
        elif args.mode == "broker":
            from .broker.main import main as run

            sys.argv = ["management-broker", "--config", str(args.config)]
            run()
        else:
            from .web.main import main as run

            platform = load_platform(
                args.config.parent / json.loads(args.config.read_text())["platformConfig"]
            )
            sys.argv = [
                "management-web",
                "--config",
                str(args.config),
                "--assets",
                str(args.assets),
                "--bind",
                platform.get("addresses.admin"),
                "--port",
                "8080",
            ]
            run()
    if args.smoke:
        print(f"management-smoke={args.mode}:ok")


if __name__ == "__main__":
    main()
