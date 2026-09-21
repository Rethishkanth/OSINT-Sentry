"""Published fact-checks about the same subject as an article.

What this layer adds
--------------------
Everything else in Module 2 measures *coverage*: who else reported this, and
do their figures match. That is a machine reading text. This layer asks a
different question — **has a professional fact-checker already ruled on
something like this?** — and the answer, when there is one, was written by a
person who did the work.

The Google Fact Check Tools API indexes ClaimReview markup published by IFCN
signatories: Full Fact, PolitiFact, Snopes, AFP, Boom Live, Africa Check and
several hundred others. It is a lookup over their published work, not a
service that judges anything. So what comes back is a citation, and citations
are the one output of this platform that need no defending.

The honesty problem, and how it is handled
------------------------------------------
The match is the weak link. We search the index with the same terms we search
the news index with, and a query about a flood in Kerala will happily return a
fact-check of a *different* claim about a flood in Kerala. Two stories sharing
three proper nouns are not the same assertion.

So:

1. **A fact-check never moves the rating.** Not down, not up. A number that
   shifts on a keyword overlap is arithmetic wearing a finding's clothes, and
   this module has already had one of those. What the analyst gets instead is
   the publisher, the claim as the fact-checker stated it, their verdict in
   their own words, and a link — which is enough to settle the question in
   about ten seconds, by reading, which is the correct way to settle it.
2. **Close and loose matches are labelled differently** and never merged. A
   close match shares most of the article's search terms with the claim the
   fact-checker actually reviewed; a loose one shares enough to be worth a
   glance and no more.
3. **`textualRating` is the publisher's free text and is always shown as
   written.** The bucket below exists only to decide which lines deserve
   emphasis. Bucketing is lossy on purpose and never replaces the words:
   "Four Pinocchios", "Pants on Fire" and "Altered photo" are all FALSE here,
   and all three mean something more specific than that.
4. **No key means no answer, not an empty answer.** An unconfigured client
   reports `disabled`, exactly as `NullSearch` does — because a console that
   renders "no fact-checks found" when nobody looked is lying to an analyst.

What it cannot do
-----------------
It cannot tell you an unchecked claim is true; the overwhelming majority of
news has never been fact-checked by anyone, and absence here means only
absence. It cannot find a fact-check written in a language we did not ask for.
And it indexes published ClaimReview markup, so a fact-check that exists but
was never marked up is, to this layer, a fact-check that does not exist.
"""

from __future__ import annotations

import json
import logging
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime

from django.conf import settings
from django.utils.module_loading import import_string

logger = logging.getLogger(__name__)

CLIENT_VERSION = 'factcheck-v1'

DEFAULT_ENDPOINT = 'https://factchecktools.googleapis.com/v1alpha1/claims:search'
DEFAULT_TIMEOUT = 12.0
DEFAULT_MAX_RESULTS = 10
DEFAULT_LANGUAGE = 'en'
# Share of the article's search terms a reviewed claim must contain before the
# match is called close rather than loose.
DEFAULT_MIN_MATCH = 0.6
# Below two shared terms nothing is reported at all. One shared proper noun
# between an article and a fact-check is a coincidence, and coincidences
# printed next to the word "FALSE" are how a console libels someone.
MIN_SHARED_TERMS = 2


class Status:
    """Why the fact-check panel shows what it shows.

    The five are kept apart because they mean entirely different things to an
    analyst, and four of them are routinely collapsed into "no results" by
    software that should know better.
    """

    DISABLED = 'disabled'        # switched off in settings
    NO_KEY = 'no_key'            # configured, but nobody supplied credentials
    UNAVAILABLE = 'unavailable'  # we asked and the lookup failed
    NONE_FOUND = 'none_found'    # we asked, it answered, nothing matched
    FOUND = 'found'              # we asked, it answered, something matched


STATUS_LABELS = {
    Status.DISABLED: 'Fact-check lookup is switched off',
    Status.NO_KEY: 'Fact-check lookup has no API key',
    Status.UNAVAILABLE: 'Fact-check lookup did not complete',
    Status.NONE_FOUND: 'No published fact-check matched',
    Status.FOUND: 'Published fact-checks matched',
}

