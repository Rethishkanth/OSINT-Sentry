"""Tests for Module 4 — media provenance and re-encoding forensics.

No network and no fixtures on disk: every image is constructed in memory so
each test states exactly which property it depends on.

The cases worth testing are the judgements, not the parsing. That a file whose
own embedded preview shows a different picture is called manipulated; that a
file whose preview agrees is not punished for having one; that a format the
engine cannot do error level analysis on says so instead of scoring zero; and
-- the one that matters most here -- that error level analysis never moves a
verdict, because measurement says it cannot tell an edited image from an
untouched one.
"""

from __future__ import annotations

import io
import random
import shutil
import struct
import tempfile

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase, override_settings

from PIL import Image, ImageDraw, ImageFilter

from .forensics import HeuristicForensics, Verdict, dimensions_of, sha256_of
from .models import MediaItem, TamperingResult
from .tasks import analyze_media_item

ENGINE = HeuristicForensics()


def photo(seed: int = 1, width: int = 640, height: int = 480) -> Image.Image:
    """A picture with *varied* detail: smooth sky, flat walls, rough ground.

    A uniformly noisy test image is useless here. Every statistic in this
    module normalises by local detail, so a frame with the same detail
    everywhere cannot exercise any of them.
    """
    rnd = random.Random(seed)
    image = Image.new('RGB', (width, height))
    draw = ImageDraw.Draw(image)

    top = (rnd.randint(90, 150), rnd.randint(130, 190), rnd.randint(180, 235))
    for y in range(height):
        fade = y / height
        draw.line(
            [(0, y), (width, y)],
            fill=tuple(int(c + (235 - c) * fade * 0.7) for c in top),
        )

    for _ in range(5):
        x = rnd.randint(0, width - 110)
        block_width = rnd.randint(50, 110)
        block_height = rnd.randint(90, 220)
        shade = rnd.randint(60, 140)
        draw.rectangle(
            [x, height - 150 - block_height, x + block_width, height - 150],
            fill=(shade, shade - 8, shade - 16),
        )

    ground = Image.effect_noise((width, 150), 55).filter(ImageFilter.SMOOTH)
    image.paste(
        Image.merge('RGB', (
            ground.point(lambda v: v // 2 + 40),
            ground.point(lambda v: v // 2 + 55),
            ground.point(lambda v: v // 3 + 30),
        )),
        (0, height - 150),
    )
    return image


def as_jpeg(image: Image.Image, quality: int = 85, **kwargs) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, 'JPEG', quality=quality, **kwargs)
    return buffer.getvalue()


def exif_with_thumbnail(thumbnail: bytes, make: bytes = b'Nikon\x00\x00\x00') -> bytes:
    """An APP1 payload whose IFD1 points at `thumbnail`.

    Pillow will not write an EXIF thumbnail, so the TIFF structure is built by
    hand: IFD0 carrying Make and a pointer to IFD1, and IFD1 carrying the
    thumbnail's offset and length. Offsets are measured from the TIFF header,
    which starts after the six-byte Exif introducer.
    """
    header = b'II' + struct.pack('<HI', 42, 8)
    ifd0_offset = 8
    ifd0_size = 2 + 1 * 12 + 4
    make_offset = ifd0_offset + ifd0_size
    ifd1_offset = make_offset + len(make)
    ifd1_size = 2 + 2 * 12 + 4
    thumb_offset = ifd1_offset + ifd1_size

    ifd0 = struct.pack('<H', 1)
    ifd0 += struct.pack('<HHII', 0x010F, 2, len(make), make_offset)
    ifd0 += struct.pack('<I', ifd1_offset)

    ifd1 = struct.pack('<H', 2)
    ifd1 += struct.pack('<HHII', 0x0201, 4, 1, thumb_offset)
    ifd1 += struct.pack('<HHII', 0x0202, 4, 1, len(thumbnail))
    ifd1 += struct.pack('<I', 0)

    return b'Exif\x00\x00' + header + ifd0 + make + ifd1 + thumbnail


def labelled(result, needle: str):
    """The first reason whose label mentions `needle`, or None."""
    lowered = needle.lower()
    return next((r for r in result.reasons if lowered in r['label'].lower()), None)


class ReadsWhatIsThereTests(SimpleTestCase):
    """Observation: what the engine can say about a file at all."""

    def test_unreadable_bytes_are_inconclusive_not_authentic(self):
        result = ENGINE.analyze(b'this is plain text, not a picture')
        self.assertEqual(result.verdict, Verdict.INCONCLUSIVE)
        self.assertFalse(result.findings.readable)
        self.assertTrue(result.findings.unreadable_reason)

    def test_png_says_error_level_analysis_does_not_apply(self):
        buffer = io.BytesIO()
        photo(2).save(buffer, 'PNG')
        result = ENGINE.analyze(buffer.getvalue())

        self.assertFalse(result.findings.ela_supported)
        self.assertIsNone(result.ela_score)
        self.assertTrue(
            any('JPEG' in note for note in result.findings.notes),
            'a PNG should say why no error level analysis was done',
        )

    def test_quantization_tables_reveal_the_save_quality(self):
        result = ENGINE.analyze(as_jpeg(photo(3), quality=78))
        self.assertEqual(result.findings.estimated_quality, 78)
        self.assertEqual(result.findings.quantization_profile, 'ijg-standard')

    def test_dimensions_and_hash_come_from_the_bytes(self):
        data = as_jpeg(photo(4))
        self.assertEqual(dimensions_of(data), (640, 480))
        self.assertEqual(len(sha256_of(data)), 64)
        self.assertNotEqual(sha256_of(data), sha256_of(as_jpeg(photo(5))))

    def test_no_deepfake_number_is_invented(self):
        """No model is installed, so the field stays empty rather than 0.0."""
        result = ENGINE.analyze(as_jpeg(photo(6)))
        self.assertIsNone(result.deepfake_score)


class ThumbnailTests(SimpleTestCase):
    """The one finding in this module close to proof."""

    def test_preview_showing_a_different_picture_is_manipulated(self):
        image = photo(7)
        other = Image.new('RGB', (160, 120), (10, 10, 10))
        ImageDraw.Draw(other).ellipse((10, 10, 150, 110), fill=(250, 250, 250))

        buffer = io.BytesIO()
        image.save(
            buffer, 'JPEG', quality=90,
            exif=exif_with_thumbnail(as_jpeg(other, quality=70)),
        )
        result = ENGINE.analyze(buffer.getvalue())

        self.assertEqual(result.verdict, Verdict.MANIPULATED)
        self.assertTrue(result.findings.thumbnail_present)
        self.assertGreaterEqual(result.findings.thumbnail_distance, 0.18)

    def test_a_preview_that_agrees_is_not_held_against_the_file(self):
        image = photo(8)
        honest = as_jpeg(image.resize((160, 120)), quality=70)

        buffer = io.BytesIO()
        image.save(buffer, 'JPEG', quality=90, exif=exif_with_thumbnail(honest))
        result = ENGINE.analyze(buffer.getvalue())

        self.assertNotEqual(result.verdict, Verdict.MANIPULATED)
        matched = labelled(result, 'thumbnail matches')
        self.assertIsNotNone(matched)
        self.assertEqual(matched['points'], 0.0)


class MetadataTests(SimpleTestCase):
    """What a file says about its own history."""

    def _with_software(self, name: str, seed: int = 9):
        exif = Image.Exif()
        exif[0x0131] = name
        return ENGINE.analyze(as_jpeg(photo(seed), exif=exif))

    def test_a_named_generator_outweighs_a_named_editor(self):
        generated = self._with_software('Stable Diffusion WebUI')
        edited = self._with_software('Adobe Photoshop 25.0 (Windows)', seed=10)

        self.assertEqual(generated.findings.generator, 'Stable Diffusion')
        self.assertEqual(edited.findings.editor, 'Adobe Photoshop')
        self.assertGreater(generated.suspicion, edited.suspicion)
        self.assertEqual(generated.verdict, Verdict.SUSPICIOUS)
        self.assertEqual(edited.verdict, Verdict.SUSPICIOUS)

    def test_an_editor_tag_is_not_called_proof(self):
        """Cropping leaves the same trace as a composite, so it stops short."""
        edited = self._with_software('Adobe Photoshop 25.0 (Windows)')
        self.assertNotEqual(edited.verdict, Verdict.MANIPULATED)

    def test_camera_exif_over_library_tables_means_software_re_encoded_it(self):
        exif = Image.Exif()
        exif[0x010F] = 'Canon'
        exif[0x0110] = 'Canon EOS R6'
        exif[0x0132] = '2024:03:01 10:00:00'
        exif.get_ifd(0x8769)[0x9003] = '2024:03:01 09:11:22'

        result = ENGINE.analyze(as_jpeg(photo(11), exif=exif))

        self.assertIsNotNone(labelled(result, 're-encoded by software'))
        self.assertIsNotNone(labelled(result, 'disagree'))
        self.assertEqual(result.verdict, Verdict.SUSPICIOUS)

    def test_a_stripped_file_is_noted_but_barely_counted(self):
        result = ENGINE.analyze(as_jpeg(photo(12)))
        stripped = labelled(result, 'No EXIF at all')

        self.assertIsNotNone(stripped)
        self.assertLessEqual(stripped['points'], 0.1)
        self.assertNotEqual(result.verdict, Verdict.SUSPICIOUS)


class ErrorLevelAnalysisTests(SimpleTestCase):
    """Error level analysis is measured, reported, and never scored.

    Two formulations were swept against composites built from a donor with a
    visibly different compression history. Neither separated edited frames
    from untouched ones -- the single-quality residual flagged 15 of 64
    originals against 78 of 256 composites, and the JPEG ghost 16 of 30
    against 41 of 90. So the engine shows the number and withholds the
    conclusion, and these tests hold it to that.
    """

    def _composite(self, save_quality=85, patch_quality=20):
        base = photo(13)
        donor = Image.open(
            io.BytesIO(as_jpeg(photo(113), quality=patch_quality))
        ).convert('RGB')
        base.paste(donor.crop((0, 0, 220, 180)), (300, 150))
        return ENGINE.analyze(as_jpeg(base, quality=save_quality))

    def test_it_runs_and_reports_a_number(self):
        result = self._composite()
        self.assertTrue(result.findings.ela_supported)
        self.assertIsNotNone(result.findings.ela_hotspot_ratio)
        self.assertIsNotNone(result.ela_score)

    def test_it_contributes_no_points_to_the_verdict(self):
        reason = labelled(self._composite(), 'Error level analysis')
        self.assertIsNotNone(reason, 'the measurement should still be shown')
        self.assertEqual(reason['points'], 0.0)

    def test_a_plain_photograph_scores_the_same_as_a_composite(self):
        """The regression guard on today's decision.

        If someone re-attaches points to the hotspot ratio, this fails: the
        engine must reach the same suspicion for an untouched frame as for a
        spliced one, because the only thing separating them is a measure that
        was shown not to separate them.
        """
        untouched = ENGINE.analyze(as_jpeg(photo(13), quality=85))
        self.assertEqual(self._composite().suspicion, untouched.suspicion)

    def test_block_means_are_not_collapsed_to_whole_numbers(self):
        """Guards the float conversion inside _hotspot_ratio.

        Resizing the 8-bit difference image rounds every block mean to an
        integer, and these means are small: one measured frame had three
        distinct values across 1200 blocks, which drove the median and the
        MAD onto the same number and made the function return 0.0 for
        everything. A set of frames with real structure must not all read as
        perfectly uniform.
        """
        ratios = [
            ENGINE.analyze(as_jpeg(photo(seed), quality=85))
            .findings.ela_hotspot_ratio
            for seed in range(20, 26)
        ]
        self.assertTrue(
            any(ratios),
            'every frame reported a hotspot ratio of exactly zero, which is '
            'what the collapsed-to-integer bug looked like',
        )

    def test_re_encoding_at_the_files_own_quality_is_called_uninformative(self):
        """A flat error map here means nothing was measured, not that the
        image is consistent, and the engine has to say which."""
        result = ENGINE.analyze(as_jpeg(photo(14), quality=90))
        self.assertTrue(
            any('uninformative' in note for note in result.findings.notes),
            result.findings.notes,
        )


class PipelineTests(TestCase):
    """The task, the model, and the details blob the console reads."""

    # These tests really do save files, so they save them somewhere
    # disposable rather than littering the project's upload directory.
    def setUp(self):
        self._uploads = tempfile.mkdtemp()
        override = override_settings(MEDIA_ROOT=self._uploads)
        override.enable()
        self.addCleanup(override.disable)
        self.addCleanup(shutil.rmtree, self._uploads, ignore_errors=True)

    def _item(self, data: bytes, name='shot.jpg'):
        return MediaItem.objects.create(
            file=SimpleUploadedFile(name, data, content_type='image/jpeg'),
            kind=MediaItem.Kind.IMAGE,
        )

    def test_analysis_records_a_result_with_its_reasons(self):
        item = self._item(as_jpeg(photo(15)))
        analyze_media_item.run(item.pk)

        result = TamperingResult.objects.get(media_item=item)
        self.assertIn(result.verdict, TamperingResult.Verdict.values)
        self.assertTrue(result.model_version)
        self.assertTrue(result.details['summary'])
        self.assertTrue(result.details['reasons'])
        self.assertIn('findings', result.details)

    def test_analysis_fingerprints_the_file_it_read(self):
        data = as_jpeg(photo(16))
        item = self._item(data)
        analyze_media_item.run(item.pk)
        item.refresh_from_db()

        self.assertEqual(item.sha256, sha256_of(data))
        self.assertEqual((item.width, item.height), (640, 480))

    def test_a_missing_file_does_not_raise(self):
        item = self._item(as_jpeg(photo(17)))
        item.file.storage.delete(item.file.name)

        self.assertIsNone(analyze_media_item.run(item.pk))
        self.assertFalse(TamperingResult.objects.filter(media_item=item).exists())

    def test_analysing_a_deleted_item_does_not_raise(self):
        self.assertIsNone(analyze_media_item.run(999999))
