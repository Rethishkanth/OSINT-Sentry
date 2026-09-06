from django.contrib import admin

from .models import InteractionEdge, SocialAccount


@admin.register(SocialAccount)
class SocialAccountAdmin(admin.ModelAdmin):
    list_display = ('handle', 'platform', 'display_name', 'follower_count', 'bot_likelihood')
    list_filter = ('platform',)
    search_fields = ('handle', 'display_name', 'external_id')


@admin.register(InteractionEdge)
class InteractionEdgeAdmin(admin.ModelAdmin):
    list_display = ('from_account', 'edge_type', 'to_account', 'weight', 'observed_at')
    list_filter = ('edge_type',)
    search_fields = ('from_account__handle', 'to_account__handle')
    autocomplete_fields = ('from_account', 'to_account')
