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
import urllib.error
from datetime import datetime, timedelta, timezone as dt_timezone
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from core.models import Source
from credibility.models import CredibilityScore, TrustTier

from . import corroborate, factcheck, http, robots
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


# ---------------------------------------------------------------------------
# Corroboration v2: query building, deep reads, and claim checking
# ---------------------------------------------------------------------------

# A page as it actually arrives from a scraper: the story, wrapped in the
# navigation and promo text that appears on every article the site publishes.
# The chrome is repeated because that is what chrome does, and repetition is
# exactly what fooled the v1 term ranker.
CHROME = (
    'Budget 2026 Share Price Market News Advertisement Subscribe Newsletter '
) * 12
RUPEE_STORY = (
    'The Indian rupee strengthened past 88 against the dollar on Tuesday. '
    'RBI intervention and steady dollar flows lifted the rupee to a two-month '
    'peak. Traders said the RBI sold dollars through state-run banks as the '
    'rupee gained. '
) * 3


@override_settings(CONTENT_MIN_CREDIBILITY=None, CORROBORATION_DEEP_READS=0)
class QueryBuildingTests(TestCase):
    """The regression that mattered: v1 searched for the page furniture.

    Every article came back rated 3.1 — the source-credibility half of the
    formula with corroboration contributing zero — because the query was built
    from whatever proper noun appeared most often anywhere on the page.
    """

    def setUp(self):
        self.source = Source.objects.create(domain='examplenews.com')
        CredibilityScore.objects.create(
            source=self.source, credibility_score=68.0, trust_tier=TrustTier.HIGH
        )

    def _item(self, title, body, url='https://examplenews.com/a'):
        item = ContentItem.objects.create(
            source=self.source, url=url, title=title, body=body,
            published_at=timezone.now(),
        )
        from .tasks import analyze_content
        analyze_content.run(item.pk)
        item.refresh_from_db()
        return item

    def test_the_headline_beats_the_navigation_bar(self):
        item = self._item(
            'RBI intervention, dollar flows push Indian rupee to two-month peak',
            CHROME + RUPEE_STORY + CHROME,
        )
        keywords = [k.lower() for k in corroborate.query_terms(corroborate.terms_for(item))]

        # v1 produced ['Indian', 'Budget', 'share', 'price'] here.
        self.assertIn('rbi', keywords)
        for chrome_word in ('budget', 'share', 'price', 'advertisement'):
            self.assertNotIn(chrome_word, keywords, f'{chrome_word} is furniture, not the story')

    def test_a_term_on_every_page_of_a_site_is_discounted(self):
        # Three articles from one source, all carrying the same promo. The
        # promo is the site; it is not any of the stories.
        for index in range(3):
            self._item(
                f'Story number {index} about penguins',
                CHROME + f'Antarctic penguins were counted again in survey {index}. ',
                url=f'https://examplenews.com/penguins-{index}',
            )
        latest = ContentItem.objects.order_by('-created_at').first()

        boilerplate = corroborate._boilerplate_terms(latest)
        self.assertTrue(boilerplate, 'repeated chrome should be detected')

        weights = {t.text.lower(): t.weight for t in corroborate.terms_for(latest)}
        self.assertLess(weights.get('advertisement', 0.0), weights.get('penguins', 0.0))

    def test_a_weekday_is_never_a_search_term(self):
        # Every story filed on a Tuesday says Tuesday, and none is about it.
        item = self._item('Rupee gains on Tuesday', RUPEE_STORY)
        keywords = [k.lower() for k in corroborate.query_terms(corroborate.terms_for(item))]
        self.assertNotIn('tuesday', keywords)

    def test_the_query_stays_short_enough_to_return_something(self):
        # Backends AND their terms, so each extra one narrows the search
        # towards nothing.
        item = self._item(
            'RBI intervention, dollar flows push Indian rupee to two-month peak',
            RUPEE_STORY,
        )
        self.assertLessEqual(len(corroborate.query_terms(corroborate.terms_for(item))), 3)


