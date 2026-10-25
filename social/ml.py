"""Module 3's trained scorers.

Two things live here, and they are at very different stages.

`RobertaTextScorer` replaces the word lexicons with the two RoBERTa models
the plan named. Both are published fine-tuned weights, both were trained on
tweets, and both are measured against the held-out TweetEval test splits by
`manage.py evaluate_text_scorer`. The numbers are in
social/artifacts/roberta_text.metrics.json.

`RgcnGraphScorer` is the relational graph network, written against PyTorch
Geometric as the plan specifies, and it ships with no weights. That is not an
oversight. A bot classifier has to be trained on accounts somebody has
labelled, and the labelled Twitter graphs this field uses - TwiBot-20,
Cresci-2017 - are distributed under access agreements with their authors.
Copies of them sit on public model hubs; training on one of those would be
routing around the terms the authors set, and the fact that it is technically
easy is not an argument. So what ships is the architecture and a command that
trains it on the labels in *your* database, which is the only labelled graph
this project can honestly claim access to. Until that command has been run,
the scorer declines to load rather than guessing, and the heuristic in
social.analysis stays the default.

The lexicon scorer is not deleted and is still the default. It needs no
weights, no torch and no downloads, and a clone that installs
requirements.txt alone still scores every post.
"""

from __future__ import annotations

import os
from collections import defaultdict
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from .analysis import (
    BaseGraphScorer,
    BaseTextScorer,
    GraphScore,
    HeuristicGraphScorer,
    TextScore,
    reason,
)
from .features import (
    FEATURE_NAMES,
    RELATIONS,
    AccountGraphFacts,
    feature_row,
)

# TweetEval's sentiment labels, as the model card spells them.
NEGATIVE, NEUTRAL, POSITIVE = 'negative', 'neutral', 'positive'

# A post longer than the model's window is truncated rather than chunked.
# Unlike an article, a social post that runs past 512 wordpieces is a thread
# dump or a copy-paste, and the sentiment of its first few hundred words is
# as good a reading as any.
MAX_TOKENS = 512


def _cache_dir() -> str:
    return str(getattr(settings, 'CONTENT_MODEL_CACHE', '.model-cache'))


@lru_cache(maxsize=2)
def _classifier(model_name: str):
    """A text classifier, loaded once per process per model."""
    os.environ.setdefault('HF_HOME', _cache_dir())
    from transformers import pipeline

    return pipeline(
        'text-classification', model=model_name,
        top_k=None, truncation=True, max_length=MAX_TOKENS,
    )


def _probabilities(model_name: str, text: str) -> dict[str, float]:
    """Label to probability, with the labels lowercased.

    The two models disagree about capitalisation of their own label names,
    so normalising here keeps the callers from caring.
    """
    scored = _classifier(model_name)(text)[0]
    return {str(d['label']).lower(): float(d['score']) for d in scored}


