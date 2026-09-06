from django.conf import settings
from django.contrib.gis.db import models as gis_models
from django.db import models

from core.models import TimeStampedModel


class GeoEvent(TimeStampedModel):
    """A geolocated event extracted from a piece of content."""

    class Confidence(models.TextChoices):
        LOW = 'low', 'Low'
        MEDIUM = 'medium', 'Medium'
        HIGH = 'high', 'High'

    content_item = models.ForeignKey(
        'content.ContentItem',
        on_delete=models.CASCADE,
        related_name='geo_events',
    )
    location = gis_models.PointField(geography=True, srid=4326)
    place_name = models.CharField(max_length=255, blank=True)
    occurred_at = models.DateTimeField(null=True, blank=True)
    event_type = models.CharField(max_length=64, blank=True)
    confidence = models.CharField(
        max_length=8,
        choices=Confidence.choices,
        default=Confidence.LOW,
    )

    class Meta:
        ordering = ['-occurred_at', '-created_at']

    def __str__(self):
        return f'{self.place_name or "unnamed"} @ {self.location.coords}'


class Report(TimeStampedModel):
    """A generated analyst report bundling a set of geolocated events."""

    class Status(models.TextChoices):
        DRAFT = 'draft', 'Draft'
        IN_REVIEW = 'in_review', 'In review'
        PUBLISHED = 'published', 'Published'

    title = models.CharField(max_length=255)
    summary = models.TextField(blank=True)
    geo_events = models.ManyToManyField(GeoEvent, related_name='reports', blank=True)
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='reports',
    )
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.DRAFT,
        db_index=True,
    )
    pdf = models.FileField(upload_to='reports/%Y/%m/', blank=True)
    generated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return self.title
