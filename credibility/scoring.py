"""Source credibility scoring.

The plan calls for a gradient-boosted-tree model over domain metadata combined
with a BERT classifier over publisher text. Neither is trained yet, so the
scorer is an interface with a deterministic heuristic default. Swap in the real
model by pointing the CREDIBILITY_SCORER setting at another BaseScorer subclass
— nothing that calls score() has to change.
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


@dataclass(frozen=True)
class SourceFeatures:
    """Everything the scorer is allowed to look at, in one auditable place."""

    domain: str
    domain_age_days: int | None
    has_https: bool
    has_publisher: bool
    is_active: bool
    content_item_count: int
    # WHOIS lookups are a live network call, so nothing here performs one.
    # Populate this from an ingestion job that has a WHOIS source available.
    whois_registrar: str | None = None

    def provenance_hash(self) -> str:
        """Stable hash of the exact inputs behind a score."""
        blob = json.dumps(asdict(self), sort_keys=True, default=str)
        return hashlib.sha256(blob.encode()).hexdigest()


def extract_features(source) -> SourceFeatures:
    age_days = None
    if source.first_seen:
        age_days = (timezone.now() - source.first_seen) // timedelta(days=1)

    return SourceFeatures(
        domain=source.domain,
        domain_age_days=age_days,
        has_https=source.url.startswith('https://'),
        has_publisher=bool(source.publisher),
        is_active=source.is_active,
        content_item_count=source.content_items.count(),
        whois_registrar=None,
    )


@dataclass(frozen=True)
class ScoreResult:
    score: float
    tier: str
    rationale: str
    model_version: str


class BaseScorer(ABC):
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
    """Transparent stand-in for the GBT+BERT model.

    Deliberately simple and explainable: it produces a defensible ordering of
    sources so the pipeline around it can be built and tested, and it says so
    in the rationale rather than pretending to be a trained model.
    """

    model_version = 'heuristic-v1'

    def score(self, features: SourceFeatures) -> ScoreResult:
        score = 50.0
        reasons = []

        if features.has_https:
            score += 10
            reasons.append('serves over HTTPS (+10)')
        else:
            score -= 10
            reasons.append('no HTTPS (-10)')

        if features.has_publisher:
            score += 10
            reasons.append('named publisher (+10)')
        else:
            reasons.append('no publisher attributed (0)')

        if features.domain_age_days is None:
            reasons.append('domain age unknown (0)')
        elif features.domain_age_days > 365:
            score += 15
            reasons.append('known for over a year (+15)')
        elif features.domain_age_days < 30:
            score -= 15
            reasons.append('first seen under 30 days ago (-15)')

        if features.content_item_count >= 10:
            score += 5
            reasons.append('substantial content history (+5)')

        if not features.is_active:
            score -= 20
            reasons.append('marked inactive (-20)')

        score = max(0.0, min(100.0, score))
        rationale = f'{self.model_version}: ' + '; '.join(reasons)
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
