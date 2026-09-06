from django.contrib import admin

from .models import Source


@admin.register(Source)
class SourceAdmin(admin.ModelAdmin):
    list_display = ('domain', 'name', 'publisher', 'country', 'is_active', 'last_seen')
    list_filter = ('is_active', 'country')
    search_fields = ('domain', 'name', 'publisher')
    date_hierarchy = 'created_at'
