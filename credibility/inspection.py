"""Looking inside a website, not just checking that it answers.

Module 1's credibility score reads *registration metadata* — how old the
domain is, who registered it. That says nothing about what the site actually
serves. This does the other half: it fetches the homepage, reads the
certificate, the response headers, and the markup, and rates out of 10 how
safe and how cluttered the site is.

What this is and is not
-----------------------
It is a **hygiene and transparency inspection**. It can tell you that a site
serves plaintext HTTP, that its certificate expired last week, that it loads
eleven advertising networks and three session recorders, that it runs a
cryptocurrency miner, or that its login form posts your password unencrypted.
Those are facts about what the site chose to serve you.

It is **not a malware scanner**. It does not detect exploit kits, phishing
content, or a compromised page, and a clean 9/10 is not a guarantee of
safety. It also **does not execute JavaScript**, so a site that injects its
advertising at runtime through a tag manager will be undercounted — which is
why the presence of a tag manager is itself reported.

Unreachable is not unsafe. A site that will not answer gets no rating at all,
for the same reason a failed registry lookup never condemns a domain.
"""

from __future__ import annotations

import logging
import re
import socket
import ssl
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone as dt_timezone
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from django.conf import settings

from core.domains import registrable_domain

from .trackers import INLINE_SIGNATURES, classify

logger = logging.getLogger(__name__)

ENGINE_VERSION = 'inspection-v1'

# Headers a site sets to protect its own visitors. Absence is not an attack,
# but a site that sets none of them is not paying attention.
SECURITY_HEADERS = (
    ('strict-transport-security', 'HSTS', 0.4),
    ('content-security-policy', 'Content-Security-Policy', 0.4),
    ('x-content-type-options', 'X-Content-Type-Options', 0.2),
    ('x-frame-options', 'clickjacking protection', 0.3),
    ('referrer-policy', 'Referrer-Policy', 0.2),
)

# Tokens in an element's id or class that mark an interruption rather than
# content.
#
# Matched as whole tokens on container elements only, and deliberately
# narrow. A first pass included "modal" and "overlay" as substrings and
# reported 109 pop-ups on the Guardian's homepage — those words appear
# throughout ordinary utility CSS, and a count that absurd discredits every
# other number beside it. Bare "modal" and "overlay" are gone; the compounds
# that survive only appear on something that actually interrupts you.
#
# Cookie banners are excluded on purpose: they are a legal requirement, not a
# dark pattern.
INTERRUPTION_TOKENS = frozenset({
    'popup', 'pop-up', 'popover-ad', 'interstitial', 'lightbox', 'paywall',
    'exit-intent', 'exitintent', 'subscribe-modal', 'newsletter-modal',
    'signup-modal', 'welcome-mat', 'popup-overlay', 'modal-newsletter',
    'newsletter-popup', 'subscription-overlay',
})

# Only these can plausibly *be* an interruption. A <span> with a popup class
# is a trigger, not the thing that covers the page.
INTERRUPTION_TAGS = frozenset({'div', 'section', 'aside', 'dialog', 'dialogue'})

_TOKEN_SPLIT_RE = re.compile(r'[\s_]+')

DEFAULT_TLS_TIMEOUT = 8.0


class Rating:
    SAFE = 'safe'
    MOSTLY_SAFE = 'mostly_safe'
    SOME_CONCERNS = 'some_concerns'
    CAUTION = 'caution'
    UNSAFE = 'unsafe'
    UNKNOWN = 'unknown'


RATING_LABELS = {
    Rating.SAFE: 'SAFE',
    Rating.MOSTLY_SAFE: 'MOSTLY SAFE',
    Rating.SOME_CONCERNS: 'SOME CONCERNS',
    Rating.CAUTION: 'CAUTION',
    Rating.UNSAFE: 'UNSAFE',
    Rating.UNKNOWN: 'COULD NOT CHECK',
}


@dataclass
class CertificateInfo:
    present: bool = False
    valid: bool = False
    issuer: str = ''
    subject: str = ''
    expires_at: datetime | None = None
    days_remaining: int | None = None
    protocol: str = ''
    problem: str = ''


@dataclass
class ThirdParty:
    domain: str
    category: str
    name: str
    count: int = 1


