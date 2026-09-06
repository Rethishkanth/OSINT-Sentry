from celery import chain
from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from core.models import Source

from .tasks import refresh_domain_registration, score_source


@receiver(post_save, sender=Source, dispatch_uid='credibility.autoscore_source')
def autoscore_source(sender, instance, created, **kwargs):
    """Every new Source gets looked up and scored without manual triggering.

    The registry lookup runs first so the very first score already knows the
    domain's real age; scoring immediately would otherwise record every new
    source as 'age unknown'. Queued on commit so the worker cannot read the
    row before the transaction that created it lands.
    """
    if not created:
        return

    pk = instance.pk
    transaction.on_commit(
        lambda: chain(
            refresh_domain_registration.si(pk),
            score_source.si(pk),
        ).delay()
    )
