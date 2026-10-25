"""Train the gradient-boosted credibility model.

The plan this project is built from calls for a GBT over domain metadata. This
is that model. It is trained on Media Bias/Fact Check factual-reporting
ratings, which makes it a model of MBFC's judgement rather than of truth, and
the artifact records that so nothing downstream can forget it.

Two things here matter more than the headline accuracy.

First, the model abstains. A classifier always emits a number, including for
domains unlike anything it was trained on, and this platform's rule is that no
rating is given rather than a bad one. So the probability is calibrated and
the scorer stays silent below a confidence floor. The command prints the
coverage and precision at each threshold so the floor is chosen from evidence
rather than taste.

Second, it measures whether registration metadata earns its place. The
heuristic scorer docks 25 points from a domain under a month old. That is a
testable claim, and this command tests it by training on the domain string
alone, on registration alone, and on both.

    manage.py train_credibility_model
"""

from __future__ import annotations

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from credibility.features import (CATEGORICAL_COLUMNS, FEATURE_COLUMNS,
                                  LEXICAL_COLUMNS, REGISTRATION_COLUMNS,
                                  to_frame)

DATA_DIR = Path(__file__).resolve().parents[2] / 'data'
ARTIFACT_DIR = Path(__file__).resolve().parents[2] / 'artifacts'
THRESHOLDS = (0.50, 0.60, 0.70, 0.80, 0.85, 0.90, 0.95)


