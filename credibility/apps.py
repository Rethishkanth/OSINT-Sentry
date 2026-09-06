from django.apps import AppConfig


class CredibilityConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'credibility'

    def ready(self):
        from . import signals  # noqa: F401
