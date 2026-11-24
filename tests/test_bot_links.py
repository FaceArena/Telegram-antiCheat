import unittest

from bot import (
    build_admin_add_links,
    build_settings_deep_link,
    is_channel_info_callback,
    settings_button_label,
    protection_callback_data,
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
            "https://t.me/KimGuardBot?startchannel&admin=invite_users",
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

    def test_settings_button_labels_match_chat_type(self):
        self.assertEqual(settings_button_label("group"), "Настройки группы")
        self.assertEqual(settings_button_label("supergroup"), "Настройки группы")
        self.assertEqual(settings_button_label("channel"), "Настройка канала")


    def test_protection_hub_routes_groups_and_channels_to_separate_panels(self):
        self.assertEqual(protection_callback_data("group", -1001), "m:-1001")
        self.assertEqual(protection_callback_data("supergroup", -1002), "m:-1002")
        self.assertEqual(protection_callback_data("channel", -1003), "c:-1003")

    def test_protection_callback_rejects_unknown_chat_type(self):
        with self.assertRaises(ValueError):
            protection_callback_data("private", 123)

    def test_settings_button_rejects_unknown_chat_type(self):
        with self.assertRaises(ValueError):
            settings_button_label("private")


if __name__ == "__main__":
    unittest.main()
