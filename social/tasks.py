import logging

from celery import shared_task
from django.core.exceptions import ObjectDoesNotExist
from django.utils import timezone

from .analysis import get_graph_scorer, get_text_scorer
from .models import InteractionEdge, SocialAccount, SocialPost

logger = logging.getLogger(__name__)


@shared_task
def refresh_graph_scores():
    """Rebuild the interaction graph and rescore every account.

    Loads ids only — the graph is what matters here, not the row contents —
    and writes results back in one bulk update.
    """
    account_ids = list(SocialAccount.objects.values_list('pk', flat=True))
    if not account_ids:
        return 0

    edges = list(
        InteractionEdge.objects.values_list('from_account_id', 'to_account_id')
    )
    scores = get_graph_scorer().score_graph(account_ids, edges)
    by_id = {s.account_id: s for s in scores}

    now = timezone.now()
    accounts = list(SocialAccount.objects.filter(pk__in=by_id))
    for account in accounts:
        result = by_id[account.pk]
        account.bot_likelihood = result.bot_likelihood
        account.coordination_score = result.coordination_score
        account.graph_model_version = result.model_version
        account.scored_at = now

    SocialAccount.objects.bulk_update(
        accounts,
        ['bot_likelihood', 'coordination_score', 'graph_model_version', 'scored_at'],
        batch_size=500,
    )
    logger.info('Refreshed graph scores for %d account(s)', len(accounts))
    return len(accounts)


@shared_task
def score_post(post_id):
    """Sentiment and toxicity for one post."""
    try:
        post = SocialPost.objects.get(pk=post_id)
    except ObjectDoesNotExist:
        logger.warning('score_post: SocialPost %s no longer exists', post_id)
        return None

    result = get_text_scorer().score_text(post.text)
    post.sentiment = result.sentiment
    post.toxicity = result.toxicity
    post.text_model_version = result.model_version
    post.scored_at = timezone.now()
    post.save(update_fields=[
        'sentiment', 'toxicity', 'text_model_version', 'scored_at', 'updated_at',
    ])
    return post.pk


@shared_task
def score_unscored_posts(limit=500):
    """Catch-up sweep for posts the scorer has not seen yet."""
    ids = list(
        SocialPost.objects.filter(scored_at__isnull=True)
        .values_list('pk', flat=True)[:limit]
    )
    for post_id in ids:
        score_post.delay(post_id)
    return len(ids)
