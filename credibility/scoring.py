"""Source credibility scoring.

The plan calls for a gradient-boosted-tree model over domain metadata combined
with a BERT classifier over publisher text. Neither is trained yet, so the
scorer is an interface with a deterministic heuristic default. Swap in the real
model by pointing the CREDIBILITY_SCORER setting at another BaseScorer subclass
- nothing that calls score() has to change.

What this scorer is and is not
------------------------------
It reads *registration metadata*. That genuinely separates a domain registered
last Tuesday from one registered in 1993, which is a real and well-evidenced
disinformation signal. It says nothing whatsoever about whether the content is
true. The tier ceiling below is deliberate: metadata alone cannot earn the
VERIFIED tier, because no amount of clean WHOIS makes a publisher accurate.
"""

from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from django.utils.module_loading import import_string

from .models import TrustTier
from .registry import RegistryStatus, registrable_domain

# TLDs with persistently high abuse rates in published registry reporting.
# This is a weak prior about the neighbourhood, not a claim about any one site,
# so it carries a correspondingly small penalty.
HIGH_ABUSE_TLDS = frozenset({
    'tk', 'ml', 'ga', 'cf', 'gq', 'top', 'xyz', 'click', 'link', 'work',
    'buzz', 'rest', 'cyou', 'sbs', 'icu', 'cam', 'bond', 'quest', 'zip',
    'mov', 'country', 'kim', 'loan', 'download', 'racing', 'win',
})

# Metadata alone cannot exceed this. Reaching VERIFIED requires evidence this
# scorer does not have access to (editorial review, corroboration, a trained
# model over publisher text).
METADATA_SCORE_CEILING = 82.0


@dataclass(frozen=True)
class SourceFeatures:
    """Everything the scorer is allowed to look at, in one auditable place."""

    domain: str
    tld: str
    # Real registration age from the registry (RDAP), cached on the Source.
    domain_age_days: int | None
    registry_status: str
    registrar: str
    # Tri-state on purpose. None means no URL was ever recorded for this
    # source, which is not the same as having observed a plaintext one.
    has_https: bool | None
    has_publisher: bool
    is_active: bool
    content_item_count: int
    # How long *we* have known about the source. Recorded for provenance, and
    # deliberately not scored: rewarding it would mean a source's credibility
    # rises simply because the row has been sitting in our database, which is
    # circular. It is here so the hash covers it, not so it counts.
    known_to_us_days: int | None = None
    # The registry's own status codes, expiry and last-changed dates. The
    # heuristic below reads none of them; they are here because the trained
    # scorer is fitted on them, and SourceFeatures is the one place a scorer
    # is allowed to read from. Tuple rather than list so the dataclass stays
    # hashable and provenance_hash keeps covering every input.
    registry_epp_statuses: tuple[str, ...] = ()
    registry_expires_in_days: int | None = None
    registry_last_changed_days: int | None = None

    def provenance_hash(self) -> str:
        """Stable hash of the exact inputs behind a score."""
        blob = json.dumps(asdict(self), sort_keys=True, default=str)
        return hashlib.sha256(blob.encode()).hexdigest()


def extract_features(source) -> SourceFeatures:
    """Read features off the Source. Never makes a network call.

    Registry data is whatever credibility.tasks.refresh_domain_registration
    last cached; if it has never run, domain age reads as unknown.
    """
    now = timezone.now()

    domain_age_days = None
    if source.domain_registered_at:
        domain_age_days = (now - source.domain_registered_at) // timedelta(days=1)

    known_to_us_days = None
    if source.first_seen:
        known_to_us_days = (now - source.first_seen) // timedelta(days=1)

    # Signed on purpose: a domain whose registration lapsed last week is a
    # different thing from one with a year left, and clamping the expired
    # case to zero would make them look the same.
    expires_in_days = None
    if source.domain_expires_at:
        expires_in_days = (source.domain_expires_at - now) // timedelta(days=1)

    last_changed_days = None
    if source.registry_last_changed_at:
        last_changed_days = (now - source.registry_last_changed_at) // timedelta(days=1)

    name = registrable_domain(source.domain)
    tld = name.rsplit('.', 1)[-1] if '.' in name else ''

    return SourceFeatures(
        domain=source.domain,
        tld=tld,
        domain_age_days=domain_age_days,
        registry_status=source.registry_status or '',
        registrar=source.registrar or '',
        has_https=source.url.startswith('https://') if source.url else None,
        has_publisher=bool(source.publisher),
        is_active=source.is_active,
        content_item_count=source.content_items.count(),
        known_to_us_days=known_to_us_days,
        registry_epp_statuses=tuple(source.registry_epp_statuses or ()),
        registry_expires_in_days=expires_in_days,
        registry_last_changed_days=last_changed_days,
    )


