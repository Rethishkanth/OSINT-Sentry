"""Outbound HTTP for the content fetcher.

This is the first part of the system that contacts servers we do not own, on
addresses a user typed in. That makes it the place where politeness and
safety have to be enforced, so all of it lives here rather than being spread
across the callers:

* an identifying User-Agent with a contact address, so an operator who does
  not want us can say so and knows who to tell;
* a per-host minimum interval between requests;
* a hard cap on how many bytes are read, applied to the stream rather than
  trusting Content-Length;
* a content-type allowlist, so we do not download a 4GB video by accident;
* redirects followed manually, one hop at a time, with every hop re-checked
  against the same rules as the original request.

The DNS check exists because a domain is attacker-controlled input. Without
it, submitting a hostname that resolves to 127.0.0.1 or 169.254.169.254 turns
this fetcher into a proxy into the machine it runs on.

Standard library only. Nothing here needs requests or Scrapy.
"""

from __future__ import annotations

import gzip
import ipaddress
import logging
import re
import socket
import threading
import time
import urllib.error
import urllib.request
import zlib
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

from django.conf import settings

logger = logging.getLogger(__name__)

DEFAULT_USER_AGENT = (
    'OSINT-Sentry/1.0 (+content fetcher; contact: admin@localhost)'
)
DEFAULT_TIMEOUT = 15.0
DEFAULT_MAX_BYTES = 2_000_000
DEFAULT_CRAWL_DELAY = 2.0
DEFAULT_MAX_REDIRECTS = 5

ALLOWED_SCHEMES = frozenset({'http', 'https'})

# Types worth parsing. Anything else is refused before the body is read: this
# fetcher reads articles and feeds, and has no business pulling down images,
# archives, or video.
ALLOWED_CONTENT_TYPES = (
    'text/html',
    'application/xhtml+xml',
    'application/xml',
    'text/xml',
    'application/rss+xml',
    'application/atom+xml',
    'application/json',
    'text/plain',
)

_CHARSET_RE = re.compile(rb'charset=["\']?([a-zA-Z0-9_\-]+)')


class FetchError(Exception):
    """A request could not be completed. The message is shown to the analyst."""


class Blocked(FetchError):
    """Refused by our own policy - robots.txt, scheme, host, or content type.

    Separate from FetchError because it is not a failure: the system worked
    and the answer was no. Callers record it rather than retrying it.
    """


def _setting(name, default):
    return getattr(settings, name, default)


def user_agent() -> str:
    return _setting('CONTENT_USER_AGENT', DEFAULT_USER_AGENT)


@dataclass(frozen=True)
class FetchResponse:
    """One retrieved document."""

    url: str              # final URL, after any redirects
    status: int
    content_type: str
    body: bytes
    truncated: bool
    # Response headers, names lowercased. Kept because what a server declares
    # about itself - HSTS, CSP, Set-Cookie - is evidence in its own right,
    # and credibility.inspection reads it.
    headers: dict = field(default_factory=dict)
    cookies: tuple = ()
    # Every URL in the redirect chain, first to last.
    hops: tuple = ()

    def header(self, name: str) -> str:
        return self.headers.get(name.lower(), '')

    def text(self) -> str:
        """Decode the body, preferring the charset the server declared."""
        charset = ''
        if 'charset=' in self.content_type:
            charset = self.content_type.split('charset=', 1)[1].split(';')[0].strip()
        if not charset:
            # Fall back to a <meta charset> / XML declaration in the first
            # kilobytes, which is where both are required to appear.
            match = _CHARSET_RE.search(self.body[:4096])
            if match:
                charset = match.group(1).decode('ascii', 'ignore')
        for candidate in (charset, 'utf-8', 'cp1252'):
            if not candidate:
                continue
            try:
                return self.body.decode(candidate)
            except (LookupError, UnicodeDecodeError):
                continue
        # Last resort: never let an encoding problem lose the document.
        return self.body.decode('utf-8', errors='replace')


# --------------------------------------------------------------------------
# Host safety
# --------------------------------------------------------------------------

