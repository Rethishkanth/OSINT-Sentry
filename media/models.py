from django.db import models

from core.models import TimeStampedModel


class MediaItem(TimeStampedModel):
    """An image or video pulled in for forensic analysis."""

    class Kind(models.TextChoices):
        IMAGE = 'image', 'Image'
        VIDEO = 'video', 'Video'
        AUDIO = 'audio', 'Audio'

    content_item = models.ForeignKey(
        'content.ContentItem',
        on_delete=models.CASCADE,
        related_name='media_items',
        null=True,
        blank=True,
    )
    file = models.FileField(upload_to='media_items/%Y/%m/')
    kind = models.CharField(max_length=8, choices=Kind.choices, default=Kind.IMAGE)
    source_url = models.URLField(max_length=1000, blank=True)
    # sha256 of the file bytes — used to dedupe and to tie results to exact content.
    sha256 = models.CharField(max_length=64, blank=True, db_index=True)
    width = models.PositiveIntegerField(null=True, blank=True)
    height = models.PositiveIntegerField(null=True, blank=True)
    captured_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.get_kind_display()} #{self.pk}'


class TamperingResult(TimeStampedModel):
    """Output of the forensics pipeline for one MediaItem."""

    class Verdict(models.TextChoices):
        AUTHENTIC = 'authentic', 'Authentic'
        SUSPICIOUS = 'suspicious', 'Suspicious'
        MANIPULATED = 'manipulated', 'Manipulated'
        INCONCLUSIVE = 'inconclusive', 'Inconclusive'

    media_item = models.ForeignKey(
        MediaItem,
        on_delete=models.CASCADE,
        related_name='tampering_results',
    )
    ela_score = models.FloatField(
        null=True,
        blank=True,
        help_text='Error Level Analysis score',
    )
    deepfake_score = models.FloatField(
        null=True,
        blank=True,
        help_text='0-1, probability the media is synthetic',
    )
    verdict = models.CharField(
        max_length=16,
        choices=Verdict.choices,
        default=Verdict.INCONCLUSIVE,
    )
    model_version = models.CharField(max_length=64, blank=True)
    details = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.media_item} -> {self.verdict}'
