"""Turning whatever a human pasted into a domain a registry will answer for.

Analysts paste what they have: a full article URL, a copied address bar, a
bare hostname, sometimes with tracking junk on the end. All of those name the
same publisher, and the registry only answers for one of them. Normalising
here - rather than in the console - means the API, the admin, and a shell
one-liner all agree on what `bbc.co.uk` is.

Pure string handling, no Django imports, so it can be used from anywhere in
the project without a dependency cycle.
"""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlsplit

# A leading scheme, per RFC 3986. Matched on the raw input because the parse
# below rewrites the string, and by then "mailto:" is indistinguishable from
# userinfo - which is how mailto:a@b.com would otherwise be read as b.com.
_SCHEME_RE = re.compile(r'^([a-zA-Z][a-zA-Z0-9+.\-]*):')

# Public-suffix handling without pulling in the full Public Suffix List. These
# are the multi-label suffixes common enough to matter; anything else falls
# back to the last two labels. A wrong guess here means the registry lookup
# 404s, which is recorded as 'not_found' rather than silently scoring the
# domain as new.
MULTI_LABEL_SUFFIXES = frozenset({
    'co.uk', 'org.uk', 'ac.uk', 'gov.uk', 'me.uk', 'net.uk', 'sch.uk',
    'com.au', 'net.au', 'org.au', 'edu.au', 'gov.au', 'id.au',
    'co.nz', 'net.nz', 'org.nz', 'govt.nz', 'ac.nz',
    'co.in', 'net.in', 'org.in', 'gen.in', 'firm.in', 'ac.in', 'edu.in', 'gov.in',
    'co.za', 'org.za', 'net.za', 'gov.za', 'ac.za',
    'co.jp', 'or.jp', 'ne.jp', 'ac.jp', 'go.jp',
    'com.br', 'net.br', 'org.br', 'gov.br',
    'com.cn', 'net.cn', 'org.cn', 'gov.cn', 'edu.cn',
    'com.mx', 'com.ar', 'com.tr', 'com.sg', 'com.hk', 'com.tw', 'com.my',
    'co.kr', 'or.kr', 'go.kr',
    'com.pl', 'com.ua', 'co.il', 'org.il', 'ac.il',
})

# Schemes that name a host we could plausibly score. Anything else (mailto:,
# file:, javascript:) is rejected outright rather than half-parsed. A missing
# scheme is fine and handled separately -- a bare domain is the common case.
ALLOWED_SCHEMES = frozenset({'http', 'https'})

MAX_DOMAIN_LENGTH = 253
MAX_LABEL_LENGTH = 63
_LABEL_CHARS = set('abcdefghijklmnopqrstuvwxyz0123456789-')


class InvalidDomain(ValueError):
    """Raised when input cannot be reduced to a lookupable domain.

    The message is written to be shown to the person who typed it, so it says
    what was wrong rather than which parsing step failed.
    """


def registrable_domain(hostname: str) -> str:
    """Reduce a hostname to the domain a registry will actually answer for.

    news.bbc.co.uk -> bbc.co.uk, www.example.com -> example.com
    """
    host = (hostname or '').strip().lower().rstrip('.')
    if host.startswith('www.'):
        host = host[4:]
    labels = host.split('.')
    if len(labels) <= 2:
        return host
    if '.'.join(labels[-2:]) in MULTI_LABEL_SUFFIXES:
        return '.'.join(labels[-3:])
    return '.'.join(labels[-2:])


def _host_from(raw: str) -> str:
    """Pull the host out of a URL, a bare hostname, or something in between."""
    text = raw.strip().strip('<>').strip()
    if not text:
        raise InvalidDomain('Enter a website address.')
    if any(ch.isspace() for ch in text):
        raise InvalidDomain('That looks like more than one address - paste a single link.')

    scheme_match = _SCHEME_RE.match(text)
    if scheme_match and scheme_match.group(1).lower() not in ALLOWED_SCHEMES:
        raise InvalidDomain(
            f'Only http and https addresses can be scored, '
            f'not "{scheme_match.group(1)}:".'
        )

    # Without a scheme, urlsplit reads the whole string as a path, so
    # "bbc.co.uk/news" would yield an empty host. Leading "//" forces it to
    # parse as an authority instead.
    candidate = text if scheme_match or text.startswith('//') else '//' + text
    try:
        parts = urlsplit(candidate)
    except ValueError as exc:
        raise InvalidDomain(f'That is not a usable web address ({exc}).') from exc

    try:
        host = parts.hostname or ''
    except ValueError as exc:
        # urlsplit defers port validation to .hostname/.port.
        raise InvalidDomain(f'That is not a usable web address ({exc}).') from exc

    if not host:
        raise InvalidDomain('No website name found in that address.')
    return host


def _reject_non_domains(host: str) -> None:
    """Refuse inputs that are addresses but not registered domain names."""
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        # registrable_domain would happily cut 192.168.1.10 down to "1.10".
        raise InvalidDomain(
            'That is an IP address. Credibility scoring needs a domain name, '
            'because only a domain has a registration record.'
        )

    if '.' not in host:
        raise InvalidDomain(
            f'"{host}" has no domain suffix - did you mean {host}.com?'
        )


def scheme_of(raw: str) -> str:
    """The http/https scheme the input carried, or '' if it carried none.

    Worth keeping: a pasted https:// link is direct evidence that the site
    serves TLS, which the scorer would otherwise have to record as unknown.
    """
    match = _SCHEME_RE.match((raw or '').strip())
    if not match:
        return ''
    scheme = match.group(1).lower()
    return scheme if scheme in ALLOWED_SCHEMES else ''


def normalize_domain(raw: str) -> str:
    """Reduce anything a user might paste to its registrable domain.

    https://www.bbc.co.uk/news/uk-123?utm_source=x  ->  bbc.co.uk
    NEWS.BBC.CO.UK.                                 ->  bbc.co.uk
    münchen.de                                      ->  xn--mnchen-3ya.de

    Raises InvalidDomain, with a message meant for the person who typed it,
    when the input names no domain at all.
    """
    host = _host_from(raw).lower().rstrip('.')

    if any(ord(ch) > 127 for ch in host):
        # Internationalised names have to reach the registry as punycode.
        try:
            host = host.encode('idna').decode('ascii')
        except UnicodeError as exc:
            raise InvalidDomain(f'"{raw.strip()}" is not a valid domain name.') from exc

    _reject_non_domains(host)

    if len(host) > MAX_DOMAIN_LENGTH:
        raise InvalidDomain('That domain name is too long to be real.')

    for label in host.split('.'):
        if not label:
            raise InvalidDomain(f'"{host}" has an empty part in it.')
        if len(label) > MAX_LABEL_LENGTH:
            raise InvalidDomain(f'"{label[:20]}..." is too long to be part of a domain.')
        if set(label) - _LABEL_CHARS:
            bad = ''.join(sorted(set(label) - _LABEL_CHARS))
            raise InvalidDomain(f'"{host}" contains characters a domain cannot have: {bad}')

    domain = registrable_domain(host)
    if not domain or '.' not in domain:
        raise InvalidDomain(f'"{raw.strip()}" does not name a domain we can look up.')
    return domain
