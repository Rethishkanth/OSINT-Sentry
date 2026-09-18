"""Reading an image for signs it was edited, without a trained model.

The plan calls for a ViT deepfake detector. That model is not installed, and
pretending otherwise would be the one thing this project has consistently
refused to do. So this module does the part that *can* be done honestly with
arithmetic over the file itself:

- **Error Level Analysis.** Re-encode the image at a known JPEG quality and
  measure where the result differs from the original. A picture compressed
  once degrades evenly; a region pasted in from a different source has its own
  compression history and degrades at a different rate. What matters is not
  how large the error is but how *unevenly* it is distributed, so the signal
  reported here is the share of blocks whose error stands out from the rest.

- **Quantization tables.** Cameras ship their own JPEG tables. Libraries —
  Pillow, libjpeg, most editors — use the IJG standard table scaled to a
  quality setting. A file whose EXIF names a camera but whose tables are plain
  IJG has been re-encoded by software since it left that camera.

- **The EXIF thumbnail.** A camera writes a small preview alongside the image
  and most editors rewrite it. When they don't, the thumbnail still shows the
  *original* picture while the full-size image shows the edited one. That is
  the closest thing here to proof, and the only finding allowed to return a
  verdict of "manipulated".

- **Metadata consistency.** Software tags naming an editor or an image
  generator, capture and modification timestamps that disagree, EXIF stripped
  from a file that claims to be a camera original.

What this is and is not
-----------------------
It is a **provenance and re-encoding inspection**. It can say that a file was
re-saved, that it passed through Photoshop, that its thumbnail disagrees with
its content, or that its error levels are suspiciously uneven.

It is **not a deepfake detector**, and `deepfake_score` is deliberately left
empty rather than filled with a number no model produced. It also cannot see
an edit that was flattened and re-saved cleanly — a skilled edit re-encoded
once looks like any other photograph.

"Suspicious" here means *worth a human look*, never *fake*. A file with
nothing to report is called `authentic` only when it still carries intact
camera metadata; otherwise the verdict is `inconclusive`, because most images
on the web have had their metadata stripped by the platform that served them
and absence of evidence is not evidence of authenticity.
"""

from __future__ import annotations

import hashlib
import io
import logging
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field

from django.conf import settings
from django.utils.module_loading import import_string

logger = logging.getLogger(__name__)

ENGINE_VERSION = 'forensics-v1'

try:
    from PIL import ExifTags, Image, ImageChops, ImageFilter, ImageStat

    PILLOW_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only on a bare install
    PILLOW_AVAILABLE = False


# Software tags that mean the file went through an editor. Not incriminating
# on its own — cropping and levels are editing too — but it is a fact about
# the file's history and the analyst should be told.
EDITORS = (
    ('photoshop', 'Adobe Photoshop'),
    ('lightroom', 'Adobe Lightroom'),
    ('illustrator', 'Adobe Illustrator'),
    ('affinity', 'Affinity Photo'),
    ('pixelmator', 'Pixelmator'),
    ('gimp', 'GIMP'),
    ('krita', 'Krita'),
    ('paint.net', 'Paint.NET'),
    ('imagemagick', 'ImageMagick'),
    ('snapseed', 'Snapseed'),
    ('picsart', 'PicsArt'),
    ('facetune', 'Facetune'),
    ('canva', 'Canva'),
    ('ffmpeg', 'FFmpeg'),
)

# Software tags that mean the picture was not photographed at all.
GENERATORS = (
    ('midjourney', 'Midjourney'),
    ('stable diffusion', 'Stable Diffusion'),
    ('stablediffusion', 'Stable Diffusion'),
    ('automatic1111', 'Stable Diffusion (AUTOMATIC1111)'),
    ('comfyui', 'ComfyUI'),
    ('dall-e', 'DALL-E'),
    ('dalle', 'DALL-E'),
    ('firefly', 'Adobe Firefly'),
    ('imagen', 'Google Imagen'),
    ('nano banana', 'Google Nano Banana'),
)

