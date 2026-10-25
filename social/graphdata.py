"""Reading the account graph out of the database, once.

Both the scoring task and the graph model's training command need the same
graph, and the R-GCN is only as good as the agreement between them: it is
trained on one reading of the graph and serves on another, and a column that
means something different between the two is the bug Module 1 shipped and
spent a long time finding. So there is one function, and both call it.
"""

from __future__ import annotations

from django.db.models import Count
from django.utils import timezone

from .analysis import AccountNode
from .models import InteractionEdge, SocialAccount


def load_graph(queryset=None):
    """Every account as an AccountNode, and every edge as a typed triple.

    Counts and dates only. No scorer reads what an account says about
    itself: a display name and a biography are what an operator writes, and
    a classifier reading them learns this year's bot biography rather than
    this year's bot behaviour.
    """
    now = timezone.now()
    accounts = (queryset if queryset is not None else SocialAccount.objects.all())

    nodes = [
        AccountNode(
            id=row['pk'],
            handle=row['handle'],
            follower_count=row['follower_count'],
            age_days=(
                (now - row['account_created_at']).days
                if row['account_created_at'] else None
            ),
            post_count=row['post_count'],
        )
        for row in accounts.annotate(post_count=Count('posts')).values(
            'pk', 'handle', 'follower_count', 'account_created_at', 'post_count'
        )
    ]

    edges = list(
        InteractionEdge.objects.values_list(
            'from_account_id', 'to_account_id', 'edge_type'
        )
    )
    return nodes, edges
