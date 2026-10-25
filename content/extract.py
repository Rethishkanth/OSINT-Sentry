"""Pulling an article out of a web page, with the standard library.

The plan calls for Scrapy spiders. Scrapy is a crawling framework, and what is
actually needed here is one page turned into one document - html.parser does
that without adding a dependency, and the result is inspectable.

This is readability-lite, not readability. It strips the furniture (script,
style, nav, header, footer, aside), prefers text inside <article>, falls back
to paragraphs, and falls back again to the whole body. It will do a poor job
on a page that is mostly JavaScript, and there is no pretending otherwise: the
extracted body is stored verbatim so an analyst can see what was actually read
rather than trusting a summary of a bad extraction.

Metadata is taken from the places publishers actually maintain - OpenGraph,
JSON-LD, and the article:* meta tags - before falling back to <title>.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone as dt_timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import urljoin

logger = logging.getLogger(__name__)

# Content inside these never belongs to the article.
SKIP_TAGS = frozenset({
    'script', 'style', 'noscript', 'svg', 'canvas', 'template', 'iframe',
    'form', 'nav', 'header', 'footer', 'aside', 'button', 'select', 'video',
    'audio', 'figure', 'picture',
})

# Tags whose boundaries are line breaks in the extracted text.
BLOCK_TAGS = frozenset({
    'p', 'div', 'br', 'li', 'tr', 'blockquote', 'section', 'article',
    'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'pre', 'td', 'dd', 'dt', 'main',
})

FEED_TYPES = frozenset({
    'application/rss+xml', 'application/atom+xml', 'application/feed+json',
    'application/json', 'text/xml', 'application/xml',
})

# Below this, a candidate extraction is treated as having found nothing and we
# fall back to a broader one. Roughly a short paragraph.
MIN_BODY_CHARS = 200

MAX_JSONLD_BLOCKS = 5
MAX_JSONLD_CHARS = 200_000

_WS_RE = re.compile(r'[ \t\r\f\v]+')
_BLANKS_RE = re.compile(r'\n{3,}')


def parse_datetime(value) -> datetime | None:
    """Parse the date formats feeds and pages actually use.

    ISO 8601 for HTML metadata and Atom, RFC 2822 for RSS. Anything naive is
    read as UTC: a wrong timezone is far less damaging here than discarding a
    publication date, which is a signal the whole pipeline uses.
    """
    if isinstance(value, datetime):
        parsed = value
    elif not value or not isinstance(value, str):
        return None
    else:
        text = value.strip()
        parsed = None
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            pass
        if parsed is None:
            try:
                parsed = parsedate_to_datetime(text)
            except (TypeError, ValueError):
                parsed = None
        if parsed is None:
            for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d', '%d %B %Y', '%B %d, %Y'):
                try:
                    parsed = datetime.strptime(text, fmt)
                    break
                except ValueError:
                    continue
        if parsed is None:
            return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt_timezone.utc)
    return parsed


def _clean(text: str) -> str:
    text = _WS_RE.sub(' ', text)
    lines = [line.strip() for line in text.split('\n')]
    return _BLANKS_RE.sub('\n\n', '\n'.join(lines)).strip()


class _Stripper(HTMLParser):
    """Tags out, text in. Feed titles and summaries routinely carry markup."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in SKIP_TAGS:
            self._skip += 1
        elif tag in BLOCK_TAGS:
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
        elif tag in BLOCK_TAGS:
            self.parts.append('\n')

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def strip_html(fragment: str) -> str:
    """Plain text from a fragment that may or may not contain markup."""
    if not fragment:
        return ''
    stripper = _Stripper()
    try:
        stripper.feed(fragment)
        stripper.close()
    except Exception:
        logger.debug('Fragment parse ended early', exc_info=True)
    return _clean(''.join(stripper.parts))


@dataclass
class Article:
    """One page, reduced to the fields a ContentItem needs."""

    url: str = ''
    title: str = ''
    body: str = ''
    author: str = ''
    language: str = ''
    description: str = ''
    published_at: datetime | None = None
    feeds: list[str] = field(default_factory=list)
    canonical_url: str = ''