class FakeArticle:
    """The two attributes claim extraction reads off a ContentItem."""

    summary = ''

    def __init__(self, title, body):
        self.title, self.body = title, body


class ClaimExtractionTests(SimpleTestCase):
    """What the article actually asserts, as opposed to what it is about."""

    def _claims(self, title, body):
        from . import claims
        return claims.extract_claims(FakeArticle(title, body))

    def test_a_counted_figure_is_a_claim(self):
        found = self._claims(
            'Flood toll rises',
            'Officials said 47 people died in the Mumbai floods on the weekend.',
        )
        self.assertIn('47', [c.value for c in found])

    def test_money_and_percentages_are_kept_apart(self):
        from . import claims
        found = self._claims(
            'Damage assessed',
            'Damage was put at 1,200 crore rupees, up 15% on the previous total.',
        )
        kinds = {c.kind for c in found}
        self.assertIn(claims.MONEY, kinds)
        self.assertIn(claims.PERCENT, kinds)
        # "15" as a percentage must never be checked against "15" as a sum.
        self.assertNotEqual(
            {c.value for c in found if c.kind == claims.MONEY},
            {c.value for c in found if c.kind == claims.PERCENT},
        )

    def test_a_year_is_not_a_statistic(self):
        # A live check reported "this article says 03, the Guardian says 2022"
        # as a factual conflict. Years match almost any figure they are
        # compared against, so they are never treated as quantities.
        from . import claims
        found = self._claims(
            'Case reopened',
            'The Anderson case was reopened in 2022 after new evidence emerged.',
        )
        self.assertNotIn('2022', [c.value for c in found if c.kind == claims.QUANTITY])

    def test_a_loose_number_with_nothing_to_count_is_not_a_claim(self):
        found = self._claims(
            'Actor on her childhood',
            'The first half of my 20s was hell, she said of that period.',
        )
        self.assertEqual([], [c.value for c in found])

    def test_the_articles_own_timestamp_is_not_a_claim(self):
        # Checking a publication date against other outlets measures nothing:
        # everything filed today says today.
        found = self._claims(
            'Envoys set for talks',
            'Published 6 September 2026. Updated 6 September 2026. Talks continue.',
        )
        self.assertEqual([], found)

    def test_a_clock_time_does_not_become_a_disputed_figure(self):
        # "03" normalises to 3 and is discarded; without that it survived as a
        # two-digit statistic with nothing to count.
        from . import claims
        found = self._claims('Timeline', 'The meeting began at 03:12 local time.')
        self.assertEqual([], [c.value for c in found if c.kind == claims.QUANTITY])


class ClaimVerificationTests(SimpleTestCase):
    """Does what this article states match what other outlets state?"""

    TITLE = 'Flood toll rises to 47 in Mumbai as damage hits 1,200 crore'
    BODY = (
        'Officials said 47 people died in the Mumbai floods. Damage was put at '
        '1,200 crore rupees. The rainfall reached 32.5 cm overnight, up 15% on '
        'the previous total.'
    )

    def _verified(self, evidence):
        from . import claims
        found = claims.extract_claims(FakeArticle(self.TITLE, self.BODY))
        claims.verify_claims(found, evidence)
        return {c.value: c for c in found}

    def test_a_matching_figure_is_recorded_as_agreement(self):
        from . import claims
        found = self._verified([(
            'reuters.example',
            'Damage in Mumbai was 1,200 crore rupees, officials said. Rainfall hit 32.5 cm.',
        )])
        self.assertEqual(claims.AGREED, found['1200 crore'].status)
        self.assertIn('reuters.example', found['1200 crore'].agreed_by)

    def test_a_different_figure_is_recorded_as_a_conflict(self):
        from . import claims
        found = self._verified([(
            'reuters.example',
            '42 people died in the Mumbai floods, officials said on Sunday.',
        )])
        self.assertEqual(claims.DISPUTED, found['47'].status)
        self.assertEqual([['reuters.example', '42']], found['47'].disputed_by)

    def test_silence_is_not_a_conflict(self):
        # An outlet that simply did not print a figure has not contradicted it.
        from . import claims
        found = self._verified([(
            'reuters.example', 'Flooding continued across Mumbai for a third day.',
        )])
        self.assertEqual(claims.UNVERIFIED, found['47'].status)

    def test_a_figure_about_something_else_is_not_a_conflict(self):
        # The failure mode worth fearing is accusing a correct article of
        # getting a fact wrong, so an unrelated number nearby proves nothing.
        from . import claims
        found = self._verified([(
            'reuters.example', 'Cricket drew 42 spectators to the ground.',
        )])
        self.assertNotEqual(claims.DISPUTED, found['47'].status)

    def test_agreement_outranks_disagreement(self):
        # Outlets carry an original figure and a later correction. That
        # somebody printed ours is the finding worth surfacing.
        from . import claims
        found = self._verified([
            ('a.example', '47 people died in the Mumbai floods.'),
            ('b.example', '42 people died in the Mumbai floods.'),
        ])
        self.assertEqual(claims.AGREED, found['47'].status)