@dataclass(frozen=True)
class ScoreResult:
    score: float
    tier: str
    rationale: str
    model_version: str


class BaseScorer(ABC):
    # Overridden to True by anything fitted to data. See credibility.ml.
    is_trained = False

    """Contract every credibility model implements."""

    model_version: str = 'base'

    @abstractmethod
    def score(self, features: SourceFeatures) -> ScoreResult:
        ...

    @staticmethod
    def tier_for(score: float) -> str:
        if score >= 85:
            return TrustTier.VERIFIED
        if score >= 65:
            return TrustTier.HIGH
        if score >= 40:
            return TrustTier.MEDIUM
        if score > 0:
            return TrustTier.LOW
        return TrustTier.UNKNOWN


class HeuristicScorer(BaseScorer):
    """Registration-metadata scorer, standing in for the GBT+BERT model.

    Every weight below is justified by what the signal actually evidences, and
    the rationale string states the reasoning on every record it writes.
    """

    model_version = 'heuristic-v2'

    def score(self, features: SourceFeatures) -> ScoreResult:
        score = 50.0
        reasons = []

        # HTTPS earns nothing. It is free, automatic, and universal - every
        # phishing site has it - so its presence is not evidence of anything.
        # Its *absence* in 2026 is genuinely anomalous, so that still costs.
        # An unrecorded URL is neither: penalising a source for a scheme we
        # never observed would mark down every domain added without one.
        if features.has_https is False:
            score -= 10
            reasons.append('no HTTPS (-10)')
        elif features.has_https is None:
            reasons.append('no URL recorded, so transport is unknown (0)')

        if features.has_publisher:
            score += 8
            reasons.append('named publisher, so there is someone to hold to account (+8)')
        else:
            reasons.append('no publisher attributed (0)')

        age = features.domain_age_days
        if age is None:
            reasons.append(
                f'domain age unknown, registry lookup '
                f'{features.registry_status or "never run"} (0)'
            )
        elif age < 30:
            score -= 25
            reasons.append(f'domain registered {age}d ago, freshly created (-25)')
        elif age < 180:
            score -= 15
            reasons.append(f'domain registered {age}d ago, under 6 months (-15)')
        elif age < 365:
            score -= 5
            reasons.append(f'domain registered {age}d ago, under a year (-5)')
        elif age >= 1825:
            score += 18
            reasons.append(f'domain registered {age // 365}y ago, long-established (+18)')
        elif age >= 730:
            score += 12
            reasons.append(f'domain registered {age // 365}y ago, established (+12)')
        else:
            score += 4
            reasons.append(f'domain registered {age}d ago, over a year (+4)')

        if features.tld in HIGH_ABUSE_TLDS:
            score -= 10
            reasons.append(f'.{features.tld} has a high published abuse rate (-10)')

        if features.content_item_count >= 10:
            score += 5
            reasons.append('substantial content history (+5)')

        if not features.is_active:
            score -= 20
            reasons.append('marked inactive (-20)')

        score = max(0.0, min(METADATA_SCORE_CEILING, score))
        rationale = (
            f'{self.model_version}: ' + '; '.join(reasons)
            + f'. Registration metadata only - capped at {METADATA_SCORE_CEILING:.0f}, '
            'says nothing about whether the content is accurate.'
        )
        return ScoreResult(
            score=score,
            tier=self.tier_for(score),
            rationale=rationale,
            model_version=self.model_version,
        )


def get_scorer() -> BaseScorer:
    path = getattr(
        settings,
        'CREDIBILITY_SCORER',
        'credibility.scoring.HeuristicScorer',
    )
    return import_string(path)()


__all__ = [
    'BaseScorer',
    'HeuristicScorer',
    'RegistryStatus',
    'ScoreResult',
    'SourceFeatures',
    'extract_features',
    'get_scorer',
]
