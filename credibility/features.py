"""Feature extraction shared by credibility model training and inference.

Training reads a domain name and an RDAP record. Inference reads a
SourceFeatures built from a Source row. Both paths must produce the same
feature names in the same order or the model is being asked a different
question at serving time than it was trained on, which is the quietest way to
ship a model that is wrong, so both go through feature_row() below.

Deliberately excluded: has_https, has_publisher and content_item_count. The
first two are not knowable for the 9,776 training domains without another
10,000 requests, and the third is a fact about our database rather than about
the source. The heuristic scorer still reads all three; the model does not get
features it could only have learned from on one of the two paths.
"""

from __future__ import annotations

import collections
import math
import re
from datetime import datetime, timezone

# Token families from the published work on deceptive news domains. Kept as
# three named groups rather than a bag of words so a feature-importance
# readout says something a person can disagree with.
NEWS_WORDS = frozenset({
    'news', 'daily', 'times', 'post', 'report', 'reporter', 'herald',
    'tribune', 'gazette', 'journal', 'press', 'media', 'wire', 'chronicle',
    'observer', 'review', 'bulletin', 'dispatch', 'sentinel', 'star', 'sun',
    'globe', 'record', 'examiner', 'inquirer', 'telegraph', 'courier',
})
CHARGED_WORDS = frozenset({
    'patriot', 'patriots', 'freedom', 'truth', 'truther', 'liberty',
    'conservative', 'liberal', 'america', 'american', 'usa', 'real',
    'exposed', 'expose', 'wake', 'awake', 'alert', 'infowar', 'infowars',
    'resist', 'rant', 'fury', 'eagle', 'nation', 'republic', 'deplorable',
    'maga', 'woke', 'redpill', 'patriotic',
})
HEALTH_WORDS = frozenset({
    'health', 'natural', 'cure', 'heal', 'remedy', 'organic', 'detox',
    'vaccine', 'holistic', 'wellness', 'nutrition', 'herbal', 'superfood',
})

# A site on free hosting has no registration record of its own, which is a
# different situation from a registered domain whose age we failed to read.
FREE_HOSTS = (
    'blogspot.', 'wordpress.com', 'weebly.com', 'wixsite.com', 'tumblr.com',
    'medium.com', 'substack.com', 'blogger.com', 'github.io', 'neocities.org',
)

LEXICAL_COLUMNS = [
    'len_domain', 'len_stem', 'n_labels', 'n_hyphen', 'n_digits',
    'digit_ratio', 'vowel_ratio', 'entropy', 'n_tokens', 'max_token_len',
    'free_host', 'n_news_words', 'n_charged_words', 'n_health_words',
]
REGISTRATION_COLUMNS = [
    'reg_age_days', 'reg_age_known', 'n_status', 'has_hold',
    'has_transfer_lock', 'registrar_known', 'last_changed_days',
    'expires_in_days',
]
# 'tld' is categorical and handled separately by the estimator.
CATEGORICAL_COLUMNS = ['tld']
FEATURE_COLUMNS = LEXICAL_COLUMNS + REGISTRATION_COLUMNS + CATEGORICAL_COLUMNS


def _shannon_entropy(text: str) -> float:
    if not text:
        return 0.0
    counts = collections.Counter(text)
    n = len(text)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def _tokens(text: str) -> list[str]:
    return [t for t in re.split(r'[^a-z0-9]+', text.lower()) if t]


def lexical_features(domain: str) -> dict:
    """Everything derivable from the domain string alone."""
    name = (domain or '').strip().lower().strip('.')
    labels = name.split('.') if name else []
    stem = labels[0] if labels else ''
    word_part = '.'.join(labels[:-1]) if len(labels) > 1 else name
    toks = _tokens(word_part)

    digits = sum(c.isdigit() for c in name)
    vowels = sum(c in 'aeiou' for c in name)
    letters = sum(c.isalpha() for c in name) or 1
    length = len(name) or 1

    return {
        'len_domain': len(name),
        'len_stem': len(stem),
        'n_labels': len(labels),
        'n_hyphen': name.count('-'),
        'n_digits': digits,
        'digit_ratio': digits / length,
        'vowel_ratio': vowels / letters,
        'entropy': _shannon_entropy(stem),
        'n_tokens': len(toks),
        'max_token_len': max((len(t) for t in toks), default=0),
        'free_host': int(any(h in name for h in FREE_HOSTS)),
        'n_news_words': sum(t in NEWS_WORDS for t in toks),
        'n_charged_words': sum(t in CHARGED_WORDS for t in toks),
        'n_health_words': sum(t in HEALTH_WORDS for t in toks),
    }


