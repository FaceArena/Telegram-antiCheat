import unittest

from bot import (
    build_admin_add_links,
    build_settings_deep_link,
    is_channel_info_callback,
)
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
            "https://t.me/KimGuardBot?startchannel&admin=post_messages+edit_messages+delete_messages",
        )

    def test_channel_info_callback_matches_only_a_single_chat_id(self):
        self.assertTrue(is_channel_info_callback("c:-1001234567890"))
        self.assertFalse(is_channel_info_callback("c:-1001234567890:12345:token"))
        self.assertFalse(is_channel_info_callback("c:"))
        self.assertFalse(is_channel_info_callback("c:not-a-chat-id"))
        self.assertFalse(is_channel_info_callback("captcha:-1001234567890:12345"))

    def test_settings_link_targets_the_added_group(self):
        self.assertEqual(
            build_settings_deep_link("@KimGuardBot", -1001234567890),
            "https://t.me/KimGuardBot?start=settings_-1001234567890",
        )

    def test_invalid_settings_link_username_is_rejected(self):
        with self.assertRaises(ValueError):
            build_settings_deep_link("https://example.com", -1001234567890)

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
