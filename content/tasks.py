import logging

from celery import shared_task
from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from core.models import Source
from credibility.models import CredibilityScore
from credibility.tasks import refresh_domain_registration, score_source

from .fetchers import fetch_page, get_fetcher
from .http import Blocked, FetchError, assert_fetchable
from .models import (
    ContentItem,
    ContentTopic,
    Corroboration,
    CorroborationMatch,
    Entity,
    Topic,
)
from .pipeline import get_analyzer

logger = logging.getLogger(__name__)

# Sources scoring below this are not ingested at all. None disables the gate.
DEFAULT_MIN_CREDIBILITY = 25.0


class CredibilityGateRejection(Exception):
    """Raised when a source is too low-scoring to ingest content from."""


def _min_credibility():
    return getattr(settings, 'CONTENT_MIN_CREDIBILITY', DEFAULT_MIN_CREDIBILITY)


def check_credibility_gate(source) -> CredibilityScore | None:
    """Module 1 gate: every item clears credibility before it is stored.

    A source that has never been scored is scored inline rather than waved
    through — otherwise the gate is trivially bypassed by ingesting fast.
    """
    threshold = _min_credibility()
    if threshold is None:
        return None

    latest = source.credibility_scores.first()
    if latest is None:
        # Cache the registry data first, or this first score — the one the gate
        # acts on — is computed with the domain's age unknown. The lookup
        # short-circuits when the cache is already fresh.
        refresh_domain_registration.run(source.pk)
        score_id = score_source.run(source.pk)
        latest = CredibilityScore.objects.filter(pk=score_id).first()

    if latest is None:
        raise CredibilityGateRejection(f'{source.domain}: could not be scored')

    if latest.credibility_score < threshold:
        raise CredibilityGateRejection(
            f'{source.domain}: credibility {latest.credibility_score:.1f} '
            f'below threshold {threshold}'
        )
    return latest


@shared_task
def ingest_item(source_id, url, title='', body='', author='', language='', published_at=None):
    """Store one fetched document as a ContentItem, gated on credibility.

    Returns the ContentItem id, or None if the gate rejected it.
    """
    try:
        source = Source.objects.get(pk=source_id)
    except ObjectDoesNotExist:
        logger.warning('ingest_item: Source %s no longer exists', source_id)
        return None

    try:
        check_credibility_gate(source)
    except CredibilityGateRejection as exc:
        logger.info('Rejected by credibility gate: %s', exc)
        return None

    # Celery serialises arguments as JSON, so a datetime arrives here as an
    # ISO string. Parsing it in one place means callers can pass either.
    if isinstance(published_at, str):
        published_at = parse_datetime(published_at) if published_at else None

    item, created = ContentItem.objects.get_or_create(
        url=url,
        defaults={
            'source': source,
            'title': title,
            'body': body,
            'author': author,
            'language': language,
            'published_at': published_at,
            'fetched_at': timezone.now(),
        },
    )
    if not created:
        return item.pk

    transaction.on_commit(lambda: analyze_content.delay(item.pk))
    return item.pk


@shared_task(bind=True, max_retries=3)
def analyze_content(self, content_item_id):
    """Run NER, topic detection, and summarisation over one ContentItem."""
    try:
        item = ContentItem.objects.get(pk=content_item_id)
    except ObjectDoesNotExist:
        logger.warning('analyze_content: ContentItem %s no longer exists', content_item_id)
        return None

    ContentItem.objects.filter(pk=item.pk).update(status=ContentItem.Status.PROCESSING)

    text = f'{item.title}\n\n{item.body}'.strip()
    try:
        result = get_analyzer().analyze(text)
    except Exception as exc:
        ContentItem.objects.filter(pk=item.pk).update(status=ContentItem.Status.FAILED)
        logger.exception('Analysis failed for ContentItem %s', item.pk)
        raise self.retry(exc=exc, countdown=30)

    with transaction.atomic():
        # Analysis is idempotent: a re-run replaces the previous output rather
        # than accumulating duplicates alongside it.
        item.entities.all().delete()
        Entity.objects.bulk_create(
            Entity(
                content_item=item,
                text=span.text[:255],
                label=span.label,
                confidence=span.confidence,
                mention_count=span.mention_count,
            )
            for span in result.entities
        )

        item.content_topics.all().delete()
        for hit in result.topics:
            topic, _ = Topic.objects.get_or_create(
                label=hit.label,
                defaults={'keywords': hit.keywords},
            )
            ContentTopic.objects.create(content_item=item, topic=topic, weight=hit.weight)

        item.summary = result.summary
        item.sentiment = result.sentiment
        item.analyzer_version = result.analyzer_version
        item.analyzed_at = timezone.now()
        item.status = ContentItem.Status.ANALYZED
        item.save(update_fields=[
            'summary', 'sentiment', 'analyzer_version', 'analyzed_at',
            'status', 'updated_at',
        ])

    logger.info(
        'Analyzed ContentItem %s: %d entities, %d topics',
        item.pk, len(result.entities), len(result.topics),
    )
    return item.pk


