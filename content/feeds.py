"""RSS and Atom parsing.

Feeds are how a publisher says "here is what I published, come and get it".
Reading them instead of crawling link graphs is both kinder to the site and
better data: the entries are already deduplicated, dated, and attributed.

Parsed with xml.etree, with one guard bolted on. ElementTree is documented as
vulnerable to entity-expansion attacks ("billion laughs"), and this parses
XML from arbitrary websites. Every one of those attacks needs a document type
declaration to define the entities, so a document containing <!DOCTYPE or
<!ENTITY is refused before the parser ever sees it. Real feeds do not have
one, so nothing legitimate is lost. That is a smaller and more checkable
mitigation than adding defusedxml as a dependency.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urljoin
from xml.etree import ElementTree

from .extract import parse_datetime, strip_html

logger = logging.getLogger(__name__)

_DOCTYPE_RE = re.compile(rb'<!\s*(?:DOCTYPE|ENTITY)', re.IGNORECASE)
_FEED_ROOT_RE = re.compile(rb'<\s*(?:\w+:)?(rss|feed|rdf)\b', re.IGNORECASE)

# Common paths, tried only when a page advertises no feed of its own.
COMMON_FEED_PATHS = (
    '/feed', '/feed/', '/rss', '/rss.xml', '/feed.xml', '/atom.xml',
    '/index.xml', '/feeds/all.atom.xml', '/blog/feed',
)


class FeedError(ValueError):
    """The document is not a feed we can read."""


@dataclass
class FeedEntry:
    url: str
    title: str = ''
    summary: str = ''
    author: str = ''
    published_at: datetime | None = None


def looks_like_feed(body: bytes) -> bool:
    """Cheap check on the first bytes, so HTML is not handed to the XML parser."""
    head = body[:1024].lstrip()
    return bool(_FEED_ROOT_RE.search(head))


def _localname(tag: str) -> str:
    return tag.rsplit('}', 1)[-1].lower()


def _find(element, *names):
    """First direct child matching any of `names`, ignoring XML namespaces."""
    wanted = {n.lower() for n in names}
    for child in element:
        if _localname(child.tag) in wanted:
            return child
    return None


def _text(element, *names) -> str:
    child = _find(element, *names)
    if child is None:
        return ''
    return (child.text or '').strip()


def _entry_link(entry) -> str:
    """The entry's URL, from whichever of the three conventions it uses."""
    # Atom: <link rel="alternate" href="...">, possibly several.
    fallback = ''
    for child in entry:
        if _localname(child.tag) != 'link':
            continue
        href = (child.get('href') or '').strip()
        if href:
            rel = (child.get('rel') or 'alternate').lower()
            if rel == 'alternate':
                return href
            fallback = fallback or href
        elif (child.text or '').strip():
            # RSS: the URL is the element's text.
            return child.text.strip()
    if fallback:
        return fallback
    # RSS 1.0 falls back to rdf:about on the item itself.
    for key, value in entry.attrib.items():
        if _localname(key) == 'about':
            return value.strip()
    return ''


def _entry_author(entry) -> str:
    author = _find(entry, 'author')
    if author is not None:
        name = _text(author, 'name')
        if name:
            return name
        if (author.text or '').strip():
            return author.text.strip()
    # dc:creator, whose local name is just "creator" once the namespace is off.
    return _text(entry, 'creator')


def parse_feed(body: bytes, base_url: str = '') -> list[FeedEntry]:
    """Parse RSS 2.0, RSS 1.0/RDF, or Atom into entries.

    Raises FeedError when the document is not a feed or is refused.
    """
    if _DOCTYPE_RE.search(body[:8192]):
        raise FeedError(
            'Feed declares a DOCTYPE. Refused, because entity expansion in '
            'XML from an untrusted host is a denial-of-service vector.'
        )
    if not looks_like_feed(body):
        raise FeedError('Not an RSS or Atom document.')

    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError as exc:
        raise FeedError(f'Malformed feed XML ({exc}).') from exc

    root_name = _localname(root.tag)
    if root_name == 'rss':
        channel = _find(root, 'channel')
        items = [c for c in (channel if channel is not None else root)
                 if _localname(c.tag) == 'item']
    elif root_name == 'feed':
        items = [c for c in root if _localname(c.tag) == 'entry']
    elif root_name == 'rdf':
        items = [c for c in root if _localname(c.tag) == 'item']
    else:
        raise FeedError(f'Unrecognised feed root <{root_name}>.')

    entries = []
    for item in items:
        url = _entry_link(item)
        if not url:
            continue
        if base_url and not url.startswith(('http://', 'https://')):
            url = urljoin(base_url, url)

        summary = _text(item, 'description', 'summary', 'encoded', 'content')
        entries.append(FeedEntry(
            url=url,
            title=strip_html(_text(item, 'title'))[:500],
            summary=strip_html(summary),
            author=strip_html(_entry_author(item))[:255],
            published_at=parse_datetime(
                _text(item, 'pubdate', 'published', 'date', 'updated')
            ),
        ))
    return entries
