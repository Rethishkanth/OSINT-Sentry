"""The seam where a crawler plugs in.

The plan runs Scrapy spiders via CrawlerProcess. Scrapy is not installed, and
more to the point nothing here should start crawling live websites on its own,
so the default fetcher returns nothing. Implement BaseFetcher against Scrapy
(or any client) and point CONTENT_FETCHER at it when crawling is actually
wanted — the ingest task and its credibility gate stay unchanged.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime

from django.conf import settings
from django.utils.module_loading import import_string


@dataclass
class FetchedItem:
    """One crawled document, before it becomes a ContentItem."""

    url: str
    title: str = ''
    body: str = ''
    author: str = ''
    language: str = ''
    published_at: datetime | None = None


class BaseFetcher(ABC):
    @abstractmethod
    def fetch(self, source) -> list[FetchedItem]:
        """Return newly seen documents for one Source."""


class NullFetcher(BaseFetcher):
    """Default: crawls nothing. Content arrives via ingest_item() instead."""

    def fetch(self, source) -> list[FetchedItem]:
        return []


def get_fetcher() -> BaseFetcher:
    path = getattr(settings, 'CONTENT_FETCHER', 'content.fetchers.NullFetcher')
    return import_string(path)()
