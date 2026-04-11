import unittest

import fetcher


class NotificationDispatchTest(unittest.TestCase):
    def test_source_rule_matches_case_insensitive_source_id(self):
        sent_payloads = []
        original_send = fetcher.send_bot_message
        try:
            def _fake_send(token, chat_id, text):
                sent_payloads.append((token, chat_id, text))
                return True

            fetcher.send_bot_message = _fake_send
            items = [
                {
                    "id": "n1",
                    "title": "Test",
                    "summary": "Body",
                    "importance": 5,
                    "source": "My Source",
                    "source_id": "KozYtskyy_Maksym_Official",
                    "matched_keywords": [],
                    "url": "",
                }
            ]
            rules = [
                {
                    "enabled": True,
                    "type": "source_hit",
                    "target_chat_id": "-100123",
                    "params": {"source_ids": ["kozytskyy_maksym_official"]},
                }
            ]

            sent = fetcher.notify_by_rules(items, rules, "token")
            self.assertEqual(sent, 1)
            self.assertEqual(len(sent_payloads), 1)
        finally:
            fetcher.send_bot_message = original_send


if __name__ == "__main__":
    unittest.main()
