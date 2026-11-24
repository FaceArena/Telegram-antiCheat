import unittest

from flood import record_flood_message, _event_key


class FakeTransaction:
    async def __aenter__(self): return self
    async def __aexit__(self, exc_type, exc, tb): return False


class FakeConnection:
    def __init__(self, count=1):
        self.count = count
        self.execute_calls = []
        self.fetchval_calls = []
    def transaction(self): return FakeTransaction()
    async def execute(self, query, *args):
        self.execute_calls.append((query, args))
        return "OK"
    async def fetchval(self, query, *args):
        self.fetchval_calls.append((query, args))
        return self.count


class FakeAcquire:
    def __init__(self, connection): self.connection = connection
    async def __aenter__(self): return self.connection
    async def __aexit__(self, exc_type, exc, tb): return False


class FakePool:
    def __init__(self, connection): self.connection = connection
    def acquire(self): return FakeAcquire(self.connection)


class FloodDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_records_under_advisory_lock_and_counts(self):
        conn = FakeConnection(count=4)
        count = await record_flood_message(
            FakePool(conn), chat_id=-1001, user_id=42,
            message_id=101, media_group_id=None, window=10,
        )
        self.assertEqual(count, 4)
        self.assertIn("pg_advisory_xact_lock", conn.execute_calls[0][0])
        self.assertIn("DELETE FROM flood_events", conn.execute_calls[1][0])
        self.assertIn("INSERT INTO flood_events", conn.execute_calls[2][0])
        self.assertEqual(conn.execute_calls[2][1], (-1001, 42, "message:101"))
        self.assertIn("COUNT(*)", conn.fetchval_calls[0][0])

    async def test_album_messages_share_one_key(self):
        self.assertEqual(_event_key(10, "album-xyz"), "album:album-xyz")
        self.assertEqual(_event_key(11, "album-xyz"), "album:album-xyz")

    async def test_message_redelivery_has_stable_key(self):
        self.assertEqual(_event_key(77, None), _event_key(77, None))

    async def test_rejects_invalid_window(self):
        with self.assertRaises(ValueError):
            await record_flood_message(
                FakePool(FakeConnection()), chat_id=1, user_id=2,
                message_id=3, media_group_id=None, window=120,
            )


if __name__ == "__main__":
    unittest.main()
