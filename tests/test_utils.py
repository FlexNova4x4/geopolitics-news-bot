import unittest
from collectors import normalize_url, story_key
from engine import _close_title


class TestDedupeHelpers(unittest.TestCase):
    def test_tracking_query_removed(self):
        self.assertEqual(
            normalize_url("https://News.Example.com/story/?utm_source=foo&x=1#frag"),
            "https://news.example.com/story?x=1",
        )

    def test_same_url_same_key(self):
        self.assertEqual(
            story_key("https://example.com/a?utm_medium=x", "A story"),
            story_key("https://example.com/a", "A story"),
        )

    def test_similar_long_headlines(self):
        self.assertTrue(_close_title(
            "Country A announces new sanctions against Country B after diplomatic talks",
            "Country A announces new sanctions against Country B after diplomatic talks today",
        ))

    def test_different_headlines(self):
        self.assertFalse(_close_title(
            "Oil prices rise as shipping tensions increase",
            "Diplomats announce a new regional peace agreement",
        ))


if __name__ == "__main__":
    unittest.main()
