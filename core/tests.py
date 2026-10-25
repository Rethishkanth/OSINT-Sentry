"""Tests for the public pages and for two project-wide rules.

The landing page and the privacy policy are the only things here a person can
read without an account, so the cases worth testing are that they stay
readable without one, that the privacy policy does not quietly claim to have
been updated today, and that the API index a developer needs did not get lost
when the landing page took over the root URL.

The last two cases are repository guards rather than tests of behaviour. They
exist because a typographic rule and an attribution rule are both the kind of
thing that holds for a week and then erodes one commit at a time, and a test
is the only form of such a rule that argues back.
"""

from __future__ import annotations

import io
import subprocess
from pathlib import Path

from django.test import SimpleTestCase, TestCase
from django.urls import reverse

REPO_ROOT = Path(__file__).resolve().parent.parent

# Built from codepoints rather than written out, so this file does not
# trip the guard below by containing the very characters it forbids.
EM_DASH = chr(0x2014)
EN_DASH = chr(0x2013)

# Text files whose dashes are not ours to fix.
DASH_EXEMPT_SUFFIXES = ('.svg', '.lock', '.json')


def tracked_text_files():
    """Every file git tracks, as a path, skipping binaries and vendored data."""
    listing = subprocess.run(
        ['git', 'ls-files'], cwd=REPO_ROOT,
        capture_output=True, text=True, check=False,
    )
    for relative in listing.stdout.split('\n'):
        relative = relative.strip()
        if not relative or relative.lower().endswith(DASH_EXEMPT_SUFFIXES):
            continue
        path = REPO_ROOT / relative
        if not path.is_file():
            continue
        yield relative, path


class PublicPageTests(TestCase):
    def test_the_landing_page_needs_no_account(self):
        response = self.client.get(reverse('landing'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'No rating is given rather than a bad one')

    def test_the_privacy_policy_needs_no_account(self):
        response = self.client.get(reverse('privacy'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Privacy policy')

    def test_the_privacy_policy_states_a_fixed_date(self):
        """A policy rendered with today's date claims an edit that never
        happened, so the date is a constant and this says so."""
        from core.views import PRIVACY_LAST_UPDATED

        response = self.client.get(reverse('privacy'))
        self.assertContains(response, PRIVACY_LAST_UPDATED)

    def test_the_public_pages_load_nothing_from_another_domain(self):
        for name in ('landing', 'privacy'):
            with self.subTest(page=name):
                body = self.client.get(reverse(name)).content.decode()
                for marker in ('http://', 'https://'):
                    for chunk in body.split(f'src="{marker}'):
                        self.assertNotIn('"', chunk.split('>')[0][:200],
                                         'a script or image is loaded off-site')

    def test_both_pages_reference_the_favicon(self):
        for name in ('landing', 'privacy'):
            with self.subTest(page=name):
                self.assertContains(self.client.get(reverse(name)), 'favicon.svg')

    def test_the_favicon_file_exists(self):
        self.assertTrue((REPO_ROOT / 'core' / 'static' / 'favicon.svg').is_file())

    def test_the_api_index_survived_the_move_off_the_root_url(self):
        response = self.client.get('/api/index/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['service'], 'osint-sentry')


class RepositoryConventionTests(SimpleTestCase):
    """Rules about the source itself, which nothing else would catch."""

    def test_no_em_or_en_dashes_anywhere_in_the_source(self):
        offenders = []
        for relative, path in tracked_text_files():
            try:
                text = io.open(path, encoding='utf-8').read()
            except (UnicodeDecodeError, PermissionError):
                continue
            count = text.count(EM_DASH) + text.count(EN_DASH)
            if count:
                offenders.append(f'{relative} ({count})')
        self.assertEqual(
            offenders, [],
            'em or en dashes found; use a plain hyphen:\n  '
            + '\n  '.join(offenders),
        )

    def test_no_ai_authorship_claims_in_the_source(self):
        """Attribution to an assistant is not a claim this project makes."""
        # Assembled from fragments for the same reason as the dashes above:
        # spelled out, each marker would be found in this file.
        markers = (
            'co-authored' + '-by: claude',
            'generated with [' + 'claude',
            'made with ' + 'ai',
            'built with ' + 'ai',
        )
        offenders = []
        for relative, path in tracked_text_files():
            try:
                text = io.open(path, encoding='utf-8').read()
            except (UnicodeDecodeError, PermissionError):
                continue
            lowered = text.lower()
            for marker in markers:
                if marker in lowered:
                    offenders.append(f'{relative}: {marker}')
        self.assertEqual(offenders, [], 'AI authorship markers found:\n  '
                         + '\n  '.join(offenders))


class EngineHonestyTests(TestCase):
    """The overview tells an analyst which figures came from a fitted model.

    That claim is derived from the configured class rather than from its
    name, so these check the derivation rather than the wording.
    """

    def test_the_heuristic_default_is_not_reported_as_trained(self):
        from core.views import engine_is_trained

        self.assertFalse(engine_is_trained('credibility.scoring.HeuristicScorer'))

    def test_a_fitted_scorer_reports_itself_as_trained(self):
        from core.views import engine_is_trained

        # Imports the class only; credibility.ml keeps lightgbm inside the
        # call that needs it, so this works without the optional extras.
        self.assertTrue(engine_is_trained('credibility.ml.BlendedScorer'))
        self.assertTrue(engine_is_trained('credibility.ml.GradientBoostedScorer'))

    def test_an_unimportable_setting_understates_rather_than_fails(self):
        from core.views import engine_is_trained

        self.assertFalse(engine_is_trained('nowhere.at.all.Scorer'))

    def test_the_geocoder_is_real_but_is_not_a_trained_model(self):
        """It resolves names against a compiled table. Honest, not fitted."""
        from core.views import engine_is_trained

        self.assertFalse(engine_is_trained('reportgis.geocode.GazetteerGeocoder'))

    def test_overview_reports_the_flag_for_every_module(self):
        from django.contrib.auth import get_user_model

        user = get_user_model().objects.create_user('analyst-engines', password='x')
        self.client.force_login(user)

        modules = self.client.get('/api/overview/').json()['modules']

        self.assertEqual(sorted(modules), sorted(
            ['credibility', 'content', 'social', 'media', 'reportgis']
        ))
        for name, stats in modules.items():
            with self.subTest(module=name):
                self.assertIn('trained', stats)
                self.assertIsInstance(stats['trained'], bool)
                # Every shipped default is a hand-written engine.
                self.assertFalse(stats['trained'])