@override_settings(CONTENT_MIN_CREDIBILITY=None, CORROBORATION_DEEP_READS=0)
class ScoreCompositionTests(TestCase):
    """The rating has to be auditable: the parts must add up to the whole."""

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

    def test_the_reasons_sum_to_the_rating(self):
        result = self._assess(stub_search([
            candidate('other.example', 'Mumbai floods shut rail lines',
                      snippet='Flooding across Mumbai closed rail lines.'),
        ]))
        total = sum(r.effect for r in result.reasons if r.effect is not None)
        self.assertAlmostEqual(result.score, round(total, 1), places=1)

    def test_every_component_of_the_score_is_named(self):
        result = self._assess(stub_search([]))
        labels = [r.label for r in result.reasons]
        self.assertIn('Who published it', labels)
        self.assertIn('Who else reported it', labels)
        self.assertIn('Where we looked', labels)

    def test_a_withheld_rating_still_explains_itself(self):
        result = self._assess(stub_search([], available=False, failed=['stub']))
        self.assertIsNone(result.score)
        self.assertTrue(result.reasons, 'a refusal to rate is still a finding')

    def test_finding_nobody_lowers_the_rating_without_condemning_the_article(self):
        result = self._assess(stub_search([]))
        # Being first to report something is not dishonesty.
        self.assertEqual(corroborate.Verdict.UNCORROBORATED, result.verdict)