# The luminance quantization table from the JPEG standard, Annex K. Every
# standard encoder scales this one table by its quality setting.
IJG_LUMA = (
    16, 11, 10, 16, 24, 40, 51, 61,
    12, 12, 14, 19, 26, 58, 60, 55,
    14, 13, 16, 24, 40, 57, 69, 56,
    14, 17, 22, 29, 51, 87, 80, 62,
    18, 22, 37, 56, 68, 109, 103, 77,
    24, 35, 55, 64, 81, 104, 113, 92,
    49, 64, 78, 87, 103, 121, 120, 101,
    72, 92, 95, 98, 112, 100, 103, 99,
)

EXIF_MAKE = 0x010F
EXIF_MODEL = 0x0110
EXIF_SOFTWARE = 0x0131
EXIF_DATETIME = 0x0132
EXIF_ORIENTATION = 0x0112
EXIF_DATETIME_ORIGINAL = 0x9003
THUMB_OFFSET = 0x0201  # JPEGInterchangeFormat
THUMB_LENGTH = 0x0202  # JPEGInterchangeFormatLength


class Verdict:
    """Mirrors media.models.TamperingResult.Verdict without importing it."""

    AUTHENTIC = 'authentic'
    SUSPICIOUS = 'suspicious'
    MANIPULATED = 'manipulated'
    INCONCLUSIVE = 'inconclusive'


@dataclass
class Findings:
    """Everything observed about the file, before any of it is weighed."""

    readable: bool = False
    unreadable_reason: str = ''

    # -- container ---------------------------------------------------------
    format: str = ''
    mode: str = ''
    width: int | None = None
    height: int | None = None
    size_bytes: int | None = None
    progressive: bool = False
    icc_profile: bool = False

    # -- metadata ----------------------------------------------------------
    exif_present: bool = False
    camera_make: str = ''
    camera_model: str = ''
    software: str = ''
    editor: str = ''
    generator: str = ''
    captured_at: str = ''
    modified_at: str = ''
    gps_present: bool = False
    orientation: int | None = None

    # -- compression history ----------------------------------------------
    quantization_profile: str = ''  # 'ijg-standard' | 'custom' | ''
    estimated_quality: int | None = None

    # -- error level analysis ----------------------------------------------
    ela_supported: bool = False
    ela_mean: float | None = None
    ela_max: int | None = None
    ela_hotspot_ratio: float | None = None

    # -- embedded preview --------------------------------------------------
    thumbnail_present: bool = False
    thumbnail_distance: float | None = None

    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class ForensicResult:
    """One assessment, with the arithmetic that produced it left visible."""

    verdict: str
    summary: str
    ela_score: float | None
    deepfake_score: float | None
    suspicion: float
    reasons: list[dict]
    findings: Findings
    engine_version: str = ENGINE_VERSION

    def as_details(self) -> dict:
        """The JSON blob stored on TamperingResult.details."""
        return {
            'engine': self.engine_version,
            'summary': self.summary,
            'suspicion': round(self.suspicion, 3),
            'reasons': self.reasons,
            'findings': self.findings.as_dict(),
        }


class BaseForensics(ABC):
    """Contract a real detector slots into.

    A trained model subclasses this, fills in `deepfake_score`, and is named
    in MEDIA_FORENSICS_ANALYZER. The task that calls it does not change.
    """

    engine_version: str = 'base'

    @abstractmethod
    def analyze(self, data: bytes, *, kind: str = 'image') -> ForensicResult:
        """Assess the raw bytes of one file."""


