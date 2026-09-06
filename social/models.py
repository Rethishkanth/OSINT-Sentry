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
    # Set by the coordinated-behaviour detector in a later phase.
    bot_likelihood = models.FloatField(null=True, blank=True)

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
