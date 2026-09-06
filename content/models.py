from django.db import models

from core.models import TimeStampedModel


class ContentItem(TimeStampedModel):
    """A single scraped article, post, or document awaiting analysis."""

    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        PROCESSING = 'processing', 'Processing'
        ANALYZED = 'analyzed', 'Analyzed'
        FAILED = 'failed', 'Failed'

    source = models.ForeignKey(
        'core.Source',
        on_delete=models.CASCADE,
        related_name='content_items',
    )
    url = models.URLField(max_length=1000, unique=True)
    title = models.CharField(max_length=500, blank=True)
    body = models.TextField(blank=True)
    author = models.CharField(max_length=255, blank=True)
    language = models.CharField(max_length=8, blank=True)
    published_at = models.DateTimeField(null=True, blank=True)
    fetched_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )
    # Populated by the NLP pipeline in a later phase.
    sentiment = models.FloatField(null=True, blank=True)
    stance = models.CharField(max_length=32, blank=True)

    class Meta:
        ordering = ['-published_at', '-created_at']
        indexes = [models.Index(fields=['source', '-published_at'])]

    def __str__(self):
        return self.title or self.url
