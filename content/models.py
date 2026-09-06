from django.core.validators import MaxValueValidator, MinValueValidator
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


class Corroboration(TimeStampedModel):
    """One credibility check of a ContentItem against other outlets.

    Kept as a log rather than a field on ContentItem: a check made when a
    story broke and one made a day later are different findings, and the
    earlier one should not be silently overwritten by the later.
    """

    class Verdict(models.TextChoices):
        TRUSTWORTHY = 'trustworthy', 'Trustworthy'
        LIKELY_RELIABLE = 'likely_reliable', 'Likely reliable'
        PARTIALLY_CORROBORATED = 'partially_corroborated', 'Partially corroborated'
        UNCORROBORATED = 'uncorroborated', 'Uncorroborated'
        DISPUTED = 'disputed', 'Disputed by others'
        DO_NOT_TRUST = 'do_not_trust', 'Do not trust'
        UNAVAILABLE = 'unavailable', 'Could not check'

    content_item = models.ForeignKey(
        ContentItem,
        on_delete=models.CASCADE,
        related_name='corroborations',
    )
    # Null when the search itself failed. A rating is withheld rather than
    # guessed, because "we could not look" must not read as "nobody agrees".
    score = models.FloatField(
        null=True, blank=True,
        validators=[MinValueValidator(0), MaxValueValidator(10)],
        help_text='0-10, how well other outlets corroborate this story.',
    )
    verdict = models.CharField(
        max_length=32, choices=Verdict.choices, default=Verdict.UNAVAILABLE, db_index=True
    )
    keywords = models.JSONField(default=list, blank=True)
    rationale = models.TextField(blank=True)
    # The score's working, one entry per component: {label, detail, effect},
    # where effect is that component's signed contribution out of 10. Stored
    # structured rather than as prose so a rating can be audited — and so a
    # later engine version's reasoning can be compared against this one's.
    reasons = models.JSONField(default=list, blank=True)
    searched = models.CharField(max_length=255, blank=True)
    sources_checked = models.PositiveIntegerField(default=0)
    independent_sources = models.PositiveIntegerField(default=0)
    # Other outlets' articles fetched and parsed in full, rather than judged
    # from a headline. Claims can only be checked against these.
    outlets_read = models.PositiveIntegerField(default=0)

    # Checkable specifics the article states — figures, sums, dates — and how
    # they compared with what the outlets we read printed. See content.claims.
    claims = models.JSONField(default=list, blank=True)
    claims_checked = models.PositiveIntegerField(default=0)
    claims_agreed = models.PositiveIntegerField(default=0)
    claims_disputed = models.PositiveIntegerField(
        default=0,
        help_text='Details another outlet stated differently. Not a ruling on '
                  'which one is right.',
    )
    engine_version = models.CharField(max_length=64, blank=True)

    class Meta:
        # Newest first: the check you just ran is the one you want to read.
        ordering = ['-created_at']
        indexes = [models.Index(fields=['content_item', '-created_at'])]

    def __str__(self):
        rating = f'{self.score:.1f}/10' if self.score is not None else 'no rating'
        return f'{self.content_item}: {rating} ({self.verdict})'


class CorroborationMatch(models.Model):
    """One other outlet's coverage that matched, kept as the evidence."""

    report = models.ForeignKey(
        Corroboration, on_delete=models.CASCADE, related_name='matches'
    )
    source = models.ForeignKey(
        'core.Source', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='corroboration_matches',
        help_text='Set only when the matching outlet is one we already track.',
    )
    domain = models.CharField(max_length=255)
    url = models.URLField(max_length=1000)
    title = models.CharField(max_length=500, blank=True)
    similarity = models.FloatField(default=0.0)
    matched_terms = models.JSONField(default=list, blank=True)
    credibility = models.FloatField(null=True, blank=True)
    published_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-similarity']

    def __str__(self):
        return f'{self.domain} ({self.similarity:.0%})'


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
