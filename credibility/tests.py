"""Tests for Module 1 — the site safety inspection.

No network. The one function that fetches (`content.http.request`) and the one
that opens a TLS socket (`inspection.inspect_certificate`) are replaced, and
the rest of the engine runs on canned HTML.

The cases worth testing are the judgements, not the parsing: that a plaintext
login form cannot be rated safe however tidy the rest is, that an unreachable
site gets no rating rather than a bad one, and that ordinary CSS class names
are not counted as pop-ups.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone as dt_timezone
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase

from core.models import Source

from . import inspection
from .inspection import CertificateInfo, Rating
from .models import SiteInspection
from .trackers import classify

GOOD_CERT = CertificateInfo(
    present=True, valid=True, issuer='Example CA', protocol='TLSv1.3',
    expires_at=datetime.now(dt_timezone.utc) + timedelta(days=200),
    days_remaining=200,
)

CLEAN_HEADERS = {
    'content-type': 'text/html',
    'strict-transport-security': 'max-age=63072000',
    'content-security-policy': "default-src 'self'",
    'x-content-type-options': 'nosniff',
    'x-frame-options': 'DENY',
    'referrer-policy': 'no-referrer',
}

CLEAN_PAGE = """
<html><head><title>Clean</title></head><body>
  <h1>A quiet website</h1>
  <p>Some words.</p>
  <a href="/privacy">Privacy policy</a>
  <a href="/contact">Contact us</a>
</body></html>
"""

ADS_PAGE = """
<html><head>
  <script src="https://securepubads.g.doubleclick.net/tag/js/gpt.js"></script>
  <script src="https://www.googletagmanager.com/gtag/js?id=X"></script>
  <script src="https://cdn.taboola.com/libtrc/loader.js"></script>
  <script src="https://static.criteo.net/js/ld/ld.js"></script>
  <script src="https://static.hotjar.com/c/hotjar.js"></script>
  <script>gtag('config', 'X'); fbq('init', '1');</script>
</head><body>
  <div class="newsletter-popup">Subscribe!</div>
  <video autoplay src="/promo.mp4"></video>
  <a href="/privacy">Privacy</a>
</body></html>
"""

# Ordinary utility CSS. An early version counted 109 "pop-ups" on a real news
# homepage from markup that looked exactly like this.
UTILITY_CSS_PAGE = """
<html><body>
  <div class="modal-body content-overlay">Article text</div>
  <span class="popup-trigger">More</span>
  <div class="overlay-gradient"><p>Words.</p></div>
  <div class="card modal">Ordinary card</div>
