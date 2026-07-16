"""Tests for WO-5: static dashboard serving, traversal guard, view-source audit."""
from __future__ import annotations

import http.client
import os
import re
import tempfile
import threading
import unittest

import _path  # noqa: F401

from pelositracker import db
from pelositracker.api import DISCLAIMER, WEB_ROOT, build_server


class UiServingTests(unittest.TestCase):
    def setUp(self) -> None:
        handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        handle.close()
        self.db_path = handle.name
        conn = db.connect(self.db_path)
        db.init_schema(conn)
        conn.close()
        self.server = build_server(self.db_path, "127.0.0.1", 0)
        self.port = int(self.server.server_address[1])
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        os.unlink(self.db_path)

    def _raw_get(self, path: str) -> tuple[int, str, bytes]:
        """GET without client-side path normalization (urllib collapses ../)."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request("GET", path)
            response = conn.getresponse()
            return (
                response.status,
                response.getheader("Content-Type") or "",
                response.read(),
            )
        finally:
            conn.close()

    def test_ui_index_served_with_content_type_and_disclaimer(self) -> None:
        for path in ("/ui", "/ui/", "/ui/index.html"):
            status, content_type, body = self._raw_get(path)
            self.assertEqual(status, 200, path)
            self.assertEqual(content_type, "text/html; charset=utf-8", path)
            self.assertIn(DISCLAIMER, body.decode("utf-8"), path)

    def test_static_assets_served_with_correct_types(self) -> None:
        status, content_type, _ = self._raw_get("/ui/style.css")
        self.assertEqual(status, 200)
        self.assertEqual(content_type, "text/css; charset=utf-8")
        status, content_type, _ = self._raw_get("/ui/app.js")
        self.assertEqual(status, 200)
        self.assertEqual(content_type, "text/javascript; charset=utf-8")

    def test_traversal_attempts_rejected(self) -> None:
        attempts = (
            "/ui/../src/pelositracker/config.py",
            "/ui/../../etc/passwd",
            "/ui/%2e%2e/src/pelositracker/config.py",
            "/ui/..%2fsrc%2fpelositracker%2fconfig.py",
            "/ui/..%5cconfig.py",
            "/ui/....//config.py",
            "/ui/C:/Windows/win.ini",
        )
        for path in attempts:
            status, _, body = self._raw_get(path)
            self.assertEqual(status, 404, path)
            self.assertNotIn(b"HOUSE_ALL_TRANSACTIONS_URL", body, path)

    def test_unknown_file_and_type_rejected(self) -> None:
        status, _, _ = self._raw_get("/ui/missing.html")
        self.assertEqual(status, 404)
        # Whitelist check: even a path shaped like source code is refused.
        status, _, _ = self._raw_get("/ui/app.py")
        self.assertEqual(status, 404)

    def test_non_get_rejected(self) -> None:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request("POST", "/ui/", body=b"{}")
            response = conn.getresponse()
            self.assertEqual(response.status, 405)
            self.assertEqual(response.getheader("Allow"), "GET")
            response.read()
        finally:
            conn.close()


class ViewSourceAuditTests(unittest.TestCase):
    """Batch-3 gate addition: served assets contain no external URLs."""

    def test_no_external_urls_in_assets(self) -> None:
        pattern = re.compile(r"https?://|//cdn|@import|url\(", re.IGNORECASE)
        for name in ("index.html", "style.css", "app.js"):
            content = (WEB_ROOT / name).read_text(encoding="utf-8")
            self.assertIsNone(
                pattern.search(content),
                f"external reference found in {name}",
            )

    def test_no_storage_apis_in_js(self) -> None:
        content = (WEB_ROOT / "app.js").read_text(encoding="utf-8")
        for token in ("localStorage", "sessionStorage", "indexedDB", "document.cookie", "caches."):
            self.assertNotIn(token, content)

    def test_fetches_only_contract_endpoints(self) -> None:
        content = (WEB_ROOT / "app.js").read_text(encoding="utf-8")
        for match in re.finditer(r"fetch\(([^)]*)\)", content):
            self.assertIn("path", match.group(1))  # single fetch helper only
        for match in re.finditer(r"apiFetch\(\s*[`'\"](.+?)[`'\"]", content):
            self.assertTrue(
                match.group(1).startswith("/api/v1/"),
                f"non-contract fetch target: {match.group(1)}",
            )

    def test_amounts_rendered_as_ranges_never_midpoints(self) -> None:
        content = (WEB_ROOT / "app.js").read_text(encoding="utf-8")
        self.assertIn("min_cents", content)
        self.assertIn("max_cents", content)
        for token in ("midpoint", "average", "(min_cents + max_cents)", "/ 2)"):
            self.assertNotIn(token, content)


if __name__ == "__main__":
    unittest.main()
