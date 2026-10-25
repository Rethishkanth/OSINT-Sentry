"""Measure the RoBERTa text scorer, and pick its confidence floor.

Both models here were fine-tuned on TweetEval, so TweetEval's test splits are
held out for them and are the right place to ask what they are worth. The
command runs the serving path - the same pipeline, the same truncation - over
each split and reports the numbers, plus the coverage table the sentiment
floor is chosen from.

    manage.py evaluate_text_scorer
    manage.py evaluate_text_scorer --limit 2000

The caveat that matters more than any number: these are tweet models. They
read short, informal, English posts well, and a long post, a formal one, or
one in another language is outside what they were trained on. Module 3 scores
social posts, which is the right domain, but "social post" is not one thing
and the console's rationale says so on every post it scores.

A second caveat, on the word toxicity. The offensive model predicts whether
an annotator would mark a tweet offensive, which in TweetEval means open
abuse and profanity. A polite smear scores low. The field is called toxicity
because that is what the schema calls it; what is measured is narrower.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

ARTIFACT_DIR = Path(settings.BASE_DIR) / 'social' / 'artifacts'
DEFAULT_OUTPUT = ARTIFACT_DIR / 'roberta_text.metrics.json'

DATASET = 'cardiffnlp/tweet_eval'
FLOORS = (0.34, 0.40, 0.50, 0.60, 0.70, 0.80, 0.90, 0.95)


def macro_f1(gold, predicted, classes):
    """Unweighted mean of the per-class F1 scores.

    Written out rather than imported because it is six lines and because the
    reader of this file should be able to see which average is meant: with
    TweetEval sentiment's uneven classes, macro and micro differ by enough to
    matter.
    """
    scores = []
    for label in classes:
        true_positive = sum(1 for g, p in zip(gold, predicted)
                            if g == label and p == label)
        predicted_count = sum(1 for p in predicted if p == label)
        gold_count = sum(1 for g in gold if g == label)
        precision = true_positive / predicted_count if predicted_count else 0.0
        recall = true_positive / gold_count if gold_count else 0.0
        scores.append((2 * precision * recall / (precision + recall))
                      if precision + recall else 0.0)
    return sum(scores) / len(scores) if scores else 0.0


def roc_auc(gold, scores):
    """Area under the ROC curve, by rank, with ties averaged."""
    paired = sorted(zip(scores, gold))
    ranks, index = {}, 0
    while index < len(paired):
        stop = index
        while stop + 1 < len(paired) and paired[stop + 1][0] == paired[index][0]:
            stop += 1
        average = (index + stop) / 2 + 1
        for position in range(index, stop + 1):
            ranks[position] = average
        index = stop + 1
    positives = sum(1 for _, label in paired if label == 1)
    negatives = len(paired) - positives
    if not positives or not negatives:
        return 0.0
    rank_sum = sum(ranks[i] for i, (_, label) in enumerate(paired) if label == 1)
    return (rank_sum - positives * (positives + 1) / 2) / (positives * negatives)


class Command(BaseCommand):
    help = 'Measure the RoBERTa sentiment and offensive models on TweetEval.'

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=0,
                            help='Score only the first N posts per split.')
        parser.add_argument('--batch-size', type=int, default=32)
        parser.add_argument('--output', default=str(DEFAULT_OUTPUT))

    def handle(self, *args, **options):
        os.environ.setdefault('HF_HOME', str(settings.CONTENT_MODEL_CACHE))

        try:
            from datasets import load_dataset

            from social.ml import RobertaTextScorer, _classifier
        except ImportError as exc:
            raise CommandError(
                'the ML extras are not installed. pip install -r requirements-ml.txt'
            ) from exc

        scorer = RobertaTextScorer()
        payload = {
            'sentiment_model': scorer.sentiment_model,
            'offensive_model': scorer.offensive_model,
            'dataset': DATASET,
        }
        payload['sentiment'] = self.sentiment(
            load_dataset, _classifier, scorer, options)
        payload['offensive'] = self.offensive(
            load_dataset, _classifier, scorer, options)

        out_path = Path(options['output'])
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload, indent=2) + '\n', encoding='utf-8')
        self.stdout.write(self.style.SUCCESS(f'wrote {out_path}'))

    def predict(self, classifier, texts, batch_size):
        """Label to probability for every text, in order."""
        out = []
        started = time.monotonic()
        for index in range(0, len(texts), batch_size):
            for scored in classifier(texts[index:index + batch_size]):
                out.append({str(d['label']).lower(): float(d['score'])
                            for d in scored})
            if index and index % (batch_size * 40) == 0:
                self.stdout.write(f'  {index}/{len(texts)}')
        return out, time.monotonic() - started

    # -- sentiment --------------------------------------------------------

    def sentiment(self, load_dataset, _classifier, scorer, options):
        self.stdout.write(f'loading {DATASET} [sentiment/test]')
        data = load_dataset(DATASET, 'sentiment', split='test')
        if options['limit']:
            data = data.select(range(min(options['limit'], len(data))))
        names = [n.lower() for n in data.features['label'].names]
        gold = [names[row] for row in data['label']]
        texts = list(data['text'])

        self.stdout.write(f'{len(texts)} posts')
        probabilities, elapsed = self.predict(
            _classifier(scorer.sentiment_model), texts, options['batch_size'])

        predicted = [max(p, key=p.get) for p in probabilities]
        best = [max(p.values()) for p in probabilities]
        accuracy = sum(g == p for g, p in zip(gold, predicted)) / len(gold)
        f1 = macro_f1(gold, predicted, names)
        # The majority class, which is what "no model at all" would score.
        baseline = max(gold.count(n) for n in names) / len(gold)

        self.stdout.write('')
        self.stdout.write(f'sentiment: {accuracy:.4f} accuracy, {f1:.4f} macro F1, '
                          f'{baseline:.4f} majority baseline, {elapsed:.0f}s')
        self.stdout.write('')
        self.stdout.write(f'{"floor":>6}  {"speaks on":>9}  {"right then":>10}')
        table = []
        for floor in FLOORS:
            kept = [(g, p) for g, p, b in zip(gold, predicted, best) if b >= floor]
            coverage = len(kept) / len(gold)
            correct = (sum(g == p for g, p in kept) / len(kept)) if kept else 0.0
            table.append({'floor': floor,
                          'speaks_on': round(coverage, 4),
                          'right_when_it_speaks': round(correct, 4)})
            self.stdout.write(f'{floor:>6.2f}  {coverage:>9.4f}  {correct:>10.4f}')

        configured = float(settings.SOCIAL_TEXT_MIN_CONFIDENCE)
        kept = [(g, p) for g, p, b in zip(gold, predicted, best) if b >= configured]
        self.stdout.write('')
        self.stdout.write(
            f'SOCIAL_TEXT_MIN_CONFIDENCE is {configured:.2f}: scores '
            f'{len(kept) / len(gold):.1%} of posts, right '
            f'{sum(g == p for g, p in kept) / len(kept):.1%} of those.'
        )

        return {
            'split': 'sentiment/test', 'posts': len(texts),
            'classes': names, 'seconds': round(elapsed, 1),
            'accuracy': round(accuracy, 4), 'macro_f1': round(f1, 4),
            'majority_baseline': round(baseline, 4),
            'configured_floor': configured, 'by_floor': table,
        }

    # -- offensive --------------------------------------------------------

    def offensive(self, load_dataset, _classifier, scorer, options):
        self.stdout.write('')
        self.stdout.write(f'loading {DATASET} [offensive/test]')
        data = load_dataset(DATASET, 'offensive', split='test')
        if options['limit']:
            data = data.select(range(min(options['limit'], len(data))))
        gold = list(data['label'])  # 1 is offensive
        texts = list(data['text'])

        self.stdout.write(f'{len(texts)} posts')
        probabilities, elapsed = self.predict(
            _classifier(scorer.offensive_model), texts, options['batch_size'])
        scores = [p.get('offensive', 0.0) for p in probabilities]

        predicted = [1 if s >= 0.5 else 0 for s in scores]
        accuracy = sum(g == p for g, p in zip(gold, predicted)) / len(gold)
        f1 = macro_f1(gold, predicted, [0, 1])
        auc = roc_auc(gold, scores)
        baseline = max(gold.count(0), gold.count(1)) / len(gold)

        self.stdout.write('')
        self.stdout.write(
            f'offensive: {accuracy:.4f} accuracy at 0.50, {f1:.4f} macro F1, '
            f'{auc:.4f} AUC, {baseline:.4f} majority baseline, {elapsed:.0f}s'
        )
        self.stdout.write(
            'The stored number is this probability, not a verdict, so the '
            'AUC is the figure that matters: it says the ranking is '
            'informative without committing anyone to a threshold.'
        )

        return {
            'split': 'offensive/test', 'posts': len(texts),
            'seconds': round(elapsed, 1), 'accuracy_at_half': round(accuracy, 4),
            'macro_f1_at_half': round(f1, 4), 'roc_auc': round(auc, 4),
            'majority_baseline': round(baseline, 4),
        }
