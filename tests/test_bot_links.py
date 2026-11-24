import unittest

from bot import build_admin_add_links
from protection import format_moderation_notice


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
            "https://t.me/KimGuardBot?startchannel&admin=delete_messages",
        )

    def test_invalid_bot_username_is_rejected(self):
        with self.assertRaises(ValueError):
            build_admin_add_links("https://example.com")

    def test_notice_reports_action_and_reason(self):
        notice = format_moderation_notice("@bad_user", "ban", "флуд")
        self.assertIn("@bad_user — заблокирован", notice)
        self.assertIn("Причина: флуд", notice)

    def test_notice_is_bounded_for_untrusted_details(self):
        notice = format_moderation_notice("x" * 500, "ban", "y" * 500, "z" * 500)
        self.assertLessEqual(len(notice), 500)


if __name__ == "__main__":
    unittest.main()
