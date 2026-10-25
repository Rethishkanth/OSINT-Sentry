"""Domain registration lookups over RDAP.

RDAP is the registries' own JSON-over-HTTP successor to WHOIS: no API key, no
scraping, no third-party dependency. It answers the one question the scorer
most needs and previously faked - how old is this domain, actually.

Network I/O lives here and nowhere else. The scorer reads cached fields off
Source, so scoring itself stays pure and fast enough to run inside a request.
"""

from __future__ import annotations

import json
import logging
import socket
import time
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone as dt_timezone

from django.conf import settings
from django.utils.module_loading import import_string

# Re-exported: scoring.py and the serializers import registrable_domain from
# here. It lives in core because the API and admin need it to normalise input
# long before anything credibility-specific runs.
from core.domains import registrable_domain  # noqa: F401

logger = logging.getLogger(__name__)

DEFAULT_BOOTSTRAP = 'https://rdap.org/domain/'
DEFAULT_TIMEOUT = 10.0
DEFAULT_ATTEMPTS = 2
DEFAULT_RETRY_DELAY = 1.5
DEFAULT_USER_AGENT = 'OSINT-Sentry/1.0 (+registry lookup; contact: admin@localhost)'


class RegistryStatus:
    """Why a lookup produced (or failed to produce) a registration date."""

    OK = 'ok'
    NOT_FOUND = 'not_found'
    REDACTED = 'redacted'
    UNSUPPORTED = 'unsupported'
    ERROR = 'error'
    DISABLED = 'disabled'


@dataclass(frozen=True)
class RegistrationInfo:
    """What a registry told us about a domain."""

    registered_at: datetime | None = None
    registrar: str = ''
    status: str = RegistryStatus.ERROR
    detail: str = ''
    # The rest of what the record carries. The heuristic scorer reads none of
    # these; the trained one is fitted on all three, and a model served
    # columns its training set had but its caller does not supply is being
    # asked a different question from the one it was measured on.
    epp_statuses: tuple[str, ...] = ()
    expires_at: datetime | None = None
    last_changed_at: datetime | None = None


def _parse_rdap_date(value: str) -> datetime | None:
    """RDAP dates are RFC 3339; Python 3.11 handles the 'Z' suffix natively."""
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt_timezone.utc)
    return parsed


def _registrar_name(payload: dict) -> str:
    """Pull the registrar's display name out of its jCard."""
    for entity in payload.get('entities') or []:
        if 'registrar' not in (entity.get('roles') or []):
            continue
        vcard = entity.get('vcardArray') or []
        if len(vcard) < 2:
            continue
        for field in vcard[1]:
            # Each field is [name, params, type, value].
            if len(field) >= 4 and field[0] == 'fn':
                return str(field[3])[:255]
    return ''


class BaseRegistryClient(ABC):
    """Contract for anything that can answer 'when was this registered?'."""

    @abstractmethod
    def lookup(self, domain: str) -> RegistrationInfo:
        ...


class NullRegistryClient(BaseRegistryClient):
    """Makes no network calls. Set CREDIBILITY_REGISTRY_CLIENT to this to
    run fully offline; domain age then reads as unknown rather than as new."""

    def lookup(self, domain: str) -> RegistrationInfo:
        return RegistrationInfo(
            status=RegistryStatus.DISABLED,
            detail='registry lookups disabled',
        )


