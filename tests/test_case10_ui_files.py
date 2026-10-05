"""Case 10: the UI ships as plain files (index.html, theme.js, app.css, app.js), no build step.

The page and its three assets are served as they are on disk and, like the page
always was, without the bearer token. They come from a fixed list, so nothing
else on disk can be fetched. theme.js is the one script in the head: it picks
day or dark before the first paint, so a remembered choice never flashes.
"""
import os
import re
import threading
import unittest
import urllib.error
import urllib.request

from simrack.api import serve
from tests.fakes import TempDir, make_manager

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "simrack", "static")
JS = "text/javascript; charset=utf-8"
ASSETS = (("/theme.js", JS), ("/app.css", "text/css; charset=utf-8"), ("/app.js", JS))


class TestUiFiles(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDir()
        self.tmp = self._tmp.__enter__()
        self.httpd = serve(make_manager(self.tmp), "127.0.0.1", 0, token="s3cret")
        self.httpd.log = lambda message: None
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self._tmp.__exit__(None, None, None)

    def _get(self, path):
        try:
            with urllib.request.urlopen(self.base + path, timeout=5) as reply:
                return reply.status, reply.headers.get("Content-Type", ""), reply.read()
        except urllib.error.HTTPError as error:
            return error.code, error.headers.get("Content-Type", ""), error.read()

    def test_the_page_loads_its_css_and_js_from_files(self):
        status, kind, body = self._get("/")
        self.assertEqual((status, kind), (200, "text/html; charset=utf-8"))
        page = body.decode()
        self.assertIn('<link rel="stylesheet" href="/app.css">', page)
        self.assertIn('<script src="/app.js"></script>', page)
        self.assertNotIn("<style>", page)
        self.assertNotIn("<script>", page)

    def test_the_theme_is_picked_in_the_head_before_anything_paints(self):
        page = self._get("/")[2].decode()
        self.assertIn('<script src="/theme.js"></script>', page)
        self.assertLess(page.index('<script src="/theme.js"></script>'), page.index("</head>"))

    def test_the_assets_are_the_files_on_disk_with_their_types(self):
        for path, kind in ASSETS:
            status, got, body = self._get(path)
            self.assertEqual((status, got), (200, kind), path)
            with open(os.path.join(STATIC, path.lstrip("/")), "rb") as handle:
                self.assertEqual(body, handle.read(), path)

    def test_the_page_and_assets_need_no_token_but_the_api_does(self):
        for path in ("/", "/index.html", *(path for path, _ in ASSETS)):
            self.assertEqual(self._get(path)[0], 200, path)
        self.assertEqual(self._get("/api/state")[0], 401)

    def test_the_browser_keeps_the_token_only_until_the_tab_closes(self):
        with open(os.path.join(STATIC, "app.js"), encoding="utf-8") as handle:
            uses = sorted(set(re.findall(r'(\w+Storage)\.(\w+)\("simrack_token"', handle.read())))
        # localStorage only clears a token an older SimRack left on disk.
        self.assertEqual(uses, [("localStorage", "removeItem"), ("sessionStorage", "getItem"), ("sessionStorage", "setItem")])

    def test_nothing_else_on_disk_is_served(self):
        for path in ("/static/app.js", "/simrack/ui.py", "/app.js/../ui.py", "/app.js.map", "/%2e%2e/simrack.env", "/state/sandboxes.json"):
            status, kind, _ = self._get(path)
            self.assertEqual((status, kind), (401, "application/json"), path)


if __name__ == "__main__":
    unittest.main()
