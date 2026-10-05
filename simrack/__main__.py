"""Entry point: python3 -m simrack serve"""

from __future__ import annotations

import argparse
import sys

from .api import serve
from .config import Settings
from .service import SandboxManager


def _seconds(text: str) -> float:
    seconds = float(text)
    if not seconds > 0:
        raise argparse.ArgumentTypeError(f"{text} is not a number of seconds above 0")
    return seconds


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="simrack", description="Sandbox front end for a vJunos + Mist lab on Proxmox")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("serve", help="run the web front end")
    run.add_argument("--host", default=None)
    run.add_argument("--port", type=int, default=None)

    sub.add_parser("state", help="print the inventory JSON and exit")
    sub.add_parser("recipes", help="list the built-in fabric recipes")
    mcp = sub.add_parser("mcp", help="let an assistant drive SimRack over MCP, on stdin and stdout")
    mcp.add_argument("--url", default="http://127.0.0.1:8787", help="where SimRack's web front end listens")
    mcp.add_argument(
        "--wait",
        type=_seconds,
        default=50.0,
        help="seconds a tool waits for SimRack before handing the assistant a job to check on; "
        "keep it under the assistant's own time limit for a call (default 50)",
    )
    mcp.add_argument(
        "--poll",
        type=_seconds,
        default=15.0,
        help="seconds between looks at SimRack, to tell the assistant when the tools on offer change (default 15)",
    )
    mcp.add_argument(
        "--read-only",
        action="store_true",
        help="offer the assistant looks only and refuse every change, whatever SimRack itself allows",
    )

    args = parser.parse_args(argv)
    if args.command == "mcp":
        from .mcp import main as serve_mcp

        return serve_mcp(args.url, wait=args.wait, poll=args.poll, read_only=args.read_only)
    settings = Settings.load()
    for problem in settings.problems:
        print(f"simrack: {problem['error']}\n{problem['detail']}", file=sys.stderr)
    if settings.problems and args.command == "state":
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
