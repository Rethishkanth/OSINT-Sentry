"""Measure the keyphrase topics, and pick the similarity floor from data.

The entity extractor has a held-out corpus with gold answers to score
against. Topics do not: nobody has annotated the right five keyphrases for
an arbitrary news article, and if somebody had, two annotators would
disagree. So this measures the one thing that can be measured without gold
labels, which is also the thing the floor is for.

The floor's job is to stop the analyzer offering a topic that is not what
the document is about. A phrase lifted out of a different article is, by
construction, not what this document is about. So: score every document
against its own candidate phrases and against a sample of phrases from other
documents, and read off what each floor lets through.

    manage.py evaluate_topics
    manage.py evaluate_topics --documents 500

A floor is worth setting where foreign phrases stop passing and the
document's own phrases still do. The output names that point rather than
leaving the constant in settings to look like taste.

The corpus is AG News, which is news written for a general reader, the same
shape of text Module 2 reads. It is not a topic benchmark and is not being
used as one: nothing here scores a predicted topic as right or wrong.
"""

from __future__ import annotations

import json
import os
import random
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

ARTIFACT_DIR = Path(settings.BASE_DIR) / 'content' / 'artifacts'
DEFAULT_OUTPUT = ARTIFACT_DIR / 'topic_floor.metrics.json'

DATASET = 'fancyzhx/ag_news'
FLOORS = (0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.45, 0.50)

# Foreign phrases drawn per document. Enough to put a number on the tail
# without embedding the whole corpus against every document in it.
FOREIGN_PER_DOCUMENT = 40


class Command(BaseCommand):
    help = 'Measure how well the topic similarity floor separates on-topic phrases.'

    def add_arguments(self, parser):
        parser.add_argument('--documents', type=int, default=300)
        parser.add_argument('--seed', type=int, default=20261025)
        parser.add_argument('--output', default=str(DEFAULT_OUTPUT))

    def handle(self, *args, **options):
        os.environ.setdefault('HF_HOME', str(settings.CONTENT_MODEL_CACHE))

        try:
            import numpy as np
            from datasets import load_dataset

            from content.ml import TransformerAnalyzer, _embedder, candidate_phrases
        except ImportError as exc:
            raise CommandError(
                'the ML extras are not installed. pip install -r requirements-ml.txt'
            ) from exc

        rng = random.Random(options['seed'])
        self.stdout.write(f'loading {DATASET}')
        data = load_dataset(DATASET, split='test')
        rows = [data[i]['text'] for i in
                rng.sample(range(len(data)), min(options['documents'], len(data)))]

        model = _embedder()
        analyzer = TransformerAnalyzer()

        self.stdout.write(f'{len(rows)} documents; building candidates')
        per_document = [candidate_phrases(text)[:analyzer.max_candidates]
                        for text in rows]
        keep = [i for i, phrases in enumerate(per_document) if phrases]
        rows = [rows[i] for i in keep]
        per_document = [per_document[i] for i in keep]

        vocabulary = sorted({p for phrases in per_document for p in phrases})
        self.stdout.write(f'{len(vocabulary)} distinct phrases; embedding')
        phrase_index = {phrase: i for i, phrase in enumerate(vocabulary)}
        phrase_vectors = model.encode(vocabulary, normalize_embeddings=True,
                                      batch_size=128, show_progress_bar=False)
        document_vectors = model.encode(rows, normalize_embeddings=True,
                                        batch_size=32, show_progress_bar=False)

        own_best: list[float] = []     # each document's strongest own phrase
        own_fifth: list[float] = []    # its fifth, the weakest it would show
        foreign: list[float] = []

        for index, phrases in enumerate(per_document):
            document = document_vectors[index]
            scores = sorted(
                (float(phrase_vectors[phrase_index[p]] @ document) for p in phrases),
                reverse=True,
            )
            own_best.append(scores[0])
            own_fifth.append(scores[min(4, len(scores) - 1)])

            others = [p for other, phrases_ in enumerate(per_document)
                      if other != index for p in phrases_]
            for phrase in rng.sample(others, min(FOREIGN_PER_DOCUMENT, len(others))):
                if phrase in phrases:
                    # It is in both documents, so it is not foreign to this
                    # one. Counting it as a false pass would charge the floor
                    # for a phrase two articles genuinely share.
                    continue
                foreign.append(float(phrase_vectors[phrase_index[phrase]] @ document))

        own_best = np.array(own_best)
        own_fifth = np.array(own_fifth)
        foreign = np.array(foreign)

        self.stdout.write('')
        self.stdout.write(f'{"floor":>6}  {"own best":>9}  {"own 5th":>9}  '
                          f'{"foreign":>9}')
        self.stdout.write(f'{"":>6}  {"passes":>9}  {"passes":>9}  {"passes":>9}')
        table = []
        for floor in FLOORS:
            row = {
                'floor': floor,
                'own_best_passes': round(float((own_best >= floor).mean()), 4),
                'own_fifth_passes': round(float((own_fifth >= floor).mean()), 4),
                'foreign_passes': round(float((foreign >= floor).mean()), 4),
            }
            table.append(row)
            self.stdout.write(
                f'{floor:>6.2f}  {row["own_best_passes"]:>9.4f}  '
                f'{row["own_fifth_passes"]:>9.4f}  {row["foreign_passes"]:>9.4f}'
            )

        configured = float(settings.CONTENT_TOPIC_MIN_SIMILARITY)
        self.stdout.write('')
        self.stdout.write(
            f'own phrases: best {own_best.mean():.4f} mean, '
            f'fifth {own_fifth.mean():.4f} mean'
        )
        self.stdout.write(f'foreign phrases: {foreign.mean():.4f} mean, '
                          f'{len(foreign)} sampled')
        self.stdout.write(
            f'CONTENT_TOPIC_MIN_SIMILARITY is {configured:.2f}: '
            f'{float((foreign >= configured).mean()):.1%} of phrases taken from '
            f'another article would clear it.'
        )

        payload = {
            'model': settings.CONTENT_EMBEDDING_MODEL,
            'dataset': f'{DATASET} [test]',
            'documents': len(rows),
            'phrases': len(vocabulary),
            'foreign_sampled': len(foreign),
            'own_best_mean': round(float(own_best.mean()), 4),
            'own_fifth_mean': round(float(own_fifth.mean()), 4),
            'foreign_mean': round(float(foreign.mean()), 4),
            'configured_floor': configured,
            'by_floor': table,
        }
        out_path = Path(options['output'])
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload, indent=2) + '\n', encoding='utf-8')
        self.stdout.write(self.style.SUCCESS(f'wrote {out_path}'))
