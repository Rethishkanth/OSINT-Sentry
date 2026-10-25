"""Tests for the trained credibility scorer.

No network and no committed model: the tests that need an artifact train a
small one on synthetic rows in a temporary directory, so they exercise the
real joblib round trip rather than a stub, and leave nothing behind.

The cases worth testing are not the accuracy. They are the promises the
scorer makes around the number: that training and serving build the same
feature row, that a domain the model is unsure about gets no score rather
than a middling one, that an unseen TLD does not raise, and that a missing
artifact fails with an instruction instead of a traceback.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from . import features as feat
from .models import TrustTier
from .scoring import METADATA_SCORE_CEILING, SourceFeatures

try:  # The ML extras are optional; a fresh clone does not install them.
    import joblib  # noqa: F401
    import lightgbm  # noqa: F401
    import pandas  # noqa: F401
    import sklearn  # noqa: F401
    HAVE_ML = True
except ImportError:  # pragma: no cover - depends on the environment
    HAVE_ML = False

needs_ml = unittest.skipUnless(HAVE_ML, 'optional ML dependencies not installed')


def source_features(domain='example.com', **kwargs):
    defaults = dict(
        domain=domain,
        tld=domain.rsplit('.', 1)[-1],
        domain_age_days=4000,
        registry_status='active',
        registrar='Example Registrar',
        has_https=True,
        has_publisher=True,
        is_active=True,
        content_item_count=5,
    )
    defaults.update(kwargs)
    return SourceFeatures(**defaults)


class FeatureRowTests(SimpleTestCase):
    """Training and serving must build the same row, or the model is being
    asked a different question than it was trained on."""

    def test_both_paths_produce_the_same_keys(self):
        from_source = feat.feature_row_from_source(source_features())
        from_rdap = feat.feature_row_from_rdap('example.com', {
            'error': None,
            'events': [{'action': 'registration', 'date': '2010-01-01T00:00:00Z'}],
            'status': ['active'],
            'registrar': 'Example Registrar',
        })
        self.assertEqual(sorted(from_source), sorted(from_rdap))
        self.assertEqual(sorted(from_source), sorted(feat.FEATURE_COLUMNS))

    def test_both_paths_produce_the_same_values(self):
        """Matching keys are not enough.

        An earlier version passed this file's key check while serving five of
        the eight registration columns wrong: `registry_status` ('ok') was
        being fed where the registry's EPP codes belonged, which pinned
        `n_status` to 1 and both lock flags to 0, and the two date columns
        were never passed at all. Every row still had the right shape. The
        model simply saw blanks where its training data had signal.
        """
        now = timezone.now()
        registered = now - timedelta(days=3000)
        changed = now - timedelta(days=20)
        expires = now + timedelta(days=400)
        statuses = ['client transfer prohibited', 'client delete prohibited']

        from_rdap = feat.feature_row_from_rdap('example.com', {
            'error': None,
            'events': [
                {'action': 'registration', 'date': registered.isoformat()},
                {'action': 'last changed', 'date': changed.isoformat()},
                {'action': 'expiration', 'date': expires.isoformat()},
            ],
            'status': statuses,
            'registrar': 'Example Registrar',
        })
        from_source = feat.feature_row_from_source(source_features(
            domain_age_days=3000,
            registry_epp_statuses=tuple(statuses),
            registry_last_changed_days=20,
            registry_expires_in_days=400,
        ))

        differing = {
            column for column in feat.FEATURE_COLUMNS
            if from_rdap[column] != from_source[column]
        }
        self.assertEqual(differing, set())
        # Named explicitly: these are the ones that were wrong, and a future
        # refactor that drops them again should fail here by name.
        self.assertEqual(from_source['n_status'], 2)
        self.assertEqual(from_source['has_transfer_lock'], 1)
        self.assertEqual(from_source['last_changed_days'], 20)
        self.assertEqual(from_source['expires_in_days'], 400)

    def test_the_serving_row_is_not_built_from_the_lookup_outcome(self):
        """`registry_status` is our record of how the lookup went, not the
        registry's status codes. Feeding it to the model is the bug above."""
        row = feat.feature_row_from_source(source_features(
            registry_status='ok', registry_epp_statuses=(),
        ))
        self.assertEqual(row['n_status'], 0)

    def test_unknown_age_stays_unknown_rather_than_zero(self):
        row = feat.feature_row_from_source(source_features(domain_age_days=None))
        self.assertIsNone(row['reg_age_days'])
        self.assertEqual(row['reg_age_known'], 0)

    def test_failed_rdap_lookup_does_not_invent_registration_data(self):
        row = feat.feature_row_from_rdap('example.com', {'error': 'http_404'})
        self.assertIsNone(row['reg_age_days'])
        self.assertEqual(row['reg_age_known'], 0)
        self.assertEqual(row['registrar_known'], 0)

    def test_charged_and_news_tokens_are_counted_separately(self):
        row = feat.lexical_features('patriot-daily-truth.com')
        self.assertEqual(row['n_news_words'], 1)     # daily
        self.assertEqual(row['n_charged_words'], 2)  # patriot, truth

    def test_free_hosting_is_flagged(self):
        self.assertEqual(feat.lexical_features('someone.blogspot.com')['free_host'], 1)
        self.assertEqual(feat.lexical_features('bbc.co.uk')['free_host'], 0)

    def test_tld_is_the_last_label(self):
        self.assertEqual(feat.tld_of('news.example.co.uk'), 'uk')
        self.assertEqual(feat.tld_of(''), '')