@override_settings(CONTENT_MIN_CREDIBILITY=None, CORROBORATION_DEEP_READS=3)
class DeepReadTests(TestCase):
    """Reading the other outlets, rather than judging them by their headlines."""

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
        corroborate._page_cache.clear()

    @staticmethod
    def _page(html):
        class _Response:
            url = 'https://other.example/story'

            def text(self):
                return html
        return _Response()

    def test_an_outlets_own_text_is_what_gets_matched(self):
        page = self._page(
            '<html><body><article><h1>Rail chaos in the west</h1>'
            '<p>Flooding across Mumbai closed suburban rail lines on Friday, '
            'the Brihanmumbai Municipal Corporation said. Mumbai residents were '
            'told to avoid low-lying roads as flooding spread.</p>'
            '</article></body></html>'
        )
        # A headline that shares one word, over a story that shares the
        # subject. Judged on the headline alone this is barely a match.
        row = candidate('other.example', 'Rail chaos in the west',
                        snippet='Mumbai flooding latest.')

        with patch('content.corroborate.get_search', return_value=stub_search([row])), \
                patch('content.http.request', return_value=page), \
                patch('content.robots.check'):
            result = corroborate.assess(self.item)

        self.assertEqual(1, result.outlets_read)
        self.assertEqual(1, result.independent_sources)
        self.assertTrue(result.matches[0].read_in_full)

    def test_an_outlet_that_refuses_us_is_not_held_against_the_article(self):
        from .http import FetchError

        row = candidate('other.example', 'Mumbai floods close rail lines',
                        snippet='Flooding in Mumbai closed rail lines.')
        with patch('content.corroborate.get_search', return_value=stub_search([row])), \
                patch('content.http.request', side_effect=FetchError('403')), \
                patch('content.robots.check'):
            result = corroborate.assess(self.item)

        self.assertEqual(0, result.outlets_read)
        # The headline match still counts; only the deeper check was lost.
        self.assertEqual(1, result.independent_sources)
        self.assertIn('Gaps in this check', [r.label for r in result.reasons])

    def test_a_conflicting_figure_is_reported_and_costs_the_rating(self):
        item = ContentItem.objects.create(
            source=self.source,
            url='https://examplenews.com/toll',
            title='Mumbai flood toll rises to 47',
            body=(
                'Officials said 47 people died in the Mumbai floods. Rescue teams '
                'searched flooded Mumbai suburbs as the flooding spread.'
            ),
            published_at=timezone.now(),
        )
        from .tasks import analyze_content
        analyze_content.run(item.pk)
        item.refresh_from_db()

        page = self._page(
            '<html><body><article><h1>Mumbai flood toll</h1>'
            '<p>42 people died in the Mumbai floods, officials said. Rescue teams '
            'searched flooded Mumbai suburbs as the flooding spread.</p>'
            '</article></body></html>'
        )
        row = candidate('other.example', 'Mumbai flood toll reaches 42',
                        snippet='Mumbai floods death toll.')

        with patch('content.corroborate.get_search', return_value=stub_search([row])), \
                patch('content.http.request', return_value=page), \
                patch('content.robots.check'):
            result = corroborate.assess(item)

        self.assertEqual(1, result.claims_disputed)
        self.assertEqual(corroborate.Verdict.DISPUTED, result.verdict)
        conflict = next(r for r in result.reasons if r.label == 'Conflicting figures')
        self.assertLess(conflict.effect, 0)
        self.assertIn('42', conflict.detail)


class ReferenceOutletTests(SimpleTestCase):
    def test_an_outlet_never_cross_references_itself(self):
        from .reference_outlets import REFERENCE_FEEDS, feeds_excluding

        kept = feeds_excluding('bbc.co.uk')
        self.assertNotIn('bbc.co.uk', [row[0] for row in kept])
        self.assertEqual(len(REFERENCE_FEEDS) - 1, len(kept))

    def test_every_reference_feed_is_a_distinct_https_endpoint(self):
        from .reference_outlets import REFERENCE_FEEDS

        domains = [row[0] for row in REFERENCE_FEEDS]
        self.assertEqual(len(domains), len(set(domains)), 'one entry per outlet')
        for domain, url, label in REFERENCE_FEEDS:
            self.assertTrue(url.startswith('https://'), f'{domain} feed must be https')
            self.assertTrue(label, f'{domain} needs a readable label')


# ---------------------------------------------------------------------------
# Published fact-checks (content.factcheck)
# ---------------------------------------------------------------------------

def claim_payload(*rows):
    """Build a Fact Check Tools response. Each row is (claim, publisher, rating)."""
    return {
        'claims': [
            {
                'text': claim,
                'claimant': 'a politician',
                'claimReview': [{
                    'publisher': {'name': publisher, 'site': f'{publisher.lower()}.org'},
                    'url': f'https://{publisher.lower()}.org/check',
                    'title': f'Fact check: {claim[:40]}',
                    'textualRating': rating,
                    'reviewDate': '2026-09-01T00:00:00Z',
                    'languageCode': 'en',
                }],
            }
            for claim, publisher, rating in rows
        ]
    }