class HeuristicForensics(BaseForensics):
    """Arithmetic over the file. No model, and it says so."""

    engine_version = ENGINE_VERSION

    def analyze(self, data: bytes, *, kind: str = 'image') -> ForensicResult:
        if kind != 'image':
            return _unreadable(
                f'No {kind} forensics engine is installed. The file has been '
                f'hashed and recorded, but nothing about its content was '
                f'examined — an inconclusive verdict is the honest one.'
            )
        if not PILLOW_AVAILABLE:
            return _unreadable(
                'Pillow is not installed, so the image could not be decoded. '
                'Install it to enable error level analysis.'
            )

        findings = _gather(data)
        return _rate(findings)


# ---------------------------------------------------------------------------
# observation
# ---------------------------------------------------------------------------


def _gather(data: bytes) -> Findings:
    """Open the file and write down everything, weighing nothing."""
    findings = Findings(size_bytes=len(data))

    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except Exception as exc:  # Pillow raises a wide family here
        findings.unreadable_reason = f'{type(exc).__name__}: {exc}'
        return findings

    findings.readable = True
    findings.format = image.format or ''
    findings.mode = image.mode
    findings.width, findings.height = image.size
    findings.progressive = bool(image.info.get('progression'))
    findings.icc_profile = bool(image.info.get('icc_profile'))

    _read_metadata(image, findings)
    _read_quantization(image, findings)
    _read_thumbnail(image, data, findings)

    if findings.format == 'JPEG':
        _run_ela(image, findings)
    else:
        findings.notes.append(
            f'Error level analysis applies to JPEG images; this file is '
            f'{findings.format or "an unrecognised format"}.'
        )

    return findings


def _read_metadata(image, findings: Findings) -> None:
    try:
        exif = image.getexif()
    except Exception:
        return
    if not exif:
        return

    findings.exif_present = True
    findings.camera_make = str(exif.get(EXIF_MAKE, '') or '').strip()
    findings.camera_model = str(exif.get(EXIF_MODEL, '') or '').strip()
    findings.software = str(exif.get(EXIF_SOFTWARE, '') or '').strip()
    findings.modified_at = str(exif.get(EXIF_DATETIME, '') or '').strip()
    orientation = exif.get(EXIF_ORIENTATION)
    findings.orientation = int(orientation) if isinstance(orientation, int) else None

    try:
        sub = exif.get_ifd(ExifTags.IFD.Exif)
        findings.captured_at = str(sub.get(EXIF_DATETIME_ORIGINAL, '') or '').strip()
        findings.gps_present = bool(exif.get_ifd(ExifTags.IFD.GPSInfo))
    except Exception:
        pass

    lowered = findings.software.lower()
    for needle, label in GENERATORS:
        if needle in lowered:
            findings.generator = label
            break
    if not findings.generator:
        for needle, label in EDITORS:
            if needle in lowered:
                findings.editor = label
                break


def _read_quantization(image, findings: Findings) -> None:
    """Decide whether the JPEG tables came from a camera or from a library."""
    tables = getattr(image, 'quantization', None)
    if not tables:
        return

    luma = list(tables.get(0) or [])
    if len(luma) != 64:
        findings.quantization_profile = 'unknown'
        return

    for quality in range(1, 101):
        if luma == _ijg_table(quality):
            findings.quantization_profile = 'ijg-standard'
            findings.estimated_quality = quality
            return

    findings.quantization_profile = 'custom'


