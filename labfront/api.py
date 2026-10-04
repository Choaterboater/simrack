"""HTTP API and the single-page UI. Standard library only."""

from __future__ import annotations

import hmac
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .errors import LabError, NotFound
from .service import SandboxManager
from .ui import ASSETS, PAGE

#: Large enough for a Mist topology, its switches and their port stats.
MAX_BODY = 4 * 1024 * 1024


class Router:
    def __init__(self) -> None:
        self.routes: list[tuple[str, re.Pattern, object, bool]] = []
        self.patterns: list[tuple[str, str]] = []

    def add(self, method: str, pattern: str, handler, *, locked: bool = True) -> None:
        """``locked`` routes run one at a time; a quick read-only POST can skip the queue."""
        regex = re.compile("^" + re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", pattern) + "$")
        self.routes.append((method, regex, handler, locked))
        self.patterns.append((method, pattern))

    def match(self, method: str, path: str):
        allowed = set()
        for route_method, regex, handler, _ in self.routes:
            found = regex.match(path)
            if found:
                if route_method == method:
                    return handler, found.groupdict()
                allowed.add(route_method)
        if allowed:
            raise LabError(f"{method} is not allowed on {path}.", detail="Allowed: " + ", ".join(sorted(allowed)))
        raise NotFound(f"No route for {path}.")

    def needs_lock(self, method: str, path: str) -> bool:
        for route_method, regex, _, locked in self.routes:
            if route_method == method and regex.match(path):
                return locked
        return True


def build_router(manager: SandboxManager) -> Router:
    router = Router()

    router.add("GET", "/api/state", lambda **_: manager.state())
    router.add("GET", "/api/recipes", lambda **_: {"recipes": manager.state()["recipes"]})

    def create_sandbox(body, query):
        return manager.create_sandbox(
            body.get("name", ""),
            body.get("recipe", "collapsed-core"),
            template_vmid=body.get("template_vmid"),
            start=body.get("start", True),
            with_mist_site=body.get("with_mist_site", False),
            notes=body.get("notes", ""),
            image=body.get("image") or None,
        ).to_dict()

    router.add("GET", "/api/shapes", lambda **_: {"shapes": manager.list_shapes()})
    router.add("POST", "/api/shapes", lambda body, **_: manager.import_shape(body))
    router.add("GET", "/api/shapes/{name}", lambda name, **_: manager.get_shape(name))
    router.add("POST", "/api/shapes/{name}/delete", lambda name, **_: {"deleted": manager.delete_shape(name)})

    def build_from_shape(name, body, **_):
        result = manager.build_from_shape(
            name,
            body.get("name", ""),
            switches=body.get("switches"),
            template_vmid=body.get("template_vmid"),
            image=body.get("image") or None,
            start=body.get("start", True),
            with_mist_site=body.get("with_mist_site", False),
        )
        return {"sandbox": result["sandbox"].to_dict(), "dropped": result["dropped"]}

    router.add("POST", "/api/shapes/{name}/build", build_from_shape)

    router.add("POST", "/api/sandboxes", create_sandbox)
    router.add("GET", "/api/sandboxes", lambda **_: {"sandboxes": manager.list_sandboxes()})
    router.add("GET", "/api/sandboxes/{name}", lambda name, **_: manager.get(name).to_dict())

    router.add(
        "POST",
        "/api/sandboxes/{name}/nodes",
        lambda name, body, **_: manager.provision_node(
            manager.get(name),
            body.get("node", ""),
            role=body.get("role", "access"),
            kind=body.get("kind", "switch"),
            template_vmid=body.get("template_vmid"),
            image=body.get("image"),
            start=body.get("start", True),
        ).__dict__,
    )
    router.add(
        "POST",
        "/api/sandboxes/{name}/nodes/{node}/power",
        lambda name, node, body, **_: manager.set_power(manager.get(name), node, body.get("action", "start")),
    )
    router.add("POST", "/api/sandboxes/{name}/nodes/{node}/delete", lambda name, node, body, **_: {"deleted": manager.delete_node(manager.get(name), node)})
    router.add("POST", "/api/sandboxes/{name}/nodes/{node}/console", lambda name, node, body, **_: {"output": manager.console_command(manager.get(name), node, body.get("command", ""))})
    router.add("POST", "/api/sandboxes/{name}/nodes/{node}/adopt", lambda name, node, **_: manager.adopt_switch(manager.get(name), node))
    # A POST, not a GET: never cached, and a cross-site page cannot send it.
    router.add("POST", "/api/sandboxes/{name}/reveal", lambda name, **_: manager.reveal_root_password(manager.get(name)), locked=False)

    router.add(
        "POST",
        "/api/sandboxes/{name}/cables",
        lambda name, body, **_: manager.cable(
            manager.get(name), body.get("a_node", ""), body.get("a_port", ""), body.get("b_node", ""), body.get("b_port", "")
        ).to_dict(),
    )
    router.add(
        "POST",
        "/api/sandboxes/{name}/cables/{bridge}/move",
        lambda name, bridge, body, **_: manager.move_cable(manager.get(name), bridge, body.get("to_node", ""), body.get("to_port", ""), body.get("from_node")),
    )
    router.add("POST", "/api/sandboxes/{name}/cables/{bridge}/remove", lambda name, bridge, **_: {"removed": manager.remove_cable(manager.get(name), bridge)})

    router.add("POST", "/api/sandboxes/{name}/snapshot", lambda name, body, **_: manager.snapshot(manager.get(name), body.get("label", "manual")))
    router.add("POST", "/api/sandboxes/{name}/revert", lambda name, body, **_: manager.revert(manager.get(name), body.get("label", "")))
    router.add("POST", "/api/sandboxes/{name}/mist/site", lambda name, **_: manager.mist_create_site(manager.get(name)))
    router.add("POST", "/api/sandboxes/{name}/mist/fabric", lambda name, **_: manager.mist_build_fabric(manager.get(name)))
    router.add("POST", "/api/sandboxes/{name}/fabric/check", lambda name, **_: manager.fabric_check(manager.get(name)))
    router.add("POST", "/api/sandboxes/{name}/mist/snapshot", lambda name, body, **_: manager.mist_snapshot(manager.get(name), body.get("label", "manual")))
    router.add("POST", "/api/sandboxes/{name}/mist/revert", lambda name, body, **_: manager.mist_revert(manager.get(name), body.get("label", "")))
    router.add("GET", "/api/sandboxes/{name}/mist/health", lambda name, **_: manager.mist_health(manager.get(name)))
    router.add("GET", "/api/sandboxes/{name}/mist/adopt", lambda name, **_: manager.adopt_config(manager.get(name)))
    router.add("POST", "/api/sandboxes/{name}/teardown", lambda name, body, **_: manager.teardown(manager.get(name), keep_mist=body.get("keep_mist", False), confirm=body.get("confirm", False)))

    return router


class Handler(BaseHTTPRequestHandler):
    server_version = "labfront/1.0"
    router: Router
    token: str = ""
    write_lock = threading.Lock()

    def log_message(self, fmt, *args):  # quieter, and never logs the token
        self.server.log(f"{self.address_string()} {fmt % args}")

    def _authorised(self) -> bool:
        if not self.token:
            return True
        supplied = self.headers.get("Authorization", "")
        return hmac.compare_digest(supplied.encode(), ("Bearer " + self.token).encode())

    def _known_host(self) -> bool:
        """With no token, answer only to a loopback Host, so a DNS-rebinding page
        cannot drive the API through someone's tunnel."""
        if self.token:
            return True
        host = self.headers.get("Host")
        if host is None:
            return True
        host = host.strip().lower()
        if host.startswith("["):
            name = host[1 : host.find("]")] if "]" in host else host
        elif host.count(":") == 1:
            name = host.split(":", 1)[0]
        else:
            name = host
        return name in ("127.0.0.1", "localhost", "::1")

    def _same_origin(self) -> bool:
        """Block cross-site POSTs: a JSON content type forces a CORS preflight,
        and a browser-supplied Origin must match the Host it was sent to."""
        if not self.headers.get("Content-Type", "").lower().startswith("application/json"):
            return False
        origin = self.headers.get("Origin")
        if origin is None:
            return True
        return urlparse(origin).netloc == self.headers.get("Host", "")

    def _send(self, status: int, payload, content_type="application/json") -> None:
        body = payload if isinstance(payload, bytes) else json.dumps(payload, indent=2, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if not self._known_host():
            return self._send(403, {"error": "Unknown Host header refused.", "detail": "Open LabFront at http://127.0.0.1 or http://localhost."})
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            return self._send(200, PAGE.encode(), "text/html; charset=utf-8")
        if parsed.path in ASSETS:
            content_type, body = ASSETS[parsed.path]
            return self._send(200, body, content_type)
        if not self._authorised():
            return self._send(401, {"error": "Bad or missing bearer token."})
        self._dispatch("GET", parsed)

    def do_POST(self):  # noqa: N802
        if not self._known_host():
            self._drain()
            self.close_connection = True
            return self._send(403, {"error": "Unknown Host header refused.", "detail": "Open LabFront at http://127.0.0.1 or http://localhost."})
        if not self._authorised():
            return self._send(401, {"error": "Bad or missing bearer token."})
        if not self._same_origin():
            return self._send(403, {"error": "Cross-origin or non-JSON request refused.", "detail": "POST with Content-Type: application/json from the LabFront page."})
        if self._too_big():
            return
        parsed = urlparse(self.path)
        if not self.router.needs_lock("POST", parsed.path):
            return self._dispatch("POST", parsed)
        # One change at a time: two clones racing for the same vmid would collide.
        with self.write_lock:
            self._dispatch("POST", parsed)

    def _content_length(self) -> int:
        try:
            return int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return MAX_BODY + 1

    def _drain(self) -> None:
        """Read and drop a modest body, so the client sees our reply rather than a reset."""
        length = self._content_length()
        remaining = length if length <= 16 * MAX_BODY else 0
        while remaining > 0:
            chunk = self.rfile.read(min(65536, remaining))
            if not chunk:
                break
            remaining -= len(chunk)

    def _too_big(self) -> bool:
        """Refuse an oversized body before reading it into memory. A modest one is
        drained first so the client sees the 413 rather than a reset."""
        length = self._content_length()
        if length <= MAX_BODY:
            return False
        self._drain()
        self.close_connection = True
        self._send(413, {"error": f"That is too large ({length // 1024} KB).", "detail": f"The limit is {MAX_BODY // (1024 * 1024)} MB. Send only the topology, the switches and their port stats."})
        return True

    def _dispatch(self, method: str, parsed) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw) if raw else {}
            if not isinstance(body, dict):
                raise ValueError("body must be a JSON object")
        except (json.JSONDecodeError, ValueError) as error:
            return self._send(400, {"error": f"Invalid JSON body: {error}"})
        try:
            handler, params = self.router.match(method, parsed.path)
            result = handler(body=body, query=parse_qs(parsed.query), **params)
            self._send(200, result if result is not None else {"ok": True})
        except LabError as error:
            self._send(error.http_status, error.as_dict())
        except ValueError as error:  # input validation (names, ports)
            self._send(400, {"error": str(error)})
        except Exception as error:  # noqa: BLE001 - the UI needs a message, not a traceback
            self.server.log(f"unhandled: {type(error).__name__}: {error}")
            self._send(500, {"error": f"{type(error).__name__}: {error}"})


def serve(manager: SandboxManager, host: str, port: int, token: str = ""):
    if host not in ("127.0.0.1", "::1", "localhost") and not token:
        raise LabError(
            "Refusing to bind a write-capable API to a public address without a token.",
            detail="Set LABFRONT_TOKEN, or bind 127.0.0.1 and use an SSH tunnel.",
        )
    handler = type("BoundHandler", (Handler,), {"router": build_router(manager), "token": token, "write_lock": threading.Lock()})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.log = lambda message: print(f"[labfront] {message}", flush=True)
    print(f"[labfront] listening on http://{host}:{port} (auth={'bearer' if token else 'none'})", flush=True)
    return httpd
