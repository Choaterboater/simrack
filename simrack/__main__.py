"""Entry point: python3 -m simrack serve"""

from __future__ import annotations

import argparse
import sys

from .api import serve
from .config import Settings
from .profile import ProfileError
from .service import SandboxManager


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="simrack", description="Sandbox front end for a vJunos + Mist lab on Proxmox")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("serve", help="run the web front end")
    run.add_argument("--host", default=None)
    run.add_argument("--port", type=int, default=None)

    sub.add_parser("state", help="print the inventory JSON and exit")
    sub.add_parser("recipes", help="list the built-in fabric recipes")

    args = parser.parse_args(argv)
    try:
        settings = Settings.from_env()
    except ProfileError as error:
        print(f"simrack: {error.message}\n{error.detail}", file=sys.stderr)
        return 2

    if args.command == "recipes":
        from .recipes import list_recipes

        for recipe in list_recipes():
            print(f"{recipe['name']:16} {recipe['description']}")
        return 0

    manager = SandboxManager(settings)

    if args.command == "state":
        import json

        print(json.dumps(manager.state(), indent=2, default=str))
        return 0

    host = args.host or settings.bind_host
    port = args.port or settings.bind_port
    if host not in ("127.0.0.1", "::1", "localhost") and not settings.extras.get("token"):
        print("refusing to bind publicly without a token; set SIMRACK_TOKEN", file=sys.stderr)
        return 2
    if settings.allow_writes:
        restored = manager.ensure_bridges()
        if restored:
            print(f"[simrack] re-created sandbox bridges: {', '.join(restored)}", flush=True)
    httpd = serve(manager, host, port, token=settings.extras.get("token", ""))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
