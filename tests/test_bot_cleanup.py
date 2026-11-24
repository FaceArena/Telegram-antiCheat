import unittest

from aiogram.enums import ChatMemberStatus

from bot_cleanup import is_new_bot_join


class BotJoinDetectionTests(unittest.TestCase):
    def test_new_bot_join_is_recorded(self):
        self.assertTrue(is_new_bot_join(ChatMemberStatus.LEFT, ChatMemberStatus.MEMBER, True))

    def test_kicked_bot_rejoining_is_recorded(self):
        self.assertTrue(is_new_bot_join(ChatMemberStatus.KICKED, ChatMemberStatus.ADMINISTRATOR, True))

    def test_human_is_never_recorded_as_bot(self):
        self.assertFalse(is_new_bot_join(ChatMemberStatus.LEFT, ChatMemberStatus.MEMBER, False))

    def test_status_change_of_existing_member_is_not_a_join(self):
        self.assertFalse(is_new_bot_join(ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, True))

    def test_bot_leaving_is_not_a_join(self):
        self.assertFalse(is_new_bot_join(ChatMemberStatus.MEMBER, ChatMemberStatus.LEFT, True))


if __name__ == "__main__":
    unittest.main()
