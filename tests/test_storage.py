import os
import tempfile
import unittest

from storage import Storage


class StorageTest(unittest.TestCase):
    def test_upsert_and_cleanup(self):
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "nm.db")
            s = Storage(db)
            s.upsert_items([
                {"id": "1", "time": "2026-01-01T00:00:00+00:00", "source": "a", "source_id": "a", "type": "rss", "title": "t1", "text": "", "url": "", "summary": "", "category": "", "importance": 5, "is_duplicate": False, "matched_keywords": []},
                {"id": "2", "time": "2020-01-01T00:00:00+00:00", "source": "a", "source_id": "a", "type": "rss", "title": "t2", "text": "", "url": "", "summary": "", "category": "", "importance": 5, "is_duplicate": False, "matched_keywords": []},
            ])
            items = s.cleanup(keep_days=365, max_items=10)
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["id"], "1")


if __name__ == "__main__":
    unittest.main()
