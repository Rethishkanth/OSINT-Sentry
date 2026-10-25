"""Assemble the training set for the credibility model.

Labels come from Media Bias/Fact Check, read from the public data dump the
MBFC browser extension ships. Those ratings are MBFC's editorial judgement,
not ground truth, and a model trained on them reproduces one organisation's
opinions: US-centric, contested in places, and assembled by people. Anything
the model later says carries that provenance, which is why the rating source
is written into the dataset file rather than left implicit.

Registration features are fetched over RDAP, one domain at a time, roughly
one request a second, resumable so a rerun never refetches. rdap.org is a
free community service and this stays a trickle rather than a crawl.

    manage.py build_credibility_dataset                 # labels only, fast
    manage.py build_credibility_dataset --rdap 1400     # plus registration
"""

from __future__ import annotations

import json
import random
import time
import urllib.error
import urllib.request
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from credibility.features import feature_row_from_rdap

MBFC_URL = (
    'https://raw.githubusercontent.com/drmikecrowe/mbfcext/main/docs/v5/data/'
    'sources.json'
)
RDAP_BOOTSTRAP = 'https://rdap.org/domain/'
USER_AGENT = 'osint-sentry/1.0 (credibility model training)'

# MBFC's six factual-reporting levels, collapsed to the question an analyst
# actually asks. 'mostly-factual' sits on the credible side because an outlet
# that is mostly factual is one you can read with care, not one you discard.
CREDIBLE = frozenset({'very-high', 'high', 'mostly-factual'})
NOT_CREDIBLE = frozenset({'mixed', 'low', 'very-low'})

DATA_DIR = Path(__file__).resolve().parents[2] / 'data'


class Command(BaseCommand):
    help = 'Download MBFC credibility labels and optionally fetch RDAP features.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--rdap', type=int, default=0, metavar='N',
            help='Fetch RDAP for N domains, balanced across the two classes. '
                 'Roughly one second each.',
        )
        parser.add_argument(
            '--delay', type=float, default=0.7,
            help='Seconds between RDAP requests (default 0.7).',
        )
        parser.add_argument(
            '--out', default=str(DATA_DIR / 'credibility_dataset.json'),
            help='Where to write the assembled dataset.',
        )
        parser.add_argument(
            '--refresh-labels', action='store_true',
            help='Re-download the MBFC dump even if it is already cached.',
        )

    # -- fetching ---------------------------------------------------------

    def _get(self, url, *, timeout=30):
        request = urllib.request.Request(
            url, headers={'User-Agent': USER_AGENT, 'Accept': 'application/json'}
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)

    def _load_labels(self, *, refresh):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        cached = DATA_DIR / 'mbfc_sources.json'

        if cached.exists() and not refresh:
            self.stdout.write(f'labels: reusing {cached.name}')
            return json.loads(cached.read_text(encoding='utf-8'))

        self.stdout.write('labels: downloading the MBFC dump...')
        try:
            payload = self._get(MBFC_URL, timeout=60)
        except Exception as exc:
            raise CommandError(f'could not download MBFC labels: {exc}') from exc
        cached.write_text(json.dumps(payload), encoding='utf-8')
        self.stdout.write(f'labels: saved {len(payload)} entries')
        return payload

    def _fetch_rdap(self, domain):
        url = RDAP_BOOTSTRAP + domain
        request = urllib.request.Request(
            url, headers={'User-Agent': USER_AGENT, 'Accept': 'application/json'}
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                body = json.load(response)
        except urllib.error.HTTPError as exc:
            return {'error': f'http_{exc.code}'}
        except Exception as exc:
            return {'error': type(exc).__name__}

        record = {'error': None, 'events': [], 'registrar': None,
                  'status': body.get('status') or []}
        for event in body.get('events') or []:
            record['events'].append({
                'action': event.get('eventAction'),
                'date': event.get('eventDate'),
            })
        for entity in body.get('entities') or []:
            if 'registrar' in (entity.get('roles') or []):
                vcard = (entity.get('vcardArray') or [None, []])[1]
                for item in vcard:
                    if item and item[0] == 'fn':
                        record['registrar'] = item[3]
                        break
        return record

    # -- main -------------------------------------------------------------

    def handle(self, *args, **options):
        raw = self._load_labels(refresh=options['refresh_labels'])

        rows = []
        for entry in raw:
            domain = (entry.get('domain') or '').strip().lower()
            reporting = (entry.get('reporting') or '').strip().lower()
            if not domain or '.' not in domain:
                continue
            if reporting not in CREDIBLE | NOT_CREDIBLE:
                continue
            rows.append({
                'domain': domain,
                'reporting': reporting,
                'label': int(reporting in CREDIBLE),
            })

        # One row per domain. MBFC lists a few outlets more than once.
        unique = {}
        for row in rows:
            unique.setdefault(row['domain'], row)
        rows = sorted(unique.values(), key=lambda r: r['domain'])

        credible = sum(r['label'] for r in rows)
        self.stdout.write(
            f'labelled domains: {len(rows)} '
            f'({credible} credible, {len(rows) - credible} not)'
        )

        rdap_cache = {}
        cache_path = DATA_DIR / 'rdap_cache.json'
        if cache_path.exists():
            rdap_cache = json.loads(cache_path.read_text(encoding='utf-8'))

        wanted = options['rdap']
        if wanted:
            # Only registrable domains. A blogspot subdomain has no RDAP
            # record of its own and asking for one is a wasted request.
            pool = [r for r in rows if r['domain'].count('.') == 1]
            positives = [r['domain'] for r in pool if r['label']]
            negatives = [r['domain'] for r in pool if not r['label']]
            rng = random.Random(42)
            rng.shuffle(positives)
            rng.shuffle(negatives)
            half = max(1, wanted // 2)
            sample = sorted(set(positives[:half] + negatives[:half]))

            todo = [d for d in sample if d not in rdap_cache]
            self.stdout.write(
                f'rdap: {len(sample)} sampled, {len(sample) - len(todo)} cached, '
                f'{len(todo)} to fetch (~{len(todo) * options["delay"] / 60:.0f} min)'
            )
            for index, domain in enumerate(todo, start=1):
                rdap_cache[domain] = self._fetch_rdap(domain)
                if index % 50 == 0:
                    cache_path.write_text(json.dumps(rdap_cache), encoding='utf-8')
                    self.stdout.write(f'  {index}/{len(todo)}')
                time.sleep(options['delay'])
            cache_path.write_text(json.dumps(rdap_cache), encoding='utf-8')

        resolved = 0
        for row in rows:
            record = rdap_cache.get(row['domain'])
            row['features'] = feature_row_from_rdap(row['domain'], record)
            row['rdap'] = bool(record and not record.get('error'))
            if row['features']['reg_age_known']:
                resolved += 1

        out_path = Path(options['out'])
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps({
            'label_source': 'Media Bias/Fact Check factual-reporting ratings',
            'label_source_url': MBFC_URL,
            'label_mapping': {
                'credible': sorted(CREDIBLE),
                'not_credible': sorted(NOT_CREDIBLE),
            },
            'n_rows': len(rows),
            'n_with_registration_date': resolved,
            'rows': rows,
        }, indent=1), encoding='utf-8')

        self.stdout.write(self.style.SUCCESS(
            f'wrote {out_path} - {len(rows)} rows, '
            f'{resolved} with a real registration date'
        ))
