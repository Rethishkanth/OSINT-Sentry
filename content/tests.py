"""Tests for Module 2 — the content fetcher.

Not one of these makes a network request. Everything that would touch the
outside world is behind a single function, content.http.request, so the tests
replace that and drive the whole pipeline on canned bytes. The parts worth
testing here are exactly the parts that are hard to check by eye: what happens
when robots.txt is unreachable, when a redirect points at localhost, when a
feed is 400MB, and when a page is mostly navigation.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone as dt_timezone
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from core.models import Source
from credibility.models import CredibilityScore, TrustTier

from . import corroborate, http, robots
from .extract import extract_article, parse_datetime, strip_html
from .feeds import FeedError, looks_like_feed, parse_feed
from .fetchers import FeedStatus, WebFetcher
from .models import ContentItem


def response(body: bytes, url='https://example.com/', content_type='text/html'):
    return http.FetchResponse(
        url=url, status=200, content_type=content_type, body=body, truncated=False
    )


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------

class ParseDatetimeTests(SimpleTestCase):
    def test_iso_8601_with_zulu(self):
        parsed = parse_datetime('2024-03-01T09:30:00Z')
        self.assertEqual(parsed, datetime(2024, 3, 1, 9, 30, tzinfo=dt_timezone.utc))

    def test_rfc_2822_as_used_by_rss(self):
        parsed = parse_datetime('Fri, 01 Mar 2024 09:30:00 GMT')
        self.assertEqual(parsed.year, 2024)
        self.assertEqual(parsed.month, 3)

    def test_naive_input_is_read_as_utc(self):
        # Losing a publication date is worse than being a few hours out.
        parsed = parse_datetime('2024-03-01 09:30:00')
        self.assertIsNotNone(parsed.tzinfo)

    def test_nonsense_is_none_not_an_exception(self):
        self.assertIsNone(parse_datetime('last Tuesday-ish'))
        self.assertIsNone(parse_datetime(''))
        self.assertIsNone(parse_datetime(None))


# ---------------------------------------------------------------------------
# HTML extraction
# ---------------------------------------------------------------------------

ARTICLE_HTML = """
<html lang="en-GB">
<head>
  <title>Fallback title</title>
  <meta property="og:title" content="Flooding closes three bridges">
  <meta property="article:published_time" content="2024-03-01T09:30:00Z">
  <meta name="author" content="Rae Okonkwo">
  <link rel="canonical" href="https://example.com/news/flooding">
  <link rel="alternate" type="application/rss+xml" href="/feed.xml">
  <script>var tracking = "should not appear";</script>
  <style>body { color: red; }</style>
</head>
<body>
  <nav>Home About Contact Subscribe now</nav>
  <header>Example News</header>
  <article>
    <p>Three bridges across the river were closed on Friday morning after
       overnight rainfall pushed water levels past the flood barrier.</p>
    <p>The council said the closures would remain in place until engineers
       had inspected the supports, which could take several days.</p>
  </article>
  <footer>Copyright Example News. All rights reserved.</footer>
