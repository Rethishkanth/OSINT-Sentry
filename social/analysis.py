"""Social graph and text scoring.

The plan trains an RGCN (PyG or DGL) over the interaction graph for
coordinated-behaviour detection, and runs RoBERTa for sentiment and toxicity.
Neither library is installed, so this module defines the interfaces they slot
into and provides graph-theoretic and lexicon-based defaults.

A real RGCN implementation subclasses BaseGraphScorer and is named in
SOCIAL_GRAPH_SCORER; a transformer subclasses BaseTextScorer and is named in
SOCIAL_TEXT_SCORER. The tasks that call them do not change.

Both scorers return their working alongside the number, and both withhold a
number when the evidence will not carry one. Calling an account a bot because
it sent one mention nobody answered is worse than saying nothing: the analyst
has no way to tell that 1.00 from the 1.00 of an account that sprayed four
hundred unanswered mentions in an hour. So the thin case scores None and says
why, and the console shows the reason where the number would have been.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections import defaultdict
from dataclasses import dataclass, field

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


def plural(count, singular, many=None):
    """Singular or plural form. `many` covers the irregular cases."""
    return singular if count == 1 else (many or singular + 's')


def reason(label: str, detail: str, effect: str) -> dict:
    """One line of a score's working.

    `effect` names which number the line bears on - 'bot', 'coordination',
    'sentiment', 'toxicity' - or 'limits' for a line that says what the
    evidence cannot support. A social score is a ratio rather than a running
    total, so unlike Module 1 the effect is not a signed contribution: there
    is no column of numbers here that adds up to the answer.
    """
    return {'label': label, 'detail': detail, 'effect': effect}


@dataclass
class AccountNode:
    """The identity of one account, as the graph scorer needs it.

    Only an id and a handle: the scorer works on shape, not on profile
    contents. The handle is carried so the working can say *whose* targets an
    account is shadowing, which is the only form of that finding an analyst
    can act on.
    """

    id: int
    handle: str = ''

    @property
    def label(self) -> str:
        return f'@{self.handle}' if self.handle else f'account {self.id}'


@dataclass
class GraphScore:
    account_id: int
    bot_likelihood: float | None
    coordination_score: float | None
    model_version: str
    reasons: list[dict] = field(default_factory=list)


@dataclass
class TextScore:
    sentiment: float | None
    toxicity: float | None
    model_version: str
    reasons: list[dict] = field(default_factory=list)


class BaseGraphScorer(ABC):
    """Contract for coordinated-behaviour detection over the account graph."""

    model_version: str = 'base'

    @abstractmethod
    def score_graph(self, accounts, edges) -> list[GraphScore]:
        """Score every account given the full account and edge sets.

        `accounts` is a sequence of AccountNode; `edges` a sequence of
        (from_account_id, to_account_id) pairs. Returning a None score is a
        valid answer and means the graph held too little to judge on.
        """


class HeuristicGraphScorer(BaseGraphScorer):
    """Graph-theoretic stand-in for the RGCN.

    Two signals that need no training:

    - *Reciprocity*. Authentic accounts tend to have interactions returned.
      A high out-degree with almost nothing coming back looks automated.
    - *Neighbourhood overlap*. Accounts that repeatedly target exactly the
      same set of accounts as their peers are behaving in lockstep.

    Both are crude proxies. They are ordered sensibly, not calibrated, and the
    model_version says so. Both also need a minimum amount of graph to mean
    anything, and below it this returns None rather than a number: one
    unanswered mention is 0% reciprocity and a bot likelihood of 1.00, which
    is arithmetic rather than evidence.
    """

    model_version = 'heuristic-graph-v2'

    def __init__(self, min_interactions=None, min_shared_targets=None):
        self.min_interactions = (
            min_interactions
            if min_interactions is not None
            else getattr(settings, 'SOCIAL_MIN_INTERACTIONS', 3)
        )
        self.min_shared_targets = (
            min_shared_targets
            if min_shared_targets is not None
            else getattr(settings, 'SOCIAL_MIN_SHARED_TARGETS', 3)
        )

    def score_graph(self, accounts, edges) -> list[GraphScore]:
        nodes = [
            node if isinstance(node, AccountNode) else AccountNode(id=node)
            for node in accounts
        ]
        by_id = {node.id: node for node in nodes}

        out_targets: dict[int, set[int]] = defaultdict(set)
        in_sources: dict[int, set[int]] = defaultdict(set)

        for from_id, to_id in edges:
            out_targets[from_id].add(to_id)
            in_sources[to_id].add(from_id)

        results = []
        for node in nodes:
            outgoing = out_targets[node.id]
            incoming = in_sources[node.id]
            reasons: list[dict] = []

            bot = self._bot_likelihood(outgoing, incoming, reasons)
            coordination = self._coordination(node, outgoing, out_targets, by_id, reasons)

            results.append(
                GraphScore(
                    account_id=node.id,
                    bot_likelihood=bot,
                    coordination_score=coordination,
                    model_version=self.model_version,
                    reasons=reasons,
                )
            )
        return results

    def _bot_likelihood(self, outgoing, incoming, reasons) -> float | None:
        """How little of what this account sends out comes back.

        Measured over distinct counterparties rather than raw edge counts, so
        an account cannot look sociable by mentioning one friend a thousand
        times.
        """
        if not outgoing and not incoming:
            reasons.append(reason(
                'No interactions on record',
                'Nothing in the captured graph touches this account in either '
                'direction. That is a gap in what was collected, not a finding '
                'about the account.',
                'limits',
            ))
            return None

        if not outgoing:
            reasons.append(reason(
                'Only ever mentioned, never mentions',
                f'{len(incoming)} {plural(len(incoming), "account")} '
                f'{plural(len(incoming), "interacts", "interact")} with this one '
                'and it has no outgoing interactions captured. This graph shows '
                'what is said about it, not what it does, so there is no '
                'behaviour here to call automated.',
                'limits',
            ))
            return None

        if len(outgoing) < self.min_interactions:
            reasons.append(reason(
                f'Too few interactions to judge ({len(outgoing)})',
                f'This account interacts with {len(outgoing)} distinct '
                f'{plural(len(outgoing), "account")}; the scorer needs at least '
                f'{self.min_interactions} before reciprocity means anything. A '
                'single unanswered mention is 100% unreciprocated and tells you '
                'nothing.',
                'limits',
            ))
            return None

        reciprocated = len(outgoing & incoming)
        reciprocity = reciprocated / len(outgoing)
        bot = round(1.0 - reciprocity, 4)

        if reciprocated:
            opening = (
                f'{reciprocated} of the {len(outgoing)} accounts it interacts '
                f'with interact back.'
            )
        else:
            opening = (
                f'It interacts with {len(outgoing)} accounts and not one of '
                f'them interacts back.'
            )
        reasons.append(reason(
            f'{reciprocated} of {len(outgoing)} interactions reciprocated',
            opening + ' Amplifiers talk at people who do not answer; so, '
            'sometimes, do newcomers and people shouting at institutions.',
            'bot',
        ))
        return bot

    def _coordination(self, node, outgoing, out_targets, by_id, reasons) -> float | None:
        """Highest Jaccard overlap between this account's targets and a peer's.

        A peer only counts if it shares at least `min_shared_targets` of them.
        Two accounts that each mentioned the same single newspaper have a
        Jaccard of 1.0 and no story behind it.
        """
        if len(outgoing) < self.min_shared_targets:
            reasons.append(reason(
                'Not enough targets to compare',
                f'Lockstep is measured against the set of accounts this one '
                f'interacts with, and it has {len(outgoing) or "no"} '
                f'{plural(len(outgoing), "target")}. At least '
                f'{self.min_shared_targets} are needed before an overlap with '
                'another account is distinguishable from coincidence.',
                'limits',
            ))
            return None

        best = 0.0
        best_peer = None
        best_shared = 0
        for peer_id, peer_targets in out_targets.items():
            if peer_id == node.id or len(peer_targets) < self.min_shared_targets:
                continue
            shared = len(outgoing & peer_targets)
            if shared < self.min_shared_targets:
                continue
            overlap = shared / len(outgoing | peer_targets)
            if overlap > best:
                best, best_peer, best_shared = overlap, peer_id, shared

        if best_peer is None:
            reasons.append(reason(
                'No peer shares its targets',
                f'No other tracked account shares {self.min_shared_targets} or '
                f'more of the {len(outgoing)} accounts this one interacts with. '
                'Within this corpus it is not moving with anyone - a wider '
                'capture could still show that it is.',
                'coordination',
            ))
            return 0.0

        peer = by_id.get(best_peer) or AccountNode(id=best_peer)
        reasons.append(reason(
            f'Shares {best_shared} of {len(outgoing)} targets with {peer.label}',
            f'Overlap of {best:.2f} against its closest peer. Accounts working '
            'from one list look like this - and so do two reporters on the same '
            'beat, which is why this is a score and not a verdict.',
            'coordination',
        ))
        return round(best, 4)


class BaseTextScorer(ABC):
    """Contract for per-post sentiment and toxicity scoring."""

    model_version: str = 'base'

    @abstractmethod
    def score_text(self, text: str) -> TextScore:
        ...


class HeuristicTextScorer(BaseTextScorer):
    """Lexicon stand-in for RoBERTa sentiment/toxicity.

    A lexicon can only report the words it holds. Where the older version
    returned 0.0 for a post containing none of them, this returns None for
    sentiment and says the lexicon was silent - a post nobody could read as
    neutral ("they knew what was in the water and signed it off anyway")
    contains no listed word, and scoring it 0.0 put it beside a weather
    report.

    Toxicity keeps its 0.0, because the absence of blunt abuse is a real
    reading, but every post carries the caveat that the lexicon only catches
    blunt abuse.
    """

    model_version = 'heuristic-text-v2'

    def score_text(self, text: str) -> TextScore:
        words = WORD_RE.findall((text or '').lower())
        if not words:
            return TextScore(
                sentiment=None,
                toxicity=None,
                model_version=self.model_version,
                reasons=[reason(
                    'No text to read',
                    'The post has no readable words, so neither score was '
                    'attempted.',
                    'limits',
                )],
            )

        positive = sorted({w for w in words if w in POSITIVE_WORDS})
        negative = sorted({w for w in words if w in NEGATIVE_WORDS})
        toxic = sorted({w for w in words if w in TOXIC_WORDS})
        toxic_hits = sum(w in TOXIC_WORDS for w in words)

        reasons: list[dict] = []
        sentiment = self._sentiment(words, positive, negative, reasons)
        toxicity = self._toxicity(words, toxic, toxic_hits, reasons)

        return TextScore(
            sentiment=sentiment,
            toxicity=toxicity,
            model_version=self.model_version,
            reasons=reasons,
        )

    def _sentiment(self, words, positive, negative, reasons) -> float | None:
        matched = len(positive) + len(negative)
        if not matched:
            reasons.append(reason(
                'Sentiment lexicon found nothing',
                f'None of the {len(words)} {plural(len(words), "word")} in this '
                f'post appear in the {len(POSITIVE_WORDS) + len(NEGATIVE_WORDS)}-term '
                'sentiment lexicon. That is the lexicon being small, not the '
                'post being neutral, so no sentiment is recorded.',
                'limits',
            ))
            return None

        sentiment = round((len(positive) - len(negative)) / matched, 4)
        parts = []
        if positive:
            parts.append('positive: ' + ', '.join(positive))
        if negative:
            parts.append('negative: ' + ', '.join(negative))
        reasons.append(reason(
            f'{matched} sentiment {plural(matched, "term")} matched',
            '; '.join(parts) + '. The score is the balance between the two '
            'counts - it reads vocabulary, not meaning, so irony and quotation '
            'both come out the wrong way round.',
            'sentiment',
        ))
        return sentiment

    def _toxicity(self, words, toxic, toxic_hits, reasons) -> float:
        toxicity = round(min(1.0, toxic_hits / len(words) * 10), 4)
        if toxic:
            reasons.append(reason(
                f'{toxic_hits} abusive {plural(toxic_hits, "term")} in '
                f'{len(words)} {plural(len(words), "word")}',
                'Matched: ' + ', '.join(toxic) + '. Density against post length, '
                'capped at 1.00, so a short post of pure abuse tops out rather '
                'than exceeding the scale.',
                'toxicity',
            ))
        else:
            reasons.append(reason(
                'No abusive terms matched',
                f'None of the {len(TOXIC_WORDS)} terms in the toxicity lexicon '
                'appear. That lexicon catches open name-calling only; a polite '
                'smear, a dog whistle, or abuse in any other language scores '
                'zero here.',
                'limits',
            ))
        return toxicity


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
