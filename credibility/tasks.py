import logging

from celery import shared_task
from django.core.exceptions import ObjectDoesNotExist

from core.models import Source

from .models import CredibilityScore
from .scoring import extract_features, get_scorer

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=3)
def score_source(self, source_id):
    """Score one Source and persist the result.

    Returns the new CredibilityScore id so callers can chain on it.
    """
    try:
        source = Source.objects.get(pk=source_id)
    except ObjectDoesNotExist:
        # The source was deleted between enqueue and execution. Nothing to
        # retry — a missing row will stay missing.
        logger.warning('score_source: Source %s no longer exists', source_id)
        return None

    features = extract_features(source)
    result = get_scorer().score(features)

    score = CredibilityScore.objects.create(
        source=source,
        credibility_score=result.score,
        trust_tier=result.tier,
        provenance_hash=features.provenance_hash(),
        model_version=result.model_version,
        rationale=result.rationale,
    )
    logger.info(
        'Scored %s: %.1f (%s)', source.domain, result.score, result.tier
    )
    return score.pk


@shared_task
def rescore_all_sources():
    """Periodic sweep: rescore every active source."""
    ids = Source.objects.filter(is_active=True).values_list('pk', flat=True)
    for source_id in ids:
        score_source.delay(source_id)
    return len(ids)