def _train_tiny_artifact(directory: Path, min_confidence=0.90) -> Path:
    """A real artifact, trained on separable synthetic rows.

    The two classes are pushed apart so the model is confident on the shapes
    the tests then ask about; this is a fixture, not a claim about accuracy.
    """
    import joblib
    from lightgbm import LGBMClassifier
    from sklearn.calibration import CalibratedClassifierCV

    # The two classes differ on the signals the real model uses - charged
    # tokens, registration age, a transfer lock - and the stems vary so the
    # booster cannot separate them by memorising a string or counting digits.
    steady = ['journal', 'gazette', 'chronicle', 'tribune', 'herald', 'post']
    loud = ['patriot', 'truth', 'exposed', 'freedom', 'awake', 'infowars']

    rows, labels = [], []
    for index in range(150):
        rows.append(feat.feature_row(
            f'{steady[index % len(steady)]}-of-record-{index % 90}.com',
            age_days=9000,
            status=['active', 'client transfer prohibited'],
            registrar='Example Registrar',
        ))
        labels.append(1)
        rows.append(feat.feature_row(
            f'{loud[index % len(loud)]}-{loud[(index + 2) % len(loud)]}-'
            f'{index % 90}.xyz',
            age_days=10, status=[],
        ))
        labels.append(0)

    frame, categories = feat.to_frame(rows)

    model = CalibratedClassifierCV(
        LGBMClassifier(n_estimators=60, num_leaves=7, random_state=0, verbose=-1),
        method='isotonic', cv=3,
    )
    model.fit(frame, labels)

    path = directory / 'test_model.joblib'
    joblib.dump({
        'model': model,
        'feature_columns': feat.FEATURE_COLUMNS,
        'tld_categories': categories,
        'min_confidence': min_confidence,
        'label_source': 'a synthetic fixture',
        'n_training_rows': len(rows),
        'metrics': {},
    }, path)
    return path