class RobertaTextScorer(BaseTextScorer):
    """Sentiment and offensiveness from two fine-tuned RoBERTa models."""

    model_version = 'roberta-sentiment-v1+roberta-offensive-v1'
    is_trained = True

    def __init__(self, min_confidence=None):
        self.min_confidence = (
            getattr(settings, 'SOCIAL_TEXT_MIN_CONFIDENCE', 0.60)
            if min_confidence is None else min_confidence
        )

    @property
    def sentiment_model(self) -> str:
        return getattr(settings, 'SOCIAL_SENTIMENT_MODEL',
                       'cardiffnlp/twitter-roberta-base-sentiment-latest')

    @property
    def offensive_model(self) -> str:
        return getattr(settings, 'SOCIAL_OFFENSIVE_MODEL',
                       'cardiffnlp/twitter-roberta-base-offensive')

    def score_text(self, text: str) -> TextScore:
        stripped = (text or '').strip()
        if not stripped:
            return TextScore(
                sentiment=None, toxicity=None,
                model_version=self.model_version,
                reasons=[reason(
                    'No text to read',
                    'The post has no readable characters, so neither score '
                    'was attempted.',
                    'limits',
                )],
            )

        reasons: list[dict] = []
        sentiment = self._sentiment(stripped, reasons)
        toxicity = self._toxicity(stripped, reasons)
        return TextScore(
            sentiment=sentiment, toxicity=toxicity,
            model_version=self.model_version, reasons=reasons,
        )

    def _sentiment(self, text: str, reasons: list[dict]) -> float | None:
        probabilities = _probabilities(self.sentiment_model, text)
        negative = probabilities.get(NEGATIVE, 0.0)
        neutral = probabilities.get(NEUTRAL, 0.0)
        positive = probabilities.get(POSITIVE, 0.0)
        best = max(negative, neutral, positive)

        if best < self.min_confidence:
            reasons.append(reason(
                'The model could not read this post either way',
                f'Its three readings came out {negative:.0%} negative, '
                f'{neutral:.0%} neutral and {positive:.0%} positive, and none '
                f'of them reached the {self.min_confidence:.0%} floor. A split '
                'that even is the model declining, not the post being '
                'balanced, so no sentiment is recorded.',
                'limits',
            ))
            return None

        # Positive minus negative rather than the winning label, because the
        # stored field is a scale and not a class: a post read 55% positive
        # and 40% negative is nothing like one read 95% positive, and
        # collapsing both to "positive" throws that away.
        score = round(positive - negative, 4)
        reasons.append(reason(
            f'Read as {max(probabilities, key=probabilities.get)} '
            f'at {best:.0%}',
            f'{negative:.0%} negative, {neutral:.0%} neutral, '
            f'{positive:.0%} positive; the score is positive minus negative. '
            'The model was fine-tuned on tweets, so it reads short, informal '
            'posts best and is out of its domain on anything long or formal.',
            'sentiment',
        ))
        return score

    def _toxicity(self, text: str, reasons: list[dict]) -> float:
        probabilities = _probabilities(self.offensive_model, text)
        offensive = round(probabilities.get('offensive', 0.0), 4)
        reasons.append(reason(
            f'Offensive language: {offensive:.0%}',
            "The model's probability that a reader would mark this post "
            'offensive. It was trained on open abuse and profanity, so a '
            'polite smear, a dog whistle, or abuse in another language all '
            'score low here. A low number is the absence of the thing it was '
            'taught to find, not the absence of malice.',
            'toxicity',
        ))
        return offensive


def ml_dependencies():
    """Import torch and PyTorch Geometric, or explain how to get them."""
    try:
        import torch
        from torch_geometric.nn import RGCNConv
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise ImproperlyConfigured(
            'the graph model needs torch and torch-geometric. '
            'pip install -r requirements-ml.txt'
        ) from exc
    return torch, RGCNConv


# -- the relational graph network -----------------------------------------
#
# Edges are directed, and an R-GCN layer only moves information along the
# direction it is given. An account that mentions four hundred others would
# learn nothing from any of them, which is the wrong way round: who an
# account talks to is most of what there is to know about it. So every
# relation is added in both directions and the reverse of relation i is
# relation i + len(RELATIONS), as in the paper.
NUM_RELATIONS = len(RELATIONS) * 2


@lru_cache(maxsize=1)
def _rgcn_class():
    """The model class, defined on first use.

    Defined inside a function rather than at module scope because the base
    class is torch.nn.Module, and importing this file must not require
    torch: core.views imports it to ask whether the engine is trained, and
    that question has to be answerable on a machine with no ML extras.
    """
    torch, RGCNConv = ml_dependencies()

    class RGCN(torch.nn.Module):
        def __init__(self, in_channels, hidden=32, num_bases=8):
            super().__init__()
            # Basis decomposition: ten relations with a full matrix each
            # overfits a graph of a few hundred labelled accounts, which is
            # the size of graph anyone here will have.
            self.conv1 = RGCNConv(in_channels, hidden, NUM_RELATIONS,
                                  num_bases=num_bases)
            self.conv2 = RGCNConv(hidden, 2, NUM_RELATIONS, num_bases=num_bases)
            self.dropout = torch.nn.Dropout(0.3)

        def forward(self, x, edge_index, edge_type):
            hidden = torch.relu(self.conv1(x, edge_index, edge_type))
            return self.conv2(self.dropout(hidden), edge_index, edge_type)

    return RGCN