@dataclass
class Findings:
    """Everything observed, before any judgement is applied."""

    checked_url: str = ''
    reachable: bool = False
    status: int = 0
    unreachable_reason: str = ''

    https: bool = False
    http_redirects_to_https: bool | None = None
    certificate: CertificateInfo = field(default_factory=CertificateInfo)

    missing_headers: list[str] = field(default_factory=list)
    present_headers: list[str] = field(default_factory=list)
    cookies_set: int = 0

    third_parties: list[ThirdParty] = field(default_factory=list)
    inline_trackers: list[str] = field(default_factory=list)
    third_party_script_count: int = 0

    mixed_content: int = 0
    insecure_forms: int = 0
    password_over_http: bool = False

    iframes: int = 0
    interruptions: int = 0
    autoplay_media: int = 0
    uses_document_write: bool = False
    meta_refresh: bool = False
    tag_manager_present: bool = False

    has_privacy_policy: bool = False
    has_contact: bool = False

    def counts(self) -> dict[str, int]:
        totals: dict[str, int] = {}
        for item in self.third_parties:
            totals[item.category] = totals.get(item.category, 0) + 1
        return totals


@dataclass
class SafetyResult:
    score: float | None
    rating: str
    headline: str
    summary: str
    reasons: list[dict]
    findings: Findings
    engine_version: str = ENGINE_VERSION


# ---------------------------------------------------------------------------
# Parsing the page
# ---------------------------------------------------------------------------

class _SiteParser(HTMLParser):
    """Collects the structural facts a safety rating is built from."""

    def __init__(self, base_url: str):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.hosts: list[str] = []
        self.inline_scripts: list[str] = []
        self.iframes = 0
        self.interruptions = 0
        self.autoplay = 0
        self.mixed = 0
        self.insecure_forms = 0
        self.password_over_http = False
        self.meta_refresh = False
        self.link_texts: list[str] = []
        self.link_hrefs: list[str] = []
        self._in_script = False
        self._in_link = False
        self._form_insecure = False
        self._form_has_password = False

    def _note_url(self, value: str, external_only: bool = True) -> None:
        if not value:
            return
        if value.startswith('//'):
            value = 'https:' + value
        if not value.startswith(('http://', 'https://')):
            return
        if value.startswith('http://') and self.base_url.startswith('https://'):
            self.mixed += 1
        host = urlsplit(value).hostname or ''
        if host:
            self.hosts.append(host)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in INTERRUPTION_TAGS:
            tokens = set(_TOKEN_SPLIT_RE.split(
                f"{attrs.get('id', '')} {attrs.get('class', '')}".lower().strip()
            ))
            if tokens & INTERRUPTION_TOKENS:
                self.interruptions += 1
        if tag == 'dialog':
            self.interruptions += 1

        if tag == 'script':
            self._in_script = True
            self._note_url(attrs.get('src', ''))
        elif tag == 'iframe':
            self.iframes += 1
            self._note_url(attrs.get('src', ''))
        elif tag in ('img', 'source', 'embed', 'object'):
            self._note_url(attrs.get('src') or attrs.get('data') or '')
        elif tag == 'link':
            self._note_url(attrs.get('href', ''))
        elif tag in ('video', 'audio'):
            if 'autoplay' in attrs:
                self.autoplay += 1
        elif tag == 'form':
            action = (attrs.get('action') or '').strip()
            target = urljoin(self.base_url, action) if action else self.base_url
            self._form_insecure = target.startswith('http://')
            self._form_has_password = False
        elif tag == 'input':
            if (attrs.get('type') or '').lower() == 'password':
                self._form_has_password = True
        elif tag == 'a':
            self._in_link = True
            self.link_hrefs.append((attrs.get('href') or '').lower())
        elif tag == 'meta':
            if (attrs.get('http-equiv') or '').lower() == 'refresh':
                self.meta_refresh = True

    def handle_endtag(self, tag):
        if tag == 'script':
            self._in_script = False
        elif tag == 'a':
            self._in_link = False
        elif tag == 'form':
            if self._form_insecure:
                self.insecure_forms += 1
                if self._form_has_password:
                    self.password_over_http = True
            self._form_insecure = False
            self._form_has_password = False

    def handle_data(self, data):
        if self._in_script:
            self.inline_scripts.append(data)
        elif self._in_link and data.strip():
            self.link_texts.append(data.strip().lower())


