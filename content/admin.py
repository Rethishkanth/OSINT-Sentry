from django.contrib import admin

from .models import (
    ContentItem,
    ContentTopic,
    Corroboration,
    CorroborationMatch,
    Entity,
    FactCheckMatch,
    Topic,
)


class EntityInline(admin.TabularInline):
    model = Entity
    extra = 0
    fields = ('text', 'label', 'mention_count', 'confidence')


class ContentTopicInline(admin.TabularInline):
    model = ContentTopic
    extra = 0
    autocomplete_fields = ('topic',)


@admin.register(ContentItem)
class ContentItemAdmin(admin.ModelAdmin):
    list_display = ('title', 'source', 'status', 'language', 'analyzer_version', 'published_at')
    list_filter = ('status', 'language', 'analyzer_version', 'source')
    search_fields = ('title', 'url', 'author', 'body')
    autocomplete_fields = ('source',)
    date_hierarchy = 'published_at'
    readonly_fields = ('summary', 'sentiment', 'analyzer_version', 'analyzed_at')
    inlines = (EntityInline, ContentTopicInline)


@admin.register(Entity)
class EntityAdmin(admin.ModelAdmin):
    list_display = ('text', 'label', 'mention_count', 'content_item')
    list_filter = ('label',)
    search_fields = ('text',)
    autocomplete_fields = ('content_item',)


@admin.register(Topic)
class TopicAdmin(admin.ModelAdmin):
    list_display = ('label',)
    search_fields = ('label',)


class CorroborationMatchInline(admin.TabularInline):
    model = CorroborationMatch
    extra = 0
    fields = ('domain', 'title', 'similarity', 'credibility', 'url', 'published_at')
    readonly_fields = fields


class FactCheckMatchInline(admin.TabularInline):
    """Published fact-checks found alongside the check.

    `rating` shows the fact-checker's own words and `verdict` our bucket for
    them, side by side, so a bucketing mistake is visible rather than hidden
    behind a tidy label.
    """

    model = FactCheckMatch
    extra = 0
    fields = ('publisher', 'rating', 'verdict', 'close_match', 'match_strength',
              'claim', 'url')
    readonly_fields = fields


@admin.register(Corroboration)
class CorroborationAdmin(admin.ModelAdmin):
    list_display = (
        'content_item', 'rating', 'verdict', 'independent_sources',
        'fact_check_status', 'fact_checks_negative', 'engine_version', 'created_at',
    )
    list_filter = ('verdict', 'fact_check_status', 'engine_version')
    search_fields = ('content_item__title', 'content_item__source__domain', 'rationale')
    autocomplete_fields = ('content_item',)
    date_hierarchy = 'created_at'
    # Everything here is computed evidence; editing it by hand would make the
    # rationale a lie about how the score was reached.
    readonly_fields = (
        'score', 'verdict', 'keywords', 'rationale', 'searched',
        'sources_checked', 'independent_sources', 'fact_check_status',
        'fact_check_detail', 'fact_checks_found', 'fact_checks_negative',
        'engine_version',
    )
    inlines = (CorroborationMatchInline, FactCheckMatchInline)

    @admin.display(description='Rating', ordering='score')
    def rating(self, obj):
        return f'{obj.score:.1f}/10' if obj.score is not None else '—'
