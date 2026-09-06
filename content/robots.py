"""robots.txt, honoured properly.

A crawler that ignores robots.txt is not an OSINT tool, it is an incident. The
rules here follow RFC 9309, including the part people usually skip: what to do
when robots.txt itself cannot be read.

    2xx          parse it and obey it
    4xx          no file exists, so nothing is disallowed
    5xx, timeout stay out entirely

That last line is the one that matters. A site returning 500 for
/robots.txt has told us nothing about what we may read, and "we could not ask
permission" is not the same as "permission granted". Failing open there would
mean a site is crawled hardest exactly while it is struggling.

Results are cached per host with a TTL, so a crawl of fifty articles asks once.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

from django.conf import settings

from .http import Blocked, FetchError, request, user_agent

logger = logging.getLogger(__name__)

DEFAULT_CACHE_TTL = 3600.0
DEFAULT_ROBOTS_MAX_BYTES = 512_000   # RFC 9309 asks parsers to read at least 500 KiB
DEFAULT_ROBOTS_TIMEOUT = 10.0


@dataclass
class RobotsPolicy:
    """What one host's robots.txt permits."""

    host: str
    parser: RobotFileParser
    crawl_delay: float | None
    reason: str            # how the file was obtained, for the audit trail
    fetched_at: float

    def allows(self, url: str) -> bool:
        return self.parser.can_fetch(user_agent(), url)


_cache: dict[str, RobotsPolicy] = {}
_lock = threading.Lock()


def _setting(name, default):
    return getattr(settings, name, default)


def _robots_url(url: str) -> tuple[str, str]:
    """(cache key, robots.txt URL) for the origin `url` belongs to."""
    parts = urlsplit(url)
    origin = (parts.scheme, parts.netloc, '/robots.txt', '', '')
    return f'{parts.scheme}://{parts.netloc}', urlunsplit(origin)


def _load(robots_url: str) -> tuple[RobotFileParser, float | None, str]:
    parser = RobotFileParser()
    parser.set_url(robots_url)

    try:
        # No on_hop here: checking robots.txt against robots.txt does not
        # terminate. Rate limiting still applies, so this counts as a hit.
        response = request(
            robots_url,
            accept='text/plain,*/*;q=0.5',
            max_bytes=_setting('CONTENT_ROBOTS_MAX_BYTES', DEFAULT_ROBOTS_MAX_BYTES),
            timeout=_setting('CONTENT_ROBOTS_TIMEOUT', DEFAULT_ROBOTS_TIMEOUT),
            allow_types=None,
        )
    except Blocked as exc:
        # Policy said no before we ever got to the site's own opinion.
        parser.disallow_all = True
        return parser, None, f'blocked: {exc}'
    except FetchError as exc:
        message = str(exc)
        if 'HTTP 4' in message:
            # No robots.txt at all. Everything is permitted, which is the one
            # case where failing open is the specified behaviour.
            parser.allow_all = True
            return parser, None, 'no robots.txt (4xx)'
        parser.disallow_all = True
        return parser, None, f'unreadable ({message}) — treating as disallow-all'

    parser.parse(response.text().splitlines())
    delay = None
    try:
        delay = parser.crawl_delay(user_agent())
    except Exception:      # pragma: no cover - parser is lenient but not total
        delay = None
    return parser, (float(delay) if delay else None), 'robots.txt read'


def policy_for(url: str, *, refresh: bool = False) -> RobotsPolicy:
    """The cached robots policy for whichever origin `url` belongs to."""
    key, robots_url = _robots_url(url)
    ttl = _setting('CONTENT_ROBOTS_CACHE_TTL', DEFAULT_CACHE_TTL)

    with _lock:
        cached = _cache.get(key)
        if cached is not None and not refresh and (time.monotonic() - cached.fetched_at) < ttl:
            return cached

    parser, delay, reason = _load(robots_url)
    policy = RobotsPolicy(
        host=key,
        parser=parser,
        crawl_delay=delay,
        reason=reason,
        fetched_at=time.monotonic(),
    )
    logger.debug('robots for %s: %s', key, reason)
    with _lock:
        _cache[key] = policy
    return policy


def check(url: str) -> RobotsPolicy:
    """Raise Blocked unless `url` may be fetched. Returns the policy."""
    policy = policy_for(url)
    if not policy.allows(url):
        raise Blocked(f'{url} is disallowed by robots.txt ({policy.reason}).')
    return policy


def clear_cache() -> None:
    """Forget every cached robots.txt. For tests, and for the admin."""
    with _lock:
        _cache.clear()
