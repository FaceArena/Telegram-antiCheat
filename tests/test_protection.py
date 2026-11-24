import unittest
from datetime import datetime
from unittest.mock import AsyncMock

from protection import apply_punishment


class PunishmentTests(unittest.IsolatedAsyncioTestCase):
    async def test_mute_disables_every_message_permission(self):
        bot = type("BotStub", (), {})()
        bot.restrict_chat_member = AsyncMock()

        await apply_punishment(bot, -100123, 456, "mute")

        bot.restrict_chat_member.assert_awaited_once()
        kwargs = bot.restrict_chat_member.await_args.kwargs
        permissions = kwargs["permissions"]
        fields = (
            "can_send_messages",
            "can_send_audios",
            "can_send_documents",
            "can_send_photos",
            "can_send_videos",
            "can_send_video_notes",
            "can_send_voice_notes",
            "can_send_polls",
            "can_send_other_messages",
            "can_add_web_page_previews",
        )
        for field in fields:
            with self.subTest(field=field):
                self.assertIs(getattr(permissions, field), False)
        self.assertGreater(kwargs["until_date"], datetime.now().astimezone())


if __name__ == "__main__":
    unittest.main()
