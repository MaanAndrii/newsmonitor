import unittest

import fetcher
import listener


class _FakePart:
    def __init__(self, text):
        self.text = text


class _FakeResponse:
    def __init__(self, text):
        self.content = [_FakePart(text)]


class _FakeMessages:
    def __init__(self, text):
        self._text = text

    def create(self, **kwargs):
        return _FakeResponse(self._text)


class _FakeAnthropic:
    def __init__(self, api_key, text):
        self.messages = _FakeMessages(text)


class AIValidationTest(unittest.TestCase):
    def test_normalize_categories_adds_fallback_when_empty(self):
        f = fetcher.normalize_categories([])
        l = listener.normalize_categories([])
        self.assertEqual(f[0]["id"], "other")
        self.assertEqual(l[0]["id"], "other")

    def test_fetcher_analyze_batch_validates_shape_and_clamps(self):
        categories = [{"id": "war", "name": "War"}]
        fake_json = '[{"index": 1, "category": "unknown", "importance": 99, "is_duplicate": 0}]'

        original = fetcher.Anthropic
        fetcher.Anthropic = lambda api_key: _FakeAnthropic(api_key, fake_json)
        try:
            result = fetcher.analyze_batch(
                items=[{"source": "s", "title": "t", "text": "x"}],
                api_key="k",
                categories=categories,
                model="m",
                priorities="",
            )
        finally:
            fetcher.Anthropic = original

        self.assertEqual(result[0]["category"], "war")
        self.assertEqual(result[0]["importance"], 10)
        self.assertFalse(result[0]["is_duplicate"])

    def test_listener_analyze_single_validates_payload(self):
        categories = [{"id": "eco", "name": "Economy"}]
        fake_json = '{"category": "bad", "importance": "oops", "is_duplicate": 1}'

        original = listener.Anthropic
        listener.Anthropic = lambda api_key: _FakeAnthropic(api_key, fake_json)
        try:
            result = listener.analyze_single(
                item={"source": "s", "title": "t", "text": "x"},
                api_key="k",
                categories=categories,
                model="m",
                priorities="",
            )
        finally:
            listener.Anthropic = original

        self.assertEqual(result["category"], "eco")
        self.assertEqual(result["importance"], 5)
        self.assertTrue(result["is_duplicate"])


if __name__ == "__main__":
    unittest.main()
