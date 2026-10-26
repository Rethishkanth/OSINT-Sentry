"""Module 4's trained engine: a ViT that reads whether an image was generated.

The plan called for a ViT deepfake detector. This is one, and it was not
trained here. The weights are the Community Forensics detector (Park and
Owens, CVPR 2025): a ViT-Small fine-tuned on 2.7 million images from 4,803
generators, released by its authors under the MIT licence. Training a
competitive detector from scratch takes that kind of breadth, which is the
paper's whole finding - a detector trained on a handful of generators learns
those generators, not generation - and a CPU-trained model on a public
benchmark of two or three generators would have been worse than the weights
already published, and no more honest.

What this project adds is the measurement and the line. `manage.py
evaluate_detector` runs this file's own `generated_probabilities` over a
stratified sample of the authors' held-out evaluation set, including a pass
where every image is re-saved as a JPEG first, and the confidence floor
below which the detector says nothing is chosen from that table. The numbers
are in media/artifacts/vit_detector.metrics.json.

What it adds to a verdict
-------------------------
One question, answered in one direction. A confident reading that an image
was *generated* raises the verdict to `suspicious` and says why. A confident
reading that it was *photographed* is recorded and changes nothing: a
detector that has not seen a generator misses it, so "the model found
nothing" is not evidence of authenticity, any more than a missing EXIF block
is evidence of forgery. And it never produces `manipulated`. That word means
the file contradicts itself, which only the thumbnail check in
media.forensics can show; a generated image was not edited, it was made.

What it reads
-------------
The centre of the frame. The preprocessing resizes the shortest edge to 440
and crops a 384 pixel square from the middle, as the model was trained, so a
generated object pasted into the corner of a real photograph is outside what
the model looks at. It is a whole-image generation detector, not a splice
detector, and the console's rationale says so on every reading.

Everything media.forensics does still runs. This class inherits it and adds
one reading on top, so error levels, quantization tables, the thumbnail check
and the metadata are all still in the result.
"""

from __future__ import annotations

import io
import os
from functools import lru_cache

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from .forensics import ForensicResult, HeuristicForensics, Verdict

DEFAULT_MODEL = 'buildborderless/CommunityForensics-DeepfakeDet-ViT'
# Pinned, because this repository has already replaced its weights once: the
# July 2026 upload fixed a config that sliced attention into the wrong number
# of heads. A floor measured against one set of weights says nothing about
# the next, so the revision that was measured is the revision that is served.
DEFAULT_REVISION = 'ac6ee457bea904a373065754107451793b56db00'

# Images are scored in groups this size by the evaluation command. Serving
# scores one at a time through the same function.
BATCH = 16


def _cache_dir() -> str:
    return str(getattr(settings, 'CONTENT_MODEL_CACHE', '.model-cache'))


def model_name() -> str:
    return getattr(settings, 'MEDIA_DETECTOR_MODEL', DEFAULT_MODEL)


def model_revision() -> str:
    return getattr(settings, 'MEDIA_DETECTOR_REVISION', DEFAULT_REVISION)


@lru_cache(maxsize=1)
def _detector():
    """The image processor and the classifier, loaded once per process."""
    os.environ.setdefault('HF_HOME', _cache_dir())
    try:
        import torch  # noqa: F401  (transformers needs it; say so clearly)
        from transformers import AutoImageProcessor, ViTForImageClassification
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise ImproperlyConfigured(
            'the media detector needs torch and transformers. '
            'pip install -r requirements-ml.txt'
        ) from exc

    name, revision = model_name(), model_revision()
    processor = AutoImageProcessor.from_pretrained(name, revision=revision)
    model = ViTForImageClassification.from_pretrained(name, revision=revision)
    model.eval()
    if model.config.num_labels != 1:
        # The detector emits one logit for "generated". A two-label head here
        # means the wrong weights, or the pre-July upload, and reading index
        # 0 of it as a probability would be a silent wrong answer.
        raise ImproperlyConfigured(
            f'{name}@{revision} has {model.config.num_labels} output labels; '
            'the detector expects a single generated-image logit.'
        )
    return processor, model


