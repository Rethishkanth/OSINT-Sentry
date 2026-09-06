from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models

from core.models import TimeStampedModel


class TrustTier(models.TextChoices):
    UNKNOWN = 'unknown', 'Unknown'
    LOW = 'low', 'Low'
    MEDIUM = 'medium', 'Medium'
    HIGH = 'high', 'High'
    VERIFIED = 'verified', 'Verified'


class CredibilityScore(TimeStampedModel):
    """A point-in-time credibility assessment of a Source."""

    source = models.ForeignKey(
        'core.Source',
        on_delete=models.CASCADE,
        related_name='credibility_scores',
    )
    credibility_score = models.FloatField(
        validators=[MinValueValidator(0), MaxValueValidator(100)],
        help_text='0-100, higher is more credible',
    )
    trust_tier = models.CharField(
        max_length=16,
        choices=TrustTier.choices,
        default=TrustTier.UNKNOWN,
    )
    # Hash of the inputs that produced this score, so a score can be traced
    # back to the exact evidence it was computed from.
    provenance_hash = models.CharField(max_length=64, blank=True, db_index=True)
    model_version = models.CharField(max_length=64, blank=True)
    rationale = models.TextField(blank=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [models.Index(fields=['source', '-created_at'])]

    def __str__(self):
        return f'{self.source.domain}: {self.credibility_score:.1f} ({self.trust_tier})'


class SiteInspection(TimeStampedModel):
    """What a site actually serves, rated out of 10.

    Kept separate from CredibilityScore rather than folded into it. The two
    answer different questions — "is this publisher reliable" versus "is this
    site safe to open" — and a reputable outlet buried in advertising should
    show both facts, not one number that splits the difference.

    A log rather than a field on Source: sites change, and last month's
    inspection is evidence about last month.
    """

    class Rating(models.TextChoices):
        SAFE = 'safe', 'Safe'
        MOSTLY_SAFE = 'mostly_safe', 'Mostly safe'
        SOME_CONCERNS = 'some_concerns', 'Some concerns'
        CAUTION = 'caution', 'Caution'
        UNSAFE = 'unsafe', 'Unsafe'
        UNKNOWN = 'unknown', 'Could not check'

    source = models.ForeignKey(
        'core.Source',
        on_delete=models.CASCADE,
        related_name='inspections',
    )
    # Null when the site did not answer. Unreachable is not unsafe, so no
    # rating is given rather than a bad one.
    score = models.FloatField(
        null=True, blank=True,
        validators=[MinValueValidator(0), MaxValueValidator(10)],
        help_text='0-10: how safe and how uncluttered the site is.',
    )
    rating = models.CharField(
        max_length=16, choices=Rating.choices, default=Rating.UNKNOWN, db_index=True
    )
    summary = models.TextField(blank=True, help_text='Plain-language clutter summary.')
    # Every deduction and credit, so the number can be argued with.
    reasons = models.JSONField(default=list, blank=True)
    findings = models.JSONField(default=dict, blank=True)
    checked_url = models.URLField(max_length=1000, blank=True)

    # Denormalised for listing and filtering without unpacking the JSON.
    ad_networks = models.PositiveIntegerField(default=0)
    trackers = models.PositiveIntegerField(default=0)
    uses_https = models.BooleanField(default=False)
    engine_version = models.CharField(max_length=64, blank=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [models.Index(fields=['source', '-created_at'])]

    def __str__(self):
        rating = f'{self.score:.1f}/10' if self.score is not None else 'no rating'
        return f'{self.source.domain}: {rating} ({self.rating})'
