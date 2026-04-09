import json
import os
import tempfile
import threading
import unittest
import urllib.request
import urllib.error

import server
from io_utils import write_json
from storage import Storage


class ApiSmokeTest(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.base = self.td.name

        self.old = {
            "SOURCES_FILE": server.SOURCES_FILE,
            "SETTINGS_FILE": server.SETTINGS_FILE,
            "READ_FILE": server.READ_FILE,
            "LISTENER_FILE": server.LISTENER_FILE,
            "STORAGE": server.STORAGE,
            "NEWSMONITOR_AUTH_USER": os.environ.get("NEWSMONITOR_AUTH_USER"),
            "NEWSMONITOR_AUTH_PASS": os.environ.get("NEWSMONITOR_AUTH_PASS"),
        }

        server.SOURCES_FILE = os.path.join(self.base, "sources.json")
        server.SETTINGS_FILE = os.path.join(self.base, "settings.json")
        server.READ_FILE = os.path.join(self.base, "read_items.json")
        server.LISTENER_FILE = os.path.join(self.base, "listener_status.json")
        server.STORAGE = Storage(os.path.join(self.base, "newsmonitor.db"))

        write_json(server.SOURCES_FILE, server.DEFAULT_SOURCES)
        write_json(server.SETTINGS_FILE, server.DEFAULT_SETTINGS)

        os.environ.pop("NEWSMONITOR_AUTH_USER", None)
        os.environ.pop("NEWSMONITOR_AUTH_PASS", None)

        self.httpd = server.NewsMonitorHTTPServer(("127.0.0.1", 0), server.Handler)
        self.port = self.httpd.server_address[1]
        self.th = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.th.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.th.join(timeout=2)

        server.SOURCES_FILE = self.old["SOURCES_FILE"]
        server.SETTINGS_FILE = self.old["SETTINGS_FILE"]
        server.READ_FILE = self.old["READ_FILE"]
        server.LISTENER_FILE = self.old["LISTENER_FILE"]
        server.STORAGE = self.old["STORAGE"]

        if self.old["NEWSMONITOR_AUTH_USER"] is None:
            os.environ.pop("NEWSMONITOR_AUTH_USER", None)
        else:
            os.environ["NEWSMONITOR_AUTH_USER"] = self.old["NEWSMONITOR_AUTH_USER"]
        if self.old["NEWSMONITOR_AUTH_PASS"] is None:
            os.environ.pop("NEWSMONITOR_AUTH_PASS", None)
        else:
            os.environ["NEWSMONITOR_AUTH_PASS"] = self.old["NEWSMONITOR_AUTH_PASS"]

        self.td.cleanup()

    def _get(self, path: str):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))

    def _post(self, path: str, payload: dict):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))

    def _get_error(self, path: str):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=5)
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))
        raise AssertionError("Expected HTTPError")

    def test_health_endpoint(self):
        code, payload = self._get("/api/health")
        self.assertEqual(code, 200)
        self.assertTrue(payload["ok"])
        self.assertIn("fetcher_last_success_age_sec", payload)
        self.assertIn("listener_heartbeat_age_sec", payload)

    def test_news_endpoint_returns_payload(self):
        code, payload = self._get("/api/news")
        self.assertEqual(code, 200)
        self.assertIn("items", payload)
        self.assertIn("total", payload)

    def test_settings_update_smoke(self):
        code, payload = self._post("/api/settings", {"keep_days": 7, "max_items": 123})
        self.assertEqual(code, 200)
        self.assertTrue(payload["ok"])

    def test_public_dashboard_works_without_admin_session(self):
        os.environ["NEWSMONITOR_AUTH_USER"] = "admin"
        os.environ["NEWSMONITOR_AUTH_PASS"] = "secret"

        code, payload = self._get("/api/news")
        self.assertEqual(code, 200)
        self.assertIn("items", payload)

        code, payload = self._get_error("/api/settings")
        self.assertEqual(code, 401)
        self.assertEqual(payload.get("error"), "auth_required")


if __name__ == "__main__":
    unittest.main()
