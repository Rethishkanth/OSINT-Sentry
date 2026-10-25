import logging

from celery import chain, shared_task
from django.core.exceptions import ObjectDoesNotExist
from django.utils import timezone

from core.models import Source

from .models import CredibilityScore, SiteInspection
from .registry import RegistryStatus, get_registry_client
from .scoring import extract_features, get_scorer

logger = logging.getLogger(__name__)

# Registry records barely change, and every lookup is a request to someone
# else's infrastructure. Re-checking a fresh record is pure waste.
REGISTRY_TTL_DAYS = 30


@shared_task
def refresh_domain_registration(source_id, force=False):
    """Cache the registry's view of a domain onto its Source.

    This is the only place in Module 1 that touches the network, so scoring
    itself stays fast and side-effect free. A failed lookup is recorded rather
    than raised: an unreachable registry must leave the domain age *unknown*,
    never imply the domain is new.

    No Celery retry on failure, deliberately. The client already retries the
    HTTP call, and anything still unresolved is picked up by the next
    rescore_all_sources sweep, because only an 'ok' status counts as fresh.
    """
    try:
        source = Source.objects.get(pk=source_id)
    except ObjectDoesNotExist:
        logger.warning('refresh_domain_registration: Source %s is gone', source_id)
        return None

    fresh_enough = (
        source.registry_checked_at
        and source.registry_status == RegistryStatus.OK
        and (timezone.now() - source.registry_checked_at).days < REGISTRY_TTL_DAYS
    )
    if fresh_enough and not force:
        logger.debug('Registry cache still fresh for %s', source.domain)
        return source.registry_status

    info = get_registry_client().lookup(source.domain)

    # Saved through the instance rather than with a queryset .update() so the
    # post_save signal fires. Registration data is the evidence a credibility
    # score is computed from, so a change to it belongs in the audit log -
    # otherwise a score can move with no record of what moved it.
    source.domain_registered_at = info.registered_at or source.domain_registered_at
    source.registrar = info.registrar or source.registrar
    source.registry_status = info.status
    source.registry_checked_at = timezone.now()
    source.save(update_fields=[
        'domain_registered_at', 'registrar', 'registry_status',
        'registry_checked_at', 'updated_at',
    ])
    logger.info('Registry lookup for %s: %s', source.domain, info.detail or info.status)
    return info.status


@shared_task(bind=True, max_retries=3)
def score_source(self, source_id):
    """Score one Source and persist the result.

    Returns the new CredibilityScore id so callers can chain on it.
    """
    try:
        source = Source.objects.get(pk=source_id)
    except ObjectDoesNotExist:
        # The source was deleted between enqueue and execution. Nothing to
        # retry - a missing row will stay missing.
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
    """Periodic sweep: refresh registry data, then rescore every active source.

    Chained per source so each score is computed from freshly cached metadata
    rather than from whatever the last sweep happened to leave behind.
    """
    ids = list(Source.objects.filter(is_active=True).values_list('pk', flat=True))
    for source_id in ids:
        chain(
            refresh_domain_registration.si(source_id),
            score_source.si(source_id),
        ).delay()
    return len(ids)


@shared_task
def inspect_site(source_id):
    """Look inside a site and rate how safe and how cluttered it is.

    Separate from scoring on purpose. `score_source` reads cached registration
    metadata and makes no network call; this one fetches the homepage, opens a
    TLS connection, and reads the markup. Keeping them apart means a slow or
    unreachable site never delays a credibility score, and the two ratings
    stay legible as answers to two different questions.
    """
    from .inspection import findings_as_dict, inspect

    try:
        source = Source.objects.get(pk=source_id)
    except ObjectDoesNotExist:
        logger.warning('inspect_site: Source %s is gone', source_id)
        return None

    result = inspect(source.domain)
    counts = result.findings.counts()

    inspection = SiteInspection.objects.create(
        source=source,
        score=result.score,
        rating=result.rating,
        summary=result.summary,
        reasons=result.reasons,
        findings=findings_as_dict(result.findings),
        checked_url=result.findings.checked_url[:1000],
        ad_networks=counts.get('ads', 0) + counts.get('intrusive_ads', 0),
        trackers=counts.get('trackers', 0),
        uses_https=result.findings.https,
        engine_version=result.engine_version,
    )
    logger.info(
        'Inspected %s: %s (%s)', source.domain, result.headline,
        f'{result.score}/10' if result.score is not None else 'no rating',
    )
    return inspection.pk
