from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models

from core.models import TimeStampedModel


class GeoEvent(TimeStampedModel):
    """A geolocated event extracted from a piece of content.

    This started life as a PostGIS PointField, which kept the whole module out
    of INSTALLED_APPS: GeoDjango will not import without GDAL and GEOS on the
    machine, so a missing pair of DLLs took module 05 offline entirely.

    It is two plain floats now. What was actually lost: spatial indexes, and
    the ability to write `location__distance_lte=...` in a queryset. What was
    gained: the module runs everywhere the rest of the project runs, on SQLite,
    with no system packages. Radius search is done with a haversine in
    reportgis.geocode, which at this scale costs nothing — see the note there.

    The way back is open and cheap: add a PointField alongside these columns in
    a migration, backfill from them, and the rest of the module keeps working
    while the queries move over.
    """

    class Confidence(models.TextChoices):
        LOW = 'low', 'Low'
        MEDIUM = 'medium', 'Medium'
        HIGH = 'high', 'High'

    class Method(models.TextChoices):
        COORDINATES = 'coordinates', 'Coordinates stated in the text'
        GAZETTEER = 'gazetteer', 'Place name matched against the gazetteer'
        MANUAL = 'manual', 'Placed by an analyst'

    # Null for a location an analyst pinned by hand, which has no article
    # behind it.
    content_item = models.ForeignKey(
        'content.ContentItem',
        on_delete=models.CASCADE,
        related_name='geo_events',
        null=True,
        blank=True,
    )
    latitude = models.FloatField(
        validators=[MinValueValidator(-90), MaxValueValidator(90)]
    )
    longitude = models.FloatField(
        validators=[MinValueValidator(-180), MaxValueValidator(180)]
    )
    place_name = models.CharField(max_length=255, blank=True)
    country = models.CharField(max_length=128, blank=True)
    region = models.CharField(max_length=128, blank=True)
    occurred_at = models.DateTimeField(null=True, blank=True)
    event_type = models.CharField(max_length=64, blank=True)
    confidence = models.CharField(
        max_length=8,
        choices=Confidence.choices,
        default=Confidence.LOW,
        db_index=True,
    )
    method = models.CharField(
        max_length=16,
        choices=Method.choices,
        default=Method.GAZETTEER,
    )
    # The span of source text this came from. An analyst who distrusts a pin
    # can find the sentence that produced it instead of taking it on faith.
    matched_text = models.CharField(max_length=255, blank=True)
    mentions = models.PositiveIntegerField(default=1)
    # Why this location, in the platform's usual shape: {label, effect}, where
    # effect is 'located' or 'limits'. Stored structured rather than as prose
    # so the reasoning behind an old pin can be compared with a new engine's.
    reasons = models.JSONField(default=list, blank=True)
    engine_version = models.CharField(max_length=64, blank=True)

    class Meta:
        ordering = ['-occurred_at', '-created_at']
        indexes = [
            models.Index(fields=['latitude', 'longitude']),
            models.Index(fields=['content_item', '-created_at']),
        ]
        constraints = [
            # Re-running extraction over an article updates its pins instead of
            # stacking a second copy of every one.
            models.UniqueConstraint(
                fields=['content_item', 'place_name'],
                name='unique_place_per_content_item',
            ),
        ]

    def __str__(self):
        name = self.place_name or 'unnamed'
        return f'{name} @ {self.latitude:.4f}, {self.longitude:.4f}'

    @property
    def coordinates(self) -> list[float]:
        """GeoJSON order: longitude first. The most common bug in this area."""
        return [self.longitude, self.latitude]


class ExtractionRun(TimeStampedModel):
    """One pass of the geocoder over one content item.

    Kept as a log for the same reason content.Corroboration is: the run that
    found nothing is a finding. Without it, an article with no pins is
    indistinguishable from an article nobody has looked at, and an analyst
    cannot tell "no locations in this text" from "not processed yet".
    """

    content_item = models.ForeignKey(
        'content.ContentItem',
        on_delete=models.CASCADE,
        related_name='geo_extractions',
    )
    events_found = models.PositiveIntegerField(default=0)
    # Names the geocoder saw and refused to place, with the reason. This is
    # the half of the output that stops the map from looking complete.
    notes = models.JSONField(default=list, blank=True)
    engine_version = models.CharField(max_length=64, blank=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [models.Index(fields=['content_item', '-created_at'])]

    def __str__(self):
        return f'{self.content_item}: {self.events_found} located'


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