</body>
</html>
"""


class ExtractArticleTests(SimpleTestCase):
    def setUp(self):
        self.article = extract_article(ARTICLE_HTML, url='https://example.com/news/flooding?utm_source=x')

    def test_opengraph_title_beats_the_title_tag(self):
        self.assertEqual(self.article.title, 'Flooding closes three bridges')

    def test_body_comes_from_the_article_element(self):
        self.assertIn('Three bridges across the river', self.article.body)
        self.assertIn('until engineers', self.article.body)

    def test_furniture_is_excluded(self):
        for noise in ('Subscribe now', 'All rights reserved', 'tracking', 'color: red'):
            self.assertNotIn(noise, self.article.body)

    def test_author_and_date_and_language(self):
        self.assertEqual(self.article.author, 'Rae Okonkwo')
        self.assertEqual(self.article.published_at.year, 2024)
        self.assertEqual(self.article.language, 'en-GB')

    def test_feed_links_are_absolute(self):
        self.assertEqual(self.article.feeds, ['https://example.com/feed.xml'])

    def test_canonical_url_drops_tracking_parameters(self):
        # This is what stops one article being stored once per campaign link.
        self.assertEqual(self.article.canonical_url, 'https://example.com/news/flooding')


class ExtractFallbackTests(SimpleTestCase):
    def test_falls_back_to_paragraphs_without_an_article_element(self):
        html = '<html><body><nav>Menu</nav><p>%s</p></body></html>' % ('word ' * 60)
        self.assertIn('word', extract_article(html).body)

    def test_json_ld_supplies_author_and_date(self):
        html = """
        <html><head><script type="application/ld+json">
        {"@type": "NewsArticle", "headline": "Ignored",
         "datePublished": "2023-11-05T08:00:00Z",
         "author": {"@type": "Person", "name": "Sam Vittori"}}
        </script></head><body><p>Body text.</p></body></html>
        """
        article = extract_article(html)
        self.assertEqual(article.author, 'Sam Vittori')
        self.assertEqual(article.published_at.year, 2023)

    def test_json_ld_is_never_treated_as_body_text(self):
        html = '<html><body><script type="application/ld+json">{"a":1}</script>' \
               '<p>Real text.</p></body></html>'
        self.assertNotIn('"a"', extract_article(html).body)

    def test_a_profile_url_is_not_an_author_name(self):
        html = '<html><head><meta name="author" content="https://x.com/someone">' \
               '</head><body><p>Text.</p></body></html>'
        self.assertEqual(extract_article(html).author, '')

    def test_malformed_html_still_yields_what_was_parsed(self):
        article = extract_article('<html><body><p>Half a doc' * 3)
        self.assertIn('Half a doc', article.body)


class StripHtmlTests(SimpleTestCase):
    def test_tags_removed_entities_decoded(self):
        self.assertEqual(strip_html('<b>Rain</b> &amp; wind'), 'Rain & wind')

    def test_empty_input(self):
        self.assertEqual(strip_html(''), '')


# ---------------------------------------------------------------------------
# Feeds
# ---------------------------------------------------------------------------

RSS = b"""<?xml version="1.0"?>
<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/">
  <channel>
    <title>Example News</title>
    <item>
      <title>Flooding closes three bridges</title>
      <link>https://example.com/news/flooding</link>
      <description>&lt;p&gt;Three bridges closed.&lt;/p&gt;</description>
      <pubDate>Fri, 01 Mar 2024 09:30:00 GMT</pubDate>
      <dc:creator>Rae Okonkwo</dc:creator>
    </item>
    <item>
      <title>Council budget approved</title>
      <link>/news/budget</link>
    </item>
  </channel>
</rss>
"""

ATOM = b"""<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Example</title>
  <entry>
    <title>Atom entry</title>
    <link rel="edit" href="https://example.com/edit/1"/>
    <link rel="alternate" href="https://example.com/posts/1"/>
    <published>2024-02-02T12:00:00Z</published>
    <author><name>Jo Baker</name></author>
  </entry>