def _is_public(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return not (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local      # 169.254.0.0/16 - cloud metadata lives here
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
    )


def assert_fetchable(url: str) -> str:
    """Check a URL against scheme and host policy. Returns the host.

    Raises Blocked with a reason meant for a person, not a log line.
    """
    parts = urlsplit(url)
    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise Blocked(f'{parts.scheme or "that"} URLs are not fetched, only http and https.')

    try:
        host = parts.hostname
    except ValueError as exc:
        raise Blocked(f'Unusable address ({exc}).') from exc
    if not host:
        raise Blocked('That address names no host.')

    if _setting('CONTENT_ALLOW_PRIVATE_HOSTS', False):
        return host

    try:
        infos = socket.getaddrinfo(host, parts.port or (443 if parts.scheme == 'https' else 80))
    except socket.gaierror as exc:
        raise FetchError(f'{host} does not resolve ({exc.strerror or exc}).') from exc

    addresses = {info[4][0] for info in infos}
    private = [addr for addr in addresses if not _is_public(addr)]
    if private:
        # Deliberately blunt: a public news site never resolves here, so this
        # is either a misconfiguration or someone probing the host we run on.
        raise Blocked(
            f'{host} resolves to a private or loopback address ({private[0]}). '
            'Fetching it would let a submitted link reach this machine\'s own network.'
        )
    return host


# --------------------------------------------------------------------------
# Per-host rate limiting
# --------------------------------------------------------------------------

# Process-local. That is correct for the single solo-pool worker this project
# runs, and deliberately not correct for a fleet: several workers would each
# keep their own clock and collectively exceed the delay. Moving to more than
# one worker means moving this to the broker, and the docstring says so rather
# than letting someone discover it by getting blocked.
_last_request: dict[str, float] = {}
_rate_lock = threading.Lock()


def wait_for_turn(host: str, delay: float | None = None) -> None:
    """Sleep until enough time has passed since the last request to `host`."""
    interval = delay if delay is not None else _setting('CONTENT_CRAWL_DELAY', DEFAULT_CRAWL_DELAY)
    if interval <= 0:
        return
    with _rate_lock:
        previous = _last_request.get(host)
        now = time.monotonic()
        if previous is not None:
            remaining = interval - (now - previous)
            if remaining > 0:
                time.sleep(remaining)
                now = time.monotonic()
        _last_request[host] = now


def reset_rate_limiter() -> None:
    """Clear the per-host clocks. For tests."""
    with _rate_lock:
        _last_request.clear()


# --------------------------------------------------------------------------
# The request itself
# --------------------------------------------------------------------------

class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """Stops urllib following redirects so we can vet each hop ourselves.

    Returning None makes urllib raise the 3xx as an HTTPError, which the
    caller below turns into one explicit, checked hop.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_opener = urllib.request.build_opener(_NoRedirects)


def _read_capped(response, max_bytes: int) -> tuple[bytes, bool]:
    """Read at most max_bytes+1 so truncation is detectable.

    Content-Length is a claim by the server, so the cap is applied to the
    stream. Reading one extra byte is what tells us the document was cut off
    rather than merely happening to end at the limit.
    """
    body = response.read(max_bytes + 1)
    if len(body) > max_bytes:
        return body[:max_bytes], True
    return body, False


def _decompress(body: bytes, encoding: str) -> bytes:
    """Undo Content-Encoding, if a server applied one we did not ask for."""
    encoding = (encoding or '').lower().strip()
    try:
        if encoding == 'gzip':
            return gzip.decompress(body)
        if encoding in ('deflate', 'zlib'):
            return zlib.decompress(body)
    except (OSError, zlib.error) as exc:
        raise FetchError(f'Response was {encoding}-encoded but would not decompress ({exc}).')
    return body


def request(url: str, *, accept: str = 'text/html,application/xhtml+xml,*/*;q=0.5',
            timeout: float | None = None, max_bytes: int | None = None,
            allow_types: tuple[str, ...] | None = ALLOWED_CONTENT_TYPES,
            rate_limit: bool = True, crawl_delay: float | None = None,
            on_hop=None) -> FetchResponse:
    """GET a URL, following redirects manually and enforcing every limit.

    `on_hop(url)` is called before each hop, including the first. The fetcher
    passes its robots check here so a redirect cannot walk us into a path the
    site told us to stay out of.
    """
    timeout = timeout if timeout is not None else _setting('CONTENT_FETCH_TIMEOUT', DEFAULT_TIMEOUT)
    max_bytes = max_bytes if max_bytes is not None else _setting('CONTENT_MAX_BYTES', DEFAULT_MAX_BYTES)
    max_redirects = _setting('CONTENT_MAX_REDIRECTS', DEFAULT_MAX_REDIRECTS)

    current = url
    seen = set()
    hops: list[str] = []

    for hop in range(max_redirects + 1):
        hops.append(current)
        if current in seen:
            raise FetchError('Redirect loop.')
        seen.add(current)

        host = assert_fetchable(current)
        if on_hop is not None:
            on_hop(current)
        if rate_limit:
            wait_for_turn(host, crawl_delay)

        req = urllib.request.Request(current, headers={
            'User-Agent': user_agent(),
            'Accept': accept,
            # No Accept-Encoding: an uncompressed body means the byte cap is
            # the real limit, with no decompression bomb behind it.
            'Accept-Encoding': 'identity',
        })

        try:
            with _opener.open(req, timeout=timeout) as response:
                content_type = response.headers.get('Content-Type', '')
                base_type = content_type.split(';')[0].strip().lower()
                if allow_types and base_type and base_type not in allow_types:
                    raise Blocked(
                        f'{current} is {base_type}; this fetcher only reads '
                        'pages and feeds.'
                    )
                body, truncated = _read_capped(response, max_bytes)
                body = _decompress(body, response.headers.get('Content-Encoding', ''))
                if truncated:
                    logger.info('Truncated %s at %d bytes', current, max_bytes)
                return FetchResponse(
                    url=response.url or current,
                    status=response.status,
                    content_type=content_type,
                    body=body,
                    truncated=truncated,
                    headers={k.lower(): v for k, v in response.headers.items()},
                    cookies=tuple(response.headers.get_all('Set-Cookie') or ()),
                    hops=tuple(hops),
                )
        except urllib.error.HTTPError as exc:
            if exc.code in (301, 302, 303, 307, 308) and hop < max_redirects:
                location = exc.headers.get('Location')
                exc.close()
                if not location:
                    raise FetchError(f'HTTP {exc.code} with no Location header.')
                current = urljoin(current, location)
                continue
            detail = f'HTTP {exc.code}'
            exc.close()
            raise FetchError(f'{current}: {detail}.')
        except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as exc:
            reason = getattr(exc, 'reason', exc)
            raise FetchError(f'{current}: {reason}.') from exc

    raise FetchError(f'More than {max_redirects} redirects.')
