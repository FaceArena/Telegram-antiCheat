import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram.enums import ChatType, MessageEntityType
from aiogram.types import Chat, Message, MessageEntity

import protection
from protection import (
    apply_punishment,
    find_hosts,
    find_mentions,
    find_bot_username_mentions,
    find_channel_mentions,
    _CHANNEL_MENTION_CACHE,
)


class AuditLogTests(unittest.IsolatedAsyncioTestCase):
    async def test_write_log_persists_actor_and_target_separately(self):
        db = SimpleNamespace(execute=AsyncMock())
        with patch.object(protection.common, "pool", db):
            await protection.write_log(
                -100123,
                456,
                "flood",
                "6 messages in 5s",
                actor_user_id=None,
            )
        db.execute.assert_awaited_once()
        args = db.execute.await_args.args
        self.assertIn("actor_user_id, target_user_id", args[0])
        self.assertEqual(args[1:], (-100123, 456, "flood", "6 messages in 5s", None, 456))

    async def test_moderation_notice_uses_four_second_temporary_sender(self):
        bot = SimpleNamespace()
        with patch(
            "protection.common.send_temporary_message", new=AsyncMock()
        ) as send:
            await protection.notify_moderation(
                bot, -100123, "Spammer", "mute", "флуд", "6 сообщений"
            )
        send.assert_awaited_once()
        self.assertEqual(send.await_args.args[:2], (bot, -100123))
        self.assertIn("Spammer", send.await_args.args[2])


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

    def test_bot_username_mentions_match_bot_and_underscore_bot_suffix(self):
        text = "Пишите @MusicBot или @helper_bot, но не @botanical"
        mentions = ["@MusicBot", "@helper_bot", "@botanical"]
        entities = [
            MessageEntity(
                type=MessageEntityType.MENTION,
                offset=text.index(value),
                length=len(value),
            )
            for value in mentions
        ]
        message = Message(
            message_id=6,
            date=datetime.now(timezone.utc),
            chat=Chat(id=-100123, type=ChatType.SUPERGROUP),
            text=text,
            entities=entities,
        )
        self.assertEqual(
            find_bot_username_mentions(message),
            ["@MusicBot", "@helper_bot"],
        )


class BotAdvertisingModerationTests(unittest.IsolatedAsyncioTestCase):
    def make_message(self, text="@MusicBot"):
        entity = MessageEntity(
            type=MessageEntityType.MENTION,
            offset=text.index("@"),
            length=len(text) - text.index("@"),
        )
        return SimpleNamespace(
            chat=SimpleNamespace(id=-100123, type=ChatType.SUPERGROUP),
            sender_chat=None,
            is_automatic_forward=False,
            from_user=SimpleNamespace(
                id=456, is_bot=False, username="advertiser", full_name="Advertiser"
            ),
            text=text,
            caption=None,
            entities=[entity],
            caption_entities=None,
            delete=AsyncMock(),
        )

    async def test_bot_advertising_bans_message_author_even_if_group_default_is_delete(self):
        message = self.make_message()
        bot = SimpleNamespace()
        settings = {
            "block_links": False,
            "block_mentions": False,
            "block_bot_mentions": True,
            "punishment": "delete",
        }
        with (
            patch.object(protection, "get_settings", AsyncMock(return_value=settings)),
            patch.object(protection, "is_chat_admin", AsyncMock(return_value=False)),
            patch.object(protection, "apply_punishment", AsyncMock()) as punish,
            patch.object(protection, "write_log", AsyncMock()) as write_log,
            patch.object(protection, "notify_moderation", AsyncMock()) as notify,
        ):
            await protection.on_message(message, bot)

        message.delete.assert_awaited_once()
        punish.assert_awaited_once_with(bot, -100123, 456, "ban")
        write_log.assert_awaited_once()
        notify.assert_awaited_once()

    async def test_admin_is_exempt_from_bot_advertising_autoban(self):
        message = self.make_message()
        bot = SimpleNamespace()
        settings = {
            "block_links": False,
            "block_mentions": False,
            "block_bot_mentions": True,
            "punishment": "delete",
        }
        with (
            patch.object(protection, "get_settings", AsyncMock(return_value=settings)),
            patch.object(protection, "is_chat_admin", AsyncMock(return_value=True)),
            patch.object(protection, "apply_punishment", AsyncMock()) as punish,
        ):
            await protection.on_message(message, bot)

        message.delete.assert_not_awaited()
        punish.assert_not_awaited()

    async def test_bot_advertising_autoban_is_opt_in(self):
        message = self.make_message()
        bot = SimpleNamespace()
        settings = {
            "block_links": False,
            "block_mentions": False,
            "block_bot_mentions": False,
            "punishment": "delete",
        }
        with (
            patch.object(protection, "get_settings", AsyncMock(return_value=settings)),
            patch.object(protection, "is_chat_admin", AsyncMock(return_value=False)),
            patch.object(protection, "apply_punishment", AsyncMock()) as punish,
        ):
            await protection.on_message(message, bot)

        message.delete.assert_not_awaited()
        punish.assert_not_awaited()


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
