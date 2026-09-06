from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from core.models import Source

from .tasks import score_source


@receiver(post_save, sender=Source, dispatch_uid='credibility.autoscore_source')
def autoscore_source(sender, instance, created, **kwargs):
    """Every new Source gets scored without anyone triggering it by hand.

    Queued on commit so the worker cannot read the row before the transaction
    that created it lands.
    """
    if not created:
        return
    transaction.on_commit(lambda: score_source.delay(instance.pk))
