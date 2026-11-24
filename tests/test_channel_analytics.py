import unittest

from aiogram.enums import ChatMemberStatus

from channel_analytics import classify_membership_change


class ChannelMembershipClassificationTests(unittest.TestCase):
    def test_member_join(self):
        self.assertEqual(classify_membership_change(ChatMemberStatus.LEFT, ChatMemberStatus.MEMBER), "join")

    def test_member_rejoins_after_ban(self):
        self.assertEqual(classify_membership_change(ChatMemberStatus.KICKED, ChatMemberStatus.MEMBER), "join")

    def test_admin_promotion_is_not_a_new_join(self):
        self.assertIsNone(classify_membership_change(ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR))

    def test_member_leave(self):
        self.assertEqual(classify_membership_change(ChatMemberStatus.MEMBER, ChatMemberStatus.LEFT), "leave")

    def test_member_ban_is_a_leave(self):
        self.assertEqual(classify_membership_change(ChatMemberStatus.MEMBER, ChatMemberStatus.KICKED), "leave")

    def test_restriction_change_is_not_a_leave(self):
        self.assertIsNone(classify_membership_change(ChatMemberStatus.MEMBER, ChatMemberStatus.RESTRICTED))


if __name__ == "__main__":
    unittest.main()
