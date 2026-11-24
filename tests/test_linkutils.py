import unittest

from linkutils import is_allowed, normalize_host


class LinkNormalizationTests(unittest.TestCase):
    def test_normalizes_scheme_www_and_path(self):
        self.assertEqual(
            normalize_host("https://www.Example.com/path?q=1"),
            "example.com",
        )

    def test_normalizes_idn(self):
        self.assertEqual(normalize_host("https://пример.рф"), "xn--e1afmkfd.xn--p1ai")

    def test_rejects_ambiguous_and_invalid_urls(self):
        self.assertIsNone(normalize_host(r"https://example.com\@evil.test"))
        self.assertIsNone(normalize_host("https://example.com:99999"))
        self.assertIsNone(normalize_host("https://exa mple.com"))

    def test_userinfo_does_not_hide_real_host(self):
        self.assertEqual(
            normalize_host("https://example.com@evil.test/path"),
            "evil.test",
        )

    def test_whitelist_matches_domain_boundary_only(self):
        self.assertTrue(is_allowed("cdn.example.com", ["example.com"]))
        self.assertTrue(is_allowed("example.com", ["example.com"]))
        self.assertFalse(is_allowed("notexample.com", ["example.com"]))
        self.assertFalse(is_allowed("example.com.evil.test", ["example.com"]))

    def test_localhost_is_not_silently_treated_as_unknown(self):
        self.assertEqual(normalize_host("http://localhost"), "localhost")


if __name__ == "__main__":
    unittest.main()