def _ijg_table(quality: int) -> list[int]:
    """The luminance table libjpeg produces at a given quality setting."""
    quality = max(1, min(100, quality))
    scale = 5000 // quality if quality < 50 else 200 - quality * 2
    return [
        max(1, min(255, (value * scale + 50) // 100))
        for value in IJG_LUMA
    ]


def _read_thumbnail(image, data: bytes, findings: Findings) -> None:
    """Compare the embedded preview against the picture it claims to preview."""
    raw = image.info.get('exif')
    if not raw:
        return

    try:
        exif = image.getexif()
        ifd1 = exif.get_ifd(ExifTags.IFD.IFD1)
    except Exception:
        return
    if not ifd1:
        return

    offset = ifd1.get(THUMB_OFFSET)
    length = ifd1.get(THUMB_LENGTH)
    if not offset or not length:
        return

    # Offsets inside an APP1 segment are measured from the TIFF header, which
    # sits after the six-byte 'Exif\0\0' introducer.
    body = raw[6:] if raw[:4] == b'Exif' else raw
    blob = body[offset:offset + length]
    if len(blob) < length // 2:
        return

    try:
        thumb = Image.open(io.BytesIO(blob))
        thumb.load()
    except Exception:
        return

    findings.thumbnail_present = True
    findings.thumbnail_distance = _perceptual_distance(image, thumb)


def _perceptual_distance(a, b) -> float:
    """Mean absolute difference of two images reduced to a 32x32 grey grid.

    Coarse on purpose. A thumbnail is a heavily compressed, differently
    resampled copy, so fine detail disagrees even when both show the same
    picture; only gross structural difference is meaningful here.
    """
    size = (32, 32)
    left = a.convert('L').resize(size, Image.BILINEAR)
    right = b.convert('L').resize(size, Image.BILINEAR)
    diff = ImageChops.difference(left, right)
    return round(ImageStat.Stat(diff).mean[0] / 255.0, 4)


def _run_ela(image, findings: Findings) -> None:
    """Re-encode at a known quality and measure where the error concentrates."""
    quality = getattr(settings, 'MEDIA_ELA_QUALITY', 90)
    longest = getattr(settings, 'MEDIA_ELA_MAX_EDGE', 1400)

    working = image.convert('RGB')
    if max(working.size) > longest:
        scale = longest / max(working.size)
        working = working.resize(
            (max(1, int(working.width * scale)), max(1, int(working.height * scale))),
            Image.BILINEAR,
        )

    buffer = io.BytesIO()
    try:
        working.save(buffer, 'JPEG', quality=quality)
        buffer.seek(0)
        resaved = Image.open(buffer)
        resaved.load()
    except Exception as exc:
        findings.notes.append(f'Error level analysis failed: {exc}')
        return

    diff = ImageChops.difference(working, resaved).convert('L')
    stat = ImageStat.Stat(diff)
    findings.ela_supported = True
    findings.ela_mean = round(stat.mean[0], 4)
    findings.ela_max = int(diff.getextrema()[1])
    findings.ela_hotspot_ratio = _hotspot_ratio(diff, working)


def _hotspot_ratio(diff, original) -> float:
    """Size of the largest *contiguous* patch of anomalous error, in blocks.

    Two corrections to naive error level analysis, both of which matter:

    First, raw error is not comparable across a picture. A blank sky
    recompresses almost perfectly and a patch of foliage never does, so
    ranking blocks by error just finds the busiest part of every photograph.
    What is roughly constant across a singly-compressed image is error *per
    unit of detail*, so each block is divided by its own edge energy before
    anything is compared.

    Second, and more important: every photograph has outlier blocks. What a
    photograph does not have is a *connected region* of them. A pasted-in area
    carries its own compression history across its whole extent, so its blocks
    deviate together and touch each other. Scattered speckle is normal and is
    what the old version of this function kept reporting; one solid patch is
    the thing worth showing an analyst. So the number returned is the largest
    connected group of deviating blocks, not the total count.

    Deviation counts in both directions — a region taken from a heavily
    compressed source has already lost its high-frequency content and so
    re-encodes with *less* error than its surroundings, not more.
    """
    block = 16
    columns = max(1, diff.width // block)
    rows = max(1, diff.height // block)
    total = columns * rows
    if total < 64:
        # Too few blocks for "a region" to mean anything.
        return 0.0

    # A BOX resize averages each source region into one destination pixel, so
    # these are per-block means computed in two passes instead of thousands.
    errors = list(diff.resize((columns, rows), Image.BOX).getdata())
    detail = list(
        original.convert('L')
        .filter(ImageFilter.FIND_EDGES)
        .resize((columns, rows), Image.BOX)
        .getdata()
    )

    # The +2 floors the denominator: a perfectly flat block has no detail at
    # all, and dividing by it would make rounding noise look like evidence.
    ratios = [error / (edge + 2.0) for error, edge in zip(errors, detail)]

    ordered = sorted(ratios)
    median = ordered[len(ordered) // 2]
    deviations = sorted(abs(value - median) for value in ratios)
    mad = deviations[len(deviations) // 2]
    if mad <= 0:
        return 0.0

    threshold = max(3.0 * mad, 0.02)
    flagged = [abs(value - median) > threshold for value in ratios]

    return round(_largest_cluster(flagged, columns, rows) / total, 4)


def _largest_cluster(flagged: list[bool], columns: int, rows: int) -> int:
    """Blocks in the biggest 4-connected group of flagged blocks."""
    seen = [False] * len(flagged)
    largest = 0

    for start in range(len(flagged)):
        if not flagged[start] or seen[start]:
            continue
        seen[start] = True
        stack = [start]
        size = 0
        while stack:
            index = stack.pop()
            size += 1
            row, column = divmod(index, columns)
            neighbours = []
            if column > 0:
                neighbours.append(index - 1)
            if column < columns - 1:
                neighbours.append(index + 1)
            if row > 0:
                neighbours.append(index - columns)
            if row < rows - 1:
                neighbours.append(index + columns)
            for neighbour in neighbours:
                if flagged[neighbour] and not seen[neighbour]:
                    seen[neighbour] = True
                    stack.append(neighbour)
        largest = max(largest, size)

    return largest


# ---------------------------------------------------------------------------
# judgement
# ---------------------------------------------------------------------------


def _unreadable(reason: str) -> ForensicResult:
    findings = Findings(unreadable_reason=reason)
    return ForensicResult(
        verdict=Verdict.INCONCLUSIVE,
        summary=reason,
        ela_score=None,
        deepfake_score=None,
        suspicion=0.0,
        reasons=[],
        findings=findings,
    )


def _rate(findings: Findings) -> ForensicResult:
    """Weigh the observations. Every point added is named."""
    if not findings.readable:
        return _unreadable(
            f'The file could not be decoded as an image, so nothing was '
            f'examined. ({findings.unreadable_reason})'
        )

    suspicion = 0.0
    reasons: list[dict] = []
    proven = False

    def add(points: float, label: str):
        nonlocal suspicion
        suspicion += points
        reasons.append({'label': label, 'points': round(points, 2)})

    # -- the one finding strong enough to conclude with -------------------
    if findings.thumbnail_distance is not None and findings.thumbnail_distance >= 0.18:
        proven = True
        add(
            0.6,
            f'The embedded EXIF thumbnail does not show the same picture as '
            f'the image itself (difference {findings.thumbnail_distance:.2f} '
            f'on a 0-1 scale). The preview was written before the edit and '
            f'never refreshed.',
        )
    elif findings.thumbnail_present:
        reasons.append({
            'label': 'Embedded EXIF thumbnail matches the image',
            'points': 0.0,
        })

    # -- what the file says about its own history -------------------------
    if findings.generator:
        add(0.5, f'Metadata names an image generator: {findings.generator}')
    elif findings.editor:
        add(
            0.3,
            f'Metadata names an image editor: {findings.editor}. Cropping and '
            f'colour correction leave the same trace as a composite does.',
        )

    if findings.camera_make and findings.quantization_profile == 'ijg-standard':
        camera = f'{findings.camera_make} {findings.camera_model}'.strip()
        add(
            0.25,
            f'EXIF names a camera ({camera}) but the JPEG tables are the '
            f'standard library ones at quality {findings.estimated_quality}, '
            f'so the file was re-encoded by software after capture.',
        )

    if findings.captured_at and findings.modified_at:
        if findings.captured_at != findings.modified_at:
            add(
                0.2,
                f'Capture time ({findings.captured_at}) and file modification '
                f'time ({findings.modified_at}) disagree.',
            )

    if findings.format == 'JPEG' and not findings.exif_present:
        add(
            0.1,
            'No EXIF at all. Usually the platform that served the image '
            'stripped it, which is routine — but it also means the file can '
            'no longer account for where it came from.',
        )

    # -- error levels ------------------------------------------------------
    ela_score = None
    if findings.ela_supported:
        ratio = findings.ela_hotspot_ratio or 0.0
        ela_score = round(ratio * 100, 2)
        if ratio >= 0.08:
            add(
                0.35,
                f'One connected region covering {ratio * 100:.1f}% of the '
                f'frame degrades at a markedly different rate from everything '
                f'around it — the signature of an area carrying its own '
                f'compression history.',
            )
        elif ratio >= 0.035:
            add(
                0.15,
                f'A region covering {ratio * 100:.1f}% of the frame shows '
                f'error levels out of step with the rest — weak, and a large '
                f'overlay, a logo or heavy local sharpening does the same.',
            )
        else:
            reasons.append({
                'label': 'Error levels are consistent across the whole frame',
                'points': 0.0,
            })

    suspicion = round(max(0.0, min(1.0, suspicion)), 3)
    verdict, summary = _verdict_for(suspicion, proven, findings)

    return ForensicResult(
        verdict=verdict,
        summary=summary,
        ela_score=ela_score,
        # No model produced this, so no number is invented for it.
        deepfake_score=None,
        suspicion=suspicion,
        reasons=reasons,
        findings=findings,
    )


def _verdict_for(suspicion: float, proven: bool, findings: Findings) -> tuple[str, str]:
    """Turn a score into one of four words, conservatively.

    `manipulated` is reserved for the thumbnail contradiction, the only
    finding here that is close to proof. Everything else, however much of it
    piles up, tops out at `suspicious` — because every individual signal in
    this module also has an innocent explanation.
    """
    if proven:
        return (
            Verdict.MANIPULATED,
            'The image and its own embedded preview disagree. Something was '
            'changed after the file was created.',
        )

    if suspicion >= 0.45:
        return (
            Verdict.SUSPICIOUS,
            'Several independent signs that this file was not saved straight '
            'out of a camera. Worth a human look — this is not a finding of '
            'forgery.',
        )

    if suspicion >= 0.2:
        return (
            Verdict.SUSPICIOUS,
            'One or two signs of re-encoding or editing. Common in ordinary '
            'images that have been cropped or passed through a platform.',
        )

    has_provenance = bool(
        findings.camera_make
        and findings.captured_at
        and findings.quantization_profile == 'custom'
    )
    if has_provenance:
        return (
            Verdict.AUTHENTIC,
            'Intact camera metadata, camera-specific compression tables, and '
            'evenly distributed error levels. Consistent with an unedited '
            'original — which is not the same as proof of one.',
        )

    return (
        Verdict.INCONCLUSIVE,
        'Nothing suspicious found, but nothing to vouch for the file either: '
        'it carries no intact camera provenance. Most images on the web look '
        'like this, so no verdict is given rather than a reassuring one.',
    )


# ---------------------------------------------------------------------------


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def dimensions_of(data: bytes) -> tuple[int | None, int | None]:
    """Width and height, or (None, None) for anything that will not decode."""
    if not PILLOW_AVAILABLE:
        return None, None
    try:
        with Image.open(io.BytesIO(data)) as image:
            return image.size
    except Exception:
        return None, None


def get_analyzer() -> BaseForensics:
    path = getattr(
        settings, 'MEDIA_FORENSICS_ANALYZER', 'media.forensics.HeuristicForensics'
    )
    return import_string(path)()