@needs_ml
class TrainedScorerTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._tmp = tempfile.TemporaryDirectory()
        cls.artifact = _train_tiny_artifact(Path(cls._tmp.name))

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()
        super().tearDownClass()

    def setUp(self):
        from . import ml
        ml.clear_cache()

    def test_confident_credible_domain_scores_and_cites_its_labels(self):
        from .ml import GradientBoostedScorer

        with override_settings(CREDIBILITY_ML_ARTIFACT=str(self.artifact)):
            result = GradientBoostedScorer().score(
                source_features('observer-of-record-12.com', domain_age_days=9000,
                                registry_status='active',
                                registrar='Example Registrar')
            )

        self.assertGreater(result.score, 50)
        self.assertLessEqual(result.score, METADATA_SCORE_CEILING)
        self.assertIn('synthetic fixture', result.rationale)

    def test_the_metadata_ceiling_is_respected(self):
        from .ml import GradientBoostedScorer

        with override_settings(CREDIBILITY_ML_ARTIFACT=str(self.artifact)):
            result = GradientBoostedScorer().score(
                source_features('gazette-of-record-31.com', domain_age_days=9000,
                                registrar='Example Registrar')
            )
        self.assertLessEqual(result.score, METADATA_SCORE_CEILING)

    def test_an_unsure_domain_gets_no_score_rather_than_a_middling_one(self):
        """The decision rule, not the fixture's opinion.

        A cleanly separable fixture saturates to 0 and 1, so there is no
        genuinely ambiguous domain to hand it. What matters is what the
        scorer does with a middling probability, so that is what is supplied.
        """
        from .ml import GradientBoostedScorer

        with override_settings(CREDIBILITY_ML_ARTIFACT=str(self.artifact)):
            scorer = GradientBoostedScorer()
            with patch.object(scorer, 'probability', return_value=0.55):
                result = scorer.score(source_features('ambiguous.com'))

        self.assertEqual(result.tier, TrustTier.UNKNOWN)
        self.assertEqual(result.score, 0.0)
        self.assertIn('no score', result.rationale)
        self.assertIn('not a finding about the source', result.rationale)

    def test_both_edges_of_the_abstention_band_are_inclusive(self):
        from .ml import GradientBoostedScorer

        with override_settings(CREDIBILITY_ML_ARTIFACT=str(self.artifact),
                               CREDIBILITY_ML_MIN_CONFIDENCE=0.90):
            scorer = GradientBoostedScorer()
            self.assertEqual(scorer.verdict(0.90), 'credible')
            self.assertEqual(scorer.verdict(0.10), 'not_credible')
            self.assertIsNone(scorer.verdict(0.8999))
            self.assertIsNone(scorer.verdict(0.1001))

    def test_a_confident_negative_is_described_as_a_negative(self):
        from .ml import GradientBoostedScorer

        with override_settings(CREDIBILITY_ML_ARTIFACT=str(self.artifact)):
            scorer = GradientBoostedScorer()
            with patch.object(scorer, 'probability', return_value=0.02):
                result = scorer.score(source_features('loud-outlet.xyz'))

        self.assertLess(result.score, 50)
        self.assertIn('not credible', result.rationale)

    def test_a_tld_absent_from_training_does_not_raise(self):
        from .ml import GradientBoostedScorer

        with override_settings(CREDIBILITY_ML_ARTIFACT=str(self.artifact)):
            result = GradientBoostedScorer().score(
                source_features('something.frogs', tld='frogs')
            )
        self.assertIsNotNone(result.rationale)

    def test_blended_scorer_names_both_contributions(self):
        from .ml import BlendedScorer

        with override_settings(CREDIBILITY_ML_ARTIFACT=str(self.artifact)):
            result = BlendedScorer().score(
                source_features('chronicle-of-record-44.com', domain_age_days=9000,
                                registrar='Example Registrar')
            )

        self.assertIn('heuristic-v2', result.rationale)
        self.assertIn('Model', result.rationale)
        self.assertEqual(result.model_version, 'heuristic-v2+gbt-v1')

    def test_blended_scorer_says_so_when_the_model_declines(self):
        from .ml import BlendedScorer
        from .scoring import HeuristicScorer

        subject = source_features('ambiguous-outlet.com')
        with override_settings(CREDIBILITY_ML_ARTIFACT=str(self.artifact)):
            scorer = BlendedScorer()
            with patch.object(scorer, 'probability', return_value=0.55):
                blended = scorer.score(subject)

        # The model contributed nothing, so the number is the heuristic's.
        self.assertAlmostEqual(blended.score, HeuristicScorer().score(subject).score)
        self.assertIn('Model abstained', blended.rationale)

    def test_a_confident_model_moves_the_blended_score_by_the_set_amount(self):
        from .ml import BlendedScorer
        from .scoring import HeuristicScorer

        subject = source_features('gazette-of-record-31.com', domain_age_days=9000,
                                  registrar='Example Registrar')
        baseline = HeuristicScorer().score(subject).score

        with override_settings(CREDIBILITY_ML_ARTIFACT=str(self.artifact),
                               CREDIBILITY_ML_ADJUSTMENT=9.0):
            scorer = BlendedScorer()
            with patch.object(scorer, 'probability', return_value=0.02):
                blended = scorer.score(subject)

        self.assertAlmostEqual(blended.score, max(0.0, baseline - 9.0))

    def test_a_missing_artifact_explains_how_to_build_one(self):
        from . import ml

        with override_settings(CREDIBILITY_ML_ARTIFACT='/nonexistent/model.joblib'):
            with self.assertRaises(ImproperlyConfigured) as caught:
                ml.GradientBoostedScorer()

        self.assertIn('train_credibility_model', str(caught.exception))


