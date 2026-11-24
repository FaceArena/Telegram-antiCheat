import unittest

from antiraid import threshold_reached


class AntiRaidThresholdTests(unittest.TestCase):
    def test_below_threshold_does_not_trigger(self):
        self.assertFalse(threshold_reached(7, 8))

    def test_exact_threshold_triggers(self):
        self.assertTrue(threshold_reached(8, 8))

    def test_above_threshold_triggers(self):
        self.assertTrue(threshold_reached(12, 8))

    def test_limit_of_three_is_supported(self):
        self.assertFalse(threshold_reached(2, 3))
        self.assertTrue(threshold_reached(3, 3))


if __name__ == "__main__":
    unittest.main()
