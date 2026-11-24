import unittest

from join_requests import (
    decide_join_request_action,
    register_request,
    validate_request_config,
)


class FakeTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeConnection:
    def __init__(self, request_count=0, active=False):
        self.request_count = request_count
        self.active = active
        self.executed = []

    def transaction(self):
        return FakeTransaction()

    async def execute(self, query, *args):
        self.executed.append((query, args))
        return "OK"

    async def fetchval(self, query, *args):
        if "SELECT COUNT(*)" in query:
            return self.request_count
        if "SELECT EXISTS" in query:
            return self.active
        raise AssertionError(f"Unexpected SQL: {query}")


class FakeAcquire:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakePool:
    def __init__(self, connection):
        self.connection = connection

    def acquire(self):
        return FakeAcquire(self.connection)


class JoinRequestConfigTests(unittest.TestCase):
    def test_valid_config(self):
        validate_request_config(8, 10, 300)

    def test_invalid_config_is_rejected(self):
        for config in (
            (2, 10, 300),
            (31, 10, 300),
            (8, 4, 300),
            (8, 121, 300),
            (8, 10, 59),
            (8, 10, 3601),
        ):
            with self.subTest(config=config):
                with self.assertRaises(ValueError):
                    validate_request_config(*config)


class JoinRequestDatabaseFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_threshold_activates_lockdown(self):
        connection = FakeConnection(request_count=8, active=False)
        result = await register_request(FakePool(connection), -1001, 42, 8, 10, 300)

        self.assertEqual(result, (8, True, True))
        self.assertTrue(any("pg_advisory_xact_lock" in q for q, _ in connection.executed))
        self.assertTrue(any("INSERT INTO channel_request_state" in q for q, _ in connection.executed))

    async def test_below_threshold_does_not_activate_lockdown(self):
        connection = FakeConnection(request_count=7, active=False)
        result = await register_request(FakePool(connection), -1001, 42, 8, 10, 300)

        self.assertEqual(result, (7, False, False))
        self.assertFalse(any("INSERT INTO channel_request_state" in q for q, _ in connection.executed))

    async def test_existing_lockdown_is_not_extended(self):
        connection = FakeConnection(request_count=12, active=True)
        result = await register_request(FakePool(connection), -1001, 42, 8, 10, 300)

        self.assertEqual(result, (12, True, False))
        self.assertFalse(any("INSERT INTO channel_request_state" in q for q, _ in connection.executed))


class JoinRequestDecisionTests(unittest.TestCase):
    def test_auto_accept_approves_when_no_lockdown(self):
        self.assertEqual(decide_join_request_action(False, False, True), "approve")
        self.assertEqual(decide_join_request_action(True, False, True), "approve")

    def test_anti_raid_lockdown_takes_precedence_over_auto_accept(self):
        self.assertEqual(decide_join_request_action(True, True, True), "decline")

    def test_manual_mode_leaves_normal_requests_pending(self):
        self.assertEqual(decide_join_request_action(False, False, False), "pending")
        self.assertEqual(decide_join_request_action(True, False, False), "pending")

    def test_auto_accept_does_not_override_lockdown_when_protection_off(self):
        # A stale lockdown is ignored when protection is disabled.
        self.assertEqual(decide_join_request_action(False, True, True), "approve")


if __name__ == "__main__":
    unittest.main()
