"""Measure the entity extractor against CoNLL-2003 and pick its floor.

The analyzer in content.ml drops any entity the model is not confident about.
That floor has to come from somewhere, and the only defensible somewhere is a
table of what the floor costs and buys. This command builds that table.

    manage.py evaluate_ner
    manage.py evaluate_ner --limit 500 --split validation

It evaluates the serving path, not the model in the abstract: the same
pipeline object, the same aggregation, the same span cleaning that
content.ml.TransformerAnalyzer uses. A number measured on a different code
path is a number about a system nobody is running.

Two caveats worth keeping in view when reading the output.

CoNLL-2003 is Reuters newswire from 1996 and 1997, and dslim/bert-base-NER
was fine-tuned on its training split. So this is a held-out test set for that
model, which is the right kind of measurement, but it is newswire measuring
newswire. An analyst pasting in a forum post should expect worse.

The dataset is tokenised, and this joins the tokens back into a sentence
before handing it to the model, because that is the shape real input arrives
in. The join is approximate around punctuation and contractions, which costs
a little accuracy that the model itself is not responsible for.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

ARTIFACT_DIR = Path(settings.BASE_DIR) / 'content' / 'artifacts'
DEFAULT_OUTPUT = ARTIFACT_DIR / 'bert_ner.metrics.json'

DATASET = 'eriktks/conll2003'
# The repository still ships a loading script, which current `datasets`
# refuses to run. The hub's own parquet conversion of the same data is on
# this branch and loads without executing anything.
DATASET_REVISION = 'refs/convert/parquet'

# Floors worth tabulating. Below 0.5 the pipeline has already discarded the
# span, so starting there is not a choice about taste.
FLOORS = (0.50, 0.60, 0.70, 0.80, 0.85, 0.90, 0.95, 0.98, 0.99)

# Punctuation that attaches to the word before it when the tokenised dataset
# is joined back into a sentence.
CLOSING = set(",.;:!?)]}'\"%")
OPENING = set("([{$")


def detokenize(tokens):
    """Join dataset tokens into a sentence, and say where each token landed.

    Returns the sentence and a list of (start, end) character offsets, one
    per token, so a predicted character span can be mapped back onto the
    tokens it covers.
    """
    parts: list[str] = []
    spans: list[tuple[int, int]] = []
    position = 0
    previous_opened = False
    for index, token in enumerate(tokens):
        attached = (
            index == 0
            or previous_opened
            or (token and token[0] in CLOSING)
            or token.startswith("'")
        )
        if not attached:
            parts.append(' ')
            position += 1
        parts.append(token)
        spans.append((position, position + len(token)))
        position += len(token)
        previous_opened = bool(token) and token[-1] in OPENING
    return ''.join(parts), spans


def gold_entities(tags, names):
    """The (start_token, end_token, type) triples a BIO tag sequence encodes."""
    found = []
    start, kind = None, None
    for index, tag in enumerate(list(tags) + [0]):
        label = names[tag] if index < len(tags) else 'O'
        if label.startswith('B-') or (label.startswith('I-') and kind != label[2:]):
            if kind is not None:
                found.append((start, index, kind))
            start, kind = index, label[2:]
        elif label == 'O':
            if kind is not None:
                found.append((start, index, kind))
            start, kind = None, None
    return found


def predicted_entities(spans, offsets):
    """Map predicted character spans onto token ranges.

    A span that covers no whole token is dropped. That happens when the model
    marks part of a word, which is not an entity this system could store.
    """
    found = []
    for span in spans:
        start_char = span.get('start')
        end_char = span.get('end')
        if start_char is None or end_char is None:
            continue
        covered = [
            i for i, (begin, stop) in enumerate(offsets)
            if begin < end_char and stop > start_char
        ]
        if not covered:
            continue
        found.append((
            covered[0], covered[-1] + 1,
            span.get('entity_group', ''),
            float(span.get('score', 0.0)),
        ))
    return found


def score_at(floor, per_sentence):
    """Precision, recall and F1 over every sentence at one confidence floor."""
    true_positive = predicted_total = gold_total = 0
    for gold, predicted in per_sentence:
        kept = {(a, b, kind) for a, b, kind, score in predicted if score >= floor}
        true_positive += len(kept & gold)
        predicted_total += len(kept)
        gold_total += len(gold)
    precision = true_positive / predicted_total if predicted_total else 0.0
    recall = true_positive / gold_total if gold_total else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if precision + recall else 0.0
    return {
        'floor': round(floor, 2),
        'precision': round(precision, 4),
        'recall': round(recall, 4),
        'f1': round(f1, 4),
        'entities_kept': predicted_total,
        'entities_correct': true_positive,
    }


class Command(BaseCommand):
    help = "Measure the NER model on CoNLL-2003 and tabulate its confidence floor."

    def add_arguments(self, parser):
        parser.add_argument('--split', default='test',
                            help='CoNLL-2003 split. Default: test.')
        parser.add_argument('--limit', type=int, default=0,
                            help='Evaluate only the first N sentences. 0 means all.')
        parser.add_argument('--batch-size', type=int, default=32)
        parser.add_argument('--output', default=str(DEFAULT_OUTPUT))

    def handle(self, *args, **options):
        os.environ.setdefault('HF_HOME', str(settings.CONTENT_MODEL_CACHE))

        try:
            from datasets import load_dataset

            from content.ml import _ner_pipeline, span_text
        except ImportError as exc:
            raise CommandError(
                'the ML extras are not installed. pip install -r requirements-ml.txt'
            ) from exc

        self.stdout.write(f'loading {DATASET} [{options["split"]}]')
        data = load_dataset(DATASET, revision=DATASET_REVISION, split=options['split'])
        if options['limit']:
            data = data.select(range(min(options['limit'], len(data))))
        names = data.features['ner_tags'].feature.names

        sentences, offsets, golds = [], [], []
        skipped = 0
        for row in data:
            # The corpus marks each document with a -DOCSTART- line, which is
            # not a sentence and has no entities. Scoring them would pad the
            # result with free correct answers.
            if row['tokens'] in ([], ['-DOCSTART-']):
                skipped += 1
                continue
            text, spans = detokenize(row['tokens'])
            sentences.append(text)
            offsets.append(spans)
            golds.append(set(gold_entities(row['ner_tags'], names)))

        self.stdout.write(f'{len(sentences)} sentences, '
                          f'{sum(len(g) for g in golds)} gold entities '
                          f'({skipped} document markers skipped)')

        ner = _ner_pipeline()
        started = time.monotonic()
        per_sentence = []
        for index in range(0, len(sentences), options['batch_size']):
            batch = sentences[index:index + options['batch_size']]
            for offset, spans in enumerate(ner(batch)):
                # Spans are cleaned the way serving cleans them: one that
                # trims down to nothing is not an entity this system would
                # ever store, so it should not be counted as one it found.
                spans = [s for s in spans
                         if len(span_text(s, batch[offset])) >= 2]
                per_sentence.append((
                    golds[index + offset],
                    predicted_entities(spans, offsets[index + offset]),
                ))
            if index and index % (options['batch_size'] * 20) == 0:
                self.stdout.write(f'  {index}/{len(sentences)}')
        elapsed = time.monotonic() - started

        table = [score_at(floor, per_sentence) for floor in FLOORS]
        unfiltered = score_at(0.0, per_sentence)

        self.stdout.write('')
        self.stdout.write(f'{"floor":>6}  {"precision":>9}  {"recall":>7}  '
                          f'{"f1":>7}  {"kept":>7}')
        for row in [unfiltered, *table]:
            self.stdout.write(
                f'{row["floor"]:>6.2f}  {row["precision"]:>9.4f}  '
                f'{row["recall"]:>7.4f}  {row["f1"]:>7.4f}  {row["entities_kept"]:>7}'
            )

        configured = float(settings.CONTENT_NER_MIN_CONFIDENCE)
        at_configured = score_at(configured, per_sentence)
        self.stdout.write('')
        self.stdout.write(
            f'CONTENT_NER_MIN_CONFIDENCE is {configured:.2f}: '
            f'precision {at_configured["precision"]:.4f}, '
            f'recall {at_configured["recall"]:.4f}, '
            f'keeping {at_configured["entities_kept"]} of '
            f'{unfiltered["entities_kept"]} entities the model offered.'
        )

        payload = {
            'model': settings.CONTENT_NER_MODEL,
            'dataset': f'{DATASET} [{options["split"]}]',
            'sentences': len(sentences),
            'gold_entities': sum(len(g) for g in golds),
            'seconds': round(elapsed, 1),
            'unfiltered': unfiltered,
            'by_floor': table,
            'configured_floor': configured,
            'at_configured_floor': at_configured,
        }
        out_path = Path(options['output'])
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload, indent=2) + '\n', encoding='utf-8')
        self.stdout.write(self.style.SUCCESS(f'wrote {out_path}'))
