import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock

from aiogram.enums import ChatType, MessageEntityType
from aiogram.types import Chat, Message, MessageEntity

from protection import apply_punishment, find_hosts


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

    def test_unparseable_url_entity_is_preserved_for_safe_deletion(self):
        raw_url = "https://exa mple.com"
        message = Message(
            message_id=1,
            date=datetime.now(timezone.utc),
            chat=Chat(id=-100123, type=ChatType.SUPERGROUP),
            text=f"visit {raw_url}",
            entities=[
                MessageEntity(
                    type=MessageEntityType.URL,
                    offset=6,
                    length=len(raw_url),
                )
            ],
        )
        self.assertEqual(find_hosts(message), [None])

    def test_valid_url_entity_returns_normalized_host(self):
        raw_url = "https://www.Example.com/path"
        message = Message(
            message_id=2,
            date=datetime.now(timezone.utc),
            chat=Chat(id=-100123, type=ChatType.SUPERGROUP),
            text=f"visit {raw_url}",
            entities=[
                MessageEntity(
                    type=MessageEntityType.URL,
                    offset=6,
                    length=len(raw_url),
                )
            ],
        )
        self.assertEqual(find_hosts(message), ["example.com"])


if __name__ == "__main__":
    unittest.main()
