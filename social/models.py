from django.db import models

from core.models import TimeStampedModel


class SocialAccount(TimeStampedModel):
    """An account observed on a social platform."""

    class Platform(models.TextChoices):
        X = 'x', 'X/Twitter'
        REDDIT = 'reddit', 'Reddit'
        TELEGRAM = 'telegram', 'Telegram'
        FACEBOOK = 'facebook', 'Facebook'
        OTHER = 'other', 'Other'

    handle = models.CharField(max_length=255)
    platform = models.CharField(max_length=16, choices=Platform.choices)
    display_name = models.CharField(max_length=255, blank=True)
    external_id = models.CharField(max_length=128, blank=True, db_index=True)
    follower_count = models.PositiveIntegerField(null=True, blank=True)
    account_created_at = models.DateTimeField(null=True, blank=True)

    # Written by social.tasks.refresh_graph_scores.
    bot_likelihood = models.FloatField(
        null=True,
        blank=True,
        help_text='0-1, probability the account is automated',
    )
    coordination_score = models.FloatField(
        null=True,
        blank=True,
        help_text='0-1, how much the account moves in lockstep with others',
    )
    graph_model_version = models.CharField(max_length=64, blank=True)
    scored_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['platform', 'handle']
        constraints = [
            models.UniqueConstraint(
                fields=['platform', 'handle'],
                name='unique_account_per_platform',
            )
        ]

    def __str__(self):
        return f'{self.get_platform_display()}:@{self.handle}'


class InteractionEdge(TimeStampedModel):
    """A directed interaction between two accounts — an edge in the social graph."""

    class EdgeType(models.TextChoices):
        MENTION = 'mention', 'Mention'
        REPLY = 'reply', 'Reply'
        RESHARE = 'reshare', 'Reshare'
        FOLLOW = 'follow', 'Follow'
        COMENTION = 'comention', 'Co-mention'

    from_account = models.ForeignKey(
        SocialAccount,
        on_delete=models.CASCADE,
        related_name='outgoing_edges',
    )
    to_account = models.ForeignKey(
        SocialAccount,
        on_delete=models.CASCADE,
        related_name='incoming_edges',
    )
    edge_type = models.CharField(max_length=16, choices=EdgeType.choices)
    weight = models.FloatField(default=1.0)
    observed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-weight']
        indexes = [
            models.Index(fields=['from_account', 'edge_type']),
            models.Index(fields=['to_account', 'edge_type']),
        ]

    def __str__(self):
        return f'{self.from_account} -{self.edge_type}-> {self.to_account}'


class SocialPost(TimeStampedModel):
    """A post authored by a tracked account, with per-post text scoring."""

    account = models.ForeignKey(
        SocialAccount,
        on_delete=models.CASCADE,
        related_name='posts',
    )
    external_id = models.CharField(max_length=128, blank=True, db_index=True)
    url = models.URLField(max_length=1000, blank=True)
    text = models.TextField()
    posted_at = models.DateTimeField(null=True, blank=True)
    reshare_count = models.PositiveIntegerField(null=True, blank=True)

    # Written by social.tasks.score_post.
    sentiment = models.FloatField(
        null=True,
        blank=True,
        help_text='-1 (negative) to 1 (positive)',
    )
    toxicity = models.FloatField(
        null=True,
        blank=True,
        help_text='0-1, higher is more toxic',
    )
    text_model_version = models.CharField(max_length=64, blank=True)
    scored_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-posted_at', '-created_at']
        indexes = [models.Index(fields=['account', '-posted_at'])]

    def __str__(self):
        return f'{self.account}: {self.text[:60]}'
