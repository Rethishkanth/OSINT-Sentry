import logging

from celery import shared_task
from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from django.utils import timezone

from core.models import Source
from credibility.models import CredibilityScore
from credibility.tasks import score_source

from .fetchers import get_fetcher
from .models import ContentItem, ContentTopic, Entity, Topic
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


@shared_task
def run_scheduled_ingest():
    """Periodic crawl of every active source. Wired to Celery Beat."""
    fetcher = get_fetcher()
    queued = 0
    for source in Source.objects.filter(is_active=True):
        for fetched in fetcher.fetch(source):
            ingest_item.delay(
                source.pk,
                fetched.url,
                title=fetched.title,
                body=fetched.body,
                author=fetched.author,
                language=fetched.language,
                published_at=fetched.published_at,
            )
            queued += 1
    logger.info('Scheduled ingest queued %d item(s)', queued)
    return queued