def _store(source, fetched, inline: bool):
    """Hand one fetched document to the ingest path."""
    published = fetched.published_at.isoformat() if fetched.published_at else None
    args = (source.pk, fetched.url)
    kwargs = {
        'title': fetched.title,
        'body': fetched.body,
        'author': fetched.author,
        'language': fetched.language,
        'published_at': published,
    }
    if inline:
        return ingest_item.run(*args, **kwargs)
    ingest_item.delay(*args, **kwargs)
    return None


@shared_task
def corroborate_item(content_item_id):
    """Cross-reference one stored article against other outlets.

    Returns the Corroboration id. The report is always written, including
    when the search failed — a check that could not complete is a finding an
    analyst needs to see, not one to hide.
    """
    from .corroborate import assess

    try:
        item = ContentItem.objects.select_related('source').get(pk=content_item_id)
    except ObjectDoesNotExist:
        logger.warning('corroborate_item: ContentItem %s no longer exists', content_item_id)
        return None

    # Corroboration runs on entities and topics, so an unanalysed item has
    # nothing to search with. Analyse it now rather than rating it on a
    # headline alone.
    if item.status != ContentItem.Status.ANALYZED:
        analyze_content.run(item.pk)
        item.refresh_from_db()

    result = assess(item)

    with transaction.atomic():
        report = Corroboration.objects.create(
            content_item=item,
            score=result.score,
            verdict=result.verdict,
            keywords=result.keywords,
            rationale=result.rationale,
            searched=result.searched[:255],
            sources_checked=result.sources_checked,
            independent_sources=result.independent_sources,
            engine_version=result.engine_version,
        )
        tracked = {
            s.domain: s for s in Source.objects.filter(
                domain__in=[m.domain for m in result.matches]
            )
        }
        CorroborationMatch.objects.bulk_create(
            CorroborationMatch(
                report=report,
                source=tracked.get(match.domain),
                domain=match.domain[:255],
                url=match.url[:1000],
                title=(match.title or '')[:500],
                similarity=match.similarity,
                matched_terms=match.matched_terms,
                credibility=match.credibility,
                published_at=match.published_at,
            )
            for match in result.matches
        )

    logger.info(
        'Corroborated ContentItem %s: %s (%s independent outlet(s))',
        item.pk, result.headline, result.independent_sources,
    )
    return report.pk


@shared_task
def fetch_source(source_id, inline=False, force=False):
    """Crawl one source's feed and ingest whatever is new.

    The credibility gate is checked *before* any request goes out, not after.
    Letting ingest_item reject the items afterwards would mean a source we
    have already decided not to trust still gets crawled every hour — we
    would be spending someone else's bandwidth to fetch documents we intend
    to throw away.
    """
    try:
        source = Source.objects.get(pk=source_id)
    except ObjectDoesNotExist:
        logger.warning('fetch_source: Source %s no longer exists', source_id)
        return {'error': 'source not found'}

    try:
        check_credibility_gate(source)
    except CredibilityGateRejection as exc:
        logger.info('Not crawling %s: %s', source.domain, exc)
        return {'source': source.domain, 'fetched': 0, 'stored': 0, 'detail': str(exc)}

    try:
        fetched = get_fetcher().fetch(source, force=force)
    except (Blocked, FetchError) as exc:
        logger.info('Fetch failed for %s: %s', source.domain, exc)
        return {'source': source.domain, 'fetched': 0, 'stored': 0, 'detail': str(exc)}

    stored = [_store(source, item, inline) for item in fetched]
    ids = [pk for pk in stored if pk]
    if inline:
        for pk in ids:
            analyze_content.run(pk)

    source.refresh_from_db(fields=['feed_url', 'feed_status'])
    return {
        'source': source.domain,
        'feed_url': source.feed_url,
        'feed_status': source.feed_status,
        'fetched': len(fetched),
        'stored': len(ids) if inline else len(fetched),
        'item_ids': ids,
        'detail': (
            f'{len(fetched)} new item(s) from {source.feed_url}'
            if fetched else _no_items_detail(source)
        ),
    }