class _PageParser(HTMLParser):
    """Single pass over the document, collecting text and metadata."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.title = ''
        self.language = ''
        self.feeds: list[str] = []
        self.canonical = ''
        self.jsonld: list[str] = []

        self._skip_depth = 0
        self._article_depth = 0
        self._p_depth = 0
        self._in_title = False
        self._in_jsonld = False

        self._body: list[str] = []
        self._paras: list[str] = []
        self._article: list[str] = []

    # -- text collection ---------------------------------------------------

    def _emit(self, text: str) -> None:
        self._body.append(text)
        if self._p_depth:
            self._paras.append(text)
        if self._article_depth:
            self._article.append(text)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)

        if tag == 'script' and (attrs.get('type') or '').lower() == 'application/ld+json':
            self._in_jsonld = True
            self._skip_depth += 1
            return

        if tag in SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return

        if tag in BLOCK_TAGS:
            self._emit('\n')
        if tag == 'article':
            self._article_depth += 1
        elif tag == 'p':
            self._p_depth += 1
        elif tag == 'title':
            self._in_title = True
        elif tag == 'html':
            self.language = (attrs.get('lang') or '').strip()[:8]
        elif tag == 'meta':
            key = (attrs.get('property') or attrs.get('name') or '').strip().lower()
            content = (attrs.get('content') or '').strip()
            if key and content and key not in self.meta:
                self.meta[key] = content
        elif tag == 'link':
            rel = (attrs.get('rel') or '').strip().lower()
            href = (attrs.get('href') or '').strip()
            link_type = (attrs.get('type') or '').strip().lower()
            if not href:
                return
            if 'alternate' in rel and link_type in FEED_TYPES:
                self.feeds.append(href)
            elif 'canonical' in rel:
                self.canonical = href
        elif tag == 'time' and attrs.get('datetime'):
            self.meta.setdefault('__time', attrs['datetime'].strip())

    def handle_endtag(self, tag):
        if self._in_jsonld and tag == 'script':
            self._in_jsonld = False
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag in SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth:
            return

        if tag == 'article':
            self._article_depth = max(0, self._article_depth - 1)
        elif tag == 'p':
            self._p_depth = max(0, self._p_depth - 1)
        elif tag == 'title':
            self._in_title = False
        if tag in BLOCK_TAGS:
            self._emit('\n')

    def handle_data(self, data):
        if self._in_jsonld:
            if len(self.jsonld) < MAX_JSONLD_BLOCKS and len(data) < MAX_JSONLD_CHARS:
                self.jsonld.append(data)
            return
        if self._skip_depth:
            return
        if self._in_title:
            self.title += data
            return
        if data.strip():
            self._emit(data)

    # -- results -----------------------------------------------------------

    def best_body(self) -> str:
        """The narrowest extraction that actually found something.

        Ordering matters: <article> beats paragraphs beats the whole page.
        Each fallback widens what is included, and each one is more likely to
        drag in navigation text, so the widest is tried last.
        """
        for chunks in (self._article, self._paras, self._body):
            text = _clean(''.join(chunks))
            if len(text) >= MIN_BODY_CHARS:
                return text
        # Nothing cleared the bar - return whatever the page had rather than
        # an empty body, so the analyst sees the page was thin, not missing.
        return _clean(''.join(self._body))


def _walk_jsonld(node, found: dict) -> None:
    """Depth-first scan of JSON-LD for the three fields worth having."""
    if isinstance(node, list):
        for item in node:
            _walk_jsonld(item, found)
        return
    if not isinstance(node, dict):
        return

    for key, target in (('headline', 'title'), ('datePublished', 'published'),
                        ('dateCreated', 'published')):
        value = node.get(key)
        if isinstance(value, str) and value.strip() and target not in found:
            found[target] = value.strip()

    author = node.get('author')
    if 'author' not in found and author:
        if isinstance(author, dict) and isinstance(author.get('name'), str):
            found['author'] = author['name']
        elif isinstance(author, str):
            found['author'] = author
        elif isinstance(author, list) and author:
            first = author[0]
            if isinstance(first, dict) and isinstance(first.get('name'), str):
                found['author'] = first['name']
            elif isinstance(first, str):
                found['author'] = first

    for value in node.values():
        if isinstance(value, (dict, list)):
            _walk_jsonld(value, found)


def extract_article(html: str, url: str = '') -> Article:
    """Turn a page's HTML into an Article. Never raises on malformed input."""
    parser = _PageParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        # html.parser is lenient, but a truncated document can still trip it.
        # Whatever was parsed before the error is still worth keeping.
        logger.debug('HTML parse ended early for %s', url, exc_info=True)

    meta = parser.meta
    jsonld: dict[str, str] = {}
    for block in parser.jsonld:
        try:
            _walk_jsonld(json.loads(block), jsonld)
        except (ValueError, TypeError):
            continue

    title = (
        meta.get('og:title')
        or meta.get('twitter:title')
        or jsonld.get('title')
        or parser.title.strip()
    )
    author = (
        meta.get('article:author')
        or meta.get('author')
        or meta.get('dc.creator')
        or jsonld.get('author')
        or ''
    )
    # An article:author that is a profile URL is not a name; keep it out of a
    # field that is displayed as one.
    if author.startswith(('http://', 'https://')):
        author = ''

    published = parse_datetime(
        meta.get('article:published_time')
        or meta.get('datepublished')
        or jsonld.get('published')
        or meta.get('date')
        or meta.get('__time')
    )

    language = parser.language or meta.get('og:locale', '')[:8]

    return Article(
        url=url,
        title=_clean(title)[:500],
        body=parser.best_body(),
        author=_clean(author)[:255],
        language=language.replace('_', '-')[:8],
        description=_clean(meta.get('og:description') or meta.get('description') or ''),
        published_at=published,
        feeds=[urljoin(url, href) for href in parser.feeds] if url else list(parser.feeds),
        canonical_url=urljoin(url, parser.canonical) if (url and parser.canonical) else parser.canonical,
    )
