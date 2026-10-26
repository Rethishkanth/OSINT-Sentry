"""Measure the ViT detector on its authors' held-out set, and pick its floor.

The Community Forensics evaluation set was built by the same authors as the
detector and kept out of its training: 21 generators - GANs, pixel and
latent diffusion, and commercial services - each paired with real
photographs from RAISE, COCO, FFHQ or LAION. It is published openly for
non-commercial research use under CC BY-NC-SA 4.0, which is what this is: a
measurement run locally, with the images never committed.

All of it is 51,836 images and 206GB, mostly lossless PNG. This command
reads a stratified sample instead: for every generator, and for its real
images and its generated ones separately, it picks row groups of 25 at
random with a fixed seed and scores every image in them. Each generator is
represented whatever its size, so a single average cannot be carried by the
three generators with the most images. The default, four row groups per
generator per class, is about 3,600 images and an hour on a CPU.

    manage.py evaluate_detector
    manage.py evaluate_detector --groups 12 --workers 12

Each image is scored twice: as published, and again after being re-saved as
a JPEG. The second pass is the one that matters for this platform. The
evaluation set is mostly pristine files straight out of a generator; the
images an analyst uploads have been through a social network, a messaging
app or a screenshot, and every one of those re-encodes. A detector that
reads compression artefacts instead of generation artefacts looks excellent
on the first pass and falls apart on the second, and the floor is chosen
from the second pass for that reason.

Scores are cached per row group under CONTENT_MODEL_CACHE, so an interrupted
run picks up where it stopped rather than downloading everything again.
"""

from __future__ import annotations

import io
import json
import os
import random
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

ARTIFACT_DIR = Path(settings.BASE_DIR) / 'media' / 'artifacts'
DEFAULT_OUTPUT = ARTIFACT_DIR / 'vit_detector.metrics.json'

DATASET = 'OwensLab/CommunityForensics-Eval'
SHARDS = 413
META_COLUMNS = ('model_name', 'label', 'architecture', 'subset', 'format')
FLOORS = (0.50, 0.60, 0.70, 0.80, 0.90, 0.95, 0.98, 0.99)


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
        return None
    rank_sum = sum(ranks[i] for i, (_, label) in enumerate(paired) if label == 1)
    return (rank_sum - positives * (positives + 1) / 2) / (positives * negatives)


def average_precision(gold, scores):
    """Area under the precision-recall curve, the way the paper reports it.

    Precision is taken at the rank of every positive and averaged. This is
    the step-wise definition, not an interpolated one, so it can be compared
    with the per-generator AP in the paper's table.
    """
    positives = sum(gold)
    if not positives or positives == len(gold):
        return None
    ordered = sorted(zip(scores, gold), key=lambda pair: -pair[0])
    hits, total = 0, 0.0
    for rank, (_, label) in enumerate(ordered, start=1):
        if label:
            hits += 1
            total += hits / rank
    return total / positives


def accuracy(gold, scores, mark=0.5):
    if not gold:
        return None
    return sum((s >= mark) == bool(g) for g, s in zip(gold, scores)) / len(gold)


def floor_table(gold, scores):
    """What the detector is worth at each confidence floor.

    Three columns beyond coverage, because the costs are not symmetric. The
    one this platform cares most about is the last: of the real photographs
    in the sample, how many the detector confidently called generated. That
    is the detector accusing a photograph, and it is the figure the floor is
    chosen to keep down.
    """
    reals = sum(1 for g in gold if not g)
    rows = []
    for floor in FLOORS:
        kept = [(g, s) for g, s in zip(gold, scores) if max(s, 1 - s) >= floor]
        called = [(g, s) for g, s in kept if s >= 0.5]
        right = sum((s >= 0.5) == bool(g) for g, s in kept)
        false_calls = sum(1 for g, _ in called if not g)
        rows.append({
            'floor': floor,
            'speaks_on': round(len(kept) / len(gold), 4) if gold else 0.0,
            'right_when_it_speaks': round(right / len(kept), 4) if kept else None,
            'generated_calls_right': (
                round((len(called) - false_calls) / len(called), 4)
                if called else None
            ),
            'photographs_called_generated': (
                round(false_calls / reals, 4) if reals else None
            ),
            'images': len(kept),
        })
    return rows


