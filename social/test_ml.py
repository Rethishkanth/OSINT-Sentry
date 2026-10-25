"""Tests for the trained social engines.

The two halves of social.ml are tested differently because they fail
differently.

The text scorer is two downloaded classifiers behind a thin wrapper, and the
wrapper is where the promises are: that a post the model cannot read gets no
sentiment rather than a shrug, that the stored number is a scale and not a
flattened class, that the offensive probability is passed through rather than
thresholded. Those are tested with a fake pipeline. What the models are
actually worth is measured by manage.py evaluate_text_scorer against
TweetEval's held-out splits and written to
social/artifacts/roberta_text.metrics.json; no test here re-measures it.

The graph model is a different risk. It ships with no weights, so there is no
accuracy to check - but it is trained in one place and served in another, and
Module 1 shipped exactly that arrangement with a serving path that built a
different feature row than the training path did. Every test passed, because
they compared the row's *keys*. So the parity tests here compare by value,
and one of them trains a real model on a planted graph and scores it through
the serving entry point.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from django.core.exceptions import ImproperlyConfigured
from django.core.management import CommandError, call_command
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from .analysis import AccountNode
from .features import FEATURE_NAMES, RELATIONS, AccountGraphFacts, feature_row
from .graphdata import load_graph
from .ml import RobertaTextScorer, collect_facts
from .models import InteractionEdge, SocialAccount

try:  # The ML extras are optional; a fresh clone does not install them.
    import torch  # noqa: F401
    import torch_geometric  # noqa: F401
    HAVE_ML = True
except ImportError:  # pragma: no cover - depends on the environment
    HAVE_ML = False

needs_ml = unittest.skipUnless(HAVE_ML, 'optional ML dependencies not installed')


class FakeClassifier:
    """Stands in for one transformers text-classification pipeline.

    Returns the same distribution whatever it is handed, which is all these
    tests need: they are about what the wrapper does with a distribution,
    not about which one the model produces.
    """

    def __init__(self, distribution):
        self.distribution = distribution
        self.calls: list[str] = []

    def __call__(self, text):
        self.calls.append(text)
        return [[{'label': label, 'score': score}
                 for label, score in self.distribution.items()]]


def fake_models(sentiment, offensive):
    """Patch _classifier so each model name gets its own fake."""
    sentiment_fake = FakeClassifier(sentiment)
    offensive_fake = FakeClassifier(offensive)

    def pick(model_name):
        return offensive_fake if 'offensive' in model_name else sentiment_fake

    return patch('social.ml._classifier', side_effect=pick), sentiment_fake


# -- the text scorer -------------------------------------------------------


class TextScorerTests(SimpleTestCase):
    def score(self, sentiment, offensive, text='A post.', **kwargs):
        patcher, _ = fake_models(sentiment, offensive)
        with patcher:
            return RobertaTextScorer(**kwargs).score_text(text)

    def test_the_score_is_positive_minus_negative(self):
        result = self.score(
            {'positive': 0.80, 'neutral': 0.15, 'negative': 0.05},
            {'non-offensive': 0.97, 'offensive': 0.03},
        )
        self.assertAlmostEqual(result.sentiment, 0.75)

    def test_a_balanced_reading_is_not_recorded_as_neutral(self):
        """The thing this engine exists to get right.

        A 40/35/25 split is the model declining to read the post. Storing
        0.15 for it would put a near-neutral number in the same column as a
        post the model read with confidence, and nothing downstream could
        tell them apart.
        """
        result = self.score(
            {'negative': 0.40, 'neutral': 0.35, 'positive': 0.25},
            {'non-offensive': 0.90, 'offensive': 0.10},
        )
        self.assertIsNone(result.sentiment)
        self.assertTrue(any(r['effect'] == 'limits' for r in result.reasons))

    def test_an_abstention_still_reports_toxicity(self):
        """Two models, two questions. One declining does not silence the other."""
        result = self.score(
            {'negative': 0.40, 'neutral': 0.35, 'positive': 0.25},
            {'non-offensive': 0.12, 'offensive': 0.88},
        )
        self.assertIsNone(result.sentiment)
        self.assertAlmostEqual(result.toxicity, 0.88)

    def test_toxicity_is_the_probability_not_a_verdict(self):
        result = self.score(
            {'positive': 0.70, 'neutral': 0.20, 'negative': 0.10},
            {'non-offensive': 0.55, 'offensive': 0.45},
        )
        self.assertAlmostEqual(result.toxicity, 0.45)

    def test_label_capitalisation_does_not_matter(self):
        """The two model cards disagree about it, so the wrapper must not care."""
        result = self.score(
            {'POSITIVE': 0.80, 'NEUTRAL': 0.15, 'NEGATIVE': 0.05},
            {'NON-OFFENSIVE': 0.60, 'OFFENSIVE': 0.40},
        )
        self.assertAlmostEqual(result.sentiment, 0.75)
        self.assertAlmostEqual(result.toxicity, 0.40)

    def test_an_empty_post_is_not_sent_to_either_model(self):
        patcher, sentiment_fake = fake_models(
            {'positive': 1.0}, {'offensive': 1.0})
        with patcher:
            result = RobertaTextScorer().score_text('   \n  ')
        self.assertIsNone(result.sentiment)
        self.assertIsNone(result.toxicity)
        self.assertEqual(sentiment_fake.calls, [])

    def test_the_floor_is_configurable_and_respected(self):
        distribution = {'positive': 0.65, 'neutral': 0.20, 'negative': 0.15}
        offensive = {'non-offensive': 0.9, 'offensive': 0.1}
        self.assertIsNotNone(
            self.score(distribution, offensive, min_confidence=0.60).sentiment)
        self.assertIsNone(
            self.score(distribution, offensive, min_confidence=0.70).sentiment)

    def test_every_reading_carries_its_reasoning(self):
        result = self.score(
            {'positive': 0.80, 'neutral': 0.15, 'negative': 0.05},
            {'non-offensive': 0.97, 'offensive': 0.03},
        )
        effects = {r['effect'] for r in result.reasons}
        self.assertIn('sentiment', effects)
        self.assertIn('toxicity', effects)


class EngineDeclarationTests(SimpleTestCase):
    """The console asks the class whether it is trained. It has to answer."""

    def test_the_trained_engines_say_so(self):
        from .ml import RgcnGraphScorer

        self.assertTrue(RobertaTextScorer.is_trained)
        self.assertTrue(RgcnGraphScorer.is_trained)

    def test_the_heuristics_do_not(self):
        from .analysis import HeuristicGraphScorer, HeuristicTextScorer

        self.assertFalse(HeuristicGraphScorer.is_trained)
        self.assertFalse(HeuristicTextScorer.is_trained)

    def test_the_graph_version_names_both_engines(self):
        """It reports a coordination score the network did not produce."""
        from .ml import RgcnGraphScorer

        self.assertIn('rgcn', RgcnGraphScorer.model_version)
        self.assertIn('heuristic', RgcnGraphScorer.model_version)


# -- the feature row -------------------------------------------------------


class FeatureRowTests(SimpleTestCase):
    def test_the_row_has_one_value_per_name(self):
        self.assertEqual(len(feature_row(AccountGraphFacts())),
                         len(FEATURE_NAMES))

    def test_an_empty_account_is_all_zeros_and_no_nans(self):
        row = feature_row(AccountGraphFacts())
        self.assertEqual(row, [0.0] * len(FEATURE_NAMES))

    def test_reciprocity_and_repeat_rate(self):
        facts = AccountGraphFacts(
            out_by_relation={'mention': 40},
            out_targets=2,
            reciprocated_targets=1,
        )
        row = dict(zip(FEATURE_NAMES, feature_row(facts)))
        self.assertEqual(row['reciprocity'], 0.5)
        self.assertEqual(row['repeat_rate'], 20.0)

    def test_an_unknown_follower_count_is_not_a_nan(self):
        row = dict(zip(FEATURE_NAMES, feature_row(
            AccountGraphFacts(follower_count=None))))
        self.assertEqual(row['follower_log'], 0.0)


class CollectFactsTests(SimpleTestCase):
    def nodes(self, *ids):
        return [AccountNode(id=i, handle=f'a{i}') for i in ids]

    def test_edges_are_counted_by_relation_in_both_directions(self):
        facts = collect_facts(
            self.nodes(1, 2),
            [(1, 2, 'mention'), (1, 2, 'mention'), (2, 1, 'reply')],
        )
        self.assertEqual(facts[1].out_by_relation, {'mention': 2})
        self.assertEqual(facts[1].in_by_relation, {'reply': 1})
        self.assertEqual(facts[2].in_by_relation, {'mention': 2})

    def test_a_reciprocated_target_is_counted_once(self):
        facts = collect_facts(
            self.nodes(1, 2, 3),
            [(1, 2, 'mention'), (2, 1, 'mention'), (1, 3, 'mention')],
        )
        self.assertEqual(facts[1].out_targets, 2)
        self.assertEqual(facts[1].reciprocated_targets, 1)

    def test_an_edge_to_an_account_outside_the_set_is_ignored(self):
        """Counting it would give the account a target with no row."""
        facts = collect_facts(self.nodes(1), [(1, 99, 'mention')])
        self.assertEqual(facts[1].out_targets, 0)
        self.assertEqual(facts[1].out_by_relation, {})


# -- training and serving read the same graph ------------------------------


@needs_ml
class GraphBuildingTests(SimpleTestCase):
    def nodes(self, *ids):
        return [AccountNode(id=i, handle=f'a{i}', post_count=i) for i in ids]

    def test_every_relation_is_added_in_both_directions(self):
        from .ml import NUM_RELATIONS, build_graph

        graph = build_graph(self.nodes(1, 2), [(1, 2, 'mention')])
        self.assertEqual(graph.edge_index.shape[1], 2)
        self.assertEqual(graph.edge_type.tolist(),
                         [RELATIONS.index('mention'),
                          RELATIONS.index('mention') + len(RELATIONS)])
        self.assertEqual(NUM_RELATIONS, len(RELATIONS) * 2)

    def test_an_unknown_relation_is_dropped_and_counted(self):
        """Folding it into another relation would be the quieter error."""
        from .ml import build_graph

        graph = build_graph(self.nodes(1, 2),
                            [(1, 2, 'mention'), (1, 2, 'telepathy')])
        self.assertEqual(graph.dropped_edges, 1)
        self.assertEqual(graph.edge_index.shape[1], 2)

    def test_the_feature_matrix_matches_the_feature_row_by_value(self):
        """The Module 1 bug, in the form it would take here.

        Comparing the column *names* is what let that one through. These are
        the numbers the model is actually fitted on, so these are what get
        compared.
        """
        from .ml import build_graph

        nodes = self.nodes(1, 2, 3)
        edges = [(1, 2, 'mention'), (2, 1, 'reply'), (1, 3, 'follow'),
                 (3, 1, 'reshare'), (1, 2, 'mention')]
        graph = build_graph(nodes, edges)
        facts = collect_facts(nodes, edges)

        for index, node in enumerate(nodes):
            self.assertEqual(
                [round(v, 5) for v in graph.x[index].tolist()],
                [round(v, 5) for v in feature_row(facts[node.id])],
                f'account {node.id} is built differently by the two paths',
            )

    def test_the_row_order_follows_the_node_order(self):
        from .ml import build_graph

        graph = build_graph(self.nodes(7, 3, 9), [])
        self.assertEqual(graph.account_ids, [7, 3, 9])


@needs_ml
class ArtifactTests(SimpleTestCase):
    def setUp(self):
        from .ml import load_artifact

        load_artifact.cache_clear()
        self.addCleanup(load_artifact.cache_clear)

    def test_a_missing_artifact_says_how_to_make_one(self):
        from .ml import load_artifact

        with self.assertRaises(ImproperlyConfigured) as caught:
            load_artifact('social/artifacts/does-not-exist.pt')
        self.assertIn('train_graph_model', str(caught.exception))

    def test_an_artifact_trained_on_other_columns_is_refused(self):
        """Not adapted to, not padded. Refused, with a retrain instruction."""
        import tempfile
        from pathlib import Path

        import torch

        from .ml import load_artifact

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'rgcn.pt'
            torch.save({'state_dict': {}, 'hidden': 32, 'num_bases': 8,
                        'feature_names': ['out_degree'],
                        'relations': list(RELATIONS)}, path)
            with self.assertRaises(ImproperlyConfigured) as caught:
                load_artifact(str(path))
        self.assertIn('feature columns', str(caught.exception))

    def test_an_artifact_trained_on_other_relations_is_refused(self):
        import tempfile
        from pathlib import Path

        import torch

        from .ml import load_artifact

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'rgcn.pt'
            torch.save({'state_dict': {}, 'hidden': 32, 'num_bases': 8,
                        'feature_names': list(FEATURE_NAMES),
                        'relations': ['mention']}, path)
            with self.assertRaises(ImproperlyConfigured) as caught:
                load_artifact(str(path))
        self.assertIn('edge relations', str(caught.exception))


# -- the whole round trip --------------------------------------------------


class TrainingRefusalTests(TestCase):
    def test_it_refuses_to_train_on_nothing(self):
        SocialAccount.objects.create(handle='a', platform='x')
        with self.assertRaises(CommandError) as caught:
            call_command('train_graph_model', verbosity=0)
        self.assertIn('at least', str(caught.exception))

    def test_it_refuses_when_one_class_is_missing(self):
        """Twenty labels, all the same, is not a classification problem."""
        for index in range(20):
            SocialAccount.objects.create(
                handle=f'a{index}', platform='x', is_automated=False)
        with self.assertRaises(CommandError) as caught:
            call_command('train_graph_model', verbosity=0)
        self.assertIn('4 of each', str(caught.exception))


@needs_ml
class TrainAndServeTests(TestCase):
    """Train on a planted graph, then score through the serving entry point.

    The graph is planted so that the two classes really are distinguishable
    from the columns: the automated accounts all hammer the same two hubs and
    nobody answers them, the human accounts talk to each other and get
    replies. This test is not evidence that the model detects bots. It is
    evidence that the training command fits something, writes an artifact the
    serving path accepts, and that the two paths agree about what the graph
    looks like - which is the part that broke in Module 1.
    """

    @classmethod
    def setUpTestData(cls):
        now = timezone.now()
        cls.hubs = [
            SocialAccount.objects.create(handle=f'hub{i}', platform='x',
                                         follower_count=50000)
            for i in range(2)
        ]
        cls.automated, cls.humans = [], []
        for index in range(14):
            cls.automated.append(SocialAccount.objects.create(
                handle=f'bot{index}', platform='x', follower_count=3,
                account_created_at=now, is_automated=True,
            ))
        for index in range(14):
            cls.humans.append(SocialAccount.objects.create(
                handle=f'person{index}', platform='x', follower_count=400,
                account_created_at=now, is_automated=False,
            ))

        edges = []
        for account in cls.automated:
            for hub in cls.hubs:
                # Unanswered, repetitive, and all at the same two targets.
                edges += [
                    InteractionEdge(from_account=account, to_account=hub,
                                    edge_type='mention')
                    for _ in range(6)
                ]
        for index, account in enumerate(cls.humans):
            partner = cls.humans[(index + 1) % len(cls.humans)]
            edges.append(InteractionEdge(from_account=account,
                                         to_account=partner,
                                         edge_type='reply'))
            edges.append(InteractionEdge(from_account=partner,
                                         to_account=account,
                                         edge_type='reply'))
            edges.append(InteractionEdge(from_account=account,
                                         to_account=cls.hubs[index % 2],
                                         edge_type='follow'))
        InteractionEdge.objects.bulk_create(edges)

    def setUp(self):
        from .ml import load_artifact

        load_artifact.cache_clear()
        self.addCleanup(load_artifact.cache_clear)

    def test_training_writes_an_artifact_the_scorer_can_serve(self):
        import tempfile
        from pathlib import Path

        from .ml import RgcnGraphScorer

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'rgcn.pt'
            call_command('train_graph_model', output=str(path), epochs=60,
                         patience=60, verbosity=0)
            self.assertTrue(path.is_file())
            self.assertTrue(path.with_suffix('.metrics.json').is_file())

            nodes, edges = load_graph()
            with override_settings(SOCIAL_GRAPH_ARTIFACT=str(path)):
                scores = RgcnGraphScorer(min_confidence=0.0).score_graph(
                    nodes, edges)

        self.assertEqual(len(scores), len(nodes))
        for score in scores:
            self.assertIsNotNone(score.bot_likelihood)
            self.assertGreaterEqual(score.bot_likelihood, 0.0)
            self.assertLessEqual(score.bot_likelihood, 1.0)
            self.assertEqual(score.model_version,
                             'rgcn-v1+heuristic-graph-v2')

    def test_an_unsure_model_records_no_likelihood(self):
        """No rating is given rather than a bad one."""
        import tempfile
        from pathlib import Path

        from .ml import RgcnGraphScorer

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'rgcn.pt'
            call_command('train_graph_model', output=str(path), epochs=1,
                         verbosity=0)
            nodes, edges = load_graph()
            with override_settings(SOCIAL_GRAPH_ARTIFACT=str(path)):
                # Nothing a two-class softmax produces clears this.
                scores = RgcnGraphScorer(min_confidence=1.01).score_graph(
                    nodes, edges)

        self.assertTrue(all(s.bot_likelihood is None for s in scores))
        self.assertTrue(all(
            any(r['effect'] == 'limits' for r in s.reasons) for s in scores))

    def test_coordination_still_comes_from_the_heuristic(self):
        """The network was trained on one label and reports one number."""
        import tempfile
        from pathlib import Path

        from .analysis import HeuristicGraphScorer
        from .ml import RgcnGraphScorer

        nodes, edges = load_graph()
        heuristic = {s.account_id: s.coordination_score
                     for s in HeuristicGraphScorer().score_graph(nodes, edges)}

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'rgcn.pt'
            call_command('train_graph_model', output=str(path), epochs=10,
                         verbosity=0)
            with override_settings(SOCIAL_GRAPH_ARTIFACT=str(path)):
                scores = RgcnGraphScorer().score_graph(nodes, edges)

        for score in scores:
            self.assertEqual(score.coordination_score,
                             heuristic[score.account_id])

    def test_the_database_graph_and_the_feature_rows_agree(self):
        """Parity by value, over the real reader rather than a fixture."""
        from .ml import build_graph

        nodes, edges = load_graph()
        graph = build_graph(nodes, edges)
        facts = collect_facts(nodes, edges)
        for index, node in enumerate(nodes):
            self.assertEqual(
                [round(v, 5) for v in graph.x[index].tolist()],
                [round(v, 5) for v in feature_row(facts[node.id])],
            )
