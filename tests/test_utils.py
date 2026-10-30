import unittest
from unittest.mock import patch, MagicMock
from collectors import normalize_url, story_key, _source_label, _parse_feed, _domain_fallback
from engine import _close_title, _already_seen, _format_post
from config import DEFAULT_RSS_URLS, _rss_urls
from story import Story


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

    def test_cross_source_title_with_publisher_suffix(self):
        self.assertTrue(_close_title(
            "India conducts successful flight trial of long-range hypersonic missile - ET Defence",
            "India conducts successful flight trial of long-range hypersonic missile | The Diplomat",
        ))

    def test_cross_source_title_with_source_label(self):
        self.assertTrue(_close_title(
            "India, China reach agreement on patrol arrangements along LAC - Indian Defence News",
            "India, China reach agreement on patrol arrangements along LAC",
        ))

    def test_already_seen_url_deduplication(self):
        class DummyStateStore:
            state = {
                "items": {
                    "k1": {"url": "https://example.com/article?utm_source=rss", "title": "Headline A"}
                }
            }

        story_dup = Story(
            key="k2",
            source="Test Source",
            title="Headline Different But Same URL",
            url="https://example.com/article?utm_medium=social",
            published_at="2026-10-30T00:00:00Z",
            published_ts=1793318400.0,
            summary="Summary",
        )
        self.assertTrue(_already_seen(story_dup, DummyStateStore(), []))

    def test_already_seen_in_cycle(self):
        class DummyEmptyStateStore:
            state = {"items": {}}

        story1 = Story(
            key="k1",
            source="The Diplomat",
            title="India and France hold bilateral defence dialogue in New Delhi",
            url="https://example.com/story1",
            published_at="2026-10-30T00:00:00Z",
            published_ts=1793318400.0,
            summary="Summary",
        )
        story2 = Story(
            key="k2",
            source="ET Defence",
            title="India and France hold bilateral defence dialogue in New Delhi - ET Defence",
            url="https://example.com/story2",
            published_at="2026-10-30T00:00:00Z",
            published_ts=1793318400.0,
            summary="Summary",
        )
        self.assertTrue(_already_seen(story2, DummyEmptyStateStore(), [story1]))


class TestConfigRSS(unittest.TestCase):
    def test_all_11_rss_urls_present(self):
        expected_urls = [
            "https://www.mea.gov.in/press-releases.htm?dtl/rss",
            "https://thediplomat.com/regions/south-asia/feed/",
            "https://thediplomat.com/category/asia-geopolitics/feed/",
            "https://thediplomat.com/category/asia-defense/feed/",
            "https://defence.economictimes.indiatimes.com/rss/topstories",
            "https://defence.economictimes.indiatimes.com/rss/policy-partnerships",
            "https://defence.economictimes.indiatimes.com/rss/military-systems",
            "https://defence.economictimes.indiatimes.com/rss/research-security",
            "https://www.livefistdefence.com/feed/",
            "https://www.indiandefensenews.in/feeds/posts/default?alt=rss",
            "https://api.thehawk.in/rss/international-relations/india-china",
        ]
        self.assertEqual(len(DEFAULT_RSS_URLS), 11)
        self.assertEqual(DEFAULT_RSS_URLS, expected_urls)

    def test_rss_urls_default_and_custom(self):
        with patch.dict("os.environ", {}, clear=True):
            urls = _rss_urls()
            self.assertEqual(len(urls), 11)
            self.assertEqual(urls, DEFAULT_RSS_URLS)

        with patch.dict("os.environ", {"WEBSITE_RSS_URLS": "https://custom.com/rss1, https://custom.com/rss2"}, clear=True):
            urls = _rss_urls()
            self.assertEqual(urls, ["https://custom.com/rss1", "https://custom.com/rss2"])


