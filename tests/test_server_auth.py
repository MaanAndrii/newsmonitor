import os
import threading
import unittest

from server import Handler, resolve_settings_with_env, _ensure_thread_event_loop


class ServerAuthTest(unittest.TestCase):
    def test_ensure_thread_event_loop_creates_loop_in_worker_thread(self):
        result = {"ok": False}

        def worker():
            _ensure_thread_event_loop()
            import asyncio

            loop = asyncio.get_event_loop()
            result["ok"] = loop is not None

        t = threading.Thread(target=worker)
        t.start()
        t.join(timeout=2)
        self.assertTrue(result["ok"])

    def test_auth_required_depends_on_env_pair(self):
        old_user = os.environ.get("NEWSMONITOR_AUTH_USER")
        old_pass = os.environ.get("NEWSMONITOR_AUTH_PASS")
        try:
            os.environ.pop("NEWSMONITOR_AUTH_USER", None)
            os.environ.pop("NEWSMONITOR_AUTH_PASS", None)
            self.assertFalse(Handler._auth_required(object()))

            os.environ["NEWSMONITOR_AUTH_USER"] = "admin"
            os.environ.pop("NEWSMONITOR_AUTH_PASS", None)
            self.assertFalse(Handler._auth_required(object()))

            os.environ["NEWSMONITOR_AUTH_PASS"] = "secret"
            self.assertTrue(Handler._auth_required(object()))
        finally:
            if old_user is None:
                os.environ.pop("NEWSMONITOR_AUTH_USER", None)
            else:
                os.environ["NEWSMONITOR_AUTH_USER"] = old_user
            if old_pass is None:
                os.environ.pop("NEWSMONITOR_AUTH_PASS", None)
            else:
                os.environ["NEWSMONITOR_AUTH_PASS"] = old_pass

    def test_resolve_settings_env_has_priority_over_file(self):
        old_key = os.environ.get("NEWSMONITOR_ANTHROPIC_API_KEY")
        try:
            os.environ.pop("NEWSMONITOR_ANTHROPIC_API_KEY", None)
            resolved = resolve_settings_with_env({"anthropic_api_key": "from_file"})
            self.assertEqual(resolved["anthropic_api_key"], "from_file")

            os.environ["NEWSMONITOR_ANTHROPIC_API_KEY"] = "from_env"
            resolved = resolve_settings_with_env({"anthropic_api_key": "from_file"})
            self.assertEqual(resolved["anthropic_api_key"], "from_env")
        finally:
            if old_key is None:
                os.environ.pop("NEWSMONITOR_ANTHROPIC_API_KEY", None)
            else:
                os.environ["NEWSMONITOR_ANTHROPIC_API_KEY"] = old_key


if __name__ == "__main__":
    unittest.main()