def tld_of(domain: str) -> str:
    name = (domain or '').strip().lower().strip('.')
    return name.rsplit('.', 1)[-1] if '.' in name else ''


def registration_features(
    *,
    age_days: int | None = None,
    status: list[str] | str | None = None,
    registrar: str | None = None,
    last_changed_days: int | None = None,
    expires_in_days: int | None = None,
) -> dict:
    """Registration signals, with unknown left as None rather than zero.

    Zero would say "registered today". LightGBM splits on missing values
    natively, so an unknown age stays unknown all the way into the tree.
    """
    if isinstance(status, str):
        codes = [s.strip().lower() for s in status.split(',') if s.strip()]
    else:
        codes = [str(s).strip().lower() for s in (status or [])]

    return {
        'reg_age_days': age_days,
        'reg_age_known': int(age_days is not None),
        'n_status': len(codes),
        'has_hold': int(any('hold' in c for c in codes)),
        'has_transfer_lock': int(any('transfer prohibited' in c for c in codes)),
        'registrar_known': int(bool(registrar)),
        'last_changed_days': last_changed_days,
        'expires_in_days': expires_in_days,
    }


def feature_row(
    domain: str,
    *,
    age_days: int | None = None,
    status: list[str] | str | None = None,
    registrar: str | None = None,
    last_changed_days: int | None = None,
    expires_in_days: int | None = None,
) -> dict:
    """The single definition of a feature row, used by training and serving."""
    row = lexical_features(domain)
    row.update(registration_features(
        age_days=age_days,
        status=status,
        registrar=registrar,
        last_changed_days=last_changed_days,
        expires_in_days=expires_in_days,
    ))
    row['tld'] = tld_of(domain)
    return row


def parse_rdap_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def feature_row_from_rdap(domain: str, record: dict | None, *, now=None) -> dict:
    """Training path: a domain name plus whatever RDAP returned for it."""
    now = now or datetime.now(timezone.utc)
    if not record or record.get('error'):
        return feature_row(domain)

    events = {}
    for event in record.get('events') or []:
        parsed = parse_rdap_date(event.get('date'))
        if parsed:
            events[(event.get('action') or '').lower()] = parsed

    registered = events.get('registration')
    changed = events.get('last changed')
    expires = events.get('expiration')

    return feature_row(
        domain,
        age_days=(now - registered).days if registered else None,
        status=record.get('status'),
        registrar=record.get('registrar'),
        last_changed_days=(now - changed).days if changed else None,
        expires_in_days=(expires - now).days if expires else None,
    )


def feature_row_from_source(source_features) -> dict:
    """Serving path: the SourceFeatures the heuristic scorer already builds."""
    return feature_row(
        source_features.domain,
        age_days=source_features.domain_age_days,
        status=source_features.registry_status,
        registrar=source_features.registrar,
    )


def to_frame(rows, *, tld_categories=None):
    """Build the model's input frame, typed the one way the booster accepts.

    Both callers go through here. Scoring a single source means a frame whose
    registration columns are all None, which pandas types as `object` and
    LightGBM rejects outright - so the numeric columns are coerced explicitly
    and an unknown stays NaN, which the booster splits on natively.

    `tld_categories` pins the categorical to the values seen during training.
    A TLD absent from that list becomes NaN rather than an error, so a domain
    under a TLD the model has never met is scored as "TLD unknown" instead of
    taking down the request.
    """
    import pandas as pd

    frame = pd.DataFrame(list(rows), columns=FEATURE_COLUMNS)
    numeric = LEXICAL_COLUMNS + REGISTRATION_COLUMNS
    frame[numeric] = frame[numeric].apply(pd.to_numeric, errors='coerce')
    frame[numeric] = frame[numeric].astype('float64')
    categories = (sorted(frame['tld'].dropna().unique())
                  if tld_categories is None else list(tld_categories))
    frame['tld'] = pd.Categorical(frame['tld'], categories=categories)
    return frame, categories
