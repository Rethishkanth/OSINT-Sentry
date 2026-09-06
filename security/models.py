from django.conf import settings
from django.db import models


class AuditLogEntry(models.Model):
    """Append-only record of who changed what, for chain-of-custody."""

    class Action(models.TextChoices):
        CREATE = 'create', 'Create'
        UPDATE = 'update', 'Update'
        DELETE = 'delete', 'Delete'
        VIEW = 'view', 'View'
        EXPORT = 'export', 'Export'
        LOGIN = 'login', 'Login'

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='audit_entries',
    )
    action = models.CharField(max_length=16, choices=Action.choices, db_index=True)
    target_model = models.CharField(max_length=128, blank=True, db_index=True)
    target_object_id = models.CharField(max_length=64, blank=True)
    changes = models.JSONField(default=dict, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    timestamp = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-timestamp']
        verbose_name_plural = 'audit log entries'

    def __str__(self):
        return f'{self.timestamp:%Y-%m-%d %H:%M} {self.actor} {self.action} {self.target_model}'