class RatingBucketTests(SimpleTestCase):
    """Fact-checkers write their own scales, and several are jokes.

    A rating bucketed wrongly towards FALSE puts an accusation on an analyst's
    screen, so anything unrecognised must stay UNRATED rather than be guessed.
    """

    def test_the_obvious_ones(self):
        for text in ('False', 'Fake', 'Hoax', 'Fabricated', 'Debunked', 'Incorrect'):
            self.assertEqual(factcheck.FALSE, factcheck.normalise_rating(text), text)

    def test_the_publishers_own_idioms(self):
        for text in ('Pants on Fire', 'Four Pinocchios', 'Altered photo', 'Scam'):
            self.assertEqual(factcheck.FALSE, factcheck.normalise_rating(text), text)

    def test_mostly_false_is_not_read_as_false_by_accident(self):
        # It lands in the same bucket, but via its own rule — the ordering
        # matters for its mirror image below.
        self.assertEqual(factcheck.FALSE, factcheck.normalise_rating('Mostly False'))

    def test_mostly_true_is_not_read_as_true(self):
        """The one the rule order exists for.

        A bare `true` search would match inside "Mostly true" and report a
        qualified rating as an endorsement.
        """
        self.assertEqual(factcheck.MIXED, factcheck.normalise_rating('Mostly true'))
        self.assertEqual(factcheck.MIXED, factcheck.normalise_rating('Half True'))
        self.assertEqual(factcheck.MIXED, factcheck.normalise_rating('Misleading'))

    def test_a_rating_we_cannot_read_stays_unrated(self):
        for text in ('Flip flop', 'Research in progress', 'Spins the facts', ''):
            self.assertEqual(factcheck.UNRATED, factcheck.normalise_rating(text), text)

    def test_true_is_recognised(self):
        self.assertEqual(factcheck.TRUE, factcheck.normalise_rating('Correct'))
        self.assertEqual(factcheck.TRUE, factcheck.normalise_rating('True'))


class FactCheckClientTests(SimpleTestCase):
    """The key gate, and the four ways of finding nothing."""

    def test_the_null_client_reports_disabled_not_empty(self):
        outcome = factcheck.NullFactCheckClient().search(['mumbai', 'flooding'])
        self.assertEqual(factcheck.Status.DISABLED, outcome.status)
        self.assertFalse(outcome.available)
        self.assertIn('switched off', outcome.detail)

    def test_no_key_makes_no_request_at_all(self):
        """An unauthenticated call returns 403, which would read as an outage."""
        client = factcheck.GoogleFactCheckClient(api_key='')
        with patch.object(client, '_fetch') as fetch:
            outcome = client.search(['mumbai', 'flooding'])
        fetch.assert_not_called()
        self.assertEqual(factcheck.Status.NO_KEY, outcome.status)
        self.assertFalse(outcome.available)

    def test_a_failed_lookup_is_unavailable_not_none_found(self):
        client = factcheck.GoogleFactCheckClient(api_key='k')
        with patch.object(client, '_fetch', return_value=(None, 'the API returned HTTP 500.')):
            outcome = client.search(['mumbai', 'flooding'])
        self.assertEqual(factcheck.Status.UNAVAILABLE, outcome.status)
        self.assertFalse(outcome.available)
        self.assertIn('500', outcome.detail)

    def test_an_empty_index_answer_is_none_found_and_counts_as_available(self):
        client = factcheck.GoogleFactCheckClient(api_key='k')
        with patch.object(client, '_fetch', return_value=({'claims': []}, '')):
            outcome = client.search(['mumbai', 'flooding'])
        self.assertEqual(factcheck.Status.NONE_FOUND, outcome.status)
        self.assertTrue(outcome.available)

    def test_a_rejected_key_says_so_rather_than_blaming_the_network(self):
        client = factcheck.GoogleFactCheckClient(api_key='bad')
        error = urllib.error.HTTPError('u', 403, 'Forbidden', {}, None)
        with patch('urllib.request.urlopen', side_effect=error):
            outcome = client.search(['mumbai', 'flooding'])
        self.assertEqual(factcheck.Status.UNAVAILABLE, outcome.status)
        self.assertIn('rejected the key', outcome.detail)

    def test_several_publishers_reviewing_one_claim_are_all_kept(self):
        payload = {'claims': [{
            'text': 'Mumbai flooding closed the rail lines',
            'claimReview': [
                {'publisher': {'name': 'Full Fact'}, 'textualRating': 'False'},
                {'publisher': {'name': 'Boom Live'}, 'textualRating': 'Misleading'},
            ],
        }]}
        reviews = factcheck.GoogleFactCheckClient._reviews_in(payload)
        self.assertEqual(2, len(reviews))
        self.assertEqual({'Full Fact', 'Boom Live'}, {r.publisher for r in reviews})

    def test_the_publishers_wording_survives_bucketing(self):
        client = factcheck.GoogleFactCheckClient(api_key='k')
        payload = claim_payload(
            ('Mumbai flooding closed the rail lines', 'Politifact', 'Pants on Fire'),
        )
        with patch.object(client, '_fetch', return_value=(payload, '')):
            outcome = client.search(['mumbai', 'flooding'])
        review = outcome.reviews[0]
        self.assertEqual('Pants on Fire', review.rating)
        self.assertEqual(factcheck.FALSE, review.verdict)


