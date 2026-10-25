"""The one definition of an account's feature vector.

Module 1 learned this the hard way: training built one row and serving built
another, five of eight columns were wrong at serving time, every test passed
because they only compared the keys, and the model's coverage fell by a
factor of seventeen without anything failing. The fix there was a single
module both paths import, and social/test_ml.py compares the two paths here
by value for the same reason.

The features are deliberately the shape of the graph rather than the contents
of a profile. Module 3 does not read what an account says about itself; a
display name, a biography and an avatar are what an operator controls, and a
classifier reading them learns what a bot writes in its biography this year.
Degree, reciprocity and burstiness are what an operator would have to change
their behaviour to change.

The R-GCN then adds what no per-account vector can carry: who the account's
neighbours are and how they look. That is the whole reason for the graph
network rather than a tree over these columns.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

# The edge types, pinned in this order. The R-GCN learns one weight matrix
# per relation and indexes them by position, so reordering this list silently
# relabels every relation in a trained artifact. Appending is safe; moving an
# entry is not, and the artifact records this list so a mismatch is caught
# rather than acted on.
RELATIONS = ('mention', 'reply', 'reshare', 'follow', 'comention')

# Column order for the feature vector. Pinned for the same reason.
FEATURE_NAMES = (
    'out_degree',
    'in_degree',
    'out_targets',
    'in_sources',
    'reciprocity',
    'repeat_rate',
    'post_count',
    'follower_log',
    'age_log',
    *(f'out_{relation}' for relation in RELATIONS),
    *(f'in_{relation}' for relation in RELATIONS),
)


def _log1p(value) -> float:
    """log(1+x), compressing a count whose tail runs to seven figures.

    Unknown becomes 0.0, the same value a new account with no followers
    gets. That collision is tolerable here and would not be in Module 1,
    where a missing registration date is the whole question: the graph
    columns beside this one separate the two cases, and the alternative is a
    NaN, which an R-GCN propagates to every neighbour.
    """
    if value is None or value < 0:
        return 0.0
    return round(math.log1p(float(value)), 6)


@dataclass
class AccountGraphFacts:
    """Everything the feature row needs, counted once per account.

    Built by the caller from the database or from a test fixture, so this
    module stays free of Django and the row can be built without one.
    """

    out_by_relation: dict[str, int] = field(default_factory=dict)
    in_by_relation: dict[str, int] = field(default_factory=dict)
    out_targets: int = 0
    in_sources: int = 0
    reciprocated_targets: int = 0
    post_count: int = 0
    follower_count: int | None = None
    age_days: int | None = None


def feature_row(facts: AccountGraphFacts) -> list[float]:
    """The account as FEATURE_NAMES describes it, in that order."""
    out_degree = sum(facts.out_by_relation.values())
    in_degree = sum(facts.in_by_relation.values())

    # How many of the accounts this one talked to talked back. The heuristic
    # scorer's central signal, kept because it is the one that survives an
    # operator buying followers.
    reciprocity = (facts.reciprocated_targets / facts.out_targets
                   if facts.out_targets else 0.0)

    # Edges per distinct target. An account that sent forty mentions to two
    # accounts is doing something different from one that sent forty to
    # forty, and out_degree alone cannot tell them apart.
    repeat_rate = out_degree / facts.out_targets if facts.out_targets else 0.0

    return [
        float(out_degree),
        float(in_degree),
        float(facts.out_targets),
        float(facts.in_sources),
        round(reciprocity, 6),
        round(repeat_rate, 6),
        float(facts.post_count),
        _log1p(facts.follower_count),
        _log1p(facts.age_days),
        *(float(facts.out_by_relation.get(r, 0)) for r in RELATIONS),
        *(float(facts.in_by_relation.get(r, 0)) for r in RELATIONS),
    ]
