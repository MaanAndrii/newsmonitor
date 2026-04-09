import unittest

from utils import RetryConfig, retry_call, setup_logging


class RetryTest(unittest.TestCase):
    def test_setup_logging(self):
        logger = setup_logging("newsmonitor.test", level="INFO")
        self.assertEqual(logger.name, "newsmonitor.test")
        self.assertGreaterEqual(len(logger.handlers), 1)

    def test_retry_call(self):
        calls = {"n": 0}

        def unstable():
            calls["n"] += 1
            if calls["n"] < 3:
                raise RuntimeError("boom")
            return 42

        result = retry_call(
            unstable,
            RetryConfig(attempts=4, base_delay=0.01, max_delay=0.02, jitter=0),
            "unstable",
        )
        self.assertEqual(result, 42)
        self.assertEqual(calls["n"], 3)


if __name__ == "__main__":
    unittest.main()