class Command(BaseCommand):
    help = 'Train and evaluate the credibility gradient-boosted model.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dataset', default=str(DATA_DIR / 'credibility_dataset.json'),
            help='Dataset written by build_credibility_dataset.',
        )
        parser.add_argument(
            '--out', default=str(ARTIFACT_DIR / 'credibility_gbt.joblib'),
            help='Where to write the trained model.',
        )
        parser.add_argument(
            '--min-confidence', type=float, default=0.90,
            help='Confidence floor below which the scorer abstains.',
        )
        parser.add_argument(
            '--seed', type=int, default=42,
            help='Random seed, so a rerun reproduces the same model.',
        )

    def handle(self, *args, **options):
        try:
            import joblib
            import numpy as np
            import pandas as pd
            import lightgbm as lgb
            from sklearn.calibration import CalibratedClassifierCV
            from sklearn.metrics import (accuracy_score, brier_score_loss,
                                         roc_auc_score)
            from sklearn.model_selection import (StratifiedKFold,
                                                 cross_val_predict,
                                                 train_test_split)
        except ImportError as exc:
            raise CommandError(
                'training needs the optional ML dependencies. Install them '
                'with:  pip install -r requirements-ml.txt'
            ) from exc

        dataset_path = Path(options['dataset'])
        if not dataset_path.exists():
            raise CommandError(
                f'{dataset_path} not found. Run build_credibility_dataset first.'
            )
        payload = json.loads(dataset_path.read_text(encoding='utf-8'))
        rows = payload['rows']

        # Same typed-frame builder the scorer uses at serving time, so the
        # model is never trained on a column layout it will not be given.
        frame, categories = to_frame(row['features'] for row in rows)
        frame['y'] = [row['label'] for row in rows]

        self.stdout.write(f'rows            : {len(frame)}')
        self.stdout.write(f'credible        : {frame["y"].mean():.1%}')
        self.stdout.write(
            f'registration    : {int(frame["reg_age_known"].sum())} domains '
            f'with a real date'
        )
        self.stdout.write('')

        def make():
            estimator = lgb.LGBMClassifier(
                n_estimators=500, learning_rate=0.05, num_leaves=31,
                min_child_samples=20, subsample=0.9, subsample_freq=1,
                colsample_bytree=0.9, random_state=options['seed'], verbose=-1,
            )
            # An abstention threshold on an uncalibrated score is a threshold
            # on a number that does not mean what it claims to mean.
            return CalibratedClassifierCV(estimator, method='isotonic', cv=5)

        # -- does registration metadata earn its place? --------------------
        with_rdap = frame[frame['reg_age_known'] == 1]
        comparison = {}
        if len(with_rdap) >= 200:
            self.stdout.write(
                f'=== 5-fold CV on the {len(with_rdap)} domains with a '
                f'registration date ==='
            )
            splitter = StratifiedKFold(5, shuffle=True,
                                       random_state=options['seed'])
            for name, columns in (
                ('domain string only', LEXICAL_COLUMNS + CATEGORICAL_COLUMNS),
                ('registration only', REGISTRATION_COLUMNS),
                ('both', FEATURE_COLUMNS),
            ):
                probabilities = cross_val_predict(
                    make(), with_rdap[columns], with_rdap['y'],
                    cv=splitter, method='predict_proba',
                )[:, 1]
                auc = roc_auc_score(with_rdap['y'], probabilities)
                comparison[name] = round(float(auc), 4)
                self.stdout.write(f'  {name:<22} auc={auc:.3f}')
            self.stdout.write('')
        else:
            self.stdout.write(
                'skipping the registration comparison: too few domains have a '
                'registration date. Rerun build_credibility_dataset --rdap N.'
            )
            self.stdout.write('')

        # -- the shipped model --------------------------------------------
        X, y = frame[FEATURE_COLUMNS], frame['y']
        X_train, X_test, y_train, y_test = train_test_split(
            X, y, test_size=0.2, stratify=y, random_state=options['seed']
        )
        model = make()
        model.fit(X_train, y_train)

        probabilities = model.predict_proba(X_test)[:, 1]
        predictions = (probabilities >= 0.5).astype(int)
        auc = float(roc_auc_score(y_test, probabilities))
        accuracy = float(accuracy_score(y_test, predictions))
        majority = float(max(y_test.mean(), 1 - y_test.mean()))
        brier = float(brier_score_loss(y_test, probabilities))

        self.stdout.write('=== held-out test (20%) ===')
        self.stdout.write(f'  roc auc            {auc:.3f}')
        self.stdout.write(f'  accuracy           {accuracy:.3f}')
        self.stdout.write(f'  majority baseline  {majority:.3f}')
        self.stdout.write(f'  brier score        {brier:.3f}  (lower is better)')
        self.stdout.write('')

        self.stdout.write('=== what abstention buys ===')
        self.stdout.write(
            f'  {"floor":>7} {"speaks on":>11} {"precision":>11} {"n":>7}'
        )
        coverage_table = []
        for threshold in THRESHOLDS:
            confident = (probabilities >= threshold) | (probabilities <= 1 - threshold)
            n = int(confident.sum())
            if not n:
                continue
            said = (probabilities[confident] >= 0.5).astype(int)
            precision = float((said == y_test[confident]).mean())
            coverage = n / len(y_test)
            coverage_table.append({
                'floor': threshold, 'coverage': round(coverage, 4),
                'precision': round(precision, 4), 'n': n,
            })
            self.stdout.write(
                f'  {threshold:>7.2f} {coverage:>10.1%} {precision:>10.1%} {n:>7}'
            )
        self.stdout.write('')

        chosen = options['min_confidence']
        at_floor = next(
            (r for r in coverage_table if abs(r['floor'] - chosen) < 1e-9), None
        )

        ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
        out_path = Path(options['out'])
        joblib.dump({
            'model': model,
            'feature_columns': FEATURE_COLUMNS,
            'tld_categories': categories,
            'min_confidence': chosen,
            'label_source': payload.get('label_source'),
            'label_mapping': payload.get('label_mapping'),
            'n_training_rows': int(len(X_train)),
            'metrics': {
                'roc_auc': round(auc, 4),
                'accuracy': round(accuracy, 4),
                'majority_baseline': round(majority, 4),
                'brier': round(brier, 4),
                'coverage_table': coverage_table,
                'feature_set_comparison': comparison,
            },
        }, out_path)

        report = out_path.with_suffix('.metrics.json')
        report.write_text(json.dumps({
            'label_source': payload.get('label_source'),
            'n_rows': len(frame),
            'n_with_registration_date': int(frame['reg_age_known'].sum()),
            'roc_auc': round(auc, 4),
            'accuracy': round(accuracy, 4),
            'majority_baseline': round(majority, 4),
            'brier': round(brier, 4),
            'min_confidence': chosen,
            'coverage_table': coverage_table,
            'feature_set_comparison': comparison,
        }, indent=2), encoding='utf-8')

        self.stdout.write(self.style.SUCCESS(f'wrote {out_path}'))
        self.stdout.write(self.style.SUCCESS(f'wrote {report}'))
        if at_floor:
            self.stdout.write(
                f'At the {chosen:.2f} floor the model speaks on '
                f'{at_floor["coverage"]:.1%} of domains and is '
                f'{at_floor["precision"]:.1%} right when it does.'
            )