# Our bucket for the publisher's own wording. Lossy by design; see the module
# docstring. UNRATED is not a failure — it is the honest result for a rating
# whose meaning we cannot read off a phrase.
FALSE = 'false'
MIXED = 'mixed'
TRUE = 'true'
UNRATED = 'unrated'

VERDICT_LABELS = {
    FALSE: 'Rated false',
    MIXED: 'Rated partly false',
    TRUE: 'Rated true',
    UNRATED: 'Rating not machine-readable',
}

# Ordered: the first pattern that matches wins, so "mostly false" is caught
# before the bare "false" rule, and "mostly true" before the bare "true" one.
_RATING_RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(r'\b(?:mostly|largely|mainly)\s+false\b'), FALSE),
    (re.compile(r'\b(?:partly|partially|half)[\s-]+(?:false|true)\b'), MIXED),
    (re.compile(r'\bhalf[\s-]?true\b'), MIXED),
    (re.compile(r'\bmisleading\b|\bmissing context\b|\bneeds context\b'), MIXED),
    (re.compile(r'\bmixture\b|\bmixed\b'), MIXED),
    (re.compile(r'\b(?:mostly|largely|mainly)\s+true\b'), MIXED),
    (re.compile(r'\bpants on fire\b|\b(?:four|three)\s+pinocchios\b'), FALSE),
    (re.compile(r'\b(?:false|fake|hoax|fabricated|incorrect|untrue|debunked)\b'), FALSE),
    (re.compile(r'\b(?:altered|doctored|manipulated|miscaptioned)\b'), FALSE),
    (re.compile(r'\bscam\b|\bno evidence\b|\bunfounded\b|\bbaseless\b'), FALSE),
    (re.compile(r'\bsatire\b'), MIXED),
    (re.compile(r'\b(?:true|correct|accurate|confirmed)\b'), TRUE),
]


def normalise_rating(text: str) -> str:
    """Bucket a publisher's free-text rating. Returns UNRATED when unsure.

    Every fact-checker writes their own scale and several are deliberately
    playful about it. Guessing wrong in the FALSE direction puts an accusation
    on screen, so anything unrecognised stays UNRATED and the publisher's own
    words are shown instead.
    """
    lowered = (text or '').strip().lower()
    if not lowered:
        return UNRATED
    for pattern, bucket in _RATING_RULES:
        if pattern.search(lowered):
            return bucket
    return UNRATED


_WORD_RE = re.compile(r"[a-z0-9][a-z0-9'-]{2,}")


@dataclass
class FactCheckReview:
    """One published fact-check, as its publisher stated it."""

    claim: str                  # the claim the fact-checker reviewed
    rating: str                 # their own words, verbatim
    verdict: str                # our bucket; see normalise_rating
    publisher: str = ''
    publisher_site: str = ''
    url: str = ''
    title: str = ''
    claimant: str = ''
    reviewed_at: date | None = None
    language: str = ''
    matched_terms: list[str] = field(default_factory=list)
    match_strength: float = 0.0
    # True only when the reviewed claim carries most of the article's search
    # terms. A loose match is shown as a lead to follow, not as a ruling.
    close_match: bool = False

    @property
    def verdict_label(self) -> str:
        return VERDICT_LABELS.get(self.verdict, VERDICT_LABELS[UNRATED])

    def as_dict(self) -> dict:
        return {
            'claim': self.claim,
            'rating': self.rating,
            'verdict': self.verdict,
            'publisher': self.publisher,
            'publisher_site': self.publisher_site,
            'url': self.url,
            'title': self.title,
            'claimant': self.claimant,
            'reviewed_at': self.reviewed_at.isoformat() if self.reviewed_at else None,
            'language': self.language,
            'matched_terms': self.matched_terms,
            'match_strength': self.match_strength,
            'close_match': self.close_match,
        }


@dataclass
class FactCheckOutcome:
    """What the lookup produced, including the reason it produced nothing."""

    reviews: list[FactCheckReview] = field(default_factory=list)
    status: str = Status.DISABLED
    detail: str = ''
    searched: str = ''

    @property
    def available(self) -> bool:
        """Did we actually get an answer? `none_found` counts; the rest do not."""
        return self.status in (Status.FOUND, Status.NONE_FOUND)

    @property
    def close(self) -> list[FactCheckReview]:
        return [r for r in self.reviews if r.close_match]

    @property
    def negative(self) -> list[FactCheckReview]:
        """Close matches a fact-checker rated false or partly false.

        Loose matches are excluded deliberately. This is the list the console
        puts a warning colour on, and a warning colour on a coincidence is
        worse than no panel at all.
        """
        return [r for r in self.reviews if r.close_match and r.verdict in (FALSE, MIXED)]


