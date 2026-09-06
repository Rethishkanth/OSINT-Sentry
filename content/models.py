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
    # Populated by the NLP pipeline (content.tasks.analyze_content).
    summary = models.TextField(blank=True)
    sentiment = models.FloatField(null=True, blank=True)
    stance = models.CharField(max_length=32, blank=True)
    analyzer_version = models.CharField(max_length=64, blank=True)
    analyzed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-published_at', '-created_at']
        indexes = [models.Index(fields=['source', '-published_at'])]

    def __str__(self):
        return self.title or self.url


class Entity(models.Model):
    """A named entity found in a ContentItem by the NER step."""

    class Label(models.TextChoices):
        PERSON = 'PERSON', 'Person'
        ORG = 'ORG', 'Organisation'
        GPE = 'GPE', 'Place'
        EVENT = 'EVENT', 'Event'
        DATE = 'DATE', 'Date'
        OTHER = 'OTHER', 'Other'

    content_item = models.ForeignKey(
        ContentItem,
        on_delete=models.CASCADE,
        related_name='entities',
    )
    text = models.CharField(max_length=255)
    label = models.CharField(max_length=16, choices=Label.choices, default=Label.OTHER)
    confidence = models.FloatField(null=True, blank=True)
    mention_count = models.PositiveIntegerField(default=1)

    class Meta:
        ordering = ['-mention_count', 'text']
        constraints = [
            models.UniqueConstraint(
                fields=['content_item', 'text', 'label'],
                name='unique_entity_per_item',
            )
        ]
        indexes = [models.Index(fields=['label', 'text'])]

    def __str__(self):
        return f'{self.text} ({self.label})'


class Topic(models.Model):
    """A topic cluster. Shared across content items."""

    label = models.CharField(max_length=255, unique=True)
    keywords = models.JSONField(default=list, blank=True)

    class Meta:
        ordering = ['label']

    def __str__(self):
        return self.label


class ContentTopic(models.Model):
    """Membership of a ContentItem in a Topic, with the model's confidence."""

    content_item = models.ForeignKey(
        ContentItem,
        on_delete=models.CASCADE,
        related_name='content_topics',
    )
    topic = models.ForeignKey(Topic, on_delete=models.CASCADE, related_name='content_topics')
    weight = models.FloatField(default=0.0)

    class Meta:
        ordering = ['-weight']
        constraints = [
            models.UniqueConstraint(
                fields=['content_item', 'topic'],
                name='unique_topic_per_item',
            )
        ]

    def __str__(self):
        return f'{self.content_item} ~ {self.topic} ({self.weight:.2f})'
