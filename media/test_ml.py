"""Tests for Module 4's trained detector.

The model itself is not loaded here: these tests patch the one function that
calls it and check what is done with the number. What they pin down is the
part this project wrote and is answerable for - that a reading below the
floor is not recorded at all, that a confident "photograph" never makes a
file look more trustworthy than the inspection already found it, that a
confident "generated" raises the verdict to suspicious but never to
manipulated, and that the evaluation command's arithmetic is right by value.
"""

from __future__ import annotations

import io
import unittest
from unittest import mock

from django.test import SimpleTestCase, TestCase, override_settings

from PIL import Image, ImageDraw

from core.views import engine_is_trained

from .forensics import HeuristicForensics, Verdict
from .management.commands.evaluate_detector import (
    accuracy,
    average_precision,
    floor_table,
    roc_auc,
)
from .ml import ViTForensics, generated_probabilities, prepare
from .models import TamperingResult
from .tests import as_jpeg, exif_with_thumbnail, labelled, photo

try:  # The ML extras are optional; a fresh clone does not install them.
    import torch
    HAVE_TORCH = True
except ImportError:  # pragma: no cover - depends on the environment
    HAVE_TORCH = False

needs_torch = unittest.skipUnless(HAVE_TORCH, 'torch is not installed')


def detector_says(probability):
    """Patch the scoring function, and only it, to return one reading."""
    return mock.patch('media.ml.generated_probabilities',
                      return_value=[probability])


def png(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, 'PNG')
    return buffer.getvalue()


def manipulated_jpeg() -> bytes:
    """A file whose own EXIF preview shows a different picture."""
    other = Image.new('RGB', (160, 120), (10, 10, 10))
    ImageDraw.Draw(other).ellipse((10, 10, 150, 110), fill=(250, 250, 250))
    buffer = io.BytesIO()
    photo(7).save(buffer, 'JPEG', quality=90,
                  exif=exif_with_thumbnail(as_jpeg(other, quality=70)))
    return buffer.getvalue()


class AbstentionTests(SimpleTestCase):
    """Below the floor the detector says nothing, and says that it said nothing."""

    def test_a_weak_reading_is_not_recorded(self):
        engine = ViTForensics(min_confidence=0.90)
        baseline = HeuristicForensics().analyze(png(photo(1)))
        with detector_says(0.80):
            result = engine.analyze(png(photo(1)))

        self.assertIsNone(result.deepfake_score)
        self.assertEqual(result.verdict, baseline.verdict)
        self.assertEqual(result.suspicion, baseline.suspicion)
        declined = labelled(result, 'would not commit')
        self.assertIsNotNone(declined)
        self.assertEqual(declined['points'], 0.0)

    def test_the_floor_applies_in_both_directions(self):
        engine = ViTForensics(min_confidence=0.90)
        with detector_says(0.15):
            result = engine.analyze(png(photo(2)))
        self.assertIsNone(result.deepfake_score)

    def test_the_floor_comes_from_settings_by_default(self):
        with override_settings(MEDIA_DETECTOR_MIN_CONFIDENCE=0.75):
            self.assertEqual(ViTForensics().min_confidence, 0.75)


