"""Getting documents off the open web.

The plan runs Scrapy spiders. What this system actually needs is narrower and
better behaved: for each tracked source, read the feed the publisher already
maintains, then fetch the handful of articles we have not seen. That is one
request per new article instead of a crawl, and the publisher is the one
deciding what is in scope.

The politeness and safety machinery lives next door and is not optional:
:mod:`content.http` enforces the byte cap, the timeout, per-host rate limiting
and the private-address check, and :mod:`content.robots` is consulted on every
single hop, including redirect targets.

NullFetcher is still here and is still what you switch to in order to make
this system contact nobody — the same role NullRegistryClient plays for
Module 1.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime

from django.conf import settings
from django.utils import timezone
from django.utils.module_loading import import_string

from . import robots
from .extract import extract_article
from .feeds import COMMON_FEED_PATHS, FeedError, looks_like_feed, parse_feed
from .http import Blocked, FetchError, request

logger = logging.getLogger(__name__)

DEFAULT_MAX_ITEMS = 10
# How long to leave a source alone after finding it has no readable feed.
DEFAULT_RETRY_AFTER = 86_400.0


@dataclass
class FetchedItem:
    """One crawled document, before it becomes a ContentItem."""

    url: str
    title: str = ''
    body: str = ''
    author: str = ''
    language: str = ''
    published_at: datetime | None = None


class FeedStatus:
    """Why a source's feed lookup ended the way it did."""

    OK = 'ok'
    NO_FEED = 'no_feed'
    BLOCKED = 'blocked'
    ERROR = 'error'
    DISABLED = 'disabled'


class BaseFetcher(ABC):
    @abstractmethod
    def fetch(self, source, force: bool = False) -> list[FetchedItem]:
        """Return newly seen documents for one Source.

        `force` means a person asked for this right now, so back-off windows
        meant for the hourly schedule should not apply to them.
        """


class NullFetcher(BaseFetcher):
    """Contacts nothing. Set CONTENT_FETCHER to this to run fully offline;
    content then only arrives through the ingest endpoint."""

    def fetch(self, source, force: bool = False) -> list[FetchedItem]:
        _mark_feed(source, FeedStatus.DISABLED, source.feed_url)
        return []


def _mark_feed(source, status: str, feed_url: str = '') -> None:
    """Cache the feed outcome on the Source.

    A narrow UPDATE rather than save(): these are cache fields, exactly like
    the registry columns Module 1 writes, and a full save would re-run domain
    normalisation and fire the scoring signal on every crawl.
    """
    from core.models import Source

    Source.objects.filter(pk=source.pk).update(
        feed_url=feed_url or '',
        feed_status=status,
        feed_checked_at=timezone.now(),
    )
    source.feed_url = feed_url or ''
    source.feed_status = status


def fetch_page(url: str) -> FetchedItem:
    """Fetch and extract one page. Raises Blocked or FetchError.

    This is the path behind "paste a link": no feed, no crawl, one document.
    """
    response = request(url, on_hop=robots.check)
    article = extract_article(response.text(), url=response.url)
    return FetchedItem(
        # The canonical URL is what deduplicates syndicated copies and strips
        # the tracking parameters that would otherwise store one article
        # several times over.
        url=article.canonical_url or response.url,
        title=article.title,
        body=article.body,
        author=article.author,
        language=article.language,
        published_at=article.published_at,
    )


