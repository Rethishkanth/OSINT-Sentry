"""Social graph and text scoring.

The plan trains an RGCN (PyG or DGL) over the interaction graph for
coordinated-behaviour detection, and runs RoBERTa for sentiment and toxicity.
Neither library is installed, so this module defines the interfaces they slot
into and provides graph-theoretic and lexicon-based defaults.

A real RGCN implementation subclasses BaseGraphScorer and is named in
SOCIAL_GRAPH_SCORER; a transformer subclasses BaseTextScorer and is named in
SOCIAL_TEXT_SCORER. The tasks that call them do not change.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections import defaultdict
from dataclasses import dataclass

from django.conf import settings
from django.utils.module_loading import import_string

WORD_RE = re.compile(r"[a-z']+")

# Small illustrative lexicons. A trained classifier replaces both.
POSITIVE_WORDS = {
    'good', 'great', 'excellent', 'positive', 'wonderful', 'helpful', 'honest',
    'accurate', 'reliable', 'thanks', 'agree', 'support', 'best', 'clear',
}
NEGATIVE_WORDS = {
    'bad', 'terrible', 'awful', 'negative', 'wrong', 'false', 'fake', 'lie',
    'lies', 'misleading', 'corrupt', 'disagree', 'worst', 'failed', 'hoax',
}
TOXIC_WORDS = {
    'idiot', 'stupid', 'moron', 'scum', 'trash', 'hate', 'disgusting',
    'pathetic', 'shameful', 'traitor', 'vermin',
}


@dataclass
class GraphScore:
    account_id: int
    bot_likelihood: float
    coordination_score: float
    model_version: str


@dataclass
class TextScore:
    sentiment: float
    toxicity: float
    model_version: str


class BaseGraphScorer(ABC):
    """Contract for coordinated-behaviour detection over the account graph."""

    model_version: str = 'base'

    @abstractmethod
    def score_graph(self, accounts, edges) -> list[GraphScore]:
        """Score every account given the full account and edge sets."""


class HeuristicGraphScorer(BaseGraphScorer):
    """Graph-theoretic stand-in for the RGCN.

    Two signals that need no training:

    - *Reciprocity*. Authentic accounts tend to have interactions returned.
      A high out-degree with almost nothing coming back looks automated.
    - *Neighbourhood overlap*. Accounts that repeatedly target exactly the
      same set of accounts as their peers are behaving in lockstep.

    Both are crude proxies. They are ordered sensibly, not calibrated, and the
    model_version says so.
    """

    model_version = 'heuristic-graph-v1'

    def score_graph(self, accounts, edges) -> list[GraphScore]:
        out_targets: dict[int, set[int]] = defaultdict(set)
        in_sources: dict[int, set[int]] = defaultdict(set)

        for from_id, to_id in edges:
            out_targets[from_id].add(to_id)
            in_sources[to_id].add(from_id)

        results = []
        for account_id in accounts:
            outgoing = out_targets[account_id]
            incoming = in_sources[account_id]

            if not outgoing and not incoming:
                # An isolated account carries no graph evidence either way.
                results.append(GraphScore(account_id, 0.0, 0.0, self.model_version))
                continue

            reciprocated = len(outgoing & incoming)
            reciprocity = reciprocated / len(outgoing) if outgoing else 0.0
            bot_likelihood = round(1.0 - reciprocity, 4) if outgoing else 0.0

            results.append(
                GraphScore(
                    account_id=account_id,
                    bot_likelihood=bot_likelihood,
                    coordination_score=self._coordination(account_id, outgoing, out_targets),
                    model_version=self.model_version,
                )
            )
        return results

    @staticmethod
    def _coordination(account_id, outgoing, out_targets) -> float:
        """Highest Jaccard overlap between this account's targets and a peer's."""
        if not outgoing:
            return 0.0

        best = 0.0
        for peer_id, peer_targets in out_targets.items():
            if peer_id == account_id or not peer_targets:
                continue
            union = outgoing | peer_targets
            overlap = len(outgoing & peer_targets) / len(union)
            best = max(best, overlap)
        return round(best, 4)


class BaseTextScorer(ABC):
    """Contract for per-post sentiment and toxicity scoring."""

    model_version: str = 'base'

    @abstractmethod
    def score_text(self, text: str) -> TextScore:
        ...


class HeuristicTextScorer(BaseTextScorer):
    """Lexicon stand-in for RoBERTa sentiment/toxicity."""

    model_version = 'heuristic-text-v1'

    def score_text(self, text: str) -> TextScore:
        words = WORD_RE.findall((text or '').lower())
        if not words:
            return TextScore(0.0, 0.0, self.model_version)

        positive = sum(w in POSITIVE_WORDS for w in words)
        negative = sum(w in NEGATIVE_WORDS for w in words)
        toxic = sum(w in TOXIC_WORDS for w in words)

        polarity = positive - negative
        denominator = positive + negative
        sentiment = round(polarity / denominator, 4) if denominator else 0.0

        return TextScore(
            sentiment=sentiment,
            toxicity=round(min(1.0, toxic / len(words) * 10), 4),
            model_version=self.model_version,
        )


def get_graph_scorer() -> BaseGraphScorer:
    path = getattr(
        settings, 'SOCIAL_GRAPH_SCORER', 'social.analysis.HeuristicGraphScorer'
    )
    return import_string(path)()


def get_text_scorer() -> BaseTextScorer:
    path = getattr(
        settings, 'SOCIAL_TEXT_SCORER', 'social.analysis.HeuristicTextScorer'
    )
    return import_string(path)()