class DirectionTests(SimpleTestCase):
    """What a confident reading may and may not do to a verdict."""

    def test_a_confident_photograph_reading_changes_nothing(self):
        engine = ViTForensics(min_confidence=0.90)
        baseline = HeuristicForensics().analyze(png(photo(3)))
        with detector_says(0.02):
            result = engine.analyze(png(photo(3)))

        self.assertEqual(result.deepfake_score, 0.02)
        self.assertEqual(result.verdict, baseline.verdict)
        self.assertNotEqual(result.verdict, Verdict.AUTHENTIC)
        self.assertEqual(result.suspicion, baseline.suspicion)
        noted = labelled(result, 'not evidence of authenticity')
        self.assertIsNotNone(noted)
        self.assertEqual(noted['points'], 0.0)

    def test_a_confident_generated_reading_makes_the_file_suspicious(self):
        engine = ViTForensics(min_confidence=0.90)
        with detector_says(0.97):
            result = engine.analyze(png(photo(4)))

        self.assertEqual(result.deepfake_score, 0.97)
        self.assertEqual(result.verdict, Verdict.SUSPICIOUS)
        self.assertIn('generated', result.summary)
        raised = labelled(result, 'reads this as generated')
        self.assertGreater(raised['points'], 0.0)

    def test_generated_is_never_called_manipulated(self):
        engine = ViTForensics(min_confidence=0.90)
        with detector_says(0.999):
            result = engine.analyze(png(photo(5)))
        self.assertNotEqual(result.verdict, Verdict.MANIPULATED)

    def test_a_thumbnail_contradiction_stays_manipulated(self):
        engine = ViTForensics(min_confidence=0.90)
        with detector_says(0.99):
            result = engine.analyze(manipulated_jpeg())

        self.assertEqual(result.verdict, Verdict.MANIPULATED)
        # The detector's reading is still recorded beside the proof.
        self.assertEqual(result.deepfake_score, 0.99)
        self.assertIsNotNone(labelled(result, 'embedded EXIF thumbnail'))

    def test_the_file_inspection_is_still_all_there(self):
        engine = ViTForensics(min_confidence=0.90)
        with detector_says(0.97):
            result = engine.analyze(as_jpeg(photo(6)))
        self.assertTrue(result.findings.ela_supported)
        self.assertEqual(result.findings.quantization_profile, 'ijg-standard')
        self.assertIsNotNone(labelled(result, 'Error level analysis'))


class WhatItDoesNotReadTests(SimpleTestCase):

    def test_a_video_never_reaches_the_detector(self):
        with mock.patch('media.ml.generated_probabilities') as scorer:
            result = ViTForensics().analyze(b'\x00\x00', kind='video')
        scorer.assert_not_called()
        self.assertIsNone(result.deepfake_score)
        self.assertEqual(result.verdict, Verdict.INCONCLUSIVE)

    def test_undecodable_bytes_never_reach_the_detector(self):
        with mock.patch('media.ml.generated_probabilities') as scorer:
            result = ViTForensics().analyze(b'not an image at all')
        scorer.assert_not_called()
        self.assertIsNone(result.deepfake_score)

    def test_the_result_names_the_engine_that_produced_it(self):
        with detector_says(0.5):
            result = ViTForensics().analyze(png(photo(7)))
        self.assertEqual(result.engine_version, ViTForensics.engine_version)
        self.assertEqual(result.as_details()['engine'], ViTForensics.engine_version)


