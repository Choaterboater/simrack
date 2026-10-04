"""The single-page UI: plain HTML, CSS and JavaScript in ``simrack/static``.

No build step and no CDN. The files are read once at start-up and served as is.
"""
import os

_STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


def _read(name: str) -> bytes:
    with open(os.path.join(_STATIC, name), "rb") as handle:
        return handle.read()


PAGE = _read("index.html").decode("utf-8")

#: The only other files the page loads. Nothing else on disk is ever served.
ASSETS = {
    "/theme.js": ("text/javascript; charset=utf-8", _read("theme.js")),
    "/app.css": ("text/css; charset=utf-8", _read("app.css")),
    "/app.js": ("text/javascript; charset=utf-8", _read("app.js")),
}
