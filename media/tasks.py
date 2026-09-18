import logging

from celery import shared_task
from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction

from .forensics import dimensions_of, get_analyzer, sha256_of
from .models import MediaItem, TamperingResult

logger = logging.getLogger(__name__)


def _read(item: MediaItem) -> bytes | None:
    """The file's bytes, or None if the stored file has gone missing."""
    try:
        with item.file.open('rb') as handle:
            return handle.read()
    except (FileNotFoundError, ValueError) as exc:
        logger.warning('MediaItem %s: file unreadable (%s)', item.pk, exc)
        return None


@shared_task
def fingerprint_media_item(item_id):
    """Hash the file and record its dimensions.

    Separate from analysis on purpose: the hash is what ties a result to
    exact bytes, so it has to be recorded even when no analysis is possible.
    """
    try:
        item = MediaItem.objects.get(pk=item_id)
    except ObjectDoesNotExist:
        logger.warning('fingerprint_media_item: MediaItem %s is gone', item_id)
        return None

    data = _read(item)
    if data is None:
        return None

    item.sha256 = sha256_of(data)
    if item.kind == MediaItem.Kind.IMAGE:
        width, height = dimensions_of(data)
        item.width, item.height = width, height
    item.save(update_fields=['sha256', 'width', 'height', 'updated_at'])
    return item.sha256


@shared_task
def analyze_media_item(item_id):
    """Run the forensics engine over one item and store the result."""
    try:
        item = MediaItem.objects.get(pk=item_id)
    except ObjectDoesNotExist:
        logger.warning('analyze_media_item: MediaItem %s is gone', item_id)
        return None

    data = _read(item)
    if data is None:
        return None

    if not item.sha256:
        item.sha256 = sha256_of(data)
        if item.kind == MediaItem.Kind.IMAGE:
            item.width, item.height = dimensions_of(data)
        item.save(update_fields=['sha256', 'width', 'height', 'updated_at'])

    analyzer = get_analyzer()
    result = analyzer.analyze(data, kind=item.kind)

    with transaction.atomic():
        record = TamperingResult.objects.create(
            media_item=item,
            ela_score=result.ela_score,
            deepfake_score=result.deepfake_score,
            verdict=result.verdict,
            model_version=result.engine_version,
            details=result.as_details(),
        )

    logger.info('MediaItem %s analysed: %s', item.pk, result.verdict)
    return record.pk


@shared_task
def analyze_unanalyzed(limit=200):
    """Catch-up sweep for items the engine has never seen."""
    ids = list(
        MediaItem.objects.filter(tampering_results__isnull=True)
        .values_list('pk', flat=True)[:limit]
    )
    for item_id in ids:
        analyze_media_item.delay(item_id)
    return len(ids)
