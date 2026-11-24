import unittest

from textfilters import FloodTracker, find_stopword, normalize_text


class TextFilterTests(unittest.TestCase):
    def test_whitespace_is_collapsed(self):
        self.assertEqual(normalize_text("a\\t  b\\n c"), "a b c")

    def test_unicode_format_controls_are_removed(self):
        self.assertEqual(normalize_text("sp\\u200bam"), "spam")
        self.assertEqual(normalize_text("sp\\u202eam"), "spam")
        self.assertEqual(normalize_text("sp\\u00adam"), "spam")

    def test_compatibility_normalization_and_casefold(self):
        self.assertEqual(normalize_text("ＳＰＡＭ ЁЖ"), "spam еж")

    def test_stopword_matches_whole_word(self):
        self.assertEqual(find_stopword("hello SPAM!", ("spam",)), "spam")
        self.assertIsNone(find_stopword("spammer", ("spam",)))

    def test_stopword_matches_normalized_phrase(self):
        self.assertEqual(
            find_stopword("this is a\\t bad   word!", ("bad word",)),
            "bad word",
        )

    def test_empty_stopword_list(self):
        self.assertIsNone(find_stopword("anything", ()))

    def test_flood_tracker_sliding_window(self):
        tracker = FloodTracker()
        key = (1, 2)
        counts = [
            tracker.hit(key, limit=3, window=10, now=now)
            for now in (0, 1, 2, 3, 14)
        ]
        self.assertEqual(counts, [1, 2, 3, 4, 1])

    def test_album_is_counted_once_when_updates_are_consecutive(self):
        tracker = FloodTracker()
        key = (1, 2)
        self.assertFalse(tracker.is_album_repeat(key, "album-1", now=1))
        self.assertTrue(tracker.is_album_repeat(key, "album-1", now=2))
        self.assertFalse(tracker.is_album_repeat(key, "album-2", now=3))


if __name__ == "__main__":
    unittest.main()
