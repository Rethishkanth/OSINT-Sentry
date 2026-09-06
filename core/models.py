from django.db import models


class TimeStampedModel(models.Model):
    """Adds created/updated bookkeeping to any model that inherits it."""

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class Source(TimeStampedModel):
    """A publisher or origin that content is attributed to."""

    name = models.CharField(max_length=255)
    domain = models.CharField(max_length=255, unique=True, db_index=True)
    url = models.URLField(blank=True)
    publisher = models.CharField(max_length=255, blank=True)
    country = models.CharField(max_length=2, blank=True, help_text='ISO 3166-1 alpha-2')
    first_seen = models.DateTimeField(null=True, blank=True)
    last_seen = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['domain']

    def __str__(self):
        return self.domain
