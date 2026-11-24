import unittest

from flood import validate_flood_settings


class FloodSettingsValidationTests(unittest.TestCase):
    def test_accepts_only_settings_that_tighten_the_baseline(self):
        for limit in range(2, 6):
            for window in range(3, 6):
                with self.subTest(limit=limit, window=window):
                    validate_flood_settings(limit, window)

    def test_rejects_limits_that_would_be_silently_clamped(self):
        for limit in (1, 6, 30):
            with self.subTest(limit=limit):
                with self.assertRaises(ValueError):
                    validate_flood_settings(limit, 5)

    def test_rejects_windows_that_would_be_silently_clamped(self):
        for window in (2, 6, 60):
            with self.subTest(window=window):
                with self.assertRaises(ValueError):
                    validate_flood_settings(5, window)


if __name__ == "__main__":
    unittest.main()
