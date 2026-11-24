import unittest
from pathlib import Path
from unittest.mock import AsyncMock

import bot


class LogRetentionTests(unittest.IsolatedAsyncioTestCase):
    async def test_bot_blocking_is_opt_in_for_new_chats_without_rewriting_existing_rows(self):
        schema = (Path(__file__).resolve().parents[1] / "schema.sql").read_text()

        self.assertRegex(schema, r"block_bots\s+BOOLEAN NOT NULL DEFAULT FALSE")
        self.assertIn("ALTER COLUMN block_bots SET DEFAULT FALSE", schema)
        self.assertNotIn("UPDATE chat_settings SET block_bots", schema)

    async def test_cleanup_deletes_old_logs_in_bounded_batches(self):
        pool = type("PoolStub", (), {})()
        pool.execute = AsyncMock(side_effect=["DELETE 5000", "DELETE 17"])

        deleted = await bot.cleanup_old_logs(pool)

        self.assertEqual(deleted, 5017)
        self.assertEqual(pool.execute.await_count, 2)
        for call in pool.execute.await_args_list:
            self.assertIn("DELETE FROM logs", call.args[0])
            self.assertIn("created_at <", call.args[0])
            self.assertEqual(call.args[1], bot.LOG_RETENTION_DAYS)

    async def test_cleanup_stops_after_empty_batch(self):
        pool = type("PoolStub", (), {})()
        pool.execute = AsyncMock(return_value="DELETE 0")

        deleted = await bot.cleanup_old_logs(pool)

        self.assertEqual(deleted, 0)
        pool.execute.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