class PrepareTests(SimpleTestCase):
    """Every image reaches the model as the RGB picture a person would see."""

    def test_garbage_is_none(self):
        self.assertIsNone(prepare(b'\x89PNG but not really'))

    def test_transparency_and_palettes_become_rgb(self):
        for mode in ('RGBA', 'LA', 'P', 'L', 'CMYK'):
            with self.subTest(mode=mode):
                image = photo(8).resize((64, 48)).convert(mode)
                buffer = io.BytesIO()
                image.save(buffer, 'TIFF' if mode == 'CMYK' else 'PNG')
                self.assertEqual(prepare(buffer.getvalue()).mode, 'RGB')

    def test_an_animation_is_read_from_its_first_frame(self):
        first = Image.new('RGB', (32, 32), (255, 0, 0))
        second = Image.new('RGB', (32, 32), (0, 0, 255))
        buffer = io.BytesIO()
        first.save(buffer, 'GIF', save_all=True, append_images=[second])
        red, green, blue = prepare(buffer.getvalue()).getpixel((16, 16))
        self.assertGreater(red, 200)
        self.assertLess(blue, 50)

    def test_sixteen_bit_greyscale_is_scaled_not_clipped(self):
        image = Image.new('I;16', (8, 8), 40000)
        buffer = io.BytesIO()
        image.save(buffer, 'PNG')
        value = prepare(buffer.getvalue()).getpixel((0, 0))[0]
        # 40000 of 65535 is about 61% grey. A clipping convert reads 255.
        self.assertAlmostEqual(value, 40000 // 256, delta=2)


@needs_torch
class ScoringPathTests(SimpleTestCase):
    """The function serving and evaluation share, with a stand-in model."""

    class Processor:
        def __call__(self, images, return_tensors='pt'):
            return {'pixel_values': torch.zeros(len(images), 3, 4, 4)}

    class Model:
        def __init__(self, logits):
            self.logits = logits
            self.calls = []

        def __call__(self, pixel_values):
            start = sum(self.calls)
            self.calls.append(len(pixel_values))
            chunk = self.logits[start:start + len(pixel_values)]
            return mock.Mock(logits=torch.tensor(chunk).reshape(-1, 1))

    def test_logits_become_probabilities_in_order_across_batches(self):
        logits = [0.0, 2.0, -2.0] * 7  # 21 images, more than one batch
        model = self.Model(logits)
        with mock.patch('media.ml._detector', return_value=(self.Processor(), model)):
            scores = generated_probabilities([object()] * len(logits))

        expected = [float(torch.sigmoid(torch.tensor(v))) for v in logits]
        self.assertEqual([round(s, 6) for s in scores],
                         [round(e, 6) for e in expected])
        self.assertGreater(len(model.calls), 1)

    def test_the_evaluation_command_scores_through_the_same_functions(self):
        # The lesson of Module 1, applied here: the measured path must be
        # the serving path, not a copy of it that happens to agree today.
        from media.management.commands import evaluate_detector

        source = open(evaluate_detector.__file__, encoding='utf-8').read()
        self.assertIn('from media.ml import generated_probabilities, prepare',
                      source)
        self.assertNotIn('ViTForImageClassification', source)
        self.assertNotIn('AutoImageProcessor', source)


class EngineDeclarationTests(SimpleTestCase):

    def test_only_the_detector_claims_to_be_trained(self):
        self.assertTrue(engine_is_trained('media.ml.ViTForensics'))
        self.assertFalse(engine_is_trained('media.forensics.HeuristicForensics'))


class MetricTests(SimpleTestCase):
    """The evaluation's arithmetic, checked by value on cases worked by hand."""

    def test_auc_of_a_perfect_ranking_is_one_and_of_a_reversed_one_zero(self):
        self.assertEqual(roc_auc([0, 0, 1, 1], [0.1, 0.2, 0.8, 0.9]), 1.0)
        self.assertEqual(roc_auc([0, 0, 1, 1], [0.9, 0.8, 0.2, 0.1]), 0.0)

    def test_auc_averages_ties(self):
        self.assertEqual(roc_auc([0, 1], [0.5, 0.5]), 0.5)

    def test_auc_needs_both_classes(self):
        self.assertIsNone(roc_auc([1, 1], [0.2, 0.9]))

    def test_average_precision_worked_by_hand(self):
        # Ranked: 0.9 (pos), 0.8 (neg), 0.7 (pos). Precision at each
        # positive is 1/1 and 2/3, so AP is (1 + 2/3) / 2.
        self.assertAlmostEqual(
            average_precision([1, 0, 1], [0.9, 0.8, 0.7]), (1 + 2 / 3) / 2)

    def test_accuracy_at_the_half_mark(self):
        self.assertEqual(accuracy([0, 1, 1, 0], [0.2, 0.7, 0.4, 0.6]), 0.5)

    def test_the_floor_table_counts_accused_photographs(self):
        gold = [0, 0, 0, 0, 1, 1]
        scores = [0.02, 0.40, 0.97, 0.60, 0.99, 0.30]
        rows = {row['floor']: row for row in floor_table(gold, scores)}

        at_95 = rows[0.95]
        # Kept: 0.02 (real, right), 0.97 (real, wrong), 0.99 (fake, right).
        self.assertEqual(at_95['images'], 3)
        self.assertEqual(at_95['speaks_on'], 0.5)
        self.assertEqual(at_95['right_when_it_speaks'], round(2 / 3, 4))
        self.assertEqual(at_95['generated_calls_right'], 0.5)
        # One of four photographs confidently called generated.
        self.assertEqual(at_95['photographs_called_generated'], 0.25)

        at_50 = rows[0.50]
        self.assertEqual(at_50['images'], 6)
        self.assertEqual(at_50['photographs_called_generated'], 0.5)


try:
    import pyarrow  # noqa: F401
    HAVE_PYARROW = True
except ImportError:  # pragma: no cover - depends on the environment
    HAVE_PYARROW = False


@unittest.skipUnless(HAVE_PYARROW, 'the evaluation needs pyarrow')
class CachedOnlyTests(SimpleTestCase):
    """--cached-only reports what was scored and names what was not."""

    def test_it_reads_the_cache_and_downloads_nothing(self):
        import json
        import tempfile
        from pathlib import Path

        from media.management.commands.evaluate_detector import Command

        record = {'shard': 1, 'group': 0, 'index': 0, 'generator': 'g',
                  'label': 1, 'architecture': 'a', 'subset': 's',
                  'format': 'PNG', 'p': 0.9, 'p_jpeg': 0.8}
        command = Command()
        command.verbosity = 0
        command.fs = mock.Mock()
        command.root = 'unused'
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            (cache / '00001-0-q85.json').write_text(json.dumps([record]))
            records = command.score(
                [(1, 0), (2, 3)], [], cache,
                {'jpeg_quality': 85, 'workers': 2, 'cached_only': True})

        self.assertEqual(records, [record])
        self.assertEqual(command.missing, [[2, 3]])
        command.fs.open.assert_not_called()


@override_settings(MEDIA_FORENSICS_ANALYZER='media.ml.ViTForensics')
class PipelineTests(TestCase):
    """The task stores the detector's reading where the console reads it."""

    def setUp(self):
        import shutil
        import tempfile

        uploads = tempfile.mkdtemp()
        override = override_settings(MEDIA_ROOT=uploads)
        override.enable()
        self.addCleanup(override.disable)
        self.addCleanup(shutil.rmtree, uploads, ignore_errors=True)

    def test_the_reading_is_stored_on_the_result(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        from .models import MediaItem
        from .tasks import analyze_media_item

        item = MediaItem.objects.create(
            file=SimpleUploadedFile('frame.png', png(photo(9)),
                                    content_type='image/png'),
            kind=MediaItem.Kind.IMAGE,
        )
        with detector_says(0.96):
            analyze_media_item.run(item.pk)

        stored = TamperingResult.objects.get(media_item=item)
        self.assertEqual(stored.deepfake_score, 0.96)
        self.assertEqual(stored.verdict, Verdict.SUSPICIOUS)
        self.assertEqual(stored.model_version, ViTForensics.engine_version)


class EngineEndpointTests(TestCase):
    """The panel's capability list comes from the configured analyzer."""

    def setUp(self):
        from django.contrib.auth import get_user_model
        from rest_framework.test import APIClient

        self.client = APIClient()
        self.client.force_authenticate(get_user_model().objects.create_user('analyst'))

    def test_the_heuristic_does_not_claim_a_detector(self):
        body = self.client.get('/api/media/items/engine/').json()
        self.assertFalse(body['deepfake_model'])
        self.assertIsNone(body['detector'])
        self.assertTrue(any('ViTForensics' in line for line in body['not_available']))

    @override_settings(MEDIA_FORENSICS_ANALYZER='media.ml.ViTForensics',
                       MEDIA_DETECTOR_MIN_CONFIDENCE=0.9)
    def test_the_detector_is_described_with_its_floor_and_its_limits(self):
        body = self.client.get('/api/media/items/engine/').json()
        self.assertTrue(body['deepfake_model'])
        self.assertEqual(body['detector']['min_confidence'], 0.9)
        self.assertTrue(body['detector']['revision'])
        self.assertIn('0.90', body['capabilities'][0])
        self.assertTrue(any('whole' in line for line in body['not_available']))