@dataclass
class Graph:
    """The account graph as tensors, plus what each row stands for."""

    x: object            # (accounts, features) float tensor
    edge_index: object   # (2, edges) long tensor
    edge_type: object    # (edges,) long tensor
    account_ids: list[int]
    dropped_edges: int = 0


def collect_facts(nodes, edges) -> dict[int, AccountGraphFacts]:
    """Count each account's graph behaviour, without torch.

    Separated from tensor building so the counting can be tested, and
    compared between training and serving, on a machine with no ML extras.
    """
    known = {node.id for node in nodes}
    out_by: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    in_by: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    out_targets: dict[int, set[int]] = defaultdict(set)
    in_sources: dict[int, set[int]] = defaultdict(set)

    for edge in edges:
        source, target = edge[0], edge[1]
        relation = edge[2] if len(edge) > 2 else RELATIONS[0]
        if source not in known or target not in known:
            continue
        out_by[source][relation] += 1
        in_by[target][relation] += 1
        out_targets[source].add(target)
        in_sources[target].add(source)

    facts = {}
    for node in nodes:
        reciprocated = sum(
            1 for target in out_targets[node.id]
            if node.id in out_targets[target]
        )
        facts[node.id] = AccountGraphFacts(
            out_by_relation=dict(out_by[node.id]),
            in_by_relation=dict(in_by[node.id]),
            out_targets=len(out_targets[node.id]),
            in_sources=len(in_sources[node.id]),
            reciprocated_targets=reciprocated,
            post_count=node.post_count,
            follower_count=node.follower_count,
            age_days=node.age_days,
        )
    return facts


def build_graph(nodes, edges) -> Graph:
    """Tensors for one pass of the model, built the same way in both paths."""
    torch, _ = ml_dependencies()

    nodes = list(nodes)
    position = {node.id: index for index, node in enumerate(nodes)}
    facts = collect_facts(nodes, edges)

    rows = [feature_row(facts[node.id]) for node in nodes]

    sources, targets, types, dropped = [], [], [], 0
    for edge in edges:
        source, target = edge[0], edge[1]
        relation = edge[2] if len(edge) > 2 else RELATIONS[0]
        if source not in position or target not in position:
            dropped += 1
            continue
        if relation not in RELATIONS:
            # An edge type the model has no weight matrix for. Dropped and
            # counted rather than folded into another relation, because
            # calling a follow a mention is a quieter error than losing it.
            dropped += 1
            continue
        index = RELATIONS.index(relation)
        sources.append(position[source])
        targets.append(position[target])
        types.append(index)
        # The reverse edge, in its own relation.
        sources.append(position[target])
        targets.append(position[source])
        types.append(index + len(RELATIONS))

    return Graph(
        x=torch.tensor(rows, dtype=torch.float),
        edge_index=torch.tensor([sources, targets], dtype=torch.long).reshape(2, -1),
        edge_type=torch.tensor(types, dtype=torch.long),
        account_ids=[node.id for node in nodes],
        dropped_edges=dropped,
    )


def standardise(x, mean, deviation):
    """Centre and scale with the training set's own statistics.

    The columns are raw counts whose ranges differ by four orders of
    magnitude, so the model needs them scaled, and it has to be scaled by
    the numbers it was fitted with rather than by the batch in front of it.
    Scoring one account would otherwise standardise it against itself and
    hand the model a row of zeros.
    """
    return (x - mean) / deviation


def artifact_path() -> Path:
    return Path(getattr(
        settings, 'SOCIAL_GRAPH_ARTIFACT',
        Path(settings.BASE_DIR) / 'social' / 'artifacts' / 'rgcn.pt',
    ))


