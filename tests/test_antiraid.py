import unittest
from antiraid import (
    register_join,
    threshold_reached,
    validate_raid_config,
)


class FakeTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeConnection:
    def __init__(self, join_count=0, active=False):
        self.join_count = join_count
        self.active = active
        self.executed = []

    def transaction(self):
        return FakeTransaction()

    async def execute(self, query, *args):
        self.executed.append((query, args))
        return "OK"

    async def fetchval(self, query, *args):
        if "SELECT COUNT(*)" in query:
            return self.join_count
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


class AntiRaidThresholdTests(unittest.TestCase):
    def test_below_threshold_does_not_trigger(self):
        self.assertFalse(threshold_reached(7, 8))

    def test_exact_threshold_triggers(self):
        self.assertTrue(threshold_reached(8, 8))

    def test_above_threshold_triggers(self):
        self.assertTrue(threshold_reached(12, 8))

    def test_limit_of_three_is_supported(self):
        self.assertFalse(threshold_reached(2, 3))
        self.assertTrue(threshold_reached(3, 3))

    def test_invalid_limits_are_rejected(self):
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
                    validate_raid_config(*config)


class AntiRaidDatabaseFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_threshold_activates_lockdown_once(self):
        connection = FakeConnection(join_count=8, active=False)
        result = await register_join(
            FakePool(connection), -1001, 42, 8, 10, 300
        )

        self.assertEqual(result, (8, True, True))
        self.assertTrue(any("pg_advisory_xact_lock" in q for q, _ in connection.executed))
        self.assertTrue(any("INSERT INTO raid_state" in q for q, _ in connection.executed))

    async def test_below_threshold_does_not_activate_lockdown(self):
        connection = FakeConnection(join_count=7, active=False)
        result = await register_join(
            FakePool(connection), -1001, 42, 8, 10, 300
        )

        self.assertEqual(result, (7, False, False))
        self.assertFalse(any("INSERT INTO raid_state" in q for q, _ in connection.executed))

    async def test_active_lockdown_is_not_retriggered_or_extended(self):
        connection = FakeConnection(join_count=10, active=True)
        result = await register_join(
            FakePool(connection), -1001, 42, 8, 10, 300
        )

        self.assertEqual(result, (10, True, False))
        self.assertFalse(any("INSERT INTO raid_state" in q for q, _ in connection.executed))


if __name__ == "__main__":
    unittest.main()