class RegistryPlumbingTests(TestCase):
    """The registry fields have to survive the trip from RDAP to the model.

    The parity tests above compare two functions. These check the path those
    functions sit on: a lookup writes the codes and dates onto the Source,
    and extract_features reads them back as the columns the model expects.
    """

    def _source(self, **kwargs):
        from core.models import Source

        return Source.objects.create(domain='plumbing-check.com', **kwargs)

    def test_extract_features_carries_the_registry_record(self):
        from .scoring import extract_features

        now = timezone.now()
        source = self._source(
            domain_registered_at=now - timedelta(days=1000),
            registrar='Example Registrar',
            registry_status='ok',
            registry_epp_statuses=['client transfer prohibited'],
            domain_expires_at=now + timedelta(days=200),
            registry_last_changed_at=now - timedelta(days=30),
        )

        extracted = extract_features(source)

        self.assertEqual(extracted.registry_epp_statuses,
                         ('client transfer prohibited',))
        # delta=1: extract_features reads the clock a moment after this test
        # did, and whole days are floored, so an exact match is a race.
        self.assertAlmostEqual(extracted.registry_expires_in_days, 200, delta=1)
        self.assertAlmostEqual(extracted.registry_last_changed_days, 30, delta=1)

    def test_an_unlooked_up_source_leaves_them_unknown_rather_than_zero(self):
        """Zero would read as "expires today" and "changed today"."""
        from .scoring import extract_features

        extracted = extract_features(self._source())

        self.assertEqual(extracted.registry_epp_statuses, ())
        self.assertIsNone(extracted.registry_expires_in_days)
        self.assertIsNone(extracted.registry_last_changed_days)

    def test_an_expired_domain_reports_negative_days_rather_than_zero(self):
        from .scoring import extract_features

        source = self._source(
            domain_expires_at=timezone.now() - timedelta(days=10),
        )

        self.assertLess(extract_features(source).registry_expires_in_days, 0)

    def test_the_refresh_task_writes_what_the_lookup_returned(self):
        from .registry import RegistrationInfo, RegistryStatus
        from .tasks import refresh_domain_registration

        now = timezone.now()
        info = RegistrationInfo(
            registered_at=now - timedelta(days=500),
            registrar='Example Registrar',
            status=RegistryStatus.OK,
            epp_statuses=('client transfer prohibited', 'server hold'),
            expires_at=now + timedelta(days=100),
            last_changed_at=now - timedelta(days=5),
        )
        source = self._source()

        with patch('credibility.tasks.get_registry_client') as client:
            client.return_value.lookup.return_value = info
            refresh_domain_registration(source.pk)

        source.refresh_from_db()
        self.assertEqual(source.registry_epp_statuses,
                         ['client transfer prohibited', 'server hold'])
        self.assertIsNotNone(source.domain_expires_at)
        self.assertIsNotNone(source.registry_last_changed_at)

    def test_a_dropped_lock_is_recorded_rather_than_kept(self):
        """A lookup reporting no lock has told us the lock is gone. Carrying
        the old value forward would turn a stale read into a false one."""
        from .registry import RegistrationInfo, RegistryStatus
        from .tasks import refresh_domain_registration

        source = self._source(
            registry_epp_statuses=['client transfer prohibited'],
            domain_expires_at=timezone.now() + timedelta(days=300),
        )

        with patch('credibility.tasks.get_registry_client') as client:
            client.return_value.lookup.return_value = RegistrationInfo(
                registered_at=timezone.now() - timedelta(days=500),
                status=RegistryStatus.OK,
            )
            refresh_domain_registration(source.pk, force=True)

        source.refresh_from_db()
        self.assertEqual(source.registry_epp_statuses, [])
        self.assertIsNone(source.domain_expires_at)

    def test_a_failed_lookup_leaves_the_cached_record_alone(self):
        """An unreachable registry is not evidence the lock was lifted."""
        from .registry import RegistrationInfo, RegistryStatus
        from .tasks import refresh_domain_registration

        source = self._source(
            registry_epp_statuses=['client transfer prohibited'],
        )

        with patch('credibility.tasks.get_registry_client') as client:
            client.return_value.lookup.return_value = RegistrationInfo(
                status=RegistryStatus.ERROR, detail='timeout',
            )
            refresh_domain_registration(source.pk, force=True)

        source.refresh_from_db()
        self.assertEqual(source.registry_epp_statuses,
                         ['client transfer prohibited'])
