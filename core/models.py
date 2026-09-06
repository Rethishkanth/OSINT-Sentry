from django.core.exceptions import ValidationError
from django.db import models

from .domains import InvalidDomain, normalize_domain


class TimeStampedModel(models.Model):
    """Adds created/updated bookkeeping to any model that inherits it."""

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class Source(TimeStampedModel):
    """A publisher or origin that content is attributed to."""

    name = models.CharField(
        max_length=255, blank=True,
        help_text='Display name. Defaults to the domain when left empty.',
    )
    domain = models.CharField(
        max_length=255, unique=True, db_index=True,
        help_text='A full URL is accepted and reduced to its domain.',
    )
    url = models.URLField(blank=True)
    publisher = models.CharField(max_length=255, blank=True)
    country = models.CharField(max_length=2, blank=True, help_text='ISO 3166-1 alpha-2')
    first_seen = models.DateTimeField(
        null=True, blank=True,
        help_text='When this system first saw the source. NOT the domain age.',
    )
    last_seen = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    # Cached registry (RDAP) facts. Populated by
    # credibility.tasks.refresh_domain_registration so that scoring never has
    # to make a network call.
    domain_registered_at = models.DateTimeField(
        null=True, blank=True,
        help_text="Domain's actual registration date, per the registry.",
    )
    registrar = models.CharField(max_length=255, blank=True)
    registry_status = models.CharField(
        max_length=16, blank=True,
        help_text='Outcome of the last registry lookup (ok, not_found, error…).',
    )
    registry_checked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['domain']

    def __str__(self):
        return self.domain

    def clean(self):
        """Normalise on the form path, so the admin reports bad input inline."""
        try:
            self.domain = normalize_domain(self.domain)
        except InvalidDomain as exc:
            raise ValidationError({'domain': str(exc)}) from exc

    def save(self, *args, **kwargs):
        """Normalise on every write, not just the form path.

        `domain` is unique and is the key every registry lookup is made
        against, so it cannot be allowed to hold a full URL just because the
        row was created from a shell or a fixture. Idempotent: normalising an
        already-normalised domain is a no-op.
        """
        self.domain = normalize_domain(self.domain)
        if not self.name:
            self.name = self.domain
        super().save(*args, **kwargs)