class WebFetcher(BaseFetcher):
    """Feed-first fetcher: discover a feed, read it, fetch what is new."""

    def __init__(self):
        self.max_items = getattr(settings, 'CONTENT_MAX_ITEMS_PER_FETCH', DEFAULT_MAX_ITEMS)
        self.fetch_bodies = getattr(settings, 'CONTENT_FETCH_BODIES', True)
        self.retry_after = getattr(settings, 'CONTENT_FEED_RETRY_AFTER', DEFAULT_RETRY_AFTER)

    # -- feed discovery ----------------------------------------------------

    def _home_url(self, source) -> str:
        return source.url or f'https://{source.domain}/'

    def in_backoff(self, source) -> bool:
        """True if we recently established there is nothing here to read.

        Discovery costs a homepage request plus a probe of every conventional
        feed path. Repeating that hourly against a site that simply has no
        feed — and plenty of large publishers no longer do — is exactly the
        behaviour the rest of this module exists to avoid.
        """
        if source.feed_status not in (FeedStatus.NO_FEED, FeedStatus.BLOCKED):
            return False
        if not source.feed_checked_at:
            return False
        return (timezone.now() - source.feed_checked_at).total_seconds() < self.retry_after

    def discover_feed(self, source) -> str:
        """Find the source's feed: cached, then advertised, then guessed."""
        if source.feed_url:
            return source.feed_url

        home = self._home_url(source)
        try:
            response = request(home, on_hop=robots.check)
        except (Blocked, FetchError) as exc:
            logger.info('Feed discovery could not read %s: %s', home, exc)
            return ''

        advertised = extract_article(response.text(), url=response.url).feeds
        for candidate in advertised:
            if self._is_feed(candidate):
                return candidate

        # Nothing advertised. Publishers very often still have a feed at one
        # of the conventional paths, so try those before giving up — but only
        # a handful, and each one is rate-limited like any other request.
        base = response.url.rstrip('/')
        origin = '/'.join(base.split('/')[:3])
        for path in COMMON_FEED_PATHS:
            if self._is_feed(origin + path):
                return origin + path
        return ''

    @staticmethod
    def _is_feed(url: str) -> bool:
        try:
            response = request(url, accept='application/rss+xml,application/atom+xml,application/xml,text/xml', on_hop=robots.check)
        except (Blocked, FetchError):
            return False
        return looks_like_feed(response.body)

    # -- the crawl ---------------------------------------------------------

    def fetch(self, source, force: bool = False) -> list[FetchedItem]:
        from .models import ContentItem

        if not force and self.in_backoff(source):
            logger.info(
                '%s: skipping, last feed lookup was %s', source.domain, source.feed_status
            )
            return []

        feed_url = self.discover_feed(source)
        if not feed_url:
            _mark_feed(source, FeedStatus.NO_FEED)
            logger.info('%s: no feed found', source.domain)
            return []

        try:
            response = request(feed_url, on_hop=robots.check)
            entries = parse_feed(response.body, base_url=response.url)
        except Blocked as exc:
            _mark_feed(source, FeedStatus.BLOCKED, feed_url)
            logger.info('%s: feed blocked — %s', source.domain, exc)
            return []
        except (FetchError, FeedError) as exc:
            _mark_feed(source, FeedStatus.ERROR, feed_url)
            logger.info('%s: feed unreadable — %s', source.domain, exc)
            return []

        _mark_feed(source, FeedStatus.OK, feed_url)

        # Only what we have not stored. Checking before fetching is the whole
        # point: on an unchanged feed this makes zero article requests.
        known = set(
            ContentItem.objects.filter(
                url__in=[e.url for e in entries]
            ).values_list('url', flat=True)
        )
        fresh = [e for e in entries if e.url not in known][: self.max_items]

        items = [self._to_item(entry) for entry in fresh]
        logger.info(
            '%s: feed had %d entries, %d unseen, %d fetched this run',
            source.domain, len(entries), len(entries) - len(known), len(items),
        )
        return items

    def _to_item(self, entry) -> FetchedItem:
        """One feed entry, with the article body if we can get it."""
        if not self.fetch_bodies:
            return FetchedItem(
                url=entry.url, title=entry.title, body=entry.summary,
                author=entry.author, published_at=entry.published_at,
            )

        try:
            item = fetch_page(entry.url)
        except (Blocked, FetchError) as exc:
            # The feed already gave us a title, a date, and usually a summary.
            # Storing that beats discarding the entry because one page 403'd.
            logger.info('Falling back to feed summary for %s: %s', entry.url, exc)
            return FetchedItem(
                url=entry.url, title=entry.title, body=entry.summary,
                author=entry.author, published_at=entry.published_at,
            )

        # Feed metadata wins where the page left a field empty: publishers are
        # more careful with their feed than with their meta tags.
        return FetchedItem(
            url=item.url or entry.url,
            title=item.title or entry.title,
            body=item.body or entry.summary,
            author=item.author or entry.author,
            language=item.language,
            published_at=item.published_at or entry.published_at,
        )


def get_fetcher() -> BaseFetcher:
    path = getattr(settings, 'CONTENT_FETCHER', 'content.fetchers.WebFetcher')
    return import_string(path)()
