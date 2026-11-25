import unittest
from unittest.mock import patch

import ai_guard


class AIGuardTests(unittest.IsolatedAsyncioTestCase):
    def test_candidate_detection(self):
        self.assertTrue(ai_guard.is_candidate("Проверьте кошелёк и введите seed phrase"))
        self.assertTrue(ai_guard.is_candidate("Обычная новость", has_link=True))
        self.assertFalse(ai_guard.is_candidate("Привет, как дела?"))

    async def test_missing_api_key_skips_external_request(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertIsNone(await ai_guard.analyze_message("Переведи деньги"))

    async def test_violation_escalation_uses_unique_message_ids(self):
        chat_id, user_id = -1001, 42
        self.assertEqual(await ai_guard.record_violation(chat_id, user_id, 10, 0.99), (1, "delete"))
        self.assertEqual(await ai_guard.record_violation(chat_id, user_id, 10, 0.99), (1, "delete"))
        self.assertEqual(await ai_guard.record_violation(chat_id, user_id, 11, 0.93), (2, "mute"))
        self.assertEqual(await ai_guard.record_violation(chat_id, user_id, 12, 0.99), (3, "mute"))
        self.assertEqual(await ai_guard.record_violation(chat_id, user_id, 13, 0.99), (4, "ban"))

    async def test_model_failure_does_not_create_a_cached_verdict(self):
        with (
            patch.dict("os.environ", {"GROQ_API_KEY": "test-key"}),
            patch.object(ai_guard, "_request_groq", return_value=None) as request,
        ):
            self.assertIsNone(await ai_guard.analyze_message("Переведи деньги"))
            self.assertIsNone(await ai_guard.analyze_message("Переведи деньги"))
        self.assertEqual(request.call_count, 2)


if __name__ == "__main__":
    unittest.main()
