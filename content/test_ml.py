"""Tests for the transformer-backed content analyzer.

Almost nothing here loads a model. The interesting promises are not the
model's accuracy, which manage.py evaluate_ner measures against a held-out
corpus and writes to content/artifacts/bert_ner.metrics.json; they are the
promises this module makes around the model. That a long article is read
whole rather than truncated at the first 512 tokens. That a name the model
was unsure about is dropped rather than recorded with a shrug. That the same
name found in four chunks arrives as one entity with a count of four, because
content.tasks bulk-creates these against a uniqueness constraint and a
duplicate would take the whole analysis down.

So the token classifier is faked for most cases. One test at the end uses the
real weights and is skipped when they are not on disk.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from .ml import (
    TransformerAnalyzer,
    candidate_phrases,
    chunk_text,
    clean_entity,
    span_text,
)
from .pipeline import HeuristicAnalyzer

try:  # The ML extras are optional; a fresh clone does not install them.
    import sentence_transformers  # noqa: F401
    import torch  # noqa: F401
    import transformers  # noqa: F401
    HAVE_ML = True
except ImportError:  # pragma: no cover - depends on the environment
    HAVE_ML = False

needs_ml = unittest.skipUnless(HAVE_ML, 'optional ML dependencies not installed')


def span(word, group, score, start=None, end=None):
    """A span as the pipeline reports one.

    Offsets are left out by default so the fake's `word` is what gets read.
    The tests that care about offsets pass them.
    """
    return {'word': word, 'entity_group': group, 'score': score,
            'start': start, 'end': end}


class FakePipeline:
    """Stands in for the token classifier.

    Called with a list of chunks and returns one list of spans per chunk, so
    a test can say what the model saw in which part of the document.
    """

    def __init__(self, *per_chunk):
        self.per_chunk = per_chunk
        self.seen = None

    def __call__(self, chunks):
        self.seen = chunks
        return [list(self.per_chunk[i]) if i < len(self.per_chunk) else []
                for i in range(len(chunks))]


def analyze_with(pipeline, text, **kwargs):
    with patch('content.ml._ner_pipeline', return_value=pipeline):
        return TransformerAnalyzer(**kwargs).extract_entities(text)


class ChunkingTests(SimpleTestCase):
    """A document longer than the model's window is read in pieces."""

    def test_a_short_document_is_one_chunk(self):
        self.assertEqual(chunk_text('One sentence here.'), ['One sentence here.'])

    def test_chunks_stay_under_the_limit(self):
        text = ' '.join(f'Sentence number {i} says something.' for i in range(200))
        chunks = chunk_text(text, limit=200)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 200)

    def test_a_sentence_is_never_split_across_two_chunks(self):
        text = 'Alpha beta gamma. Delta epsilon zeta. Eta theta iota.'
        for chunk in chunk_text(text, limit=25):
            self.assertTrue(chunk.endswith('.'), chunk)

    def test_a_sentence_longer_than_the_limit_survives_whole(self):
        """Truncating it is the tokeniser's business, not this function's."""
        long_one = 'word ' * 100
        chunks = chunk_text(long_one.strip() + '.', limit=50)
        self.assertEqual(len(chunks), 1)

    def test_empty_text_produces_no_chunks(self):
        self.assertEqual(chunk_text(''), [])
        self.assertEqual(chunk_text('   '), [])

    def test_the_whole_document_reaches_the_model(self):
        text = ' '.join(f'Sentence {i} is here.' for i in range(60))
        pipeline = FakePipeline()
        analyze_with(pipeline, text)
        self.assertGreater(len(pipeline.seen), 1)
        self.assertEqual(''.join(pipeline.seen).replace(' ', ''),
                         text.replace(' ', ''))


class SpanCleaningTests(SimpleTestCase):
    def test_quotes_and_punctuation_are_trimmed(self):
        self.assertEqual(clean_entity(' "Reuters", '), 'Reuters')
        self.assertEqual(clean_entity('(Abuja)'), 'Abuja')

    def test_an_internal_hyphen_is_left_alone(self):
        self.assertEqual(clean_entity('Jean-Pierre'), 'Jean-Pierre')

    def test_a_span_of_pure_punctuation_cleans_to_nothing(self):
        self.assertEqual(clean_entity('...'), '')

    def test_the_document_spelling_wins_over_the_reassembled_one(self):
        """Reassembling U.S. from wordpieces yields 'U. S', with a space the
        document does not contain. The offsets are right even when the
        reassembly is not, so the offsets decide."""
        text = 'Talks with the U.S. continued.'
        self.assertEqual(
            span_text({'word': 'U. S', 'start': 15, 'end': 19}, text), 'U.S',
        )

    def test_a_span_with_no_offsets_falls_back_to_the_reassembly(self):
        self.assertEqual(
            span_text({'word': 'Reuters', 'start': None, 'end': None}, ''),
            'Reuters',
        )


