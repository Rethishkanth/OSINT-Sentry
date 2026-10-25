"""Download Module 2's weights ahead of time.

content.ml fetches its two models the first time something asks for them,
which means the first article analysed after a deploy waits several hundred
megabytes for its answer, and a worker with no outbound network fails at the
moment an analyst is watching rather than at the moment an operator is.

This command moves both of those to a deliberate step:

    manage.py fetch_content_models

It writes into CONTENT_MODEL_CACHE, which is gitignored. Re-running it is
cheap: the hub cache is content-addressed, so an unchanged model is verified
rather than downloaded again.
"""

from __future__ import annotations

import os
import time

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Download the content analyzer's models into CONTENT_MODEL_CACHE."

    def add_arguments(self, parser):
        parser.add_argument(
            '--check', action='store_true',
            help='Report whether the weights are already cached, download nothing.',
        )

    def handle(self, *args, **options):
        cache = str(settings.CONTENT_MODEL_CACHE)
        os.environ.setdefault('HF_HOME', cache)

        if options['check']:
            os.environ['HF_HUB_OFFLINE'] = '1'

        try:
            from content.ml import _embedder, _ner_pipeline
        except ImportError as exc:  # pragma: no cover - guarded by the message
            raise CommandError(
                'the ML extras are not installed. pip install -r requirements-ml.txt'
            ) from exc

        self.stdout.write(f'cache: {cache}')

        for name, load in (('NER', _ner_pipeline), ('embeddings', _embedder)):
            started = time.monotonic()
            try:
                load()
            except Exception as exc:
                if options['check']:
                    self.stdout.write(self.style.WARNING(f'{name}: not cached'))
                    continue
                raise CommandError(f'{name} model failed to load: {exc}') from exc
            took = time.monotonic() - started
            verb = 'cached' if options['check'] else 'ready'
            self.stdout.write(self.style.SUCCESS(f'{name}: {verb} ({took:.1f}s)'))