def summarise(records, key):
    gold = [r['label'] for r in records]
    scores = [r[key] for r in records]
    real = [s for g, s in zip(gold, scores) if not g]
    fake = [s for g, s in zip(gold, scores) if g]
    return {
        'images': len(records),
        'real': len(real),
        'generated': len(fake),
        'roc_auc': _round(roc_auc(gold, scores)),
        'accuracy_at_half': _round(accuracy(gold, scores)),
        'photographs_read_as_photographs': _round(
            sum(s < 0.5 for s in real) / len(real) if real else None),
        'generated_read_as_generated': _round(
            sum(s >= 0.5 for s in fake) / len(fake) if fake else None),
    }


def by_group(records, key, field):
    groups = defaultdict(list)
    for record in records:
        groups[record[field]].append(record)
    out = {}
    for name in sorted(groups):
        rows = groups[name]
        gold = [r['label'] for r in rows]
        scores = [r[key] for r in rows]
        out[name] = {
            'real': sum(1 for g in gold if not g),
            'generated': sum(gold),
            'accuracy_at_half': _round(accuracy(gold, scores)),
            'average_precision': _round(average_precision(gold, scores)),
        }
    return out


def _round(value, places=4):
    return None if value is None else round(value, places)


class Command(BaseCommand):
    help = "Measure the ViT generated-image detector on its authors' held-out set."

    def add_arguments(self, parser):
        parser.add_argument('--groups', type=int, default=4,
                            help='Row groups of 25 to read per generator per '
                                 'class (real, generated).')
        parser.add_argument('--workers', type=int, default=8,
                            help='Parallel downloads.')
        parser.add_argument('--jpeg-quality', type=int, default=85)
        parser.add_argument('--seed', type=int, default=20260101)
        parser.add_argument('--output', default=str(DEFAULT_OUTPUT))
        parser.add_argument('--cached-only', action='store_true',
                            help='Report on the row groups already scored and '
                                 'list the rest as missing, without downloading.')

    def say(self, line=''):
        if self.verbosity:
            self.stdout.write(line)

    def handle(self, *args, **options):
        self.verbosity = options.get('verbosity', 1)
        cache = Path(settings.CONTENT_MODEL_CACHE)
        os.environ.setdefault('HF_HOME', str(cache))
        try:
            import pyarrow.parquet  # noqa: F401
            from huggingface_hub import HfApi, HfFileSystem
        except ImportError as exc:
            raise CommandError(
                'evaluation needs pyarrow and huggingface_hub, which come with '
                'datasets: pip install -r requirements-ml.txt'
            ) from exc

        from media.ml import model_name, model_revision

        revision = HfApi().dataset_info(DATASET).sha
        self.fs = HfFileSystem()
        self.root = f'datasets/{DATASET}@{revision}/data'
        self.say(f'{DATASET} at {revision[:12]}')
        self.say(f'detector {model_name()} at {model_revision()[:12]}')

        manifest = self.manifest(cache / 'commfor-eval' / f'manifest-{revision[:12]}.json',
                                 options['workers'])
        chosen = self.choose(manifest, options)

        started = time.time()
        records = self.score(chosen, manifest,
                             cache / 'commfor-eval' / f'scores-{revision[:12]}',
                             options)
        elapsed = time.time() - started
        if not records:
            raise CommandError('nothing was scored.')

        report = self.report(records, options, revision, elapsed)
        out = Path(options['output'])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
        self.say(self.style.SUCCESS(f'wrote {out}'))

    # -- what is in the set ------------------------------------------------

    def manifest(self, path, workers):
        """Every row group's labels, without the images.

        Parquet stores columns separately, so the five metadata columns of a
        shard can be read without touching its image column. That is a few
        kilobytes per shard against hundreds of megabytes, and it is what
        makes a stratified sample possible without downloading the set.
        """
        if path.exists():
            return json.loads(path.read_text(encoding='utf-8'))

        import pyarrow.parquet as pq

        def read(shard):
            name = f'{self.root}/CompEval-{shard:05d}-of-{SHARDS:05d}.parquet'
            # Small blocks and no pre-buffering: the metadata columns sit
            # between multi-megabyte image chunks, and the defaults read
            # through those to fetch a few bytes, which made this step take
            # hours instead of minutes.
            with self.fs.open(name, block_size=2 ** 18) as handle:
                parquet = pq.ParquetFile(handle, pre_buffer=False)
                groups = []
                for group in range(parquet.num_row_groups):
                    table = parquet.read_row_group(group, columns=list(META_COLUMNS))
                    columns = [table.column(c).to_pylist() for c in META_COLUMNS]
                    groups.append({'shard': shard, 'group': group,
                                   'rows': [list(row) for row in zip(*columns)]})
                return groups

        self.say(f'reading the labels of {SHARDS} shards')
        entries = []
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(read, shard) for shard in range(SHARDS)]
            for done, future in enumerate(as_completed(futures), start=1):
                entries.extend(future.result())
                if done % 50 == 0:
                    self.say(f'  {done}/{SHARDS}')
        entries.sort(key=lambda e: (e['shard'], e['group']))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(entries), encoding='utf-8')
        return entries

    def choose(self, manifest, options):
        """Pick row groups so every generator and class is represented."""
        rng = random.Random(options['seed'])
        candidates = defaultdict(list)
        for entry in manifest:
            for stratum in {(row[0], row[1]) for row in entry['rows']}:
                candidates[stratum].append((entry['shard'], entry['group']))

        chosen = set()
        for stratum in sorted(candidates):
            pool = sorted(candidates[stratum])
            chosen.update(rng.sample(pool, min(options['groups'], len(pool))))

        generators = {generator for generator, _ in candidates}
        images = sum(len(e['rows']) for e in manifest
                     if (e['shard'], e['group']) in chosen)
        self.say(
            f'{len(generators)} generators; {len(chosen)} row groups chosen, '
            f'{images} images'
        )
        return sorted(chosen)

    # -- scoring -----------------------------------------------------------

    def score(self, chosen, manifest, cache_dir, options):
        import pyarrow.parquet as pq

        from media.ml import generated_probabilities, prepare

        cache_dir.mkdir(parents=True, exist_ok=True)
        self.missing = []
        rows_of = {(e['shard'], e['group']): e['rows'] for e in manifest}
        quality = options['jpeg_quality']

        def fetch(key):
            shard, group = key
            name = f'{self.root}/CompEval-{shard:05d}-of-{SHARDS:05d}.parquet'
            with self.fs.open(name, block_size=8 * 2 ** 20) as handle:
                table = pq.ParquetFile(handle, pre_buffer=False).read_row_group(
                    group, columns=['image_data'])
            return key, table.column('image_data').to_pylist()

        def cached(key):
            return cache_dir / f'{key[0]:05d}-{key[1]}-q{quality}.json'

        records, pending = [], []
        for key in chosen:
            path = cached(key)
            if path.exists():
                records.extend(json.loads(path.read_text(encoding='utf-8')))
            else:
                pending.append(key)
        if records:
            self.say(f'{len(chosen) - len(pending)} row groups already scored')
        if options.get('cached_only'):
            self.missing = [list(key) for key in pending]
            pending = []

        with ThreadPoolExecutor(max_workers=options['workers']) as pool:
            futures = {pool.submit(fetch, key): key for key in pending}
            for done, future in enumerate(as_completed(futures), start=1):
                try:
                    key, blobs = future.result()
                except Exception as exc:  # network, after the hub's own retries
                    self.missing.append(list(futures[future]))
                    self.say(self.style.WARNING(
                        f'  row group {futures[future]} not downloaded: {exc}'))
                    continue
                originals, recompressed, kept = [], [], []
                for index, blob in enumerate(blobs):
                    image = prepare(blob)
                    if image is None:
                        continue
                    buffer = io.BytesIO()
                    image.save(buffer, 'JPEG', quality=quality)
                    again = prepare(buffer.getvalue())
                    originals.append(image)
                    recompressed.append(again)
                    kept.append(index)

                scores = generated_probabilities(originals) if originals else []
                after = generated_probabilities(recompressed) if recompressed else []
                group_records = []
                for index, score, score_jpeg in zip(kept, scores, after):
                    generator, label, architecture, subset, fmt = rows_of[key][index]
                    group_records.append({
                        'shard': key[0], 'group': key[1], 'index': index,
                        'generator': generator, 'label': int(label),
                        'architecture': architecture, 'subset': subset,
                        'format': fmt, 'p': round(score, 6),
                        'p_jpeg': round(score_jpeg, 6),
                    })
                skipped = len(blobs) - len(kept)
                if skipped:
                    self.say(f'  {skipped} undecodable image(s) in {key}')
                cached(key).write_text(json.dumps(group_records), encoding='utf-8')
                records.extend(group_records)
                if done % 10 == 0 or done == len(pending):
                    self.say(f'  {done}/{len(pending)} row groups, '
                             f'{len(records)} images scored')
        if self.missing:
            self.say(self.style.WARNING(
                f'{len(self.missing)} row group(s) could not be downloaded and '
                f'are left out; run again to fill them in from the cache.'))
        return records

    # -- reporting ---------------------------------------------------------

    def report(self, records, options, revision, elapsed):
        from media.ml import model_name, model_revision

        passes = {'as_published': 'p', f'jpeg_q{options["jpeg_quality"]}': 'p_jpeg'}
        report = {
            'dataset': DATASET,
            'dataset_revision': revision,
            'detector': model_name(),
            'detector_revision': model_revision(),
            'sample': {
                'groups_per_generator_per_class': options['groups'],
                'seed': options['seed'],
                'images': len(records),
                'generators': len({r['generator'] for r in records}),
                'row_groups_missing': getattr(self, 'missing', []),
            },
            'configured_floor': getattr(settings, 'MEDIA_DETECTOR_MIN_CONFIDENCE', None),
            # A --cached-only run downloads and scores nothing, so its own
            # running time says nothing about what the measurement costs.
            'seconds': None if options.get('cached_only') else round(elapsed, 1),
            'passes': {},
        }

        for name, key in passes.items():
            generators = by_group(records, key, 'generator')
            maps = [g['average_precision'] for g in generators.values()
                    if g['average_precision'] is not None]
            accs = [g['accuracy_at_half'] for g in generators.values()]
            section = {
                'overall': summarise(records, key),
                'mean_ap_over_generators': _round(sum(maps) / len(maps)) if maps else None,
                'mean_accuracy_over_generators': _round(sum(accs) / len(accs)) if accs else None,
                'by_floor': floor_table([r['label'] for r in records],
                                        [r[key] for r in records]),
                'by_generator': generators,
                'by_architecture': by_group(records, key, 'architecture'),
                'by_format': by_group(records, key, 'format'),
            }
            report['passes'][name] = section
            self.print_pass(name, section)
        return report

    def print_pass(self, name, section):
        overall = section['overall']
        self.say('')
        self.say(f'== {name}: {overall["images"]} images, '
                 f'{overall["real"]} real, {overall["generated"]} generated')
        self.say(f'   ROC AUC {overall["roc_auc"]}  accuracy at 0.50 '
                 f'{overall["accuracy_at_half"]}')
        self.say(f'   photographs read as photographs '
                 f'{overall["photographs_read_as_photographs"]}, generated read '
                 f'as generated {overall["generated_read_as_generated"]}')
        self.say(f'   mean AP over generators {section["mean_ap_over_generators"]}, '
                 f'mean accuracy {section["mean_accuracy_over_generators"]}')
        self.say('')
        self.say(f'   {"floor":>5}  {"speaks":>7}  {"right":>7}  '
                 f'{"gen. right":>10}  {"photos accused":>14}')
        for row in section['by_floor']:
            self.say(
                f'   {row["floor"]:>5.2f}  {row["speaks_on"]:>7.4f}  '
                f'{_fmt(row["right_when_it_speaks"])}  '
                f'{_fmt(row["generated_calls_right"], 10)}  '
                f'{_fmt(row["photographs_called_generated"], 14)}'
            )
        self.say('')
        self.say('   by generator (accuracy at 0.50, AP):')
        for generator, row in section['by_generator'].items():
            self.say(f'     {generator:<36} {_fmt(row["accuracy_at_half"])}  '
                     f'{_fmt(row["average_precision"])}  '
                     f'({row["real"]} real, {row["generated"]} generated)')


def _fmt(value, width=7):
    return f'{"-":>{width}}' if value is None else f'{value:>{width}.4f}'