def _no_items_detail(source) -> str:
    """Say which kind of nothing happened — the three cases look identical."""
    from .fetchers import FeedStatus

    return {
        FeedStatus.NO_FEED: (
            f'No RSS or Atom feed found for {source.domain}. '
            'Set one on the source if you know its address.'
        ),
        FeedStatus.BLOCKED: f"{source.domain}'s robots.txt disallows the feed.",
        FeedStatus.ERROR: f'{source.domain}: the feed could not be read.',
        FeedStatus.DISABLED: 'Fetching is disabled (CONTENT_FETCHER is NullFetcher).',
    }.get(source.feed_status, f'Nothing new in {source.domain}\'s feed.')


@shared_task
def fetch_url(url, inline=True):
    """Fetch one page by URL, registering its site as a Source if needed.

    This is the "paste a link" path. It goes through exactly the same gate,
    robots check, and limits as a scheduled crawl — the only difference is
    that a person asked for it.
    """
    from core.domains import InvalidDomain, normalize_domain

    try:
        domain = normalize_domain(url)
    except InvalidDomain as exc:
        return {'error': str(exc)}

    # Check the host before creating anything. A URL we are never allowed to
    # fetch should not leave a Source row behind — or trigger an RDAP lookup
    # and a credibility score for a site we will not read.
    try:
        assert_fetchable(url)
    except Blocked as exc:
        return {'error': str(exc), 'blocked': True}
    except FetchError as exc:
        return {'error': str(exc)}

    source, created = Source.objects.get_or_create(
        domain=domain,
        defaults={'url': f'https://{domain}/', 'first_seen': timezone.now()},
    )

    try:
        check_credibility_gate(source)
    except CredibilityGateRejection as exc:
        return {'error': str(exc), 'source': source.pk, 'gate': 'rejected'}

    try:
        fetched = fetch_page(url)
    except Blocked as exc:
        return {'error': str(exc), 'source': source.pk, 'blocked': True}
    except FetchError as exc:
        return {'error': str(exc), 'source': source.pk}

    if not fetched.body.strip():
        return {
            'error': (
                f'Nothing readable at {url}. The page may be rendered entirely '
                'by JavaScript, which this fetcher does not run.'
            ),
            'source': source.pk,
        }

    item_id = _store(source, fetched, inline=True)
    if item_id is None:
        return {'error': 'Rejected by the credibility gate.', 'source': source.pk}

    report_id = None
    if inline:
        analyze_content.run(item_id)
        # Reading a page is someone asking "is this true?". Answering with a
        # stored document and no assessment would be answering a different
        # question, so the cross-reference runs as part of the same action.
        report_id = corroborate_item.run(item_id)

    return {
        'source': source.pk,
        'source_domain': domain,
        'source_created': created,
        'content_item': item_id,
        'corroboration': report_id,
        'detail': f'Read {len(fetched.body)} characters from {fetched.url}.',
    }


@shared_task
def run_scheduled_ingest():
    """Periodic crawl of every active source. Wired to Celery Beat.

    Fans out one task per source rather than crawling them in a loop: a slow
    or unreachable site then delays only itself, and the per-host rate limiter
    is not fighting a single long-running task for the whole fleet.
    """
    sources = list(Source.objects.filter(is_active=True).values_list('pk', flat=True))
    for pk in sources:
        fetch_source.delay(pk)
    logger.info('Scheduled ingest queued %d source(s)', len(sources))
    return len(sources)
