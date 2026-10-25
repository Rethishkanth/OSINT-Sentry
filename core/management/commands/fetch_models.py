"""Download the trained engines' weights ahead of time.

Every ML engine in this project fetches its weights the first time something
asks for them, which means the first article analysed after a deploy waits
several hundred megabytes for its answer, and a worker with no outbound
network fails at the moment an analyst is watching rather than at the moment
an operator is.

This moves both of those to a deliberate step:

    manage.py fetch_models
    manage.py fetch_models --module content
    manage.py fetch_models --check

It writes into CONTENT_MODEL_CACHE, which is gitignored. Re-running it is
cheap: the hub cache is content-addressed, so an unchanged model is verified
rather than downloaded again. `--check` reports what is already on disk
without reaching the network.

Nothing here downloads a model for Module 1, whose artifact is committed, or
for Module 5, whose geocoder is a table compiled into the source.
"""

from __future__ import annotations

import importlib.util
import os
import time

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

MODULES = ('content', 'social', 'media')


def loaders(module: str):
    """(name, callable) pairs for one module, imported only when asked for.

    Imports are deferred per module so that `--module content` works on a
    machine where another module's extras are missing. A module with no `ml`
    submodule at all has no trained engine in this build and returns nothing,
    which is a different thing from its dependencies being absent and reads
    differently in the output.
    """
    if not importlib.util.find_spec(f'{module}.ml'):
        return []

    if module == 'content':
        from content.ml import _embedder, _ner_pipeline

        return [('content: entities', _ner_pipeline),
                ('content: embeddings', _embedder)]

    if module == 'social':
        from social.ml import RobertaTextScorer, _classifier

        scorer = RobertaTextScorer()
        return [
            ('social: sentiment', lambda: _classifier(scorer.sentiment_model)),
            ('social: offensive', lambda: _classifier(scorer.offensive_model)),
        ]

    if module == 'media':
        from media.ml import _detector

        return [('media: generated-image detector', _detector)]

    raise CommandError(f'unknown module {module!r}')


class Command(BaseCommand):
    help = "Download the trained engines' weights into CONTENT_MODEL_CACHE."

    def add_arguments(self, parser):
        parser.add_argument(
            '--module', default='all', choices=('all', *MODULES),
            help='Fetch one module\'s weights. Default: all of them.',
        )
        parser.add_argument(
            '--check', action='store_true',
            help='Report what is already cached, download nothing.',
        )

    def handle(self, *args, **options):
        cache = str(settings.CONTENT_MODEL_CACHE)
        os.environ.setdefault('HF_HOME', cache)
        if options['check']:
            os.environ['HF_HUB_OFFLINE'] = '1'

        wanted = MODULES if options['module'] == 'all' else (options['module'],)
        self.stdout.write(f'cache: {cache}')

        missing = []
        for module in wanted:
            try:
                entries = loaders(module)
            except ImportError as exc:
                raise CommandError(
                    f'{module}: the ML extras are not installed. '
                    'pip install -r requirements-ml.txt'
                ) from exc

            if not entries:
                self.stdout.write(f'{module}: no trained engine in this build')
                continue

            for name, load in entries:
                started = time.monotonic()
                try:
                    load()
                except Exception as exc:
                    if options['check']:
                        missing.append(name)
                        self.stdout.write(self.style.WARNING(f'{name}: not cached'))
                        continue
                    raise CommandError(f'{name}: failed to load: {exc}') from exc
                took = time.monotonic() - started
                verb = 'cached' if options['check'] else 'ready'
                self.stdout.write(self.style.SUCCESS(f'{name}: {verb} ({took:.1f}s)'))

        if options['check'] and missing:
            self.stdout.write('')
            self.stdout.write(f'{len(missing)} not cached. Run without --check '
                              'to download.')