# ---------------------------------------------------------------------------
# TLS
# ---------------------------------------------------------------------------

def inspect_certificate(host: str, timeout: float | None = None) -> CertificateInfo:
    """Read the TLS certificate, and say plainly what is wrong with it."""
    timeout = timeout or getattr(settings, 'INSPECTION_TLS_TIMEOUT', DEFAULT_TLS_TIMEOUT)

    # Same host policy as any other outbound request: a domain resolving to
    # loopback must not be connected to just because this is a TLS check.
    from content.http import Blocked, FetchError, assert_fetchable
    try:
        assert_fetchable(f'https://{host}/')
    except (Blocked, FetchError) as exc:
        return CertificateInfo(problem=str(exc))

    context = ssl.create_default_context()
    try:
        with socket.create_connection((host, 443), timeout=timeout) as raw:
            with context.wrap_socket(raw, server_hostname=host) as tls:
                cert = tls.getpeercert() or {}
                protocol = tls.version() or ''
    except ssl.SSLCertVerificationError as exc:
        # The most informative failure there is: Python already phrased it.
        return CertificateInfo(
            present=True, valid=False,
            problem=getattr(exc, 'verify_message', None) or str(exc),
        )
    except ssl.SSLError as exc:
        return CertificateInfo(present=True, valid=False, problem=f'TLS error: {exc.reason or exc}')
    except (socket.timeout, TimeoutError):
        return CertificateInfo(problem='TLS handshake timed out')
    except OSError as exc:
        return CertificateInfo(problem=f'could not connect over TLS ({exc.strerror or exc})')

    def _name(entries) -> str:
        for group in entries or ():
            for key, value in group:
                if key in ('organizationName', 'commonName'):
                    return str(value)
        return ''

    expires = None
    remaining = None
    if cert.get('notAfter'):
        try:
            expires = datetime.fromtimestamp(
                ssl.cert_time_to_seconds(cert['notAfter']), tz=dt_timezone.utc
            )
            remaining = (expires - datetime.now(dt_timezone.utc)).days
        except (ValueError, OverflowError):
            pass

    return CertificateInfo(
        present=True,
        valid=True,
        issuer=_name(cert.get('issuer'))[:255],
        subject=_name(cert.get('subject'))[:255],
        expires_at=expires,
        days_remaining=remaining,
        protocol=protocol,
    )


# ---------------------------------------------------------------------------
# The inspection
# ---------------------------------------------------------------------------

def _gather(domain: str) -> Findings:
    """Fetch and read the site. Never raises; failures land in the Findings."""
    from content import robots
    from content.http import Blocked, FetchError, request

    findings = Findings()

    https_response = None
    try:
        https_response = request(f'https://{domain}/', on_hop=robots.check)
        findings.https = True
    except (Blocked, FetchError) as exc:
        findings.unreachable_reason = f'https: {exc}'

    response = https_response
    if response is None:
        try:
            response = request(f'http://{domain}/', on_hop=robots.check)
            findings.https = response.url.startswith('https://')
            findings.http_redirects_to_https = findings.https
        except (Blocked, FetchError) as exc:
            findings.unreachable_reason = f'{findings.unreachable_reason}; http: {exc}'.strip('; ')
            return findings
    else:
        # HTTPS works. Does plain HTTP get upgraded, or served as-is?
        try:
            plain = request(f'http://{domain}/', on_hop=robots.check, max_bytes=20_000)
            findings.http_redirects_to_https = plain.url.startswith('https://')
        except (Blocked, FetchError):
            # Refusing plaintext outright is a fine answer too, and better
            # than serving it — so it is not recorded as a failure.
            findings.http_redirects_to_https = True

    findings.reachable = True
    findings.status = response.status
    findings.checked_url = response.url
    findings.cookies_set = len(response.cookies)

    for header, label, _weight in SECURITY_HEADERS:
        if response.header(header):
            findings.present_headers.append(label)
        elif header == 'x-frame-options' and 'frame-ancestors' in response.header(
            'content-security-policy'
        ):
            findings.present_headers.append(label)      # CSP covers it
        else:
            findings.missing_headers.append(label)

    if findings.https:
        findings.certificate = inspect_certificate(urlsplit(response.url).hostname or domain)

    parser = _SiteParser(response.url)
    try:
        parser.feed(response.text())
        parser.close()
    except Exception:
        logger.debug('Site parse ended early for %s', domain, exc_info=True)

    own = registrable_domain(domain)
    seen: dict[str, ThirdParty] = {}
    third_party_scripts = 0
    for host in parser.hosts:
        host_domain = registrable_domain(host)
        if not host_domain or host_domain == own:
            continue
        third_party_scripts += 1
        hit = classify(host_domain)
        if hit is None:
            continue
        category, name = hit
        if host_domain in seen:
            seen[host_domain].count += 1
        else:
            seen[host_domain] = ThirdParty(domain=host_domain, category=category, name=name)

    inline = '\n'.join(parser.inline_scripts)
    findings.inline_trackers = sorted({
        name for needle, name in INLINE_SIGNATURES if needle in inline
    })
    findings.uses_document_write = 'document.write' in inline
    findings.tag_manager_present = any(
        t.domain == 'googletagmanager.com' for t in seen.values()
    ) or 'dataLayer.push' in inline

    findings.third_parties = sorted(seen.values(), key=lambda t: (t.category, t.domain))
    findings.third_party_script_count = third_party_scripts
    findings.mixed_content = parser.mixed
    findings.insecure_forms = parser.insecure_forms
    findings.password_over_http = parser.password_over_http
    findings.iframes = parser.iframes
    findings.interruptions = parser.interruptions
    findings.autoplay_media = parser.autoplay
    findings.meta_refresh = parser.meta_refresh

    hrefs = ' '.join(parser.link_hrefs)
    texts = ' '.join(parser.link_texts)
    findings.has_privacy_policy = bool(
        re.search(r'privacy|datenschutz|gizlilik', hrefs + ' ' + texts)
    )
    findings.has_contact = bool(re.search(r'contact|about|impressum|imprint', hrefs + ' ' + texts))

    return findings