def rank(reviews: list[FactCheckReview], terms: list[str],
         min_match: float | None = None) -> list[FactCheckReview]:
    """Score each review against the article's search terms, best first.

    Reviews sharing fewer than MIN_SHARED_TERMS terms with the article are
    dropped outright rather than shown faintly: the index returns whatever is
    topically adjacent, and "adjacent to a claim rated false" is not a finding
    about this article.
    """
    if min_match is None:
        min_match = getattr(settings, 'CONTENT_FACT_CHECK_MIN_MATCH', DEFAULT_MIN_MATCH)

    wanted = [t.lower() for t in terms if t]
    if not wanted:
        return []

    kept: list[FactCheckReview] = []
    for review in reviews:
        haystack = ' '.join(
            _WORD_RE.findall(f'{review.claim} {review.title}'.lower())
        )
        matched = [t for t in wanted if t in haystack]
        if len(matched) < MIN_SHARED_TERMS:
            continue
        review.matched_terms = matched
        review.match_strength = round(len(matched) / len(wanted), 4)
        review.close_match = review.match_strength >= min_match
        kept.append(review)

    kept.sort(key=lambda r: (-r.match_strength, r.publisher))
    return kept


# ---------------------------------------------------------------------------
# Clients
# ---------------------------------------------------------------------------

class BaseFactCheckClient(ABC):
    """Contract for a source of published fact-checks."""

    name = 'fact-check'

    @abstractmethod
    def search(self, terms: list[str], language: str = '') -> FactCheckOutcome:
        """Look for published fact-checks matching these terms."""


class NullFactCheckClient(BaseFactCheckClient):
    """Contacts nobody. The offline switch, and the default in tests.

    Reports `disabled` rather than an empty list, so switching the layer off
    can never be read downstream as "no fact-checker has disputed this".
    """

    name = 'disabled'

    def search(self, terms, language='') -> FactCheckOutcome:
        return FactCheckOutcome(
            status=Status.DISABLED,
            detail='Fact-check lookup is switched off, so nothing was looked '
                   'for. This is not a finding about the article.',
            searched='',
        )


