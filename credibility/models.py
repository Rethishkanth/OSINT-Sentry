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