def _rate(findings: Findings) -> SafetyResult:
    """Turn observations into a rating out of 10, showing every deduction."""
    if not findings.reachable:
        return SafetyResult(
            score=None,
            rating=Rating.UNKNOWN,
            headline=RATING_LABELS[Rating.UNKNOWN],
            summary=(
                f'The site did not answer, so nothing was inspected. '
                f'({findings.unreachable_reason}) Unreachable is not unsafe — '
                'no rating is given rather than a bad one.'
            ),
            reasons=[],
            findings=findings,
        )

    score = 10.0
    reasons: list[dict] = []

    def deduct(points: float, label: str):
        nonlocal score
        score -= points
        reasons.append({'label': label, 'points': -round(points, 2)})

    def credit(points: float, label: str):
        nonlocal score
        score += points
        reasons.append({'label': label, 'points': round(points, 2)})

    # -- transport ---------------------------------------------------------
    if not findings.https:
        deduct(4.0, 'Serves plaintext HTTP only — anything sent to this site '
                    'is readable by anyone on the network')
    else:
        cert = findings.certificate
        if cert.present and not cert.valid:
            deduct(3.0, f'TLS certificate is not valid: {cert.problem}')
        elif cert.days_remaining is not None and cert.days_remaining < 0:
            deduct(3.0, 'TLS certificate has expired')
        elif cert.days_remaining is not None and cert.days_remaining < 14:
            deduct(0.5, f'TLS certificate expires in {cert.days_remaining} days')
        if findings.http_redirects_to_https is False:
            deduct(1.0, 'Plain HTTP is served instead of redirecting to HTTPS')

    if findings.password_over_http:
        deduct(3.0, 'A password field submits over unencrypted HTTP')
    elif findings.insecure_forms:
        deduct(1.0, f'{findings.insecure_forms} form(s) submit over unencrypted HTTP')

    if findings.mixed_content:
        deduct(min(1.0, 0.2 * findings.mixed_content),
               f'{findings.mixed_content} resource(s) loaded over plaintext on an HTTPS page')

    # -- what the site declares about itself -------------------------------
    missing = set(findings.missing_headers)
    header_loss = sum(w for _h, label, w in SECURITY_HEADERS if label in missing)
    if header_loss:
        deduct(min(1.5, header_loss),
               f'No {", ".join(sorted(missing))} header(s)')

    # -- monetisation and surveillance -------------------------------------
    counts = findings.counts()
    if counts.get('miners'):
        deduct(4.0, 'Runs a cryptocurrency miner in the visitor\'s browser')
    if counts.get('intrusive_ads'):
        deduct(min(2.5, 1.25 * counts['intrusive_ads']),
               f'{counts["intrusive_ads"]} pop-under / forced-redirect ad network(s)')
    if counts.get('ads'):
        deduct(min(1.5, 0.35 * counts['ads']),
               f'{counts["ads"]} advertising network(s)')
    if counts.get('nagware'):
        deduct(min(0.8, 0.4 * counts['nagware']),
               f'{counts["nagware"]} push-notification prompt service(s)')
    if counts.get('trackers'):
        deduct(min(1.2, 0.2 * counts['trackers']),
               f'{counts["trackers"]} third-party tracker(s)')

    session_recorders = [t.name for t in findings.third_parties if 'session recording' in t.name]
    if session_recorders:
        deduct(0.5, f'Records what visitors do on the page ({", ".join(session_recorders)})')

    # -- clutter -----------------------------------------------------------
    if findings.interruptions:
        deduct(min(0.8, 0.2 * findings.interruptions),
               f'{findings.interruptions} pop-up / overlay element(s)')
    if findings.autoplay_media:
        deduct(0.4, f'{findings.autoplay_media} auto-playing video or audio element(s)')
    if findings.iframes > 5:
        deduct(0.4, f'{findings.iframes} embedded frames')
    if findings.meta_refresh:
        deduct(0.3, 'Auto-redirects the visitor with a meta refresh')
    if findings.third_party_script_count > 25:
        deduct(0.5, f'{findings.third_party_script_count} third-party resources loaded')

    # -- transparency ------------------------------------------------------
    if findings.has_privacy_policy:
        credit(0.25, 'Links to a privacy policy')
    if findings.has_contact:
        credit(0.25, 'Links to contact or about information')

    score = max(0.0, min(10.0, score))

    # One override. A site that takes passwords over plaintext cannot be
    # described as safe no matter how tidy the rest of it is.
    if findings.password_over_http:
        score = min(score, 3.0)

    if score >= 8.5:
        rating = Rating.SAFE
    elif score >= 7.0:
        rating = Rating.MOSTLY_SAFE
    elif score >= 5.0:
        rating = Rating.SOME_CONCERNS
    elif score >= 3.0:
        rating = Rating.CAUTION
    else:
        rating = Rating.UNSAFE

    return SafetyResult(
        score=round(score, 1),
        rating=rating,
        headline=RATING_LABELS[rating],
        summary=_summarise(findings),
        reasons=reasons,
        findings=findings,
    )


