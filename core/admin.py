from django.contrib import admin
from django.utils import timezone

from .models import Source


@admin.register(Source)
class SourceAdmin(admin.ModelAdmin):
    list_display = (
        'domain', 'name', 'publisher', 'domain_age', 'registry_status',
        'country', 'is_active',
    )
    list_filter = ('is_active', 'registry_status', 'country')
    search_fields = ('domain', 'name', 'publisher', 'registrar')
    date_hierarchy = 'created_at'
    readonly_fields = (
        'domain_registered_at', 'registrar', 'registry_status',
        'registry_checked_at',
    )

    @admin.display(description='Domain age', ordering='domain_registered_at')
    def domain_age(self, obj):
        """Real registration age, not how long this system has known the row."""
        if not obj.domain_registered_at:
            return '—'
        days = (timezone.now() - obj.domain_registered_at).days
        if days >= 365:
            return f'{days // 365}y'
        return f'{days}d'
