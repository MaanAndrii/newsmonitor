import os
import tempfile
import unittest

from io_utils import load_json, write_json


class IoUtilsTest(unittest.TestCase):
    def test_load_json_merges_default_dict(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "s.json")
            write_json(path, {"a": 1})
            result = load_json(path, {"a": 0, "b": 2})
            self.assertEqual(result["a"], 1)
            self.assertEqual(result["b"], 2)

    def test_load_json_bootstraps_missing_file(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "missing.json")
            result = load_json(path, {"x": True})
            self.assertTrue(os.path.exists(path))
            self.assertEqual(result, {"x": True})


if __name__ == "__main__":
    unittest.main()
