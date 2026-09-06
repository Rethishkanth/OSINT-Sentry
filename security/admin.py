from django.contrib import admin

from .models import AuditLogEntry


@admin.register(AuditLogEntry)
class AuditLogEntryAdmin(admin.ModelAdmin):
    list_display = ('timestamp', 'actor', 'action', 'target_model', 'target_object_id')
    list_filter = ('action', 'target_model')
    search_fields = ('actor__username', 'target_model', 'target_object_id')
    date_hierarchy = 'timestamp'
    # The audit log is append-only; it is inspected here, never edited.
    readonly_fields = tuple(f.name for f in AuditLogEntry._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