class RdapClient(BaseRegistryClient):
    """Queries the RDAP bootstrap service, which redirects to the registry."""

    def __init__(self, bootstrap=None, timeout=None, user_agent=None):
        self.bootstrap = bootstrap or getattr(
            settings, 'CREDIBILITY_RDAP_BOOTSTRAP', DEFAULT_BOOTSTRAP
        )
        self.timeout = timeout or getattr(
            settings, 'CREDIBILITY_RDAP_TIMEOUT', DEFAULT_TIMEOUT
        )
        self.user_agent = user_agent or getattr(
            settings, 'CREDIBILITY_USER_AGENT', DEFAULT_USER_AGENT
        )
        self.attempts = max(1, getattr(settings, 'CREDIBILITY_RDAP_ATTEMPTS', DEFAULT_ATTEMPTS))
        self.retry_delay = getattr(
            settings, 'CREDIBILITY_RDAP_RETRY_DELAY', DEFAULT_RETRY_DELAY
        )

    def _fetch(self, name: str):
        """One RDAP request. Returns (payload, RegistrationInfo-on-failure)."""
        request = urllib.request.Request(
            self.bootstrap + name,
            headers={
                'Accept': 'application/rdap+json, application/json',
                'User-Agent': self.user_agent,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.load(response), None
        except urllib.error.HTTPError as exc:
            # 404 is a real answer: the registry has no such domain. Many
            # ccTLDs also simply do not run RDAP, which surfaces as 404 here.
            if exc.code == 404:
                return None, RegistrationInfo(
                    status=RegistryStatus.NOT_FOUND,
                    detail=f'{name}: no RDAP record (HTTP 404)',
                )
            return None, RegistrationInfo(
                status=RegistryStatus.ERROR,
                detail=f'{name}: HTTP {exc.code}',
            )
        except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
            return None, RegistrationInfo(
                status=RegistryStatus.ERROR,
                detail=f'{name}: {exc.__class__.__name__}',
            )
        except (json.JSONDecodeError, ValueError) as exc:
            return None, RegistrationInfo(
                status=RegistryStatus.ERROR,
                detail=f'{name}: malformed RDAP response ({exc})',
            )

    def lookup(self, domain: str) -> RegistrationInfo:
        name = registrable_domain(domain)
        if not name or '.' not in name:
            return RegistrationInfo(
                status=RegistryStatus.UNSUPPORTED,
                detail=f'not a lookupable domain: {domain!r}',
            )

        # RDAP redirects to whichever registry owns the TLD, and those vary
        # wildly in responsiveness - timeouts here are routinely transient.
        # One retry matters because the first score is the one the ingest gate
        # acts on, and a blind gate is worse than a slow one. NOT_FOUND is a
        # real answer, so it is never retried.
        payload = failure = None
        for attempt in range(self.attempts):
            payload, failure = self._fetch(name)
            if payload is not None or failure.status != RegistryStatus.ERROR:
                break
            if attempt + 1 < self.attempts:
                logger.debug('RDAP retry for %s after %s', name, failure.detail)
                time.sleep(self.retry_delay)

        if payload is None:
            return failure

        registrar = _registrar_name(payload)

        # Status codes and event dates are read whatever the outcome, because
        # a record that withholds its registration date still tells us the
        # domain is locked, on hold, or about to expire.
        statuses = tuple(
            str(code).strip().lower()
            for code in (payload.get('status') or []) if str(code).strip()
        )
        events: dict[str, datetime] = {}
        for event in payload.get('events') or []:
            action = str(event.get('eventAction') or '').strip().lower()
            parsed = _parse_rdap_date(event.get('eventDate', ''))
            # First occurrence wins: some registries repeat an action.
            if action and parsed and action not in events:
                events[action] = parsed
        rest = {
            'epp_statuses': statuses,
            'expires_at': events.get('expiration'),
            'last_changed_at': events.get('last changed'),
        }

        registered = events.get('registration')
        if registered:
            return RegistrationInfo(
                registered_at=registered,
                registrar=registrar,
                status=RegistryStatus.OK,
                detail=f'{name}: registered {registered.date().isoformat()}',
                **rest,
            )

        # The record exists but withholds the date - common for ccTLDs and
        # GDPR-redacted registries. Distinct from "we could not reach anyone".
        return RegistrationInfo(
            registrar=registrar,
            status=RegistryStatus.REDACTED,
            detail=f'{name}: RDAP record has no registration date',
            **rest,
        )


def get_registry_client() -> BaseRegistryClient:
    path = getattr(
        settings,
        'CREDIBILITY_REGISTRY_CLIENT',
        'credibility.registry.RdapClient',
    )
    return import_string(path)()
