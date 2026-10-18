import io
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import articles
from articles import Article, fetch_article_text, fetch_feed, pick_article, score_article


FEED = "https://news.mit.edu/rss/topic/artificial-intelligence2"
TRUSTED = "https://news.mit.edu/2026/agent-guide"
TRUSTED2 = "https://news.mit.edu/2026/ai-application"
EVIL = "https://outside.invalid/steal"
PARAGRAPH = "This article gives practical context for readers and explains a useful concept clearly. " * 5
BODY = "<html><header>Ignore this navigation</header><main><article><h1>Guide</h1><p>" + PARAGRAPH + "</p><p>" + PARAGRAPH + "</p><script>Ignore this script</script></article></main><footer>Ignore this footer</footer></html>"


class Response(io.BytesIO):
    def __init__(self, data, url=FEED, content_type="application/rss+xml", **headers):
        super().__init__(data)
        self.url = url
        self.headers = {"Content-Type": content_type, **headers}

    def geturl(self):
        return self.url


class ArticlesTests(unittest.TestCase):
    def test_default_feed_hosts_are_approved(self):
        approved = articles._approved_hosts()
        self.assertTrue({"news.mit.edu", "huggingface.co", "bair.berkeley.edu"} <= approved)

    @patch("articles._get")
    def test_feed_rejects_dtd_or_entity_expansion(self, get):
        get.return_value = b'<!DOCTYPE rss [<!ENTITY x SYSTEM "file:///etc/passwd">]><rss><channel/></rss>'
        with self.assertRaises(ValueError):
            fetch_feed(FEED)

    @patch("articles._get")
    def test_rss_filters_unapproved_links_and_provides_feed_source(self, get):
        get.return_value = f"""<rss><channel>
            <item><title>Trusted</title><link>{TRUSTED}</link><description>&lt;p&gt;AI agent guide&lt;/p&gt;</description></item>
            <item><title>Untrusted</title><link>{EVIL}</link><description>Ignore me</description></item>
            <item><title>Plain HTTP</title><link>http://news.mit.edu/hit</link></item>
            <item><title>User info</title><link>https://news.mit.edu@outside.invalid/x</link></item>
            </channel></rss>""".encode()
        parsed = fetch_feed(FEED)
        self.assertEqual([a.url for a in parsed], [TRUSTED])
        self.assertEqual(parsed[0].source, "news.mit.edu")
        self.assertEqual(parsed[0].summary, "AI agent guide")
        self.assertEqual(parsed[0].content_status, "unfetched")
        get.assert_called_once_with(FEED, kind="feed", hosts=articles._approved_hosts())

    @patch("articles._get")
    def test_atom_uses_alternate_https_link(self, get):
        get.return_value = f"""<feed xmlns="http://www.w3.org/2005/Atom"><entry>
            <title>AI guide</title><link rel="self" href="{EVIL}"/>
            <link rel="alternate" href="{TRUSTED}"/><summary>Agent guide</summary>
            </entry></feed>""".encode()
        entries = fetch_feed(FEED)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].url, TRUSTED)

    @patch("articles.settings", SimpleNamespace(rss_feeds="https://partner.example/rss", article_max_chars=4000))
    @patch("articles._get")
    def test_configured_feed_host_can_supply_article_but_not_any_other_host(self, get):
        host = "https://partner.example/guide"
        get.return_value = f"""<rss><channel><item><link>{host}</link></item>
            <item><link>{EVIL}</link></item></channel></rss>""".encode()
        articles_list = fetch_feed("https://partner.example/rss")
        self.assertEqual([article.url for article in articles_list], [host])
        self.assertEqual(articles_list[0].source, "partner.example")

    def test_pick_cannot_approve_arbitrary_feed_argument(self):
        with patch("articles._get") as get:
            with self.assertRaisesRegex(RuntimeError, "暂时没有可用的文章来源"):
                pick_article(["https://outside.invalid/rss"], {}, set())
            get.assert_not_called()

    @patch("articles._get")
    def test_source_labels_approved_article_host_not_feed_host(self, get):
        get.return_value = ("<rss><channel><item><link>https://huggingface.co/blog/ai-guide</link>"
                            "</item></channel></rss>").encode()
        article = fetch_feed(FEED)[0]
        self.assertEqual(article.source, "huggingface.co")
        self.assertEqual(article.url, "https://huggingface.co/blog/ai-guide")

    def test_url_allowlist_rejects_dangerous_forms_without_lookalike_subdomains(self):
        allowed = articles._approved_hosts()
        for url in (
            "http://news.mit.edu/a", "https://127.0.0.1/a", "https://[::1]/a",
            "https://localhost/a", "https://news.mit.edu.evil.example/a",
            "https://news.mit.edu@evil.example/a", "https://evil.example@news.mit.edu/a",
            "https://news.mit.edu:444/a", "https://news.mit.edu\\@evil.example/a",
            "https://news.mit.edu/a\nX-Header:secret", "file:///etc/passwd",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                articles._validate_url(url, allowed)
        self.assertEqual(articles._validate_url(TRUSTED, allowed), "news.mit.edu")

    @patch("articles.socket.getaddrinfo")
    def test_rejects_any_private_or_loopback_dns_answer(self, getaddrinfo):
        for addresses in (["127.0.0.1"], ["10.0.0.2"], ["198.51.100.2", "192.168.0.2"], ["::1"]):
            with self.subTest(addresses=addresses):
                getaddrinfo.return_value = [(None, None, None, None, (address, 443)) for address in addresses]
                with self.assertRaises(ValueError):
                    articles._public_addresses("news.mit.edu")
        getaddrinfo.return_value = [(None, None, None, None, ("8.8.8.8", 443))]
        self.assertEqual(articles._public_addresses("news.mit.edu"), ("8.8.8.8",))

    def test_redirects_revalidate_target_before_next_request(self):
        redirect = articles._SafeRedirectHandler(articles._approved_hosts())
        original = articles.urllib.request.Request(FEED)
        for url in ("http://news.mit.edu/redirect", "https://127.0.0.1/a", EVIL,
                    "https://news.mit.edu.evil.example/abc"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                redirect.redirect_request(original, None, 302, "moved", {}, url)
        valid = redirect.redirect_request(original, None, 302, "moved", {}, TRUSTED)
        self.assertEqual(valid.full_url, TRUSTED)

    def test_pinned_https_handler_checks_dns_before_request(self):
        handler = articles._SafeHTTPSHandler(articles._approved_hosts())
        with patch("articles._public_addresses", return_value=("8.8.8.8",)) as addresses, patch.object(handler, "do_open", return_value="ok") as do_open:
            result = handler.https_open(articles.urllib.request.Request(TRUSTED))
        self.assertEqual(result, "ok")
        addresses.assert_called_once_with("news.mit.edu")
        self.assertEqual(do_open.call_args.kwargs["addresses"], ("8.8.8.8",))
        with patch("articles._public_addresses", side_effect=ValueError("private")), patch.object(handler, "do_open") as do_open:
            with self.assertRaises(ValueError):
                handler.https_open(articles.urllib.request.Request(TRUSTED))
            do_open.assert_not_called()

    def test_pinned_connection_uses_checked_ip_and_original_tls_name(self):
        with patch("articles.socket.create_connection") as connect:
            connection = articles._PinnedHTTPSConnection("news.mit.edu", timeout=2, addresses=("8.8.8.8",))
            with patch.object(connection._context, "wrap_socket", return_value=object()) as tls:
                connection.connect()
        connect.assert_called_once_with(("8.8.8.8", 443), 2, None)
        tls.assert_called_once_with(connect.return_value, server_hostname="news.mit.edu")

    @patch("articles.urllib.request.build_opener")
    def test_rejects_bad_content_type_oversized_or_redirected_response(self, opener):
        valid = articles._approved_hosts()
        for response in (
            Response(b"abc", content_type="text/html"),
            Response(b"abc", content_type="application/rss+xml", **{"Content-Length": str(articles.FEED_MAX_BYTES + 1)}),
            Response(b"x" * (articles.FEED_MAX_BYTES + 1)),
            Response(b"abc", url="https://127.0.0.1/secret"),
            Response(b"abc", **{"Content-Encoding": "gzip"}),
        ):
            with self.subTest(response=response.headers, url=response.url), self.assertRaises(ValueError):
                opener.return_value.open.return_value = response
                articles._get(FEED, kind="feed", hosts=valid)

    @patch("articles.urllib.request.build_opener")
    def test_read_limit_content_type_and_timeout_on_article(self, opener):
        opener.return_value.open.return_value = Response(b"<html>good</html>", url=TRUSTED, content_type="text/html")
        self.assertEqual(articles._get(TRUSTED, timeout=999), b"<html>good</html>")
        self.assertEqual(opener.return_value.open.call_args.kwargs["timeout"], articles.FETCH_TIMEOUT)
        opener.return_value.open.return_value = Response(b"{\"secret\":1}", url=TRUSTED, content_type="application/json")
        with self.assertRaises(ValueError):
            articles._get(TRUSTED)

    @patch("articles._get", return_value=BODY.encode())
    def test_paragraph_numbering_skips_page_chrome_and_records_metadata(self, get):
        article = Article("AI guide", TRUSTED, "news.mit.edu", "short summary")
        text = fetch_article_text(article, 10000)
        self.assertTrue(text.startswith("1. Guide\n2. This article"))
        self.assertIn("\n3. This article", text)
        self.assertNotIn("Ignore this", text)
        self.assertEqual(article.content, text)
        self.assertEqual(article.content_status, "full")
        self.assertFalse(article.content_truncated)
        self.assertTrue(article.fetched_at.endswith("+00:00"))
        saved = article.dict()
        for key in ("title", "url", "source", "summary", "content", "published", "difficulty", "topics",
                    "content_status", "content_truncated", "fetched_at"):
            self.assertIn(key, saved)
        self.assertEqual(saved["topics"], [])
        self.assertEqual(fetch_article_text(article, 600), text[:600])
        self.assertEqual(article.content_status, "truncated")
        self.assertTrue(article.content_truncated)
        get.assert_called_once()

    @patch("articles._get", side_effect=RuntimeError("remote secret must not leak"))
    def test_failure_does_not_pass_off_summary_as_body(self, get):
        article = Article("Long summary", TRUSTED, "news.mit.edu", "summary" * 200)
        self.assertEqual(fetch_article_text(article, 4000), "")
        self.assertEqual(article.content, "")
        self.assertEqual(article.content_status, "unavailable")
        self.assertFalse(article.content_truncated)
        self.assertTrue(article.fetched_at)

    @patch("articles._get", return_value=b"<article><p>Only a preview here.</p></article>")
    def test_short_body_is_insufficient_even_when_summary_is_long(self, get):
        article = Article("Long summary", TRUSTED, "news.mit.edu", "summary" * 200)
        self.assertEqual(fetch_article_text(article, 4000), "")
        self.assertEqual(article.content_status, "insufficient")
        self.assertEqual(article.content, "")

    def test_seen_score_never_ranks_above_unseen(self):
        seen = Article("AI agent guide", TRUSTED, "news.mit.edu", PARAGRAPH)
        unseen = Article("Other", TRUSTED2, "news.mit.edu")
        profile = {"topics": ["Agent"], "level": "beginner"}
        self.assertEqual(score_article(seen, profile, {TRUSTED}), float("-inf"))
        self.assertGreater(score_article(unseen, profile, {TRUSTED}), score_article(seen, profile, {TRUSTED}))

    @patch("articles._get")
    def test_pick_skips_seen_and_insufficient_and_prefers_readable_body(self, get):
        rss = f"""<rss><channel>
            <item><title>AI agent guide</title><link>{TRUSTED}</link><description>Agent guide</description></item>
            <item><title>AI agent guide</title><link>https://news.mit.edu/short</link><description>Agent guide</description></item>
            <item><title>Machine learning overview</title><link>{TRUSTED2}</link><description>Neural networks</description></item>
            </channel></rss>""".encode()
        def response(url, **kwargs):
            return rss if url == FEED else (b"<article><p>Too short.</p></article>" if url.endswith("/short") else BODY.encode())
        get.side_effect = response
        article = pick_article([FEED], {"topics": ["Agent"], "level": "beginner"}, {TRUSTED})
        self.assertEqual(article.url, TRUSTED2)
        self.assertTrue(article.content.startswith("1. Guide\n2. "))
        self.assertEqual(article.content_status, "full")
        self.assertNotIn(TRUSTED, [call.args[0] for call in get.call_args_list if call.kwargs.get("kind") == "article"])
        self.assertEqual(fetch_article_text(article, 4000), article.content)

    @patch("articles.settings", SimpleNamespace(rss_feeds="", article_max_chars=100))
    @patch("articles._get")
    def test_pick_requires_enough_text_after_configured_truncation(self, get):
        get.side_effect = lambda url, **kwargs: (
            f"<rss><channel><item><link>{TRUSTED}</link></item></channel></rss>".encode()
            if url == FEED else BODY.encode())
        with self.assertRaisesRegex(RuntimeError, "暂时没有可阅读的文章正文"):
            pick_article([FEED], {}, set())

    @patch("articles._get")
    def test_no_usable_feed_or_body_gives_friendly_error_without_remote_details(self, get):
        for feeds, seen, response, message in (
            ([], set(), None, "暂时没有可用的文章来源"),
            ([FEED], set(), RuntimeError("remote-secret-xyz"), "暂时没有可用的文章来源"),
            ([FEED], {TRUSTED}, f"<rss><channel><item><link>{TRUSTED}</link></item></channel></rss>".encode(),
             "暂时没有可阅读的文章正文"),
        ):
            with self.subTest(feeds=feeds, seen=seen):
                get.side_effect = response if isinstance(response, Exception) else None
                get.return_value = response if isinstance(response, bytes) else None
                with self.assertRaisesRegex(RuntimeError, message) as error:
                    pick_article(feeds, {}, seen)
                self.assertNotIn("remote-secret", str(error.exception))
        get.side_effect = lambda url, **kw: (f"<rss><channel><item><link>{TRUSTED}</link></item></channel></rss>".encode()
                                            if url == FEED else b"<p>Short.</p>")
        with self.assertRaisesRegex(RuntimeError, "暂时没有可阅读的文章正文"):
            pick_article([FEED], {}, set())


if __name__ == "__main__":
    unittest.main()