</body></html>
"""


def fake_response(body: str, url='https://example.com/', headers=None, cookies=()):
    from content.http import FetchResponse
    return FetchResponse(
        url=url, status=200, content_type='text/html',
        body=body.encode(), truncated=False,
        headers=headers if headers is not None else dict(CLEAN_HEADERS),
        cookies=tuple(cookies), hops=(url,),
    )


def serving(pages, headers=None, fail=None):
    """A stand-in for content.http.request driven by URL prefix."""
    from content.http import FetchError

    def _request(url, **kwargs):
        if fail and url.startswith(fail):
            raise FetchError(f'{url}: refused.')
        for prefix, body in pages.items():
            if url.startswith(prefix):
                return fake_response(body, url=url, headers=headers)
        raise FetchError(f'{url}: HTTP 404.')
    return _request


def run_inspection(domain, pages, *, headers=None, cert=GOOD_CERT, fail=None):
    with patch('content.http.request', side_effect=serving(pages, headers, fail)), \
         patch('content.robots.check'), \
         patch('credibility.inspection.inspect_certificate', return_value=cert):
        return inspection.inspect(domain)


class TrackerCatalogueTests(SimpleTestCase):
    def test_known_networks_are_classified_by_severity(self):
        self.assertEqual(classify('doubleclick.net')[0], 'ads')
        self.assertEqual(classify('google-analytics.com')[0], 'trackers')
        self.assertEqual(classify('popads.net')[0], 'intrusive_ads')
        self.assertEqual(classify('coinhive.com')[0], 'miners')

    def test_an_unknown_domain_is_not_guessed_at(self):
        self.assertIsNone(classify('some-small-blog.example'))
        self.assertIsNone(classify(''))


class SafetyRatingTests(TestCase):
    def test_a_clean_https_site_rates_well(self):
        result = run_inspection('example.com', {'https://example.com/': CLEAN_PAGE})
        self.assertGreaterEqual(result.score, 8.5)
        self.assertEqual(result.rating, Rating.SAFE)
        self.assertIn('No advertising', result.summary)

    def test_an_ad_heavy_site_rates_lower_and_names_the_networks(self):
        result = run_inspection('example.com', {'https://example.com/': ADS_PAGE})
        found = {t.domain for t in result.findings.third_parties}
        self.assertIn('doubleclick.net', found)
        self.assertIn('taboola.com', found)
        self.assertIn('hotjar.com', found)
        self.assertLess(result.score, 8.5)
        self.assertIn('ad network', result.summary)

    def test_inline_snippets_are_caught_even_without_a_script_host(self):
        result = run_inspection('example.com', {'https://example.com/': ADS_PAGE})
        self.assertTrue(
            any('Meta Pixel' in t for t in result.findings.inline_trackers),
            result.findings.inline_trackers,
        )

    def test_session_recording_is_called_out_by_name(self):
        result = run_inspection('example.com', {'https://example.com/': ADS_PAGE})
        self.assertTrue(any('session recording' in r['label'] for r in result.reasons))

    def test_autoplay_and_popups_are_counted(self):
        findings = run_inspection('example.com', {'https://example.com/': ADS_PAGE}).findings
        self.assertEqual(findings.autoplay_media, 1)
        self.assertEqual(findings.interruptions, 1)

    def test_utility_css_class_names_are_not_pop_ups(self):
        # The bug this guards: substring-matching "modal" and "overlay"
        # reported 109 pop-ups on a real news homepage.
        findings = run_inspection('example.com', {'https://example.com/': UTILITY_CSS_PAGE}).findings
        self.assertEqual(findings.interruptions, 0)

    def test_a_dialog_element_does_count(self):
        findings = run_inspection('example.com', {
            'https://example.com/': '<html><body><dialog>Sign up</dialog></body></html>',
        }).findings
        self.assertEqual(findings.interruptions, 1)

    # -- transport ---------------------------------------------------------

    def test_a_plaintext_only_site_is_penalised_heavily(self):
        result = run_inspection(
            'example.com', {'http://example.com/': CLEAN_PAGE},
            headers={'content-type': 'text/html'}, fail='https://',
        )
        self.assertFalse(result.findings.https)
        self.assertLess(result.score, 6.0)
        self.assertTrue(any('plaintext HTTP' in r['label'] for r in result.reasons))

    def test_a_password_form_over_http_can_never_be_rated_safe(self):
        page = ('<html><body><form action="http://example.com/login" method="post">'
                '<input type="password" name="p"></form>'
                '<a href="/privacy">Privacy</a><a href="/contact">Contact</a>'
                '</body></html>')
        result = run_inspection('example.com', {'https://example.com/': page})
        self.assertTrue(result.findings.password_over_http)
        # Hard ceiling: no amount of tidiness elsewhere lifts this.
        self.assertLessEqual(result.score, 3.0)
        self.assertIn(result.rating, (Rating.CAUTION, Rating.UNSAFE))

    def test_an_invalid_certificate_is_reported_in_words(self):
        bad = CertificateInfo(present=True, valid=False, problem='certificate has expired')
        result = run_inspection('example.com', {'https://example.com/': CLEAN_PAGE}, cert=bad)
        self.assertTrue(any('certificate has expired' in r['label'] for r in result.reasons))

    def test_missing_security_headers_cost_something_but_not_much(self):
        bare = run_inspection(
            'example.com', {'https://example.com/': CLEAN_PAGE},
            headers={'content-type': 'text/html'},
        )
        full = run_inspection('example.com', {'https://example.com/': CLEAN_PAGE})
        self.assertLess(bare.score, full.score)
        self.assertGreaterEqual(bare.score, full.score - 2.0)

    def test_mixed_content_on_an_https_page_is_noticed(self):
        page = '<html><body><script src="http://cdn.other.example/x.js"></script></body></html>'
        findings = run_inspection('example.com', {'https://example.com/': page}).findings
        self.assertEqual(findings.mixed_content, 1)

    # -- the honest-failure rule ------------------------------------------

    def test_an_unreachable_site_gets_no_rating_rather_than_a_bad_one(self):
        result = run_inspection('example.com', {}, fail='http')
        self.assertIsNone(result.score)
        self.assertEqual(result.rating, Rating.UNKNOWN)
        self.assertIn('Unreachable is not unsafe', result.summary)

    def test_the_javascript_caveat_is_always_stated(self):
        result = run_inspection('example.com', {'https://example.com/': CLEAN_PAGE})
        self.assertIn('JavaScript is not executed', result.summary)

    def test_a_tag_manager_strengthens_the_caveat(self):
        result = run_inspection('example.com', {'https://example.com/': ADS_PAGE})
        self.assertTrue(result.findings.tag_manager_present)
        self.assertIn('tag manager is present', result.summary)

    def test_the_rating_stays_on_the_scale(self):
        awful = """
        <html><body>
          <script src="https://coinhive.com/lib/miner.js"></script>
          <script src="https://popads.net/pop.js"></script>
          <script src="https://propellerads.com/p.js"></script>
          <script src="https://doubleclick.net/a.js"></script>
          <script src="https://taboola.com/b.js"></script>
          <div class="popup"></div><div class="interstitial"></div>
        </body></html>
        """
        result = run_inspection(
            'example.com', {'https://example.com/': awful},
            headers={'content-type': 'text/html'},
        )
        self.assertGreaterEqual(result.score, 0.0)
        self.assertLessEqual(result.score, 10.0)
        self.assertEqual(result.rating, Rating.UNSAFE)

    def test_a_miner_is_the_single_heaviest_finding(self):
        page = '<html><body><script src="https://coinhive.com/lib/miner.js"></script></body></html>'
        result = run_inspection('example.com', {'https://example.com/': page})
        miner = [r for r in result.reasons if 'miner' in r['label']]
        self.assertTrue(miner)
        self.assertEqual(miner[0]['points'], -4.0)


class InspectSiteTaskTests(TestCase):
    def test_the_inspection_is_stored_with_its_evidence(self):
        from .tasks import inspect_site

        source = Source.objects.create(domain='example.com')
        with patch('content.http.request', side_effect=serving({'https://example.com/': ADS_PAGE})), \
             patch('content.robots.check'), \
             patch('credibility.inspection.inspect_certificate', return_value=GOOD_CERT):
            inspection_id = inspect_site.run(source.pk)

        row = SiteInspection.objects.get(pk=inspection_id)
        self.assertTrue(row.uses_https)
        self.assertGreaterEqual(row.ad_networks, 3)
        self.assertGreaterEqual(row.trackers, 1)
        self.assertTrue(row.reasons)
        self.assertEqual(row.engine_version, inspection.ENGINE_VERSION)
        # The findings must survive the JSON round-trip intact.
        self.assertIn('third_parties', row.findings)
        self.assertTrue(row.findings['third_parties'])

    def test_inspections_are_a_log_not_an_overwrite(self):
        from .tasks import inspect_site

        source = Source.objects.create(domain='example.com')
        with patch('content.http.request', side_effect=serving({'https://example.com/': CLEAN_PAGE})), \
             patch('content.robots.check'), \
             patch('credibility.inspection.inspect_certificate', return_value=GOOD_CERT):
            inspect_site.run(source.pk)
            inspect_site.run(source.pk)

        rows = SiteInspection.objects.filter(source=source)
        self.assertEqual(rows.count(), 2)
        self.assertGreaterEqual(rows[0].created_at, rows[1].created_at)

    def test_safety_is_kept_separate_from_the_credibility_score(self):
        # The two answer different questions and must not contaminate each
        # other: a reputable outlet buried in advertising should show both
        # facts, not one number that splits the difference.
        from .models import CredibilityScore
        from .tasks import inspect_site

        source = Source.objects.create(domain='example.com')
        with patch('content.http.request', side_effect=serving({'https://example.com/': ADS_PAGE})), \
             patch('content.robots.check'), \
             patch('credibility.inspection.inspect_certificate', return_value=GOOD_CERT):
            inspect_site.run(source.pk)

        self.assertFalse(CredibilityScore.objects.filter(source=source).exists())
