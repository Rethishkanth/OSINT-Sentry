"""Signal-based audit logging across every tracked model.

Wired up before the analysis modules start writing data, so there is no window
where changes go unrecorded. Receivers are registered dynamically for every
concrete model outside the exclusion list below, which means a new app is
covered the moment it is added - no per-model bookkeeping to forget.
"""

import logging

from django.apps import apps
from django.db.models.signals import post_delete, post_save, pre_save
from django.forms.models import model_to_dict

from .middleware import get_current_actor, get_current_ip
from .models import AuditLogEntry

logger = logging.getLogger(__name__)

# Apps whose writes are noise, machinery, or would recurse into the log itself.
EXCLUDED_APPS = {
    'security',            # writing the log must not log the log
    'admin',               # django's own LogEntry already covers this
    'sessions',
    'contenttypes',
    'migrations',
    'django_celery_results',
    'django_celery_beat',
}

# Never copy these into the log, whatever model they appear on.
SENSITIVE_FIELDS = {'password', 'token', 'secret', 'api_key', 'access', 'refresh'}

MAX_VALUE_LENGTH = 500


def _serialize(instance):
    """Field values as JSON-safe primitives, with secrets stripped."""
    data = {}
    for key, value in model_to_dict(instance).items():
        if any(marker in key.lower() for marker in SENSITIVE_FIELDS):
            data[key] = '[redacted]'
            continue
        if value is None or isinstance(value, (bool, int, float)):
            data[key] = value
        else:
            text = str(value)
            data[key] = text[:MAX_VALUE_LENGTH] + '…' if len(text) > MAX_VALUE_LENGTH else text
    return data


def _diff(before, after):
    """Only the fields that actually changed."""
    return {
        key: {'from': before.get(key), 'to': after.get(key)}
        for key in after
        if before.get(key) != after.get(key)
    }


def _write(instance, action, changes):
    AuditLogEntry.objects.create(
        actor=get_current_actor(),
        action=action,
        target_model=f'{instance._meta.app_label}.{instance._meta.model_name}',
        target_object_id=str(instance.pk),
        changes=changes,
        ip_address=get_current_ip(),
    )


def capture_pre_save(sender, instance, **kwargs):
    """Snapshot the stored row so post_save can diff against it."""
    if instance.pk is None:
        instance._audit_before = None
        return
    try:
        instance._audit_before = _serialize(sender.objects.get(pk=instance.pk))
    except sender.DoesNotExist:
        instance._audit_before = None


def log_save(sender, instance, created, **kwargs):
    try:
        after = _serialize(instance)
        before = getattr(instance, '_audit_before', None)
        if created:
            changes = after
        else:
            changes = _diff(before or {}, after)
            if not changes:
                # A save that changed nothing is not worth a log entry.
                return
        _write(instance, AuditLogEntry.Action.CREATE if created else AuditLogEntry.Action.UPDATE, changes)
    except Exception:
        # Audit logging must never be the reason a write fails.
        logger.exception('Audit log failed for %s save', sender.__name__)


def log_delete(sender, instance, **kwargs):
    try:
        _write(instance, AuditLogEntry.Action.DELETE, _serialize(instance))
    except Exception:
        logger.exception('Audit log failed for %s delete', sender.__name__)


def audited_models():
    for model in apps.get_models():
        if model._meta.app_label in EXCLUDED_APPS:
            continue
        if model._meta.proxy or model._meta.abstract:
            continue
        yield model


def register():
    """Attach the receivers. Called once from SecurityConfig.ready()."""
    for model in audited_models():
        uid = f'audit.{model._meta.label_lower}'
        pre_save.connect(capture_pre_save, sender=model, dispatch_uid=f'{uid}.pre')
        post_save.connect(log_save, sender=model, dispatch_uid=f'{uid}.save')
        post_delete.connect(log_delete, sender=model, dispatch_uid=f'{uid}.delete')
