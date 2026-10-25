"""The trained credibility scorer.

Two scorers live here. Both read the same artifact, written by
`manage.py train_credibility_model`.

`GradientBoostedScorer` is the model on its own. It abstains below a
confidence floor, which on the shipped artifact means it declines to speak
about most domains. That is the honest behaviour for a model with this much
signal, and it is useless as the only scorer in a platform that has to put a
number on every source.

`BlendedScorer` is the one to deploy. The heuristic produces the score, the
model adjusts it only when confident, and the rationale names both
contributions so an analyst can see which part of the number came from
learned weights and which from hand-written ones. When the model declines,
the rationale says so rather than silently returning the heuristic's figure
as though nothing had been tried.

Neither is the default. `CREDIBILITY_SCORER` still points at the heuristic,
because the optional ML dependencies are not installed in a fresh clone and a
platform that fails to start because a model file is missing is worse than
one scoring on hand-written weights.

What the model is a model of
----------------------------
It was trained on Media Bias/Fact Check factual-reporting ratings. It
predicts what MBFC would say about a domain, which is one organisation's
editorial judgement, largely about US outlets, and not ground truth. Every
rationale it writes says so, because a number on a console acquires authority
it has not earned unless something on the screen keeps saying where it came
from.
"""

from __future__ import annotations

import threading
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from .models import TrustTier
from .scoring import (METADATA_SCORE_CEILING, BaseScorer, HeuristicScorer,
                      ScoreResult, SourceFeatures)

DEFAULT_ARTIFACT = Path(__file__).resolve().parent / 'artifacts' / 'credibility_gbt.joblib'

# How far a confident model call is allowed to move the heuristic's score.
# At the shipped floor the model is right about 19 times in 20 when it
# speaks, which makes it better evidence than "has a named publisher" (+8)
# and worse than a registration date (+18/-25). Fifteen points sits between
# them on purpose.
DEFAULT_ADJUSTMENT = 15.0

_lock = threading.Lock()
_cache: dict[str, dict] = {}


def _artifact_path() -> Path:
    return Path(getattr(settings, 'CREDIBILITY_ML_ARTIFACT', DEFAULT_ARTIFACT))


def load_artifact(path: Path | None = None) -> dict:
    """Load and memoise the trained model.

    Memoised because a scorer is constructed per request in some code paths
    and unpickling the model on every one would make scoring slower than the
    registry lookup it depends on.
    """
    path = Path(path or _artifact_path())
    key = str(path)
    cached = _cache.get(key)
    if cached is not None:
        return cached

    with _lock:
        cached = _cache.get(key)
        if cached is not None:
            return cached

        try:
            import joblib
        except ImportError as exc:
            raise ImproperlyConfigured(
                'The trained credibility scorer needs the optional ML '
                'dependencies. Install them with: '
                'pip install -r requirements-ml.txt'
            ) from exc

        if not path.exists():
            raise ImproperlyConfigured(
                f'No trained credibility model at {path}. Build one with: '
                'manage.py build_credibility_dataset --rdap 1400 && '
                'manage.py train_credibility_model'
            )

        artifact = joblib.load(path)
        _cache[key] = artifact
        return artifact


def clear_cache() -> None:
    """Drop the memoised artifact. Used by the tests."""
    with _lock:
        _cache.clear()


class _ModelMixin:
    """Shared loading and prediction, so the two scorers cannot drift."""

    # Read by the overview endpoint. A console that tells an analyst which
    # figures came from a fitted model should not be deciding that by
    # pattern-matching a class name, so the class says so itself.
    is_trained = True

    def __init__(self, artifact_path: str | Path | None = None):
        self.artifact = load_artifact(artifact_path)
        self.min_confidence = float(getattr(
            settings, 'CREDIBILITY_ML_MIN_CONFIDENCE',
            self.artifact.get('min_confidence', 0.90),
        ))
        self.label_source = self.artifact.get('label_source') or 'unknown ratings'

    def probability(self, features: SourceFeatures) -> float:
        """P(this domain is rated credible), from the model."""
        from .features import feature_row_from_source, to_frame

        frame, _ = to_frame(
            [feature_row_from_source(features)],
            tld_categories=self.artifact['tld_categories'],
        )
        return float(self.artifact['model'].predict_proba(frame)[0, 1])

    def verdict(self, probability: float) -> str | None:
        """'credible', 'not_credible', or None when the model declines.

        Phrased as a distance from 0.5 rather than as two comparisons against
        the floor and its complement. `1.0 - 0.90` is 0.09999999999999998 in
        binary floating point, so the naive version abstains on a probability
        of exactly 0.10 while speaking on exactly 0.90, and the band is
        quietly lopsided. Measuring confidence directly makes the two edges
        the same edge.
        """
        confidence = max(probability, 1.0 - probability)
        if confidence + 1e-9 < self.min_confidence:
            return None
        return 'credible' if probability >= 0.5 else 'not_credible'


