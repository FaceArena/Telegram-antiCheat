import unittest

from textfilters import find_stopword, normalize_text


class TextFilterTests(unittest.TestCase):
    def test_whitespace_is_collapsed(self):
        self.assertEqual(normalize_text("a\t  b\n c"), "a b c")

    def test_unicode_format_controls_are_removed(self):
        self.assertEqual(normalize_text("sp\u200bam"), "spam")
        self.assertEqual(normalize_text("sp\u202eam"), "spam")
        self.assertEqual(normalize_text("sp\u00adam"), "spam")

    def test_compatibility_normalization_and_casefold(self):
        self.assertEqual(normalize_text("ＳＰＡＭ ЁЖ"), "spam еж")

    def test_stopword_matches_whole_word(self):
        self.assertEqual(find_stopword("hello SPAM!", ("spam",)), "spam")
        self.assertIsNone(find_stopword("spammer", ("spam",)))

    def test_stopword_matches_normalized_phrase(self):
        self.assertEqual(
            find_stopword("this is a\t bad   word!", ("bad word",)),
            "bad word",
        )

    def test_empty_stopword_list(self):
        self.assertIsNone(find_stopword("anything", ()))



if __name__ == "__main__":
    unittest.main()