class GoogleFactCheckClient(BaseFactCheckClient):
    """Google's Fact Check Tools API — an index of publishers' ClaimReview.

    Key-gated. Without `CONTENT_FACT_CHECK_API_KEY` it reports `no_key` and
    makes no request at all: a 403 from an unauthenticated call would arrive
    as a lookup failure, which is a different and more alarming thing than a
    feature nobody has configured yet.

    Note on robots.txt, since Module 2 is otherwise strict about it: this is a
    documented JSON API accessed with credentials Google issued for it, not a
    crawl of a web page, so the crawler rules the rest of this module obeys do
    not apply here. The API's own quota does, and a 429 is reported as
    `unavailable`.
    """

    name = 'google-factcheck'

    def __init__(self, api_key=None, endpoint=None, timeout=None):
        self.api_key = api_key if api_key is not None else getattr(
            settings, 'CONTENT_FACT_CHECK_API_KEY', ''
        )
        self.endpoint = endpoint or getattr(
            settings, 'CONTENT_FACT_CHECK_ENDPOINT', DEFAULT_ENDPOINT
        )
        self.timeout = timeout or getattr(
            settings, 'CONTENT_FACT_CHECK_TIMEOUT', DEFAULT_TIMEOUT
        )
        self.max_results = getattr(
            settings, 'CONTENT_FACT_CHECK_MAX_RESULTS', DEFAULT_MAX_RESULTS
        )
        self.language = getattr(
            settings, 'CONTENT_FACT_CHECK_LANGUAGE', DEFAULT_LANGUAGE
        )
        self.user_agent = getattr(
            settings, 'CONTENT_USER_AGENT',
            'OSINT-Sentry/1.0 (+fact-check lookup; contact: admin@localhost)',
        )

    # -- helpers ----------------------------------------------------------

    @staticmethod
    def _parse_date(value: str) -> date | None:
        raw = (value or '').strip()
        if not raw:
            return None
        for fmt in ('%Y-%m-%dT%H:%M:%SZ', '%Y-%m-%dT%H:%M:%S%z', '%Y-%m-%d'):
            try:
                return datetime.strptime(raw, fmt).date()
            except ValueError:
                continue
        return None

    def _fetch(self, query: str, language: str) -> tuple[dict | None, str]:
        """One request. Returns (payload, failure detail)."""
        params = urllib.parse.urlencode({
            'query': query,
            'languageCode': language,
            'pageSize': str(self.max_results),
            'key': self.api_key,
        })
        request = urllib.request.Request(
            f'{self.endpoint}?{params}',
            headers={'User-Agent': self.user_agent, 'Accept': 'application/json'},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read(2_000_000).decode('utf-8', 'replace')), ''
        except urllib.error.HTTPError as exc:
            code = exc.code
            exc.close()
            if code in (401, 403):
                return None, (f'the API rejected the key (HTTP {code}). Check that '
                              'the Fact Check Tools API is enabled for it.')
            if code == 429:
                return None, 'the API quota is exhausted for now (HTTP 429).'
            return None, f'the API returned HTTP {code}.'
        except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
            return None, f'the API was unreachable ({exc.__class__.__name__}).'
        except (ValueError, json.JSONDecodeError) as exc:
            return None, f'the API sent a response we could not read ({exc}).'

    @staticmethod
    def _reviews_in(payload: dict) -> list[FactCheckReview]:
        """Flatten the API's claim -> claimReview nesting.

        One claim can carry several reviews by different publishers, and that
        is worth keeping: two fact-checkers agreeing independently is a
        stronger citation than one, and two disagreeing is worth seeing.
        """
        found: list[FactCheckReview] = []
        for claim in payload.get('claims') or []:
            text = (claim.get('text') or '').strip()
            claimant = (claim.get('claimant') or '').strip()
            for review in claim.get('claimReview') or []:
                publisher = review.get('publisher') or {}
                rating = (review.get('textualRating') or '').strip()
                found.append(FactCheckReview(
                    claim=text,
                    rating=rating,
                    verdict=normalise_rating(rating),
                    publisher=(publisher.get('name') or '').strip(),
                    publisher_site=(publisher.get('site') or '').strip(),
                    url=(review.get('url') or '').strip(),
                    title=(review.get('title') or '').strip(),
                    claimant=claimant,
                    reviewed_at=GoogleFactCheckClient._parse_date(
                        review.get('reviewDate') or ''
                    ),
                    language=(review.get('languageCode') or '').strip(),
                ))
        return found

    # -- the contract -----------------------------------------------------

    def search(self, terms, language='') -> FactCheckOutcome:
        searched = 'Google Fact Check Tools'

        if not self.api_key:
            return FactCheckOutcome(
                status=Status.NO_KEY,
                detail='No CONTENT_FACT_CHECK_API_KEY is set, so no lookup was '
                       'made. Nothing here says anything about the article.',
                searched=searched,
            )
        if not terms:
            return FactCheckOutcome(
                status=Status.NONE_FOUND,
                detail='The article yielded no usable search terms, so there '
                       'was nothing to look up.',
                searched=searched,
            )

        payload, failure = self._fetch(' '.join(terms), language or self.language)
        if payload is None:
            return FactCheckOutcome(
                status=Status.UNAVAILABLE,
                detail=f'Looked for published fact-checks but {failure} '
                       'No conclusion is drawn from a lookup that did not run.',
                searched=searched,
            )

        ranked = rank(self._reviews_in(payload), terms)
        if not ranked:
            return FactCheckOutcome(
                status=Status.NONE_FOUND,
                detail='No published fact-check in the index covers this '
                       'subject. Most news is never fact-checked by anyone, so '
                       'this is the usual result and means only that.',
                searched=searched,
            )
        return FactCheckOutcome(
            reviews=ranked,
            status=Status.FOUND,
            detail=f'{len(ranked)} published fact-check(s) share this story\'s '
                   'subject. Whether any of them reviewed this article\'s '
                   'actual claim is for you to judge by reading them.',
            searched=searched,
        )


def get_fact_check_client() -> BaseFactCheckClient:
    path = getattr(
        settings, 'CONTENT_FACT_CHECK_CLIENT',
        'content.factcheck.NullFactCheckClient',
    )
    return import_string(path)()