class FactCheckMatchingTests(SimpleTestCase):
    """The weak link: we match on search terms, not on the claim itself."""

    def review(self, claim, rating='False'):
        return factcheck.FactCheckReview(
            claim=claim, rating=rating, verdict=factcheck.normalise_rating(rating),
            publisher='Full Fact',
        )

    def test_one_shared_term_is_a_coincidence_and_is_dropped(self):
        kept = factcheck.rank(
            [self.review('A photo of Mumbai traffic is from 2019')],
            ['mumbai', 'flooding', 'railways'],
        )
        self.assertEqual([], kept)

    def test_most_of_the_terms_present_is_a_close_match(self):
        kept = factcheck.rank(
            [self.review('Mumbai flooding shut the railways this week')],
            ['mumbai', 'flooding', 'railways'],
        )
        self.assertEqual(1, len(kept))
        self.assertTrue(kept[0].close_match)
        self.assertEqual(1.0, kept[0].match_strength)

    def test_a_partial_overlap_is_kept_but_labelled_loose(self):
        kept = factcheck.rank(
            [self.review('Mumbai flooding was caused by a dam release')],
            ['mumbai', 'flooding', 'railways', 'monsoon'],
        )
        self.assertEqual(1, len(kept))
        self.assertFalse(kept[0].close_match)

    def test_only_close_negative_matches_are_counted_as_negative(self):
        outcome = factcheck.FactCheckOutcome(
            reviews=factcheck.rank(
                [
                    self.review('Mumbai flooding shut the railways', 'False'),
                    self.review('Mumbai flooding was caused by a dam', 'False'),
                ],
                ['mumbai', 'flooding', 'railways', 'monsoon'],
            ),
            status=factcheck.Status.FOUND,
        )
        self.assertEqual(2, len(outcome.reviews))
        self.assertEqual(1, len(outcome.negative))

    def test_a_close_match_rated_true_is_not_negative(self):
        outcome = factcheck.FactCheckOutcome(
            reviews=factcheck.rank(
                [self.review('Mumbai flooding shut the railways', 'Correct')],
                ['mumbai', 'flooding', 'railways'],
            ),
            status=factcheck.Status.FOUND,
        )
        self.assertEqual([], outcome.negative)


def stub_fact_check(outcome):
    class _Stub(factcheck.BaseFactCheckClient):
        def search(self, terms, language=''):
            return outcome
    return _Stub()