class EntityExtractionTests(SimpleTestCase):
    def test_conll_labels_become_the_labels_this_schema_stores(self):
        found = analyze_with(FakePipeline([
            span('Maria Okonkwo', 'PER', 0.99),
            span('Reuters', 'ORG', 0.99),
            span('Abuja', 'LOC', 0.99),
            span('Nigerian', 'MISC', 0.99),
        ]), 'Any text at all.')
        self.assertEqual(
            {s.text: s.label for s in found},
            {'Maria Okonkwo': 'PERSON', 'Reuters': 'ORG',
             'Abuja': 'GPE', 'Nigerian': 'OTHER'},
        )

    def test_a_label_the_model_invents_is_recorded_as_other(self):
        found = analyze_with(FakePipeline([span('Thing', 'WEAPON', 0.99)]), 'Text.')
        self.assertEqual(found[0].label, 'OTHER')

    @override_settings(CONTENT_NER_MIN_CONFIDENCE=0.90)
    def test_a_span_below_the_floor_is_dropped_rather_than_hedged(self):
        found = analyze_with(FakePipeline([
            span('Confident Name', 'PER', 0.97),
            span('Doubtful Name', 'PER', 0.55),
        ]), 'Text.')
        self.assertEqual([s.text for s in found], ['Confident Name'])

    @override_settings(CONTENT_NER_MIN_CONFIDENCE=0.90)
    def test_sure_once_and_unsure_four_times_is_not_sure(self):
        """The score is the mean over mentions, not the best of them."""
        found = analyze_with(FakePipeline(
            [span('Borderline', 'ORG', 0.99)],
            [span('Borderline', 'ORG', 0.20)],
            [span('Borderline', 'ORG', 0.20)],
            [span('Borderline', 'ORG', 0.20)],
            [span('Borderline', 'ORG', 0.20)],
        ), ' '.join(f'Sentence {i} is quite a long one indeed.' for i in range(200)))
        self.assertEqual(found, [])

    def test_the_same_name_in_several_chunks_is_one_entity(self):
        """content.tasks bulk-creates against a unique constraint on
        (item, text, label). A duplicate here is an IntegrityError there."""
        found = analyze_with(FakePipeline(
            [span('Reuters', 'ORG', 0.99)],
            [span('Reuters', 'ORG', 0.95)],
            [span('Reuters', 'ORG', 0.97)],
        ), ' '.join(f'Sentence {i} is quite a long one indeed.' for i in range(200)))
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].mention_count, 3)
        self.assertAlmostEqual(found[0].confidence, 0.97, places=2)

    def test_the_same_word_under_two_labels_stays_two_entities(self):
        """The constraint is on the pair, and so is the disagreement: the
        model saying ORG here and GPE there is information, not a clash."""
        found = analyze_with(FakePipeline(
            [span('Jordan', 'ORG', 0.99)],
            [span('Jordan', 'LOC', 0.99)],
        ), ' '.join(f'Sentence {i} is quite a long one indeed.' for i in range(200)))
        self.assertEqual(sorted(s.label for s in found), ['GPE', 'ORG'])

    def test_entities_are_unique_by_text_and_label(self):
        found = analyze_with(FakePipeline(
            [span('A Name', 'PER', 0.99), span('A Name', 'PER', 0.99)],
        ), 'Text.')
        self.assertEqual(len({(s.text, s.label) for s in found}), len(found))

    def test_a_span_that_cleans_down_to_nothing_is_not_an_entity(self):
        found = analyze_with(FakePipeline([
            span('"', 'ORG', 0.99), span('.', 'PER', 0.99),
        ]), 'Text.')
        self.assertEqual(found, [])

    def test_empty_text_asks_the_model_nothing(self):
        pipeline = FakePipeline()
        self.assertEqual(analyze_with(pipeline, ''), [])
        self.assertIsNone(pipeline.seen)

    def test_the_most_mentioned_entity_comes_first(self):
        found = analyze_with(FakePipeline(
            [span('Seldom', 'ORG', 0.99), span('Often', 'ORG', 0.99)],
            [span('Often', 'ORG', 0.99)],
            [span('Often', 'ORG', 0.99)],
        ), ' '.join(f'Sentence {i} is quite a long one indeed.' for i in range(200)))
        self.assertEqual([s.text for s in found], ['Often', 'Seldom'])

    def test_a_name_that_is_also_a_common_word_is_kept(self):
        """May, Will and Mark are people, and March is a month and a verb.
        A stopword filter over entity text would lose all four, so there
        isn't one: which sense this is was the model's question to answer."""
        found = analyze_with(FakePipeline([
            span('May', 'PER', 0.99), span('Will', 'PER', 0.99),
        ]), 'Text.')
        self.assertEqual(sorted(s.text for s in found), ['May', 'Will'])

    def test_confidence_is_recorded_rather_than_left_blank(self):
        """The heuristic has nothing to put here. A model does, and the
        console shows it, so it has to arrive on the span."""
        found = analyze_with(FakePipeline([span('Reuters', 'ORG', 0.9912)]), 'Text.')
        self.assertAlmostEqual(found[0].confidence, 0.9912, places=4)
        self.assertIsNone(
            HeuristicAnalyzer().extract_entities('Reuters said so.')[0].confidence
        )


