import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock

from aiogram.enums import ChatType, MessageEntityType
from aiogram.types import Chat, Message, MessageEntity

from protection import apply_punishment, find_hosts, find_mentions, find_channel_mentions, _CHANNEL_MENTION_CACHE


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


    def test_channel_username_mention_is_detected(self):
        text = "Подписывайтесь на @example_channel"
        message = Message(
            message_id=3,
            date=datetime.now(timezone.utc),
            chat=Chat(id=-100123, type=ChatType.SUPERGROUP),
            text=text,
            entities=[
                MessageEntity(
                    type=MessageEntityType.MENTION,
                    offset=18,
                    length=len("@example_channel"),
                )
            ],
        )
        self.assertEqual(find_mentions(message), ["@example_channel"])


    async def test_public_channel_mention_is_resolved(self):
        text = "Подписывайтесь на @public_channel_test"
        message = Message(
            message_id=4,
            date=datetime.now(timezone.utc),
            chat=Chat(id=-100123, type=ChatType.SUPERGROUP),
            text=text,
            entities=[MessageEntity(type=MessageEntityType.MENTION, offset=18, length=len("@public_channel_test"))],
        )
        _CHANNEL_MENTION_CACHE.pop("@public_channel_test", None)
        bot = type("BotStub", (), {})()
        bot.get_chat = AsyncMock(return_value=type("ChatStub", (), {"type": ChatType.CHANNEL})())
        self.assertEqual(await find_channel_mentions(message, bot), ["@public_channel_test"])
        bot.get_chat.assert_awaited_once_with("@public_channel_test")

    async def test_user_mention_is_not_treated_as_channel(self):
        text = "Привет @ordinary_user_test"
        message = Message(
            message_id=5,
            date=datetime.now(timezone.utc),
            chat=Chat(id=-100123, type=ChatType.SUPERGROUP),
            text=text,
            entities=[MessageEntity(type=MessageEntityType.MENTION, offset=7, length=len("@ordinary_user_test"))],
        )
        _CHANNEL_MENTION_CACHE.pop("@ordinary_user_test", None)
        bot = type("BotStub", (), {})()
        bot.get_chat = AsyncMock(return_value=type("ChatStub", (), {"type": ChatType.PRIVATE})())
        self.assertEqual(await find_channel_mentions(message, bot), [])
        bot.get_chat.assert_awaited_once_with("@ordinary_user_test")
if __name__ == "__main__":
    unittest.main()