def prepare(data: bytes):
    """Decode bytes into the RGB picture the model reads, or None.

    The first frame of an animation, the visible colours of a palette image,
    and the colour channels of one with transparency. A 16-bit greyscale TIFF
    is scaled down to 8 bits rather than clipped, which is what a plain
    convert would do to it.
    """
    from PIL import Image

    try:
        image = Image.open(io.BytesIO(data))
        image.seek(0)
        image.load()
    except Exception:  # Pillow raises a wide family here
        return None

    if image.mode in ('I;16', 'I;16B', 'I;16L', 'I'):
        image = image.point(lambda value: value / 256).convert('L')
    try:
        return image.convert('RGB')
    except Exception:
        return None


def generated_probabilities(images) -> list[float]:
    """P(generated) for each prepared image. The one path that scores.

    Serving calls this with a list of one; the evaluation command calls it
    with batches. Both go through the processor the model was trained with.
    """
    import torch

    processor, model = _detector()
    scores: list[float] = []
    for start in range(0, len(images), BATCH):
        chunk = images[start:start + BATCH]
        inputs = processor(chunk, return_tensors='pt')
        with torch.no_grad():
            logits = model(**inputs).logits[:, 0]
        scores.extend(float(p) for p in torch.sigmoid(logits))
    return scores


class ViTForensics(HeuristicForensics):
    """The file inspection, plus a trained reading of whether it was generated."""

    engine_version = 'vit-commfor-v1+forensics-v1'
    is_trained = True

    def __init__(self, min_confidence: float | None = None):
        self.min_confidence = (
            min_confidence if min_confidence is not None
            else getattr(settings, 'MEDIA_DETECTOR_MIN_CONFIDENCE', 0.95)
        )

    def analyze(self, data: bytes, *, kind: str = 'image') -> ForensicResult:
        result = super().analyze(data, kind=kind)
        result.engine_version = self.engine_version
        if kind != 'image' or not result.findings.readable:
            return result

        image = prepare(data)
        if image is None:
            result.reasons.append({
                'label': 'The detector could not decode this file, so it gave '
                         'no reading.',
                'points': 0.0,
            })
            return result

        probability = generated_probabilities([image])[0]
        return self.weigh(result, probability)

    def weigh(self, result: ForensicResult, probability: float) -> ForensicResult:
        """Fold one detector reading into a finished file inspection."""
        confidence = max(probability, 1.0 - probability)
        if confidence < self.min_confidence:
            result.reasons.append({
                'label': (
                    f'The generated-image detector would not commit: '
                    f'{probability:.2f} on a 0-1 scale, inside the band where '
                    f'it was measured to be unreliable. No reading is recorded '
                    f'rather than a weak one.'
                ),
                'points': 0.0,
            })
            return result

        result.deepfake_score = round(probability, 4)

        if probability < 0.5:
            result.reasons.append({
                'label': (
                    f'The generated-image detector reads this as a photograph '
                    f'({probability:.2f} on a 0-1 scale). That is not evidence '
                    f'of authenticity: it misses generators it was not trained '
                    f'on, and it reads only the centre of the frame.'
                ),
                'points': 0.0,
            })
            return result

        points = 0.45
        result.suspicion = round(min(1.0, result.suspicion + points), 3)
        result.reasons.append({
            'label': (
                f'The generated-image detector reads this as generated '
                f'({probability:.2f} on a 0-1 scale). It reads the centre of '
                f'the frame as a whole and cannot say which part, if any, '
                f'was photographed.'
            ),
            'points': points,
        })
        if result.verdict != Verdict.MANIPULATED:
            # Not manipulated: that verdict means the file contradicts its
            # own preview. A generated picture was not edited, it was made.
            result.verdict = Verdict.SUSPICIOUS
            result.summary = (
                f'The trained detector reads this image as generated '
                f'({probability:.2f}), above the line where it was measured '
                f'to be right most of the time. Worth a human look before it '
                f'is used as evidence of anything.'
            )
        return result