class GradientBoostedScorer(_ModelMixin, BaseScorer):
    """The model alone. Abstains below the confidence floor."""

    model_version = 'gbt-v1'

    def score(self, features: SourceFeatures) -> ScoreResult:
        probability = self.probability(features)
        verdict = self.verdict(probability)

        if verdict is None:
            return ScoreResult(
                score=0.0,
                tier=TrustTier.UNKNOWN,
                rationale=(
                    f'{self.model_version}: no score. The model put this domain at '
                    f'{probability:.0%} likely to be rated credible, inside the '
                    f'band where it is not reliable enough to speak '
                    f'(floor {self.min_confidence:.0%}). Trained on '
                    f'{self.label_source}; absence of a score here is absence of '
                    f'a confident call, not a finding about the source.'
                ),
                model_version=self.model_version,
            )

        # Map confidence onto the half of the range the verdict points at,
        # keeping the metadata ceiling the heuristic also respects.
        if verdict == 'credible':
            score = 50.0 + (probability - 0.5) * 2 * (METADATA_SCORE_CEILING - 50.0)
            finding = (
                f'{probability:.0%} likely to be rated credible, at or above '
                f'the {self.min_confidence:.0%} floor'
            )
        else:
            score = 50.0 - (0.5 - probability) * 2 * 50.0
            finding = (
                f'{1.0 - probability:.0%} likely to be rated not credible, at '
                f'or above the {self.min_confidence:.0%} floor'
            )
        score = max(0.0, min(METADATA_SCORE_CEILING, score))

        return ScoreResult(
            score=score,
            tier=self.tier_for(score),
            rationale=(
                f'{self.model_version}: {finding}, so the model speaks. '
                f'Trained on {self.label_source}, which is one '
                f'organisation\'s editorial judgement rather than ground truth. '
                f'Registration and domain metadata only, capped at '
                f'{METADATA_SCORE_CEILING:.0f}.'
            ),
            model_version=self.model_version,
        )


class BlendedScorer(_ModelMixin, BaseScorer):
    """Heuristic score, adjusted by the model only where the model is sure."""

    model_version = 'heuristic-v2+gbt-v1'

    def __init__(self, artifact_path: str | Path | None = None):
        super().__init__(artifact_path)
        self.heuristic = HeuristicScorer()
        self.adjustment = float(getattr(
            settings, 'CREDIBILITY_ML_ADJUSTMENT', DEFAULT_ADJUSTMENT
        ))

    def score(self, features: SourceFeatures) -> ScoreResult:
        base = self.heuristic.score(features)
        probability = self.probability(features)
        verdict = self.verdict(probability)

        if verdict is None:
            note = (
                f'Model abstained: {probability:.0%} likely to be rated credible, '
                f'inside the band below its {self.min_confidence:.0%} confidence '
                f'floor, so it contributed nothing (0).'
            )
            score = base.score
        elif verdict == 'credible':
            score = min(METADATA_SCORE_CEILING, base.score + self.adjustment)
            note = (
                f'Model: {probability:.0%} likely to be rated credible, above its '
                f'{self.min_confidence:.0%} floor (+{score - base.score:.0f}).'
            )
        else:
            score = max(0.0, base.score - self.adjustment)
            note = (
                f'Model: {1 - probability:.0%} likely to be rated not credible, '
                f'above its {self.min_confidence:.0%} floor '
                f'({score - base.score:.0f}).'
            )

        return ScoreResult(
            score=score,
            tier=self.tier_for(score),
            rationale=(
                f'{self.model_version}: {base.rationale} {note} '
                f'The model was trained on {self.label_source}, so its '
                f'contribution reflects that organisation\'s judgement rather '
                f'than a measurement of accuracy.'
            ),
            model_version=self.model_version,
        )


__all__ = [
    'BlendedScorer',
    'GradientBoostedScorer',
    'clear_cache',
    'load_artifact',
]
