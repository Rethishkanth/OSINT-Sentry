"""Running the geocoder over stored content and keeping the pins in step."""

from __future__ import annotations

import logging

from celery import shared_task
from django.db import transaction

from content.models import ContentItem

from .geocode import get_geocoder
from .models import ExtractionRun, GeoEvent

logger = logging.getLogger(__name__)


def locate_content_item(item: ContentItem) -> ExtractionRun:
    """Geocode one article and reconcile its pins with what was found.

    Reconcile, not append. Re-running after a gazetteer change must leave the
    item's pins as the current engine sees it, so a place that no longer
    resolves loses its pin instead of lingering as a fact nothing supports.
    Pins an analyst placed by hand are left alone - those are not this
    function's to withdraw.
    """
    geocoder = get_geocoder()
    # The headline carries the dateline as often as the body does, and it is
    # the part most likely to name the place at all.
    text = '\n'.join(part for part in (item.title, item.summary, item.body) if part)
    result = geocoder.locate(text)

    with transaction.atomic():
        kept = []
        for place in result.places:
            event, _created = GeoEvent.objects.update_or_create(
                content_item=item,
                place_name=place.name,
                defaults={
                    'latitude': place.latitude,
                    'longitude': place.longitude,
                    'country': place.country,
                    'region': place.region,
                    'confidence': place.confidence,
                    'method': place.method,
                    'matched_text': place.matched_text[:255],
                    'mentions': place.mentions,
                    'reasons': place.reasons,
                    'engine_version': result.engine_version,
                    # The article's publication time is the only timestamp
                    # available. It is when the event was *reported*, which is
                    # not when it happened - an analyst correcting this by hand
                    # is expected, and the field stays editable for that.
                    'occurred_at': item.published_at,
                },
            )
            kept.append(event.pk)

        stale = (
            GeoEvent.objects
            .filter(content_item=item)
            .exclude(pk__in=kept)
            .exclude(method=GeoEvent.Method.MANUAL)
        )
        removed = stale.count()
        stale.delete()

        run = ExtractionRun.objects.create(
            content_item=item,
            events_found=len(kept),
            notes=list(result.notes),
            engine_version=result.engine_version,
        )

    if removed:
        logger.info('reportgis: dropped %s stale pin(s) for %s', removed, item.pk)
    return run


@shared_task
def extract_locations(content_item_id):
    """Geocode one stored article."""
    try:
        item = ContentItem.objects.get(pk=content_item_id)
    except ContentItem.DoesNotExist:
        logger.warning('reportgis: content item %s is gone', content_item_id)
        return {'content_item': content_item_id, 'located': 0, 'detail': 'not found'}

    run = locate_content_item(item)
    return {
        'content_item': item.pk,
        'located': run.events_found,
        'not_placed': len(run.notes),
        'engine': run.engine_version,
    }


@shared_task
def extract_pending(limit=50):
    """Geocode analysed articles that have never been through this module.

    Bounded by `limit` on purpose: this is a sweep an analyst triggers, not a
    crawl, and an unbounded pass over a large corpus is the kind of job that
    should be scheduled rather than fired from a button.
    """
    pending = (
        ContentItem.objects
        .filter(status=ContentItem.Status.ANALYZED, geo_extractions__isnull=True)
        .order_by('-published_at', '-created_at')[:limit]
    )
    located = 0
    processed = 0
    for item in list(pending):
        run = locate_content_item(item)
        located += run.events_found
        processed += 1
    return {'processed': processed, 'located': located}
