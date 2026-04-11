import os
import tempfile
import unittest
from datetime import datetime, timezone

import listener
from storage import Storage


class PipelineFlowTest(unittest.TestCase):
    def test_listener_append_item_is_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            original_storage = listener.STORAGE
            old_flag = os.environ.get("NEWSMONITOR_WRITE_LEGACY_JSON")
            try:
                listener.STORAGE = Storage(os.path.join(td, "nm.db"))
                os.environ.pop("NEWSMONITOR_WRITE_LEGACY_JSON", None)
                item = {
                    "id": "abc",
                    "source": "src",
                    "source_id": "s1",
                    "type": "telegram",
                    "title": "title",
                    "text": "text",
                    "url": "",
                    "time": datetime.now(timezone.utc).isoformat(),
                    "summary": "",
                    "category": "",
                    "importance": 5,
                    "is_duplicate": False,
                    "matched_keywords": [],
                }

                listener.append_item(item, keep_days=14, max_items=50)
                listener.append_item(item, keep_days=14, max_items=50)

                items = listener.STORAGE.load_items()
                self.assertEqual(len(items), 1)
                self.assertEqual(listener.STORAGE.get_kv("new_count", 0), 1)
            finally:
                listener.STORAGE = original_storage
                if old_flag is None:
                    os.environ.pop("NEWSMONITOR_WRITE_LEGACY_JSON", None)
                else:
                    os.environ["NEWSMONITOR_WRITE_LEGACY_JSON"] = old_flag


if __name__ == "__main__":
    unittest.main()