@override_settings(CONTENT_MIN_CREDIBILITY=None, CORROBORATION_DEEP_READS=0)
class FactCheckInReportTests(TestCase):
    """What the layer does to a report — which, for the number, is nothing."""

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

    def _assess(self, outcome, candidates=()):
        with patch('content.corroborate.get_search', return_value=stub_search(list(candidates))), \
                patch('content.factcheck.get_fact_check_client',
                      return_value=stub_fact_check(outcome)):
            return corroborate.assess(self.item)

    def found(self, rating='False', close=True):
        review = factcheck.FactCheckReview(
            claim='Mumbai flooding closed the rail lines',
            rating=rating, verdict=factcheck.normalise_rating(rating),
            publisher='Full Fact', url='https://fullfact.org/check',
            close_match=close, match_strength=1.0 if close else 0.4,
        )
        return factcheck.FactCheckOutcome(
            reviews=[review], status=factcheck.Status.FOUND,
            detail='one matched', searched='Google Fact Check Tools',
        )

    def test_a_fact_check_does_not_move_the_rating(self):
        """The rule the whole layer is built around.

        The match is made on search terms. A number that shifts on a keyword
        overlap is a guess with a decimal point on it, so the arithmetic is
        identical with and without the fact-check.
        """
        clean = self._assess(factcheck.FactCheckOutcome(status=factcheck.Status.NONE_FOUND))
        damning = self._assess(self.found('False'))
        self.assertEqual(clean.score, damning.score)
        self.assertEqual(clean.verdict, damning.verdict)

    def test_the_fact_check_line_carries_no_contribution(self):
        """It must not appear as a term in the column of contributions."""
        result = self._assess(self.found('False'))
        line = next(r for r in result.reasons if r.label == 'Published fact-checks')
        self.assertIsNone(line.effect)
        self.assertIn('Full Fact', line.detail)
        self.assertIn('does not change the rating', line.detail)

    def test_a_loose_match_is_not_presented_as_a_ruling(self):
        result = self._assess(self.found('False', close=False))
        line = next(r for r in result.reasons if r.label == 'Published fact-checks')
        self.assertIn('none closely enough', line.detail)

    def test_switched_off_reads_as_switched_off_not_as_nothing_found(self):
        result = self._assess(factcheck.FactCheckOutcome(status=factcheck.Status.DISABLED,
                                                         detail='Fact-check lookup is switched off.'))
        self.assertEqual(factcheck.Status.DISABLED, result.fact_check_status)
        line = next(r for r in result.reasons if r.label == 'Published fact-checks')
        self.assertIn('switched off', line.detail)

    def test_the_lookup_still_runs_when_the_news_search_failed(self):
        """A fact-checker's ruling is worth citing even if GDELT rate-limited us.

        The two lookups are independent, and suppressing one because the other
        failed would hide the better evidence behind the worse.
        """
        with patch('content.corroborate.get_search',
                   return_value=stub_search([], available=False)), \
                patch('content.factcheck.get_fact_check_client',
                      return_value=stub_fact_check(self.found('False'))):
            result = corroborate.assess(self.item)

        self.assertIsNone(result.score)
        self.assertEqual(factcheck.Status.FOUND, result.fact_check_status)
        self.assertEqual(1, len(result.fact_checks))

    def test_a_client_that_raises_does_not_take_the_rating_down_with_it(self):
        class _Broken(factcheck.BaseFactCheckClient):
            def search(self, terms, language=''):
                raise RuntimeError('boom')

        with patch('content.corroborate.get_search', return_value=stub_search([])), \
                patch('content.factcheck.get_fact_check_client', return_value=_Broken()), \
                self.assertLogs('content.corroborate', level='ERROR'):
            result = corroborate.assess(self.item)

        self.assertIsNotNone(result.score)
        self.assertEqual(factcheck.Status.UNAVAILABLE, result.fact_check_status)

    def test_the_report_stores_the_publishers_words_and_the_counts(self):
        from .models import Corroboration, FactCheckMatch
        from .tasks import corroborate_item

        with patch('content.corroborate.get_search', return_value=stub_search([])), \
                patch('content.factcheck.get_fact_check_client',
                      return_value=stub_fact_check(self.found('Pants on Fire'))):
            report_id = corroborate_item.run(self.item.pk)

        report = Corroboration.objects.get(pk=report_id)
        self.assertEqual(Corroboration.FactCheckStatus.FOUND, report.fact_check_status)
        self.assertEqual(1, report.fact_checks_found)
        self.assertEqual(1, report.fact_checks_negative)

        row = FactCheckMatch.objects.get(report=report)
        self.assertEqual('Pants on Fire', row.rating)
        self.assertEqual(FactCheckMatch.Verdict.FALSE, row.verdict)
        self.assertTrue(row.close_match)

    def test_a_loose_match_is_stored_but_not_counted_as_negative(self):
        from .models import Corroboration
        from .tasks import corroborate_item

        with patch('content.corroborate.get_search', return_value=stub_search([])), \
                patch('content.factcheck.get_fact_check_client',
                      return_value=stub_fact_check(self.found('False', close=False))):
            report_id = corroborate_item.run(self.item.pk)

        report = Corroboration.objects.get(pk=report_id)
        self.assertEqual(1, report.fact_checks_found)
        self.assertEqual(0, report.fact_checks_negative)


