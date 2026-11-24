import unittest

from bot import build_admin_add_links


class TelegramAdminLinkTests(unittest.TestCase):
    def test_group_link_requests_only_moderation_rights(self):
        group_url, _ = build_admin_add_links("@KimGuardBot")
        self.assertEqual(
            group_url,
            "https://t.me/KimGuardBot?startgroup&admin=delete_messages+restrict_members",
        )

    def test_channel_link_requests_post_and_delete_rights(self):
        _, channel_url = build_admin_add_links("KimGuardBot")
        self.assertEqual(
            channel_url,
            "https://t.me/KimGuardBot?startchannel&admin=post_messages+edit_messages+delete_messages",
        )

    def test_invalid_bot_username_is_rejected(self):
        with self.assertRaises(ValueError):
            build_admin_add_links("https://example.com")


if __name__ == "__main__":
    unittest.main()