</feed>
"""

BILLION_LAUGHS = b"""<?xml version="1.0"?>
<!DOCTYPE rss [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;">]>
<rss version="2.0"><channel><item><title>&b;</title>
<link>https://example.com/x</link></item></channel></rss>
"""


class FeedParsingTests(SimpleTestCase):
    def test_rss_entries(self):
        entries = parse_feed(RSS, base_url='https://example.com/feed.xml')
        self.assertEqual(len(entries), 2)
        first = entries[0]
        self.assertEqual(first.url, 'https://example.com/news/flooding')
        self.assertEqual(first.title, 'Flooding closes three bridges')
        self.assertEqual(first.author, 'Rae Okonkwo')
        self.assertEqual(first.published_at.year, 2024)
        # Markup inside a description is stripped, not stored raw.
        self.assertEqual(first.summary, 'Three bridges closed.')

    def test_relative_entry_links_are_resolved(self):
        entries = parse_feed(RSS, base_url='https://example.com/feed.xml')
        self.assertEqual(entries[1].url, 'https://example.com/news/budget')

    def test_atom_prefers_the_alternate_link(self):
        entries = parse_feed(ATOM)
        self.assertEqual(entries[0].url, 'https://example.com/posts/1')
        self.assertEqual(entries[0].author, 'Jo Baker')

    def test_doctype_is_refused(self):
        # Entity expansion is the denial-of-service risk in parsing XML from
        # hosts we do not control; no real feed carries a DOCTYPE.
        with self.assertRaises(FeedError) as caught:
            parse_feed(BILLION_LAUGHS)
        self.assertIn('DOCTYPE', str(caught.exception))

    def test_html_is_not_mistaken_for_a_feed(self):
        self.assertFalse(looks_like_feed(b'<!doctype html><html><body>'))
        with self.assertRaises(FeedError):
            parse_feed(b'<!doctype html><html><body>hi</body></html>')

    def test_malformed_xml_raises_feed_error_not_a_crash(self):
        with self.assertRaises(FeedError):
            parse_feed(b'<rss version="2.0"><channel><item>')


# ---------------------------------------------------------------------------
# Host safety and limits
# ---------------------------------------------------------------------------

def fake_dns(ip):
    """A getaddrinfo that resolves everything to one address."""
    return lambda *args, **kwargs: [(2, 1, 6, '', (ip, 443))]


class HostPolicyTests(SimpleTestCase):
    def test_loopback_is_refused(self):
        with patch('content.http.socket.getaddrinfo', fake_dns('127.0.0.1')):
            with self.assertRaises(http.Blocked):
                http.assert_fetchable('https://totally-normal-news.example/')

    def test_cloud_metadata_address_is_refused(self):
        # 169.254.169.254 is the reason this check exists at all.
        with patch('content.http.socket.getaddrinfo', fake_dns('169.254.169.254')):
            with self.assertRaises(http.Blocked):
                http.assert_fetchable('https://news.example/')

    def test_private_range_is_refused(self):
        with patch('content.http.socket.getaddrinfo', fake_dns('10.0.0.5')):
            with self.assertRaises(http.Blocked):
                http.assert_fetchable('https://news.example/')

    def test_public_address_is_allowed(self):
        with patch('content.http.socket.getaddrinfo', fake_dns('93.184.216.34')):
            self.assertEqual(http.assert_fetchable('https://news.example/'), 'news.example')

    def test_non_http_schemes_are_refused(self):
        for url in ('file:///etc/passwd', 'ftp://example.com/x', 'gopher://example.com'):
            with self.assertRaises(http.Blocked):
                http.assert_fetchable(url)

    @override_settings(CONTENT_ALLOW_PRIVATE_HOSTS=True)
    def test_the_escape_hatch_works_for_local_testing(self):
        self.assertEqual(http.assert_fetchable('http://localhost:8000/'), 'localhost')


class ByteCapTests(SimpleTestCase):
    class _Stream:
        def __init__(self, data):
            self.data = data

        def read(self, n):
            return self.data[:n]

    def test_oversized_body_is_truncated_and_flagged(self):
        body, truncated = http._read_capped(self._Stream(b'x' * 5000), 1000)
        self.assertEqual(len(body), 1000)
        self.assertTrue(truncated)

    def test_body_at_the_limit_is_not_flagged(self):
        body, truncated = http._read_capped(self._Stream(b'x' * 800), 1000)
        self.assertEqual(len(body), 800)
        self.assertFalse(truncated)


class RateLimitTests(SimpleTestCase):
    def setUp(self):
        http.reset_rate_limiter()

    def tearDown(self):
        http.reset_rate_limiter()

    def test_second_request_to_a_host_waits(self):
        http.wait_for_turn('example.com', delay=0.25)
        started = time.monotonic()
        http.wait_for_turn('example.com', delay=0.25)
        self.assertGreaterEqual(time.monotonic() - started, 0.2)

    def test_different_hosts_do_not_block_each_other(self):
        http.wait_for_turn('a.example', delay=5)
        started = time.monotonic()
        http.wait_for_turn('b.example', delay=5)
        self.assertLess(time.monotonic() - started, 1)


class _FakeHTTPResponse:
    """Enough of an http.client.HTTPResponse for content.http.request."""

    def __init__(self, url, body=b'ok', content_type='text/html', status=200):
        import email.message
        self.url = url
        self.status = status
        self._body = body
        self.headers = email.message.Message()
        self.headers['Content-Type'] = content_type

    def read(self, n):
        return self._body[:n]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _redirect(url, location):
    import email.message
    import io
    import urllib.error
    headers = email.message.Message()
    headers['Location'] = location
    return urllib.error.HTTPError(url, 302, 'Found', headers, io.BytesIO(b''))


@override_settings(CONTENT_CRAWL_DELAY=0, CONTENT_ALLOW_PRIVATE_HOSTS=False)
class RequestPolicyTests(SimpleTestCase):
    """The parts of http.request that enforce policy on every hop."""

    def setUp(self):
        http.reset_rate_limiter()

    def test_on_hop_runs_for_the_first_request(self):
        seen = []
        with patch('content.http.socket.getaddrinfo', fake_dns('93.184.216.34')), \
             patch.object(http._opener, 'open', return_value=_FakeHTTPResponse('https://a.example/')):
            http.request('https://a.example/', on_hop=seen.append)
        self.assertEqual(seen, ['https://a.example/'])

    def test_on_hop_runs_again_for_every_redirect_target(self):
        # This is the whole reason redirects are followed by hand: without it,
        # a site could redirect us straight past its own robots.txt rules.
        seen = []
        responses = [
            _redirect('https://a.example/', 'https://a.example/private/x'),
            _FakeHTTPResponse('https://a.example/private/x'),
        ]
        with patch('content.http.socket.getaddrinfo', fake_dns('93.184.216.34')), \
             patch.object(http._opener, 'open', side_effect=responses):
            http.request('https://a.example/', on_hop=seen.append)
        self.assertEqual(seen, ['https://a.example/', 'https://a.example/private/x'])

    def test_a_redirect_into_the_local_network_is_refused(self):
        def dns(host, *args, **kwargs):
            ip = '127.0.0.1' if host == 'internal.example' else '93.184.216.34'
            return [(2, 1, 6, '', (ip, 443))]

        with patch('content.http.socket.getaddrinfo', dns), \
             patch.object(http._opener, 'open',
                          side_effect=[_redirect('https://a.example/', 'https://internal.example/admin')]):
            with self.assertRaises(http.Blocked):
                http.request('https://a.example/')

    def test_robots_can_veto_a_redirect_target(self):
        responses = [
            _redirect('https://a.example/', 'https://a.example/private/x'),
            _FakeHTTPResponse('https://a.example/private/x'),
        ]
        robots.clear_cache()
        with patch('content.robots.request', return_value=response(ROBOTS)), \
             patch('content.http.socket.getaddrinfo', fake_dns('93.184.216.34')), \
             patch.object(http._opener, 'open', side_effect=responses):
            with self.assertRaises(http.Blocked):
                http.request('https://a.example/', on_hop=robots.check)
        robots.clear_cache()

    def test_a_redirect_loop_terminates(self):
        def always_redirect(*args, **kwargs):
            raise _redirect('https://a.example/', 'https://a.example/')

        with patch('content.http.socket.getaddrinfo', fake_dns('93.184.216.34')), \
             patch.object(http._opener, 'open', side_effect=always_redirect):
            with self.assertRaises(http.FetchError) as caught:
                http.request('https://a.example/')
        self.assertIn('loop', str(caught.exception))

    def test_unwanted_content_types_are_refused_before_the_body_matters(self):
        with patch('content.http.socket.getaddrinfo', fake_dns('93.184.216.34')), \
             patch.object(http._opener, 'open',
                          return_value=_FakeHTTPResponse('https://a.example/v.mp4',
                                                         content_type='video/mp4')):
            with self.assertRaises(http.Blocked):
                http.request('https://a.example/v.mp4')

    def test_the_fetcher_passes_the_robots_check_as_its_hop_callback(self):
        # Guards against the check being quietly dropped from a call site.
        from . import fetchers
        with patch('content.fetchers.request',
                   return_value=response(b'<html><body><p>hi</p></body></html>')) as fake:
            fetchers.fetch_page('https://a.example/story')
        self.assertIs(fake.call_args.kwargs['on_hop'], robots.check)


# ---------------------------------------------------------------------------
# robots.txt
# ---------------------------------------------------------------------------

ROBOTS = b"""
User-agent: *
Disallow: /private/
Crawl-delay: 7
"""


class RobotsTests(SimpleTestCase):
    def setUp(self):
        robots.clear_cache()

    def tearDown(self):
        robots.clear_cache()

    def test_disallowed_path_is_blocked_allowed_path_is_not(self):
        with patch('content.robots.request', return_value=response(ROBOTS)):
            policy = robots.policy_for('https://example.com/')
            self.assertFalse(policy.allows('https://example.com/private/x'))
            self.assertTrue(policy.allows('https://example.com/news/x'))

    def test_crawl_delay_is_read(self):
        with patch('content.robots.request', return_value=response(ROBOTS)):
            self.assertEqual(robots.policy_for('https://example.com/').crawl_delay, 7)

    def test_missing_robots_allows_everything(self):
        with patch('content.robots.request', side_effect=http.FetchError('x: HTTP 404.')):
            self.assertTrue(robots.policy_for('https://example.com/').allows('https://example.com/a'))

    def test_server_error_blocks_everything(self):
        # The important case. A 500 means we could not ask permission, and
        # that is not the same as being given it.
        with patch('content.robots.request', side_effect=http.FetchError('x: HTTP 503.')):
            policy = robots.policy_for('https://example.com/')
            self.assertFalse(policy.allows('https://example.com/anything'))

    def test_timeout_blocks_everything(self):
        with patch('content.robots.request', side_effect=http.FetchError('x: timed out.')):
            self.assertFalse(robots.policy_for('https://example.com/').allows('https://example.com/a'))

    def test_result_is_cached_so_a_crawl_asks_once(self):
        with patch('content.robots.request', return_value=response(ROBOTS)) as fake:
            for path in ('/a', '/b', '/c'):
                robots.check('https://example.com' + path)
            self.assertEqual(fake.call_count, 1)

    def test_check_raises_blocked_with_a_readable_reason(self):
        with patch('content.robots.request', return_value=response(ROBOTS)):
            with self.assertRaises(http.Blocked) as caught:
                robots.check('https://example.com/private/secret')
            self.assertIn('robots.txt', str(caught.exception))


# ---------------------------------------------------------------------------
# The fetcher, end to end
# ---------------------------------------------------------------------------

PAGE = ARTICLE_HTML.encode()


def canned(responses):
    """A stand-in for http.request that serves a URL -> bytes mapping."""
    def _request(url, **kwargs):
        for prefix, body in responses.items():
            if url.startswith(prefix):
                ctype = 'application/rss+xml' if body.lstrip().startswith(b'<?xml') else 'text/html'
                return response(body, url=url, content_type=ctype)
        raise http.FetchError(f'{url}: HTTP 404.')
    return _request


@override_settings(CONTENT_MIN_CREDIBILITY=None, CONTENT_CRAWL_DELAY=0)
class WebFetcherTests(TestCase):
    def setUp(self):
        robots.clear_cache()
        http.reset_rate_limiter()
        self.source = Source.objects.create(
            domain='example.com',
            url='https://example.com/',
            feed_url='https://example.com/feed.xml',
        )

    def test_feed_entries_become_fetched_items(self):
        with patch('content.fetchers.request', side_effect=canned({
            'https://example.com/feed.xml': RSS,
            'https://example.com/news/flooding': PAGE,
        })):
            items = WebFetcher().fetch(self.source)

        self.assertEqual(len(items), 2)
        first = next(i for i in items if 'flooding' in i.url)
        self.assertEqual(first.title, 'Flooding closes three bridges')
        self.assertIn('Three bridges across the river', first.body)
        self.source.refresh_from_db()
        self.assertEqual(self.source.feed_status, FeedStatus.OK)

    def test_a_page_that_will_not_load_falls_back_to_the_feed_summary(self):
        # One 404 article must not cost us the entry the feed already gave us.
        with patch('content.fetchers.request', side_effect=canned({
            'https://example.com/feed.xml': RSS,
        })):
            items = WebFetcher().fetch(self.source)
        first = next(i for i in items if 'flooding' in i.url)
        self.assertEqual(first.body, 'Three bridges closed.')

    def test_already_stored_urls_are_not_fetched_again(self):
        ContentItem.objects.create(source=self.source, url='https://example.com/news/flooding')
        with patch('content.fetchers.request', side_effect=canned({
            'https://example.com/feed.xml': RSS,
            'https://example.com/news/flooding': PAGE,
        })) as fake:
            items = WebFetcher().fetch(self.source)

        self.assertEqual(len(items), 1)
        requested = [call.args[0] for call in fake.call_args_list]
        self.assertNotIn('https://example.com/news/flooding', requested)

    @override_settings(CONTENT_MAX_ITEMS_PER_FETCH=1)
    def test_the_per_run_cap_is_honoured(self):
        with patch('content.fetchers.request', side_effect=canned({
            'https://example.com/feed.xml': RSS,
            'https://example.com/news/flooding': PAGE,
        })):
            self.assertEqual(len(WebFetcher().fetch(self.source)), 1)

    def test_an_unreadable_feed_is_recorded_not_raised(self):
        with patch('content.fetchers.request', side_effect=http.FetchError('boom')):
            self.assertEqual(WebFetcher().fetch(self.source), [])
        self.source.refresh_from_db()
        self.assertEqual(self.source.feed_status, FeedStatus.ERROR)

    def test_feed_discovery_reads_the_advertised_link(self):
        self.source.feed_url = ''
        self.source.save()
        with patch('content.fetchers.request', side_effect=canned({
            'https://example.com/feed.xml': RSS,
            'https://example.com/': PAGE,
        })):
            self.assertEqual(
                WebFetcher().discover_feed(self.source), 'https://example.com/feed.xml'
            )

    def test_a_feedless_source_is_not_re_probed_on_the_next_run(self):
        # Discovery costs a homepage request plus a probe of every
        # conventional path. Repeating that hourly forever is the rudeness
        # this module is meant to avoid.
        self.source.feed_url = ''
        self.source.save()
        empty = {'https://example.com/': b'<html><body>No feed</body></html>'}

        with patch('content.fetchers.request', side_effect=canned(empty)) as first:
            WebFetcher().fetch(self.source)
        self.assertGreater(first.call_count, 1)

        self.source.refresh_from_db()
        with patch('content.fetchers.request', side_effect=canned(empty)) as second:
            WebFetcher().fetch(self.source)
        second.assert_not_called()

    def test_a_person_asking_overrides_the_back_off(self):
        self.source.feed_url = ''
        self.source.feed_status = FeedStatus.NO_FEED
        self.source.feed_checked_at = timezone.now()
        self.source.save()

        with patch('content.fetchers.request', side_effect=canned({
            'https://example.com/feed.xml': RSS,
            'https://example.com/': PAGE,
        })):
            self.assertEqual(len(WebFetcher().fetch(self.source, force=True)), 2)

    def test_no_feed_anywhere_is_recorded_as_no_feed(self):
        self.source.feed_url = ''
        self.source.save()
        with patch('content.fetchers.request', side_effect=canned({
            'https://example.com/': b'<html><head></head><body>No feed here</body></html>',
        })):
            self.assertEqual(WebFetcher().fetch(self.source), [])
        self.source.refresh_from_db()
        self.assertEqual(self.source.feed_status, FeedStatus.NO_FEED)


class CredibilityGateTests(TestCase):
    """The gate must stop the crawl, not just the storing."""

    def setUp(self):
        robots.clear_cache()
        self.source = Source.objects.create(
            domain='sketchy.example', feed_url='https://sketchy.example/feed.xml'
        )
        CredibilityScore.objects.create(
            source=self.source, credibility_score=12.0, trust_tier=TrustTier.LOW
        )

    @override_settings(CONTENT_MIN_CREDIBILITY=25.0)
    def test_a_low_scoring_source_is_never_contacted(self):
        from .tasks import fetch_source

        with patch('content.fetchers.request') as fake:
            result = fetch_source.run(self.source.pk)

        # Not one request. Crawling a source we have already decided to reject
        # spends someone else's bandwidth on documents we will discard.
        fake.assert_not_called()
        self.assertEqual(result['fetched'], 0)
        self.assertIn('below threshold', result['detail'])


# ---------------------------------------------------------------------------
# Corroboration
# ---------------------------------------------------------------------------

def stub_search(candidates, available=True, failed=()):
    """A search backend returning exactly what a test wants."""
    class _Stub(corroborate.BaseCorroborationSearch):
        name = 'stub'

        def search(self, terms, published_at, exclude_domain):
            return corroborate.SearchOutcome(
                [c for c in candidates if c.domain != exclude_domain],
                available=available, failed=list(failed), searched='a stub index',
            )
    return _Stub()


def candidate(domain, title, days_off=0, snippet=''):
    return corroborate.Candidate(
        url=f'https://{domain}/story',
        title=title,
        domain=domain,
        snippet=snippet,
        published_at=timezone.now() - timedelta(days=days_off),
    )


FLOOD_STORY = (
    'Heavy rainfall across Mumbai closed three suburban rail lines on Friday. '
    'The Brihanmumbai Municipal Corporation said pumping stations in Mumbai '
    'were operating at capacity as flooding spread. Mumbai residents were told '
    'to avoid low-lying roads while the flooding continued. '
) * 4


@override_settings(CONTENT_MIN_CREDIBILITY=None)
class CorroborationTests(TestCase):
    def setUp(self):
        self.source = Source.objects.create(domain='examplenews.com')
        CredibilityScore.objects.create(
            source=self.source, credibility_score=70.0, trust_tier=TrustTier.HIGH
        )
        self.item = ContentItem.objects.create(
            source=self.source,
            url='https://examplenews.com/mumbai-floods',
            title='Mumbai floods close three rail lines',
            body=FLOOD_STORY,
            published_at=timezone.now(),
        )
        from .tasks import analyze_content
        analyze_content.run(self.item.pk)
        self.item.refresh_from_db()

    def _assess(self, backend):
        with patch('content.corroborate.get_search', return_value=backend):
            return corroborate.assess(self.item)

    # -- keywords ----------------------------------------------------------

    def test_keywords_describe_the_story(self):
        keywords = [k.lower() for k in corroborate.query_terms(corroborate.terms_for(self.item))]
        self.assertIn('mumbai', ' '.join(keywords))
        self.assertTrue(
            any('flood' in k or 'rail' in k or 'rain' in k for k in keywords),
            f'expected a subject word among {keywords}',
        )

    def test_function_words_never_become_keywords(self):
        # "about, like, what" as a search query finds nothing or everything.
        keywords = [k.lower() for k in corroborate.query_terms(corroborate.terms_for(self.item))]
        self.assertFalse({'about', 'like', 'what', 'said'} & set(keywords))

    # -- the honest-failure rules -----------------------------------------

    def test_a_failed_search_withholds_the_rating(self):
        result = self._assess(stub_search([], available=False, failed=['stub']))
        self.assertIsNone(result.score)
        self.assertEqual(result.verdict, corroborate.Verdict.UNAVAILABLE)

    def test_a_partial_search_finding_nothing_withholds_the_rating(self):
        # The bug this guards against: one backend rate-limited, another
        # returning nothing, reported together as a confident "uncorroborated".
        result = self._assess(stub_search([], available=True, failed=['gdelt']))
        self.assertIsNone(result.score)
        self.assertEqual(result.verdict, corroborate.Verdict.UNAVAILABLE)
        self.assertIn('gdelt', result.rationale)

    def test_a_partial_search_that_finds_corroboration_still_rates(self):
        # Asymmetry on purpose: evidence found is evidence found, even if we
        # did not manage to look everywhere.
        result = self._assess(stub_search(
            [candidate('other.com', 'Mumbai floods shut rail lines after rainfall')],
            available=True, failed=['gdelt'],
        ))
        self.assertIsNotNone(result.score)
        self.assertGreater(result.independent_sources, 0)

    def test_composite_reports_a_failed_backend_even_when_another_worked(self):
        composite = corroborate.CompositeSearch(backends=[
            stub_search([candidate('a.com', 'Mumbai floods')]),
            stub_search([], available=False, failed=['gdelt']),
        ])
        outcome = composite.search(['Mumbai'], None, 'examplenews.com')
        self.assertTrue(outcome.available)
        self.assertIn('gdelt', outcome.failed)

    # -- matching ----------------------------------------------------------

    def test_matching_coverage_raises_the_rating(self):
        alone = self._assess(stub_search([]))
        backed = self._assess(stub_search([
            candidate('a.com', 'Mumbai floods close rail lines amid heavy rainfall'),
            candidate('b.com', 'Rail lines shut as flooding hits Mumbai'),
            candidate('c.com', 'Mumbai rainfall floods suburban rail network'),
        ]))
        self.assertGreater(backed.score, alone.score)
        self.assertEqual(backed.independent_sources, 3)

    def test_shared_vocabulary_alone_is_not_corroboration(self):
        # No entity overlap: same words, unrelated story.
        result = self._assess(stub_search([
            candidate('a.com', 'Heavy rainfall expected across northern Europe'),
        ]))
        self.assertEqual(result.independent_sources, 0)

    def test_an_old_story_does_not_corroborate_a_new_one(self):
        result = self._assess(stub_search([
            candidate('a.com', 'Mumbai floods close rail lines', days_off=400),
        ]))
        self.assertEqual(result.independent_sources, 0)

    def test_syndicated_copies_count_once(self):
        # Forty mastheads running one wire story is one confirmation.
        headline = 'Mumbai floods close three suburban rail lines after rainfall'
        many = self._assess(stub_search([
            candidate(f'outlet{n}.com', headline) for n in range(8)
        ]))
        self.assertEqual(many.independent_sources, 1)

    def test_one_outlet_publishing_repeatedly_counts_once(self):
        result = self._assess(stub_search([
            candidate('a.com', 'Mumbai floods close rail lines'),
            candidate('a.com', 'Mumbai flooding: rainfall latest as rail lines shut'),
        ]))
        self.assertEqual(result.independent_sources, 1)

    def test_the_source_itself_never_corroborates_itself(self):
        result = self._assess(stub_search([
            candidate('examplenews.com', 'Mumbai floods close three rail lines'),
        ]))
        self.assertEqual(result.independent_sources, 0)

    # -- verdicts ----------------------------------------------------------

    def test_being_first_is_not_untrustworthy(self):
        # A well-rated outlet standing alone is uncorroborated, never
        # "do not trust" — a scoop is not a lie.
        result = self._assess(stub_search([]))
        self.assertEqual(result.verdict, corroborate.Verdict.UNCORROBORATED)

    def test_a_condemned_source_is_not_rehabilitated_by_keyword_matches(self):
        CredibilityScore.objects.create(
            source=self.source, credibility_score=15.0, trust_tier=TrustTier.LOW
        )
        result = self._assess(stub_search([
            candidate('a.com', 'Mumbai floods close rail lines'),
            candidate('b.com', 'Flooding shuts Mumbai rail network'),
        ]))
        self.assertEqual(result.verdict, corroborate.Verdict.DO_NOT_TRUST)

    def test_well_backed_story_from_a_good_source_reads_well(self):
        result = self._assess(stub_search([
            candidate('a.com', 'Mumbai floods close rail lines after record rainfall'),
            candidate('b.com', 'Flooding shuts Mumbai suburban rail network'),
            candidate('c.com', 'Mumbai rainfall: rail lines closed as flooding spreads'),
            candidate('d.com', 'Rail lines closed by Mumbai flooding'),
        ]))
        self.assertIn(result.verdict, (
            corroborate.Verdict.TRUSTWORTHY, corroborate.Verdict.LIKELY_RELIABLE,
        ))

    def test_the_rating_never_leaves_the_scale(self):
        result = self._assess(stub_search([
            candidate(f'o{n}.com', f'Mumbai floods rail lines rainfall report {n}')
            for n in range(30)
        ]))
        self.assertLessEqual(result.score, 10.0)
        self.assertGreaterEqual(result.score, 0.0)

    # -- persistence -------------------------------------------------------

    def test_the_report_and_its_evidence_are_stored(self):
        from .models import Corroboration
        from .tasks import corroborate_item

        with patch('content.corroborate.get_search', return_value=stub_search([
            candidate('a.com', 'Mumbai floods close rail lines'),
        ])):
            report_id = corroborate_item.run(self.item.pk)

        report = Corroboration.objects.get(pk=report_id)
        self.assertEqual(report.matches.count(), 1)
        self.assertTrue(report.keywords)
        self.assertTrue(report.rationale)
        self.assertEqual(report.engine_version, corroborate.ENGINE_VERSION)

    def test_checks_are_a_log_not_an_overwrite(self):
        from .models import Corroboration
        from .tasks import corroborate_item

        with patch('content.corroborate.get_search', return_value=stub_search([])):
            corroborate_item.run(self.item.pk)
            corroborate_item.run(self.item.pk)

        reports = Corroboration.objects.filter(content_item=self.item)
        self.assertEqual(reports.count(), 2)
        # Newest first, so the check you just ran is the one you read.
        self.assertGreaterEqual(reports[0].created_at, reports[1].created_at)


class NullSearchTests(SimpleTestCase):
    def test_disabled_search_reports_unavailable_not_empty(self):
        # If it returned an empty *available* result, turning corroboration
        # off would silently rate every story as uncorroborated.
        outcome = corroborate.NullSearch().search(['x'], None, 'a.com')
        self.assertFalse(outcome.available)


class FetchUrlTests(TestCase):
    def test_a_blocked_host_leaves_no_source_behind(self):
        # A URL we are never allowed to read should not pollute the source
        # list, nor trigger an RDAP lookup and a score for a site we will
        # not fetch.
        from .tasks import fetch_url

        with patch('content.http.socket.getaddrinfo', fake_dns('127.0.0.1')):
            result = fetch_url.run('http://localtest.example/admin')

        self.assertTrue(result['blocked'])
        self.assertFalse(Source.objects.filter(domain='localtest.example').exists())

    def test_an_unusable_address_is_reported_not_raised(self):
        from .tasks import fetch_url

        self.assertIn('error', fetch_url.run('mailto:someone@example.com'))
        self.assertFalse(Source.objects.exists())
