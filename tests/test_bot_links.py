import unittest

from bot import (
    build_admin_add_link,
    build_about_text,
    build_settings_deep_link,
    settings_button_label,
    protection_callback_data,
)
from protection import format_moderation_notice


class TelegramAdminLinkTests(unittest.TestCase):
    def test_about_text_describes_current_group_features_only(self):
        text = build_about_text()
        self.assertIn("Антифлуд", text)
        self.assertIn("CAPTCHA", text)
        self.assertIn("оценить фотографии", text)
        self.assertNotIn("заявок", text.lower())
        self.assertNotIn("аналитика", text.lower())
        self.assertNotIn("каналов", text.lower())
        self.assertIn("\\n\\n", text)

    def test_group_link_requests_only_moderation_rights(self):
        self.assertEqual(
            build_admin_add_link("@KimGuardBot"),
            "https://t.me/KimGuardBot?startgroup&admin=delete_messages+restrict_members",
        )

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
            build_admin_add_link("https://example.com")

    def test_notice_reports_action_and_reason(self):
        notice = format_moderation_notice("@bad_user", "ban", "флуд")
        self.assertIn("@bad_user — заблокирован", notice)
        self.assertIn("Причина: флуд", notice)

    def test_notice_is_bounded_for_untrusted_details(self):
        notice = format_moderation_notice("x" * 500, "ban", "y" * 500, "z" * 500)
        self.assertLessEqual(len(notice), 500)

    def test_settings_button_labels_accept_only_group_types(self):
        self.assertEqual(settings_button_label("group"), "Настройки группы")
        self.assertEqual(settings_button_label("supergroup"), "Настройки группы")
        with self.assertRaises(ValueError):
            settings_button_label("channel")

    def test_protection_hub_routes_only_groups(self):
        self.assertEqual(protection_callback_data("group", -1001), "m:-1001")
        self.assertEqual(protection_callback_data("supergroup", -1002), "m:-1002")
        with self.assertRaises(ValueError):
            protection_callback_data("channel", -1003)

    def test_settings_button_rejects_unknown_chat_type(self):
        with self.assertRaises(ValueError):
            settings_button_label("private")


if __name__ == "__main__":
    unittest.main()