@lru_cache(maxsize=1)
def load_artifact(path: str):
    """The trained weights and the spec they were trained against.

    The spec is checked rather than trusted. Module 1 shipped a scorer whose
    serving path quietly built a different feature row from its training
    path, and nothing failed; the columns and relations are recorded here so
    that the same mistake stops the scorer instead of skewing it.
    """
    torch, _ = ml_dependencies()

    if not Path(path).is_file():
        raise ImproperlyConfigured(
            f'no graph model at {path}. This project ships none: a bot '
            'classifier has to be trained on accounts somebody labelled, and '
            'the public labelled graphs are under access agreements with '
            'their authors. Label accounts in the admin and run: '
            'manage.py train_graph_model'
        )

    bundle = torch.load(path, map_location='cpu', weights_only=False)

    if tuple(bundle.get('feature_names', ())) != FEATURE_NAMES:
        raise ImproperlyConfigured(
            f'{path} was trained on different feature columns than this code '
            'builds. Retrain it: manage.py train_graph_model'
        )
    if tuple(bundle.get('relations', ())) != RELATIONS:
        raise ImproperlyConfigured(
            f'{path} was trained on different edge relations than this code '
            'builds. Retrain it: manage.py train_graph_model'
        )

    model = _rgcn_class()(len(FEATURE_NAMES), hidden=bundle['hidden'],
                          num_bases=bundle['num_bases'])
    model.load_state_dict(bundle['state_dict'])
    model.eval()
    return model, bundle


class RgcnGraphScorer(BaseGraphScorer):
    """Bot likelihood from a relational graph network, abstaining when unsure.

    Coordination is not the model's question. It was trained on one label,
    automated or not, so the coordination score comes from the heuristic
    scorer and the model_version says both engines are involved. A number
    reported under a model's name that the model did not produce is the
    kind of quiet lie this project exists to avoid.
    """

    model_version = 'rgcn-v1+heuristic-graph-v2'
    is_trained = True

    def __init__(self, min_confidence=None):
        self.min_confidence = (
            getattr(settings, 'SOCIAL_GRAPH_MIN_CONFIDENCE', 0.80)
            if min_confidence is None else min_confidence
        )
        self.coordination = HeuristicGraphScorer()

    def score_graph(self, accounts, edges) -> list[GraphScore]:
        torch, _ = ml_dependencies()

        nodes = list(accounts)
        if not nodes:
            return []

        model, bundle = load_artifact(str(artifact_path()))
        graph = build_graph(nodes, edges)
        x = standardise(graph.x, bundle['mean'], bundle['deviation'])

        with torch.no_grad():
            logits = model(x, graph.edge_index, graph.edge_type)
            probabilities = torch.softmax(logits, dim=1)

        # The heuristic answers the other half, on the same graph.
        coordination = {
            score.account_id: score
            for score in self.coordination.score_graph(nodes, edges)
        }

        results = []
        for index, node in enumerate(nodes):
            automated = float(probabilities[index][1])
            confidence = max(automated, 1.0 - automated)
            reasons: list[dict] = []

            if confidence < self.min_confidence:
                bot = None
                reasons.append(reason(
                    'The model would not commit',
                    f'It put this account at {automated:.0%} automated, which '
                    f'is inside the band where it is guessing. Below '
                    f'{self.min_confidence:.0%} either way, no likelihood is '
                    'recorded.',
                    'limits',
                ))
            else:
                bot = round(automated, 4)
                reasons.append(reason(
                    f'Graph model: {automated:.0%} automated',
                    'A relational graph network over this account, its '
                    'neighbours and the five kinds of edge between them. It '
                    'was trained on the accounts reviewed in this '
                    'installation and on nothing else, so it knows the '
                    'behaviour those reviewers saw and no more.',
                    'bot',
                ))

            heuristic = coordination.get(node.id)
            if heuristic is not None:
                # The coordination lines, kept verbatim and still labelled
                # with which engine produced them.
                reasons.extend(
                    line for line in heuristic.reasons
                    if line.get('effect') in ('coordination', 'limits')
                )

            results.append(GraphScore(
                account_id=node.id,
                bot_likelihood=bot,
                coordination_score=(heuristic.coordination_score
                                    if heuristic else None),
                model_version=self.model_version,
                reasons=reasons,
            ))
        return results
