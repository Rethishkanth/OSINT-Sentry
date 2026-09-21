import logging

from celery import shared_task
from django.core.exceptions import ObjectDoesNotExist
from django.utils import timezone

from .analysis import AccountNode, get_graph_scorer, get_text_scorer
from .campaigns import get_campaign_detector
from .models import (
    Campaign,
    CampaignRun,
    InteractionEdge,
    SocialAccount,
    SocialPost,
)

logger = logging.getLogger(__name__)


@shared_task
def refresh_graph_scores():
    """Rebuild the interaction graph and rescore every account.

    Loads ids only — the graph is what matters here, not the row contents —
    and writes results back in one bulk update.
    """
    nodes = [
        AccountNode(id=pk, handle=handle)
        for pk, handle in SocialAccount.objects.values_list('pk', 'handle')
    ]
    if not nodes:
        return 0

    edges = list(
        InteractionEdge.objects.values_list('from_account_id', 'to_account_id')
    )
    scores = get_graph_scorer().score_graph(nodes, edges)
    by_id = {s.account_id: s for s in scores}

    now = timezone.now()
    accounts = list(SocialAccount.objects.filter(pk__in=by_id))
    for account in accounts:
        result = by_id[account.pk]
        account.bot_likelihood = result.bot_likelihood
        account.coordination_score = result.coordination_score
        account.reasons = result.reasons
        account.graph_model_version = result.model_version
        account.scored_at = now

    SocialAccount.objects.bulk_update(
        accounts,
        [
            'bot_likelihood', 'coordination_score', 'reasons',
            'graph_model_version', 'scored_at',
        ],
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
    post.reasons = result.reasons
    post.text_model_version = result.model_version
    post.scored_at = timezone.now()
    post.save(update_fields=[
        'sentiment', 'toxicity', 'reasons', 'text_model_version', 'scored_at',
        'updated_at',
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


@shared_task
def detect_campaigns():
    """Group the whole graph into clusters and log the pass.

    Always writes a CampaignRun, including when it finds nothing. A campaign
    list is read as a list of what is out there, so the run that came back
    empty has to be on the record beside it — otherwise "no campaigns" and
    "nobody has looked" are the same screen.

    Clusters that were found before and are not found now are left in place
    with their old last_seen_at. Deleting them would quietly rewrite what the
    graph looked like last week.
    """
    nodes = [
        AccountNode(id=pk, handle=handle)
        for pk, handle in SocialAccount.objects.values_list('pk', 'handle')
    ]
    edges = list(
        InteractionEdge.objects.values_list('from_account_id', 'to_account_id')
    )
    result = get_campaign_detector().detect(nodes, edges)

    # The run row is written first so that the clusters it found can be
    # stamped with its own timestamp. Stamping them with a separately-taken
    # now() puts them microseconds *before* the run, and every cluster then
    # reads as one the latest pass failed to find again — which the console
    # duly reported, about clusters it had just that moment detected.
    run = CampaignRun.objects.create(
        campaigns_found=len(result.campaigns),
        accounts_considered=result.accounts_considered,
        accounts_total=len(nodes),
        notes=result.notes,
        detector_version=result.detector_version,
    )

    for found in result.campaigns:
        campaign, _ = Campaign.objects.update_or_create(
            fingerprint=found.fingerprint,
            defaults={
                'name': found.name,
                'size': len(found.account_ids),
                'shared_target_count': len(found.common_target_ids),
                'cohesion': found.cohesion,
                'reasons': found.reasons,
                'detector_version': result.detector_version,
                'last_seen_at': run.created_at,
            },
        )
        campaign.members.set(found.account_ids)
        campaign.common_targets.set(found.common_target_ids)
    logger.info(
        'Campaign pass: %d found across %d comparable account(s)',
        run.campaigns_found, run.accounts_considered,
    )
    return run.pk