class CandidatePhraseTests(SimpleTestCase):
    def test_a_phrase_may_not_start_or_end_on_a_stopword(self):
        phrases = candidate_phrases('the ministry of health')
        self.assertIn('ministry of health', phrases)
        self.assertNotIn('the ministry', phrases)
        self.assertNotIn('ministry of', phrases)

    def test_a_connector_inside_a_phrase_is_allowed(self):
        """The ends carry the meaning. Rejecting 'of' in the middle would
        reject the phrase that 'of' exists to build."""
        self.assertIn('ministry of health', candidate_phrases('the ministry of health'))

    def test_a_verb_inside_a_phrase_is_not(self):
        """'warned that cholera' is a clause with its subject removed, and a
        clause is not a name for a topic."""
        phrases = candidate_phrases('aid workers warned that cholera remains')
        self.assertNotIn('warned that cholera', phrases)
        self.assertIn('aid workers', phrases)

    def test_a_phrase_never_steps_over_a_full_stop(self):
        phrases = candidate_phrases('Orders slow. Shares fell today.')
        self.assertIn('orders slow', phrases)
        self.assertIn('shares fell', phrases)
        self.assertNotIn('slow shares', phrases)

    def test_a_phrase_never_steps_over_a_comma(self):
        """A comma separates two things being listed, and welding the last
        word of one to the first of the next names neither."""
        self.assertNotIn(
            'tablets food', candidate_phrases('water tablets, food parcels'),
        )

    def test_candidates_arrive_in_the_order_the_document_says_them(self):
        """A caller that takes the first N must get the opening of the
        article, not every single word and none of the phrases."""
        phrases = candidate_phrases('Relief camps flooded. Cholera risk rising.')
        self.assertLess(phrases.index('relief camps'), phrases.index('cholera'))

    def test_short_words_are_not_candidates(self):
        self.assertEqual(candidate_phrases('an ox is up'), [])

    def test_candidates_are_lowercased_and_unique(self):
        phrases = candidate_phrases('Ceasefire talks resumed. Ceasefire talks failed.')
        self.assertEqual(phrases.count('ceasefire talks'), 1)

    def test_no_text_means_no_candidates(self):
        self.assertEqual(candidate_phrases(''), [])


class EngineDeclarationTests(SimpleTestCase):
    def test_the_transformer_analyzer_reports_itself_as_trained(self):
        from core.views import engine_is_trained

        self.assertTrue(engine_is_trained('content.ml.TransformerAnalyzer'))

    def test_the_heuristic_analyzer_does_not(self):
        from core.views import engine_is_trained

        self.assertFalse(engine_is_trained('content.pipeline.HeuristicAnalyzer'))

    def test_importing_the_module_does_not_load_a_model(self):
        """Django has to start on a machine with no weights on disk, so the
        heavy imports live inside the loaders rather than at module scope."""
        import content.ml as module

        self.assertFalse(hasattr(module, 'torch'))
        self.assertFalse(hasattr(module, 'pipeline'))

    def test_article_sentiment_is_withheld(self):
        self.assertIsNone(TransformerAnalyzer().sentiment('A perfectly nice day.'))


@needs_ml
class RealModelTests(SimpleTestCase):
    """One pass with the actual weights, skipped when they are not cached."""

    SENTENCE = (
        'Maria Okonkwo, a spokeswoman for the Nigerian Ministry of Health, '
        'told Reuters in Abuja that the vaccination programme would continue '
        'through the rainy season despite the flooding in Bayelsa State.'
    )

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        import os

        from django.conf import settings

        os.environ.setdefault('HF_HOME', str(settings.CONTENT_MODEL_CACHE))
        os.environ['HF_HUB_OFFLINE'] = '1'
        try:
            from .ml import _ner_pipeline

            _ner_pipeline()
        except Exception as exc:  # pragma: no cover - depends on the cache
            raise unittest.SkipTest(f'NER weights are not cached: {exc}')

    def test_it_finds_the_person_the_regex_could_not_label(self):
        found = TransformerAnalyzer().extract_entities(self.SENTENCE)
        by_text = {s.text: s.label for s in found}
        self.assertEqual(by_text.get('Maria Okonkwo'), 'PERSON')
        self.assertEqual(by_text.get('Reuters'), 'ORG')
        self.assertEqual(by_text.get('Abuja'), 'GPE')

    def test_it_does_not_call_a_place_a_person(self):
        """The heuristic labels any two capitalised words PERSON, so it calls
        Bayelsa State a person. That is the failure this model is here for."""
        found = TransformerAnalyzer().extract_entities(self.SENTENCE)
        places = {s.text for s in found if s.label == 'GPE'}
        self.assertTrue(any('Bayelsa' in p for p in places), places)

        heuristic = {s.text: s.label for s in
                     HeuristicAnalyzer().extract_entities(self.SENTENCE)}
        self.assertEqual(heuristic.get('Bayelsa State'), 'PERSON')
