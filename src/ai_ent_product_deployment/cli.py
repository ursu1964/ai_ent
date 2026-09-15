from __future__ import annotations

import argparse
import json
from pathlib import Path

from ai_ent_product_deployment.config import (
    LocalDeploymentConfigError,
    apply_private_environment,
    load_local_deployment_config,
    validate_postgresql_authority,
)
from ai_ent_product_deployment.server import LocalProductServer
from ai_ent_product_ui.services import hash_password


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command == "hash-password":
        print(hash_password(args.password))
        return 0
    env_file = None if args.env_file == "none" else Path(args.env_file)
    try:
        config = load_local_deployment_config(env_file)
    except LocalDeploymentConfigError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        return 1
    if args.command == "validate-config":
        try:
            postgresql = validate_postgresql_authority(config.database)
        except LocalDeploymentConfigError as exc:
            print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
            return 1
        print(
            json.dumps(
                {
                    "ok": True,
                    "config": config.safe_summary(),
                    "postgresql": postgresql.safe_summary(),
                },
                sort_keys=True,
                indent=2,
            )
        )
        return 0
    if args.command == "serve":
        apply_private_environment(env_file)
        server = LocalProductServer(config)
        host, port = server.server_address
        print(
            json.dumps(
                {
                    "ok": True,
                    "event": "local_product_server_started",
                    "bind_host": host,
                    "port": port,
                    "network_scope": "loopback",
                    "use_existing_postgres": config.use_existing_postgres,
                },
                sort_keys=True,
            ),
            flush=True,
        )
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.shutdown()
        return 0
    parser.print_help()
    return 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the local AI-Enterprise product surface.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("validate-config", "serve"):
        sub = subparsers.add_parser(command)
        sub.add_argument("--env-file", default=".env")
    hash_parser = subparsers.add_parser("hash-password")
    hash_parser.add_argument("password")
    return parser


if __name__ == "__main__":
    raise SystemExit(main())
