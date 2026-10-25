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

    # Written by social.tasks.refresh_graph_scores. Null means the graph held
    # too little to judge on, which is a different state from a low score and
    # is shown as such: no rating is given rather than a bad one.
    bot_likelihood = models.FloatField(
        null=True,
        blank=True,
        help_text='0-1, probability the account is automated. Null = not enough graph.',
    )
    coordination_score = models.FloatField(
        null=True,
        blank=True,
        help_text='0-1, how much the account moves in lockstep with others.',
    )
    # Why those two numbers, as {label, detail, effect}, where effect is
    # 'bot', 'coordination', or 'limits' for a line saying what the graph
    # cannot support. Structured rather than prose so an old score's working
    # can be compared against a new scorer's.
    reasons = models.JSONField(default=list, blank=True)
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
    """A directed interaction between two accounts - an edge in the social graph."""

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

    # Written by social.tasks.score_post. Null sentiment means no word in the
    # post was in the lexicon at all - which is not the same as the post being
    # even-handed, and must not be displayed as 0.00.
    sentiment = models.FloatField(
        null=True,
        blank=True,
        help_text='-1 (negative) to 1 (positive). Null = the lexicon found nothing.',
    )
    toxicity = models.FloatField(
        null=True,
        blank=True,
        help_text='0-1, higher is more toxic',
    )
    # {label, detail, effect}, effect being 'sentiment', 'toxicity' or
    # 'limits'. Every post carries at least one line, including the ones that
    # scored zero.
    reasons = models.JSONField(default=list, blank=True)
    text_model_version = models.CharField(max_length=64, blank=True)
    scored_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-posted_at', '-created_at']
        indexes = [models.Index(fields=['account', '-posted_at'])]

    def __str__(self):
        return f'{self.account}: {self.text[:60]}'


class Campaign(TimeStampedModel):
    """A cluster of accounts that all interact with the same set of others.

    Written by social.tasks.detect_campaigns; never edited here. The membership
    *is* the identity - `fingerprint` hashes it - so re-running the detector
    over an unchanged graph updates this row instead of stacking a second copy
    of it, and an account joining makes a genuinely different cluster.

    Nothing in this model asserts intent. A cluster is a shape in the
    interaction graph, and `reasons` carries the line saying so on every row.
    """

    fingerprint = models.CharField(
        max_length=64,
        unique=True,
        help_text='Hash of the sorted member ids. The cluster is its membership.',
    )
    name = models.CharField(max_length=255)
    members = models.ManyToManyField(
        SocialAccount,
        related_name='campaigns',
        help_text='The accounts moving together.',
    )
    common_targets = models.ManyToManyField(
        SocialAccount,
        related_name='targeted_by_campaigns',
        blank=True,
        help_text='Accounts every member interacts with.',
    )
    size = models.PositiveIntegerField(default=0)
    shared_target_count = models.PositiveIntegerField(default=0)
    cohesion = models.FloatField(
        null=True,
        blank=True,
        help_text='0-1, mean overlap between every pair of members.',
    )
    reasons = models.JSONField(default=list, blank=True)
    detector_version = models.CharField(max_length=64, blank=True)
    # Stamped by every pass that finds this cluster again. A campaign whose
    # last_seen_at predates the newest run is not deleted: the graph changing
    # under it is a fact about the capture, and destroying the row would take
    # the evidence with it. The console marks it stale instead.
    last_seen_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-size', '-cohesion']
        indexes = [models.Index(fields=['-last_seen_at'])]

    def __str__(self):
        return self.name


class CampaignRun(TimeStampedModel):
    """One pass of the detector over the whole graph.

    Kept for the same reason reportgis.ExtractionRun is: the pass that found
    nothing is a finding. Without it, a graph with no campaigns in it cannot
    be told apart from a graph nobody has run the detector over, and "no
    coordinated activity" is exactly the conclusion that must never be
    reached by default.
    """

    campaigns_found = models.PositiveIntegerField(default=0)
    accounts_considered = models.PositiveIntegerField(
        default=0,
        help_text='Accounts with enough targets to be compared at all.',
    )
    accounts_total = models.PositiveIntegerField(default=0)
    # What the detector looked at and declined to report, as {label, detail,
    # effect}. This is the half of the output that stops an empty campaign
    # list from reading as an all-clear.
    notes = models.JSONField(default=list, blank=True)
    detector_version = models.CharField(max_length=64, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.created_at:%Y-%m-%d %H:%M}: {self.campaigns_found} found'