class FactCheckResolverTests(SimpleTestCase):
    def test_the_default_client_contacts_nobody(self):
        """A fresh clone has no API key. It must not fail a check over that."""
        self.assertIsInstance(
            factcheck.get_fact_check_client(), factcheck.NullFactCheckClient
        )

    @override_settings(CONTENT_FACT_CHECK_CLIENT='content.factcheck.GoogleFactCheckClient')
    def test_the_google_client_is_reachable_by_its_dotted_path(self):
        self.assertIsInstance(
            factcheck.get_fact_check_client(), factcheck.GoogleFactCheckClient
        )


@override_settings(CONTENT_MIN_CREDIBILITY=None)
class ContentSummaryTests(TestCase):
    """Headline counts come from the database, not from the rows on screen."""

    def setUp(self):
        from django.contrib.auth import get_user_model
        from rest_framework.test import APIClient

        user = get_user_model().objects.create_user('analyst2', password='x' * 24)
        self.client = APIClient()
        self.client.force_authenticate(user=user)
        self.source = Source.objects.create(domain='examplenews.com')
        CredibilityScore.objects.create(
            source=self.source, credibility_score=70.0, trust_tier=TrustTier.HIGH
        )

    def test_the_summary_counts_past_the_page_size(self):
        """The list endpoint pages at 25 and has no page-size parameter.

        A console panel built from `rows.length` reports 25 on a corpus of any
        size above 25, and looks entirely plausible doing it.
        """
        for n in range(30):
            ContentItem.objects.create(
                source=self.source, url=f'https://examplenews.com/{n}', title=f'Story {n}'
            )

        listed = self.client.get('/api/content/').json()
        summary = self.client.get('/api/content/summary/').json()

        self.assertEqual(25, len(listed['results']))
        self.assertEqual(30, summary['items'])

    def test_unchecked_counts_the_articles_nobody_has_cross_referenced(self):
        from .models import Corroboration

        a = ContentItem.objects.create(
            source=self.source, url='https://examplenews.com/a', title='A'
        )
        ContentItem.objects.create(
            source=self.source, url='https://examplenews.com/b', title='B'
        )
        # Two checks on one article is still one article checked.
        Corroboration.objects.create(content_item=a, score=7.0)
        Corroboration.objects.create(content_item=a, score=7.2)

        summary = self.client.get('/api/content/summary/').json()
        self.assertEqual(1, summary['checked'])
        self.assertEqual(1, summary['unchecked'])
        self.assertEqual(2, summary['checks'])

    def test_the_summary_says_whether_fact_checking_is_switched_on(self):
        """Otherwise a zero in the panel means both "none" and "not looking"."""
        summary = self.client.get('/api/content/summary/').json()
        self.assertFalse(summary['fact_check_enabled'])

    @override_settings(CONTENT_FACT_CHECK_CLIENT='content.factcheck.GoogleFactCheckClient')
    def test_the_summary_reports_the_layer_as_on_when_it_is(self):
        summary = self.client.get('/api/content/summary/').json()
        self.assertTrue(summary['fact_check_enabled'])

    def test_the_dashboard_shows_the_unchecked_beside_the_analyzed(self):
        """The overview panel renders three sub-metrics and drops the rest."""
        ContentItem.objects.create(
            source=self.source, url='https://examplenews.com/a', title='A'
        )

        content = self.client.get('/api/overview/').json()['modules']['content']
        shown = [
            key for key in content
            if key not in ('online', 'items', 'model_version')
        ][:3]
        self.assertIn('unchecked', shown)
        self.assertEqual(1, content['unchecked'])
