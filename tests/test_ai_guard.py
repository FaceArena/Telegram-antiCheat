import io
import json
import unittest
from urllib.error import HTTPError
from unittest.mock import patch

import ai_guard


class AIGuardTests(unittest.IsolatedAsyncioTestCase):
    def test_candidate_detection(self):
        self.assertTrue(ai_guard.is_candidate("Проверьте кошелёк и введите seed phrase"))
        self.assertTrue(ai_guard.is_candidate("Обычная новость", has_link=True))
        self.assertFalse(ai_guard.is_candidate("Привет, как дела?"))
        self.assertTrue(ai_guard.is_candidate("Кто может оценить мои фоточки? я не против❤️"))
        self.assertTrue(ai_guard.is_candidate("Ищу людей для работы, пишите в личку, 200 долларов"))

    async def test_missing_api_key_skips_external_request(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertIsNone(await ai_guard.analyze_message("Переведи деньги"))

    async def test_violation_escalation_uses_unique_message_ids(self):
        class Transaction:
            async def __aenter__(self): return self
            async def __aexit__(self, *args): return False

        class Connection:
            def __init__(self):
                self.events = set()
                self.queries = []
            def transaction(self): return Transaction()
            async def execute(self, query, *args):
                self.queries.append((query, args))
                if "INSERT INTO ai_guard_events" in query:
                    self.events.add(tuple(args[:3]))
            async def fetchval(self, query, *args):
                return sum(1 for event in self.events if event[:2] == tuple(args))

        class Acquire:
            def __init__(self, conn): self.conn = conn
            async def __aenter__(self): return self.conn
            async def __aexit__(self, *args): return False

        class Pool:
            def __init__(self): self.conn = Connection()
            def acquire(self): return Acquire(self.conn)

        pool = Pool()
        chat_id, user_id = -1001, 42
        self.assertEqual(await ai_guard.record_violation(pool, chat_id, user_id, 10, "phishing", 0.99), (1, "delete"))
        self.assertEqual(await ai_guard.record_violation(pool, chat_id, user_id, 10, "phishing", 0.99), (1, "delete"))
        self.assertEqual(await ai_guard.record_violation(pool, chat_id, user_id, 11, "scam", 0.93), (2, "mute"))
        self.assertEqual(await ai_guard.record_violation(pool, chat_id, user_id, 12, "phishing", 0.99), (3, "mute"))
        self.assertEqual(await ai_guard.record_violation(pool, chat_id, user_id, 13, "malware", 0.99), (4, "ban"))
        insert = next(query for query, _ in pool.conn.queries if "INSERT INTO ai_guard_events" in query)
        self.assertIn("ON CONFLICT (chat_id, user_id, message_id) DO NOTHING", insert)


    def test_request_uses_json_mode_and_completion_budget(self):
        model_content = json.dumps({
            "risk": "safe",
            "category": "none",
            "confidence": 0.9,
            "reason": "Безопасное сообщение",
        }, ensure_ascii=False)
        response_body = json.dumps({
            "choices": [{"message": {"content": model_content}}]
        }, ensure_ascii=False).encode("utf-8")
        captured = {}

        def fake_urlopen(request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return io.BytesIO(response_body)

        with (
            patch.dict("os.environ", {"GROQ_API_KEY": "test-key"}),
            patch.object(ai_guard, "urlopen", side_effect=fake_urlopen),
        ):
            result = ai_guard._request_groq("Проверьте аккаунт", False)

        self.assertEqual(result.risk, "safe")
        self.assertEqual(captured["timeout"], ai_guard.REQUEST_TIMEOUT)
        self.assertEqual(
            captured["request"].get_header("User-agent"),
            "KimGuard/1.0 (+https://github.com/FaceArena/Telegram-antiCheat)",
        )
        payload = json.loads(captured["request"].data.decode("utf-8"))
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertEqual(payload["max_completion_tokens"], 512)

    def test_http_403_logs_structured_groq_error_without_secret(self):
        body = (
            b'{"error":{"message":"Model is blocked at project level",'
            b'"code":"model_permission_blocked_project"}}'
        )
        error = HTTPError(ai_guard.API_URL, 403, "Forbidden", None, io.BytesIO(body))
        with (
            patch.dict("os.environ", {"GROQ_API_KEY": "never-log-this-key"}),
            patch.object(ai_guard, "urlopen", side_effect=error),
            self.assertLogs(level="WARNING") as captured,
        ):
            self.assertIsNone(ai_guard._request_groq("Проверьте аккаунт", False))
        output = "\n".join(captured.output)
        self.assertIn("model_permission_blocked_project", output)
        self.assertIn("Model is blocked at project level", output)
        self.assertNotIn("never-log-this-key", output)

    def test_http_403_logs_plain_text_body_and_request_id(self):
        body = b"Access denied by upstream policy"
        headers = {"Content-Type": "text/plain", "x-request-id": "req-test-123"}
        error = HTTPError(ai_guard.API_URL, 403, "Forbidden", headers, io.BytesIO(body))
        with (
            patch.dict("os.environ", {"GROQ_API_KEY": "test-key"}),
            patch.object(ai_guard, "urlopen", side_effect=error),
            self.assertLogs(level="WARNING") as captured,
        ):
            self.assertIsNone(ai_guard._request_groq("Проверьте аккаунт", False))
        output = "\\n".join(captured.output)
        self.assertIn("Access denied by upstream policy", output)
        self.assertIn("req-test-123", output)
        self.assertIn("text/plain", output)

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