def _summarise(findings: Findings) -> str:
    """One plain sentence about the clutter, which is what people ask about."""
    counts = findings.counts()
    parts = []
    if counts.get('ads') or counts.get('intrusive_ads'):
        total = counts.get('ads', 0) + counts.get('intrusive_ads', 0)
        word = 'aggressive ' if counts.get('intrusive_ads') else ''
        parts.append(f'{total} {word}ad network(s)')
    if counts.get('trackers'):
        parts.append(f'{counts["trackers"]} tracker(s)')
    if counts.get('nagware'):
        parts.append('push-notification prompts')
    if counts.get('miners'):
        parts.append('a cryptocurrency miner')
    if findings.interruptions:
        parts.append(f'{findings.interruptions} pop-up/overlay element(s)')
    if findings.autoplay_media:
        parts.append('auto-playing media')

    if not parts:
        clutter = 'No advertising, trackers, or pop-ups were found on the homepage'
    else:
        clutter = 'The homepage loads ' + ', '.join(parts)

    caveat = (
        ' JavaScript is not executed, so anything injected at runtime is not '
        'counted' + (' — and a tag manager is present, which usually loads more.'
                     if findings.tag_manager_present else '.')
    )
    return clutter + '.' + caveat


def inspect(domain: str) -> SafetyResult:
    """Fetch a site and rate how safe and how cluttered it is, out of 10."""
    return _rate(_gather(domain))


def findings_as_dict(findings: Findings) -> dict:
    """JSON-safe form for storage."""
    data = asdict(findings)
    cert = data.get('certificate') or {}
    if cert.get('expires_at'):
        cert['expires_at'] = findings.certificate.expires_at.isoformat()
    return data