class TestSourceLabeling(unittest.TestCase):
    def test_source_labels(self):
        cases = [
            ("https://www.mea.gov.in/press-releases.htm?dtl/rss", "MEA India"),
            ("https://thediplomat.com/regions/south-asia/feed/", "The Diplomat (South Asia)"),
            ("https://thediplomat.com/category/asia-geopolitics/feed/", "The Diplomat (Asia Geopolitics)"),
            ("https://thediplomat.com/category/asia-defense/feed/", "The Diplomat (Asia Defense)"),
            ("https://defence.economictimes.indiatimes.com/rss/topstories", "ET Defence"),
            ("https://defence.economictimes.indiatimes.com/rss/policy-partnerships", "ET Defence"),
            ("https://defence.economictimes.indiatimes.com/rss/military-systems", "ET Defence"),
            ("https://defence.economictimes.indiatimes.com/rss/research-security", "ET Defence"),
            ("https://www.livefistdefence.com/feed/", "Livefist Defence"),
            ("https://www.indiandefensenews.in/feeds/posts/default?alt=rss", "Indian Defence News"),
            ("https://api.thehawk.in/rss/international-relations/india-china", "The Hawk"),
        ]
        for url, expected_label in cases:
            with self.subTest(url=url):
                self.assertEqual(_source_label(url), expected_label)


class TestErrorHandling(unittest.TestCase):
    def test_domain_fallback(self):
        fb_url, fb_source = _domain_fallback("MEA India", "mea.gov.in")
        self.assertIn("mea.gov.in", fb_url)
        self.assertIn("MEA India (indexed fallback)", fb_source)

        fb_url, fb_source = _domain_fallback("The Diplomat (South Asia)", "thediplomat.com")
        self.assertIn("thediplomat.com", fb_url)
        self.assertIn("The Diplomat (indexed fallback)", fb_source)

        # Do not loop infinitely if already an indexed fallback
        fb_url, fb_source = _domain_fallback("MEA India (indexed fallback)", "news.google.com")
        self.assertIsNone(fb_url)

    @patch("collectors._get_with_retries")
    def test_feed_http_error_handled_gracefully(self, mock_get):
        mock_response = MagicMock()
        mock_response.ok = False
        mock_response.status_code = 404
        mock_get.return_value = mock_response

        # Non-fallback feed returns empty list without raising
        res = _parse_feed("https://example.com/bad-feed.xml", "Unknown Source", 36)
        self.assertEqual(res, [])

    @patch("collectors._get_with_retries")
    def test_malformed_xml_handled_gracefully(self, mock_get):
        mock_response = MagicMock()
        mock_response.ok = True
        mock_response.content = b"<html><head><title>Not RSS</title></head><body>Bad content</body></html>"
        mock_get.return_value = mock_response

        res = _parse_feed("https://example.com/malformed.xml", "Custom Source", 36)
        self.assertEqual(res, [])


class TestTelegramFormat(unittest.TestCase):
    def test_format_post_output(self):
        story = Story(
            key="k123",
            source="The Diplomat (South Asia)",
            title="India and US Sign Major Bilateral Agreement",
            url="https://thediplomat.com/2026/10/india-us-agreement/",
            published_at="2026-10-30T04:00:00Z",
            published_ts=1793332800.0,
            summary="Sample summary",
        )
        item = {
            "headline": "India and US Sign Major Bilateral Agreement",
            "summary": "भारत और अमेरिका ने आज एक महत्वपूर्ण द्विपक्षीय समझौते पर हस्ताक्षर किए।",
            "analysis": "इस समझौते से रक्षा और प्रौद्योगिकी सहयोग मजबूत होगा।",
            "topics": ["Defence", "Diplomacy"],
        }
        post_text = _format_post(item, story)
        self.assertIn("🌍 <b>India and US Sign Major Bilateral Agreement</b>", post_text)
        self.assertIn("IST", post_text)
        self.assertIn("🏷️ Defence · Diplomacy", post_text)
        self.assertIn("भारत और अमेरिका ने", post_text)
        self.assertIn("🇮🇳 <b>भारत पर असर:</b> इस समझौते से रक्षा और प्रौद्योगिकी सहयोग मजबूत होगा।", post_text)
        self.assertIn('🗞️ स्रोत: <a href="https://thediplomat.com/2026/10/india-us-agreement/">The Diplomat (South Asia)</a>', post_text)


if __name__ == "__main__":
    unittest.main()
