"""Cross-referencing an article against other outlets.

What this measures, precisely
-----------------------------
Whether *independent outlets reported the same thing*. That is corroboration,
not truth. A widely repeated falsehood corroborates beautifully, and a genuine
scoop corroborates not at all until everyone catches up. The rating below is
useful for the question "is this the only place saying this?" and is not a
fact-check, which is why every report stores the terms it searched, the
outlets it found, and the arithmetic that produced the number.

Three things this deliberately gets right
-----------------------------------------
1. **A failed search is not a negative result.** If the index rate-limits us,
   the report says so and carries no score. Reporting "uncorroborated" when we
   simply could not look would be a false accusation dressed as a measurement.
2. **Syndication is not corroboration.** Forty outlets running the same wire
   copy is one source, not forty. Near-identical headlines are collapsed into
   a single group before anything is counted.
3. **Silence is not guilt.** A trustworthy outlet being first to report
   something is scored as uncorroborated, never as untrustworthy. Only
   Module 1 deciding the *source* is bad produces DO NOT TRUST.

What v2 changed, and why
------------------------
v1 rated almost everything 3.1/10 — which was the source-credibility half of
the formula with corroboration contributing exactly zero, on every article.
The cause was not the arithmetic. It was the query.

`terms_for` ranked a story's search terms by how often each proper noun
appeared anywhere on the fetched page, so navigation menus and sidebar chrome
outvoted the story. An article headlined "RBI intervention, dollar flows push
Indian rupee to two-month peak" was searched for as *Indian, Budget, share,
price* — not one word of which is in the headline — and the 139 unrelated
articles that came back were correctly rejected, one at a time, down to zero.

So v2:

* **builds the query from the headline out.** The headline is the claim being
  made. An entity in it outranks one repeated forty times in the page
  furniture, and terms this site prints on *every* article are recognised as
  furniture and discounted.
* **looks somewhere by default.** `ReferenceFeedSearch` consults a standing
  panel of major newsrooms whether or not anyone registered them as sources,
  so a fresh install is not asked to corroborate the world against two feeds.
* **reads the other outlets rather than skimming their headlines.** The best
  candidates are fetched in full, through the same robots-respecting, rate-
  limited fetcher as everything else, and matched on their actual text.
* **checks the article's specifics against that text.** See `claims`: the
  figures, sums, and dates the article asserts are looked for in what the
  other outlets printed, so the report can say *four outlets print the same
  number* — or *this one prints a different one*, which is the single most
  useful thing this module can tell an analyst.
* **shows its working.** Every component of the score is emitted as a
  structured reason with its own contribution, so the number is auditable
  instead of merely asserted.
"""

from __future__ import annotations

import json
import logging
import math
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone as dt_timezone

from django.conf import settings
from django.utils.module_loading import import_string

from core.domains import registrable_domain

from . import claims as claim_check
from .pipeline import STOPWORDS

logger = logging.getLogger(__name__)

ENGINE_VERSION = 'corroboration-v2'

DEFAULT_WINDOW_DAYS = 7
DEFAULT_MAX_QUERY_TERMS = 4
DEFAULT_MAX_CANDIDATES = 40
DEFAULT_MIN_SIMILARITY = 0.30
# Credibility assumed for an outlet we do not track. Neutral on purpose: a
# domain we have never scored is unknown, not suspect.
DEFAULT_UNKNOWN_CREDIBILITY = 50.0
# Other outlets' articles fetched in full per check. Each one is a real
# request to someone else's server, spaced by the crawl delay, so this trades
# directly against how long an analyst waits.
DEFAULT_DEEP_READS = 5
# The opening stretch of the article, where its own assertions live. Past this
# is related coverage and page furniture.
LEDE_CHARS = 2000

_WORD_RE = re.compile(r"[a-z0-9][a-z0-9'-]{2,}")
# Words that survive stopword filtering but say nothing about a story.
NEWSROOM_NOISE = frozenset({
    'news', 'says', 'said', 'report', 'reports', 'reported', 'update',
    'updates', 'live', 'latest', 'video', 'photos', 'read', 'more',
    'published', 'story', 'article', 'today', 'yesterday', 'week',
    # Days and months. They are capitalised, so the proper-noun extractor
    # offers them as entities, and they are worthless as search terms: every
    # story filed on a Friday says Friday, and no story is *about* Friday.
    'monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday',
    'sunday', 'january', 'february', 'march', 'april', 'june', 'july',
    'august', 'september', 'october', 'november', 'december',
})


class Verdict:
    """The tag shown next to the rating."""

    TRUSTWORTHY = 'trustworthy'
    LIKELY_RELIABLE = 'likely_reliable'
    PARTIALLY_CORROBORATED = 'partially_corroborated'
    UNCORROBORATED = 'uncorroborated'
    # Another outlet, read in full, printed a different figure for something
    # this article states. Not a ruling on which is right — see `claims`.
    DISPUTED = 'disputed'
    DO_NOT_TRUST = 'do_not_trust'
    UNAVAILABLE = 'unavailable'


@dataclass
class Candidate:
    """One other article that might be reporting the same thing."""

    url: str
    title: str
    domain: str
    snippet: str = ''
    published_at: datetime | None = None
    origin: str = ''          # which backend found it
    similarity: float = 0.0
    matched_terms: list[str] = field(default_factory=list)
    credibility: float | None = None
    # Set only once the page has actually been fetched and parsed. Its
    # emptiness is the difference between "this headline looks similar" and
    # "this outlet says the same thing", which the scorer treats differently.
    body: str = ''
    read_in_full: bool = False
    # Whether the overlap included a proper noun. Two stories sharing only
    # words like "police" or "rain" share a vocabulary, not a subject.
    entity_hit: bool = False


@dataclass
class SearchOutcome:
    """Results, plus whether the search actually happened.

    `available=False` means we could not look at all, which must never be
    scored as "nothing was found". `failed` names backends that did not
    answer while others did — a partial search, whose *negative* result is
    worthless even though its positive results are perfectly good.
    """

    candidates: list[Candidate]
    available: bool = True
    detail: str = ''
    searched: str = ''
    failed: list[str] = field(default_factory=list)


@dataclass
class Term:
    text: str
    weight: float
    is_entity: bool


# ---------------------------------------------------------------------------
# Query building
# ---------------------------------------------------------------------------

def _tokens(text: str) -> set[str]:
    return {
        w for w in _WORD_RE.findall((text or '').lower())
        if w not in STOPWORDS and w not in NEWSROOM_NOISE
    }


STEM_CHARS = 5


def _lede_hits(word: str, index: Counter) -> int:
    """How many times the article returns to a headline word.

    Matched on a short prefix rather than exactly, because a headline says
    "floods" where the copy underneath says "flooding" — and an exact match
    would score the story's own subject at zero for the crime of being
    inflected differently in the headline.
    """
    stem = word[:STEM_CHARS]
    return sum(
        count for token, count in index.items()
        if token.startswith(stem) or stem.startswith(token[:STEM_CHARS])
    )


TITLE_PHRASE_RE = re.compile(r"\b([A-Z][\w'’-]*(?:[ ]+[A-Z][\w'’-]*)*)\b")
# An all-caps run in a headline is an acronym — RBI, NATO, WHO — and those are
# the most findable terms a story has. The proper-noun pattern above already
# catches them, but they must survive the "is it just a capitalised first
# word" check that discards sentence-initial ordinary words.
ACRONYM_RE = re.compile(r'\b([A-Z]{2,6})\b')


def _boilerplate_terms(item) -> set[str]:
    """Terms this site prints on every page, rather than in this story.

    A newspaper's masthead, its section names, and its standing promos recur
    on every article it publishes; a story's actual subjects do not. So an
    entity this source has attached to several *other* items is furniture,
    and weighting a search by how often "Budget" appears in a navigation bar
    is how v1 came to search for a rupee story under "Indian Budget share
    price".

    Needs a few articles from a source before it can tell. Until then it
    returns nothing and the headline weighting below carries the work.
    """
    from django.db.models import Count

    from .models import Entity

    try:
        rows = (
            Entity.objects
            .filter(content_item__source_id=item.source_id)
            .exclude(content_item_id=item.pk)
            .values('text')
            .annotate(items=Count('content_item', distinct=True))
            .filter(items__gte=2)
        )
        return {row['text'].strip().lower() for row in rows if row['text']}
    except Exception:            # pragma: no cover - never fail a check on this
        logger.debug('Boilerplate detection unavailable', exc_info=True)
        return set()


def _headline_terms(title: str, lede: str = '') -> dict[str, Term]:
    """Proper nouns and acronyms from the headline, at the top weight.

    The headline is the assertion being made. Everything else on the page is
    context, navigation, or somebody else's story, so nothing outranks it.
    """
    terms: dict[str, Term] = {}
    text = (title or '').strip()
    if not text:
        return terms

    for match in TITLE_PHRASE_RE.finditer(text):
        phrase = match.group(1).strip()
        lowered = phrase.lower()
        if lowered in STOPWORDS or lowered in NEWSROOM_NOISE or len(lowered) < 3:
            continue
        # The first word of a headline is capitalised whatever it is, so on
        # its own it is no evidence of a proper noun.
        if match.start() == 0 and ' ' not in phrase and not phrase.isupper():
            continue
        terms[lowered] = Term(text=phrase, weight=4.0, is_entity=True)

    for match in ACRONYM_RE.finditer(text):
        acronym = match.group(1)
        lowered = acronym.lower()
        if lowered in STOPWORDS or lowered in NEWSROOM_NOISE:
            continue
        terms.setdefault(lowered, Term(text=acronym, weight=4.0, is_entity=True))

    # The remaining headline words. Not entities — matching only on these means
    # two stories share a vocabulary — but the good ones name the subject.
    #
    # Which are the good ones is settled by whether the article goes on to use
    # them. In "Tupac trial spotlights decades of US police failures",
    # "spotlights" appears once and never again: it is a verb the desk chose,
    # not a thing the story is about. "floods", in a story about flooding,
    # recurs in every other paragraph. How often the article returns to a
    # headline word is the cheapest available proxy for which kind of word it
    # is, and it separates the subject from the sub-editor.
    index = Counter(_WORD_RE.findall(lede))
    for word in _tokens(text):
        if word in terms:
            continue
        # Capped well below a proper noun found in the body: a name is a
        # better search term than a common word however central that word is,
        # because every outlet covering the story must use the name while each
        # phrases the rest differently.
        weight = 1.6 + min(0.8, 0.3 * _lede_hits(word, index))
        terms[word] = Term(text=word, weight=weight, is_entity=False)

    return terms


def terms_for(item) -> list[Term]:
    """The search terms for one ContentItem, strongest first.

    Ranked by *where* a term appears, not by how often. Position is the signal
    that survives scraping: a page's furniture repeats endlessly and sits
    everywhere, while a story's subject is in its headline and its opening
    paragraphs. Ranking by frequency, as v1 did, reliably picked the furniture.

    Entities outrank topic words because proper nouns are what make a story
    findable — "Mumbai" narrows a search, "flooding" does not.
    """
    title_tokens = _tokens(item.title)
    # The body, not the summary. The summary is three sentences, which is far
    # too little text for "how often does the article come back to this word"
    # to mean anything — every headline word would appear once and tie. Falls
    # back to the summary for feed-only items, which have no body.
    lede = ((item.body or '') or (item.summary or ''))[:LEDE_CHARS].lower()
    boilerplate = _boilerplate_terms(item)

    terms: dict[str, Term] = _headline_terms(item.title, lede)

    for entity in item.entities.all()[:20]:
        text = entity.text.strip()
        if not text:
            continue
        key = text.lower()
        if key in STOPWORDS or key in NEWSROOM_NOISE:
            continue

        # Frequency is a tie-breaker now, not the ranking. It is capped low
        # enough that no amount of repetition promotes a sidebar link above a
        # word from the headline.
        bump = min(0.4, (entity.mention_count or 1) / 25.0)
        if key in terms or _tokens(text) & title_tokens:
            weight = 4.0 + bump           # in the headline: the story's subject
        elif key in lede:
            # A multi-word capitalised phrase really is a name. A single
            # capitalised word from the body is as likely to be an ordinary
            # word that happened to open a sentence — "Heavy rainfall closed…"
            # is not a story about somebody called Heavy — so it is ranked
            # below the headline's own vocabulary rather than above it.
            weight = (2.2 if ' ' in text else 1.9) + bump
        else:
            weight = 0.7 + bump           # further down: probably not this story

        if key in boilerplate:
            # On every page this site publishes, so it identifies the site
            # rather than the story.
            weight *= 0.2

        existing = terms.get(key)
        if existing is None or weight > existing.weight:
            terms[key] = Term(text=text, weight=weight, is_entity=True)

    for hit in item.content_topics.select_related('topic')[:8]:
        label = hit.topic.label.strip()
        key = label.lower()
        if not key or key in terms or key in STOPWORDS or key in NEWSROOM_NOISE:
            continue
        weight = 0.9 + min(0.6, (hit.weight or 0) * 20)
        if key in title_tokens:
            weight += 1.0
        elif key not in lede:
            weight *= 0.5
        if key in boilerplate:
            weight *= 0.2
        terms[key] = Term(text=label, weight=weight, is_entity=False)

    return sorted(terms.values(), key=lambda t: (-t.weight, -len(t.text)))


def query_terms(terms: list[Term], limit: int | None = None) -> list[str]:
    """The handful of terms actually sent to a search index.

    Kept short on purpose: search backends AND their terms, so a long query
    returns nothing at all rather than returning something imperfect.
    """
    limit = limit or getattr(settings, 'CORROBORATION_MAX_QUERY_TERMS', DEFAULT_MAX_QUERY_TERMS)

    # Strongest first, whatever kind they are. v1 capped entities at half the
    # query and filled the rest with topic words, so a headline full of good
    # proper nouns was diluted with generic vocabulary before being sent to a
    # backend that ANDs its terms.
    chosen: list[str] = []
    for term in terms:
        if len(chosen) >= limit:
            break
        lowered = term.text.lower()
        if any(lowered in c.lower() or c.lower() in lowered for c in chosen):
            continue              # skip a term already implied by another
        chosen.append(term.text)

    # Every term is ANDed, so a fifth one does not refine the search, it
    # empties it. Precision is the similarity filter's job, not the query's.
    return chosen[:limit]


def similarity_of(candidate: Candidate, terms: list[Term]) -> tuple[float, list[str], bool]:
    """Weighted overlap between a candidate and the article's terms.

    Returns (similarity, matched terms, whether any entity matched). The
    entity flag matters: overlapping only on words like "police" or "rain"
    means two stories share a vocabulary, not a subject.

    Normalised against the article's *strongest few* terms rather than all of
    them. Most candidates are known only by their headline, and a headline
    cannot contain twelve terms — dividing by all of them scored genuinely
    matching coverage at 25% and threw it away. The question worth asking is
    how many of the article's key terms this candidate carries, so the
    denominator is those, and the result is capped at 1.

    Once a candidate has been fetched in full its body is matched too, which
    is both fairer and stricter: an outlet whose headline shares one word but
    whose article never mentions the subject stops scoring, and one that
    covers the story properly under a cryptic headline starts.
    """
    haystack = f'{candidate.title} {candidate.snippet} {candidate.body}'.lower()
    haystack_tokens = _tokens(haystack)

    top_k = getattr(settings, 'CORROBORATION_SIMILARITY_TERMS', 5)
    total = sum(t.weight for t in terms[:top_k]) or 1.0
    hit = 0.0
    matched: list[str] = []
    entity_hit = False

    for term in terms:
        lowered = term.text.lower()
        # Multi-word terms must appear as a phrase; single words match tokens.
        found = lowered in haystack if ' ' in lowered else lowered in haystack_tokens
        if not found:
            continue
        hit += term.weight
        matched.append(term.text)
        entity_hit = entity_hit or term.is_entity

    return min(1.0, hit / total), matched, entity_hit


# ---------------------------------------------------------------------------
# Search backends
# ---------------------------------------------------------------------------

class BaseCorroborationSearch(ABC):
    """Where to look for other coverage of the same story."""

    name = 'base'

    @abstractmethod
    def search(self, terms: list[str], published_at, exclude_domain: str) -> SearchOutcome:
        ...


class NullSearch(BaseCorroborationSearch):
    """Looks nowhere. Reports itself unavailable rather than empty, so it can
    never be mistaken for evidence that nothing corroborates a story."""

    name = 'none'

    def search(self, terms, published_at, exclude_domain) -> SearchOutcome:
        return SearchOutcome(
            candidates=[], available=False, failed=[self.name],
            detail='Corroboration search is disabled.', searched='nothing',
        )


class StoredItemSearch(BaseCorroborationSearch):
    """Searches what this system has already ingested.

    No third party and no network: it looks through the ContentItems stored
    from every *other* source. Narrow, but the matches it finds come with a
    real Module 1 credibility score attached.
    """

    name = 'stored'

    def search(self, terms, published_at, exclude_domain) -> SearchOutcome:
        from django.db.models import Q

        from .models import ContentItem

        if not terms:
            return SearchOutcome([], available=True, searched='stored items')

        query = Q()
        for term in terms:
            query |= Q(title__icontains=term) | Q(summary__icontains=term)

        rows = (
            ContentItem.objects
            .filter(query)
            .exclude(source__domain=exclude_domain)
            .select_related('source')
            .order_by('-published_at')[:DEFAULT_MAX_CANDIDATES]
        )

        candidates = [
            Candidate(
                url=row.url,
                title=row.title or row.url,
                domain=row.source.domain,
                snippet=(row.summary or row.body or '')[:600],
                published_at=row.published_at,
                origin=self.name,
            )
            for row in rows
        ]
        return SearchOutcome(candidates, available=True, searched='items already ingested')


# Feeds fetched during corroboration, so checking five articles in a row does
# not re-request the same feeds five times. Process-local, like the rate
# limiter next door, and for the same reason: it is right for one worker.
_feed_cache: dict[str, tuple[float, list]] = {}
_feed_cache_lock = threading.Lock()


class TrackedFeedSearch(BaseCorroborationSearch):
    """Reads the live feeds of the other outlets this system tracks.

    Wider than the stored items — it sees what those outlets published in the
    last few hours, whether or not we have ingested it — and it goes through
    the same robots-respecting, rate-limited fetcher as everything else in
    Module 2.

    Only sources with a feed we have already located take part. Running full
    discovery here would mean probing ten paths per source per check, which
    is both slow and rude.
    """

    name = 'feeds'

    def __init__(self):
        self.ttl = getattr(settings, 'CORROBORATION_FEED_CACHE_TTL', 900.0)
        self.max_sources = getattr(settings, 'CORROBORATION_MAX_FEEDS', 12)

    def _entries(self, feed_url: str):
        with _feed_cache_lock:
            cached = _feed_cache.get(feed_url)
            if cached and (time.time() - cached[0]) < self.ttl:
                return cached[1]

        from . import robots
        from .feeds import parse_feed
        from .http import request

        try:
            response = request(feed_url, on_hop=robots.check)
            entries = parse_feed(response.body, base_url=response.url)
        except Exception as exc:
            logger.info('Corroboration could not read feed %s: %s', feed_url, exc)
            raise

        with _feed_cache_lock:
            _feed_cache[feed_url] = (time.time(), entries)
        return entries

    def search(self, terms, published_at, exclude_domain) -> SearchOutcome:
        from core.models import Source

        sources = list(
            Source.objects
            .filter(is_active=True, feed_status='ok')
            .exclude(domain=exclude_domain)
            .exclude(feed_url='')[: self.max_sources]
        )
        if not sources:
            return SearchOutcome(
                [], available=True, searched='no tracked feeds are known yet',
            )

        candidates, failures = [], 0
        for source in sources:
            try:
                entries = self._entries(source.feed_url)
            except Exception:
                failures += 1
                continue
            for entry in entries:
                candidates.append(Candidate(
                    url=entry.url,
                    title=entry.title,
                    domain=source.domain,
                    snippet=(entry.summary or '')[:600],
                    published_at=entry.published_at,
                    origin=self.name,
                ))

        # Every feed failing is a failed search; some failing is a narrower
        # one, which only matters if we end up finding nothing.
        all_failed = failures == len(sources)
        return SearchOutcome(
            candidates,
            available=not all_failed,
            failed=[self.name] if failures else [],
            detail=f'{failures} of {len(sources)} tracked feeds did not respond.' if failures else '',
            searched=f'live feeds of {len(sources) - failures} tracked outlet(s)',
        )


class ReferenceFeedSearch(TrackedFeedSearch):
    """Reads a standing panel of major newsrooms, tracked here or not.

    This is the backend that answers "who else is reporting this?" on a fresh
    install. `TrackedFeedSearch` above can only see outlets somebody already
    added as a Source, which on a new deployment is one or two — so every
    story came back uncorroborated, truthfully but uselessly, because nothing
    had actually been consulted.

    The outlets in `reference_outlets` are consulted regardless of
    registration. None of them is treated as authoritative: an outlet we do
    not track is scored at neutral credibility, exactly like any other
    unknown domain, so appearing on this list buys a match nothing beyond
    being looked at. See that module for how the panel was chosen.
    """

    name = 'reference'

    def __init__(self):
        super().__init__()
        self.max_sources = getattr(settings, 'CORROBORATION_MAX_REFERENCE_FEEDS', 10)

    def search(self, terms, published_at, exclude_domain) -> SearchOutcome:
        from .reference_outlets import feeds_excluding

        panel = feeds_excluding(exclude_domain)[: self.max_sources]
        if not panel:
            return SearchOutcome([], available=True, searched='no reference outlets')

        candidates, failures = [], 0
        for domain, feed_url, _label in panel:
            try:
                entries = self._entries(feed_url)
            except Exception:
                failures += 1
                continue
            for entry in entries:
                candidates.append(Candidate(
                    url=entry.url,
                    title=entry.title,
                    domain=domain,
                    snippet=(entry.summary or '')[:600],
                    published_at=entry.published_at,
                    origin=self.name,
                ))

        reached = len(panel) - failures
        return SearchOutcome(
            candidates,
            # Some outlets being unreachable narrows the search; all of them
            # being unreachable means it did not happen.
            available=reached > 0,
            failed=[self.name] if failures else [],
            detail=(
                f'{failures} of {len(panel)} reference outlets did not respond.'
                if failures else ''
            ),
            searched=f'{reached} major newsroom feed(s)',
        )


class GdeltSearch(BaseCorroborationSearch):
    """GDELT's DOC 2.0 API: a free, key-less index of global news coverage.

    Chosen for the same reason Module 1 uses RDAP — it answers the question
    directly, needs no account, and adds no dependency. It rate-limits hard,
    so a 429 is reported as *unavailable*, never as an empty result.
    """

    name = 'gdelt'
    endpoint = 'https://api.gdeltproject.org/api/v2/doc/doc'

    def __init__(self):
        self.timeout = getattr(settings, 'CORROBORATION_SEARCH_TIMEOUT', 20.0)
        self.max_records = getattr(settings, 'CORROBORATION_MAX_RECORDS', 30)
        self.attempts = max(1, getattr(settings, 'CORROBORATION_SEARCH_ATTEMPTS', 3))
        self.retry_delay = getattr(settings, 'CORROBORATION_RETRY_DELAY', 5.0)
        self.user_agent = getattr(
            settings, 'CONTENT_USER_AGENT',
            'OSINT-Sentry/1.0 (+corroboration; contact: admin@localhost)',
        )

    @staticmethod
    def _phrase(term: str) -> str:
        return f'"{term}"' if ' ' in term.strip() else term

    def _window(self, published_at) -> str:
        days = getattr(settings, 'CORROBORATION_WINDOW_DAYS', DEFAULT_WINDOW_DAYS)
        if published_at is None:
            return f'{days * 24}h'
        # Cover the window either side of publication, clamped to what the
        # API will answer for.
        age = (datetime.now(dt_timezone.utc) - published_at).days
        return f'{max(1, min(365, age + days)) * 24}h'

    @staticmethod
    def _parse_date(value: str) -> datetime | None:
        try:
            return datetime.strptime(value, '%Y%m%dT%H%M%SZ').replace(tzinfo=dt_timezone.utc)
        except (TypeError, ValueError):
            return None

    def search(self, terms, published_at, exclude_domain) -> SearchOutcome:
        if not terms:
            return SearchOutcome([], available=True, searched='GDELT')

        params = urllib.parse.urlencode({
            'query': ' '.join(self._phrase(t) for t in terms),
            'mode': 'artlist',
            'format': 'json',
            'maxrecords': str(self.max_records),
            'timespan': self._window(published_at),
            'sort': 'hybridrel',
        })
        url = f'{self.endpoint}?{params}'

        payload = None
        detail = ''
        for attempt in range(self.attempts):
            try:
                request = urllib.request.Request(url, headers={
                    'User-Agent': self.user_agent,
                    'Accept': 'application/json',
                })
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    payload = json.loads(response.read(4_000_000).decode('utf-8', 'replace'))
                break
            except urllib.error.HTTPError as exc:
                detail = f'GDELT returned HTTP {exc.code}'
                exc.close()
            except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
                detail = f'GDELT unreachable ({exc.__class__.__name__})'
            except (ValueError, json.JSONDecodeError) as exc:
                detail = f'GDELT sent a malformed response ({exc})'
            if attempt + 1 < self.attempts:
                # Exponential: GDELT's 429 is a shared-IP throttle that clears
                # in seconds, so backing off properly is the difference
                # between an answer and a shrug.
                time.sleep(self.retry_delay * (2 ** attempt))

        if payload is None:
            # Unavailable, emphatically not empty.
            return SearchOutcome(
                [], available=False, failed=[self.name],
                detail=f'{detail}. No conclusion drawn — this is a failed lookup, '
                       'not an absence of coverage.',
                searched='GDELT',
            )

        candidates = []
        for article in payload.get('articles') or []:
            domain = registrable_domain(article.get('domain') or '')
            if not domain or domain == exclude_domain:
                continue
            candidates.append(Candidate(
                url=article.get('url') or '',
                title=(article.get('title') or '').strip(),
                domain=domain,
                published_at=self._parse_date(article.get('seendate', '')),
                origin=self.name,
            ))
        return SearchOutcome(candidates, available=True, searched='GDELT global news index')


class CompositeSearch(BaseCorroborationSearch):
    """Every backend at once.

    Availability is deliberately not a simple OR. An earlier version reported
    the whole search "available" whenever any one backend answered, and since
    the stored-item backend never fails and usually finds nothing, a
    rate-limited GDELT silently became a confident "uncorroborated" on a
    story that had never actually been cross-referenced. Failures are carried
    through in `failed` so the scorer can refuse to draw a negative
    conclusion from a search that did not fully happen.
    """

    name = 'composite'

    def __init__(self, backends=None):
        if backends is None:
            paths = getattr(settings, 'CORROBORATION_BACKENDS', (
                'content.corroborate.StoredItemSearch',
                'content.corroborate.TrackedFeedSearch',
                'content.corroborate.ReferenceFeedSearch',
                'content.corroborate.GdeltSearch',
            ))
            backends = [import_string(path)() for path in paths]
        self.backends = backends

    def search(self, terms, published_at, exclude_domain) -> SearchOutcome:
        candidates: list[Candidate] = []
        working = 0
        searched, details, failed = [], [], []

        for backend in self.backends:
            try:
                outcome = backend.search(terms, published_at, exclude_domain)
            except Exception as exc:      # a backend must not take the rest down
                logger.exception('Corroboration backend %s failed', backend.name)
                details.append(f'{backend.name}: {exc}')
                failed.append(backend.name)
                continue
            candidates.extend(outcome.candidates)
            if outcome.available:
                working += 1
            failed.extend(outcome.failed)
            if outcome.searched:
                searched.append(outcome.searched)
            if outcome.detail:
                details.append(outcome.detail)

        return SearchOutcome(
            candidates=candidates,
            available=working > 0,
            failed=failed,
            detail=' '.join(details),
            searched=', '.join(searched),
        )


def get_search() -> BaseCorroborationSearch:
    path = getattr(
        settings, 'CORROBORATION_SEARCH', 'content.corroborate.CompositeSearch'
    )
    return import_string(path)()


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

@dataclass
class Reason:
    """One line of the score's working.

    `effect` is what this contributed to the rating out of 10, signed, or None
    when the line is context rather than arithmetic. Kept structured rather
    than prose so the console can show the sum and an analyst can see which
    part of a rating to argue with.
    """

    label: str
    detail: str
    effect: float | None = None

    def as_dict(self) -> dict:
        return {'label': self.label, 'detail': self.detail, 'effect': self.effect}


@dataclass
class CorroborationResult:
    score: float | None            # out of 10; None when the search failed
    verdict: str
    headline: str                  # the tag, in words
    rationale: str
    keywords: list[str]
    matches: list[Candidate]
    sources_checked: int
    independent_sources: int
    searched: str
    reasons: list[Reason] = field(default_factory=list)
    claims: list = field(default_factory=list)
    claims_checked: int = 0
    claims_agreed: int = 0
    claims_disputed: int = 0
    outlets_read: int = 0          # other outlets fetched in full, not skimmed
    # Published fact-checks about the same subject. Reported beside the score
    # and deliberately absent from the arithmetic that produced it; see
    # content.factcheck for why a keyword match must not move a number.
    fact_checks: list = field(default_factory=list)
    fact_check_status: str = 'disabled'
    fact_check_detail: str = ''
    engine_version: str = ENGINE_VERSION


VERDICT_LABELS = {
    Verdict.TRUSTWORTHY: 'TRUSTWORTHY',
    Verdict.LIKELY_RELIABLE: 'LIKELY RELIABLE',
    Verdict.PARTIALLY_CORROBORATED: 'PARTIALLY CORROBORATED',
    Verdict.UNCORROBORATED: 'UNCORROBORATED',
    Verdict.DISPUTED: 'DISPUTED BY OTHERS',
    Verdict.DO_NOT_TRUST: 'DO NOT TRUST',
    Verdict.UNAVAILABLE: 'COULD NOT CHECK',
}


# Full pages read during this process's lifetime, so re-checking an article
# does not re-fetch the same five outlets. Process-local and short-lived, like
# the feed cache above and for the same reason.
_page_cache: dict[str, tuple[float, str]] = {}
_page_cache_lock = threading.Lock()


def read_candidates(candidates: list[Candidate], limit: int) -> tuple[list[tuple[str, str]], int, list[str]]:
    """Fetch the best candidates in full, so they can be matched on their text.

    This is the step that makes the rating mean something. A headline is nine
    words; deciding whether two newsrooms are describing the same event from
    nine words each is guesswork, and no figure an article states can possibly
    be checked against a headline that does not contain it.

    Every request goes through the same fetcher as the rest of Module 2 —
    robots.txt honoured, per-host rate limit honoured, size and redirect caps
    honoured, private hosts refused. Outlets that are not tracked Sources are
    read on exactly the same terms as ones that are; being unregistered is
    not a reason to treat a site differently, in either direction.

    Returns (evidence, pages read, domains that could not be read).
    """
    from . import robots
    from .extract import extract_article
    from .http import request

    ttl = getattr(settings, 'CORROBORATION_PAGE_CACHE_TTL', 1800.0)
    evidence: list[tuple[str, str]] = []
    failed: list[str] = []
    read = 0

    for candidate in candidates[:limit]:
        if not candidate.url:
            continue

        with _page_cache_lock:
            cached = _page_cache.get(candidate.url)
        if cached and (time.time() - cached[0]) < ttl:
            evidence.append((candidate.domain, cached[1]))
            candidate.body = cached[1]
            read += 1
            continue

        try:
            response = request(candidate.url, on_hop=robots.check)
            article = extract_article(response.text(), url=response.url)
        except Exception as exc:
            # One outlet refusing us is normal and says nothing about the
            # story. It narrows the evidence; it does not damn the article.
            logger.info('Could not read %s for corroboration: %s', candidate.url, exc)
            failed.append(candidate.domain)
            continue

        text = f'{article.title}\n{article.description}\n{article.body}'.strip()
        if not text:
            failed.append(candidate.domain)
            continue

        with _page_cache_lock:
            _page_cache[candidate.url] = (time.time(), text)
        candidate.body = text
        evidence.append((candidate.domain, text))
        read += 1

    return evidence, read, failed


def _normalised_headline(title: str) -> str:
    """A key for spotting the same wire copy republished under many mastheads."""
    words = _WORD_RE.findall((title or '').lower())
    return ' '.join(words[:9])


def _credibility_for(domains: set[str]) -> dict[str, float]:
    """Module 1 scores for the outlets we already track; neutral for the rest."""
    from core.models import Source

    scores = {}
    rows = Source.objects.filter(domain__in=domains).prefetch_related('credibility_scores')
    for source in rows:
        latest = source.credibility_scores.first()
        if latest:
            scores[source.domain] = latest.credibility_score
    return scores


def _verdict_for(score: float, source_credibility: float, independent: int,
                 disputed: int = 0) -> str:
    """Turn the number into a tag, with three guards that matter.

    Being first to report something is not dishonesty, so an outlet Module 1
    rates well is never tagged DO NOT TRUST merely for standing alone. A
    source Module 1 has already condemned does not get rehabilitated by a
    handful of keyword matches. And a concrete conflict — another outlet, read
    in full, printing a different figure — is a specific, checkable finding
    that deserves its own tag rather than being averaged into a number.
    """
    if source_credibility < 30:
        return Verdict.DO_NOT_TRUST
    if disputed:
        return Verdict.DISPUTED
    if independent == 0 and source_credibility >= 55:
        return Verdict.UNCORROBORATED
    if score >= 7.5:
        return Verdict.TRUSTWORTHY
    if score >= 6.0:
        return Verdict.LIKELY_RELIABLE
    if score >= 4.0:
        return Verdict.PARTIALLY_CORROBORATED
    if score >= 2.0:
        return Verdict.UNCORROBORATED
    return Verdict.DO_NOT_TRUST


def _shortlist(item, candidates, terms, own_domain, window) -> list[Candidate]:
    """Candidates plausibly about the same story, before anything is fetched.

    Deliberately looser than the final filter. Most candidates arrive as a
    headline and nothing else, and a headline that shares one strong proper
    noun with the article is worth *reading* even though it has not yet earned
    a place in the count. The strict test is applied after the page has been
    read, when there is something real to apply it to.
    """
    prefilter = min(
        getattr(settings, 'CORROBORATION_MIN_SIMILARITY', DEFAULT_MIN_SIMILARITY),
        getattr(settings, 'CORROBORATION_PREFILTER_SIMILARITY', 0.15),
    )

    shortlist: list[Candidate] = []
    for candidate in candidates:
        if not candidate.url or candidate.domain == own_domain:
            continue
        if (item.published_at and candidate.published_at
                and abs(candidate.published_at - item.published_at) > window):
            # Same words, different year. The 2005 Mumbai floods do not
            # corroborate a report of flooding this week.
            continue
        similarity, matched, entity_hit = similarity_of(candidate, terms)
        if similarity < prefilter or not entity_hit:
            continue
        candidate.similarity = round(similarity, 4)
        candidate.matched_terms = matched
        candidate.entity_hit = entity_hit
        shortlist.append(candidate)
    return shortlist


def _independent(candidates: list[Candidate]) -> list[Candidate]:
    """One entry per distinct voice, best first.

    Collapses syndication — forty mastheads running one wire story is one
    confirmation, not forty — and then collapses each outlet's own follow-ups,
    which are also one voice.
    """
    groups: dict[str, Candidate] = {}
    for candidate in sorted(candidates, key=lambda c: (-c.similarity, -(c.credibility or 0))):
        key = _normalised_headline(candidate.title) or candidate.domain
        groups.setdefault(key, candidate)

    by_domain: dict[str, Candidate] = {}
    for candidate in groups.values():
        by_domain.setdefault(candidate.domain, candidate)
    return sorted(by_domain.values(), key=lambda c: (-c.similarity, -(c.credibility or 0)))


def fact_check_lookup(keywords: list[str], language: str = ''):
    """Ask the configured fact-check client, and never let it break a check.

    The rest of the report does not depend on this layer, so a client that
    raises — a bad dotted path in settings, a library that changed under us —
    degrades to `unavailable` rather than taking the whole rating down with
    it. The exception is logged, not swallowed silently.
    """
    from .factcheck import FactCheckOutcome, Status, get_fact_check_client

    try:
        return get_fact_check_client().search(keywords, language)
    except Exception as exc:
        logger.exception('Fact-check lookup failed: %s', exc)
        return FactCheckOutcome(
            status=Status.UNAVAILABLE,
            detail=f'The fact-check lookup raised {exc.__class__.__name__} and '
                   'was skipped. The rest of this check is unaffected.',
        )


def fact_check_reason(outcome) -> Reason:
    """One line of working for the fact-check layer.

    `effect` is always None. That is the point: this line is a citation, not a
    term in the sum, and an analyst reading the column of contributions must
    not find a number here implying the rating already accounts for it.
    """
    from .factcheck import Status

    if outcome.status == Status.FOUND:
        close = outcome.close
        if close:
            named = '; '.join(
                f'{r.publisher or "a fact-checker"} rated "{r.claim[:90]}" '
                f'{r.rating or "with no stated rating"}'
                for r in close[:3]
            )
            extra = len(outcome.reviews) - len(close)
            detail = (
                f'{named}. {outcome.detail}'
                + (f' {extra} looser match(es) are listed with the report.' if extra else '')
                + ' This does not change the rating above: the match was made '
                'on search terms, and only reading the fact-check can tell you '
                'whether it addresses this article\'s claim.'
            )
        else:
            detail = (
                f'{len(outcome.reviews)} published fact-check(s) share some of '
                'this story\'s terms, but none closely enough to be treated as '
                'a ruling on it. Listed with the report as leads.'
            )
        return Reason('Published fact-checks', detail)

    return Reason('Published fact-checks', outcome.detail)


def assess(item) -> CorroborationResult:
    """Rate one ContentItem out of 10, and show the working.

    The rating is a weighted sum of three things an analyst can check
    independently: who published it, who else reported it, and whether the
    specifics it states match what those others printed. Each contribution is
    returned as its own `Reason`, so the number can be argued with rather than
    merely believed.
    """
    window = timedelta(days=getattr(settings, 'CORROBORATION_WINDOW_DAYS', DEFAULT_WINDOW_DAYS))
    floor = getattr(settings, 'CORROBORATION_MIN_SIMILARITY', DEFAULT_MIN_SIMILARITY)
    deep_reads = getattr(settings, 'CORROBORATION_DEEP_READS', DEFAULT_DEEP_READS)

    terms = terms_for(item)
    keywords = query_terms(terms)
    own_domain = item.source.domain

    latest = item.source.credibility_scores.first()
    source_credibility = latest.credibility_score if latest else DEFAULT_UNKNOWN_CREDIBILITY

    outcome = get_search().search(keywords, item.published_at, own_domain)

    # Asked independently of the news search, and answered independently: a
    # published fact-check is worth citing whether or not the corroboration
    # lookup succeeded, and it would be perverse to hide a fact-checker's
    # ruling because GDELT happened to be rate-limiting us.
    fact_check = fact_check_lookup(keywords, item.language)

    def unavailable(reason: str, why: str) -> CorroborationResult:
        return CorroborationResult(
            score=None,
            verdict=Verdict.UNAVAILABLE,
            headline=VERDICT_LABELS[Verdict.UNAVAILABLE],
            rationale=reason,
            keywords=keywords,
            matches=[],
            sources_checked=len(outcome.candidates),
            independent_sources=0,
            searched=outcome.searched,
            reasons=[
                Reason('No rating given', why),
                Reason('Terms searched', ', '.join(keywords) or 'no usable terms'),
                fact_check_reason(fact_check),
            ],
            fact_checks=fact_check.reviews,
            fact_check_status=fact_check.status,
            fact_check_detail=fact_check.detail,
        )

    if not outcome.available:
        return unavailable(
            f'Searched for {", ".join(keywords) or "nothing usable"} but the '
            f'lookup did not complete. {outcome.detail} No rating is given: '
            'a failed search is not evidence that nobody else reported this.',
            'The search itself failed, so there is nothing to rate. '
            f'{outcome.detail}'.strip(),
        )

    shortlist = _shortlist(item, outcome.candidates, terms, own_domain, window)

    credibility = _credibility_for({c.domain for c in shortlist})
    for candidate in shortlist:
        candidate.credibility = credibility.get(candidate.domain, DEFAULT_UNKNOWN_CREDIBILITY)

    ranked = _independent(shortlist)

    # Read the best few in full. Deduplicating first matters: without it the
    # budget is spent fetching five mastheads carrying one wire story.
    _, outlets_read, unreadable = read_candidates(ranked, deep_reads)
    for candidate in ranked[:deep_reads]:
        if not candidate.body:
            continue
        candidate.read_in_full = True
        similarity, matched, entity_hit = similarity_of(candidate, terms)
        candidate.similarity = round(similarity, 4)
        candidate.matched_terms = matched
        candidate.entity_hit = entity_hit

    # Now the strict test, on the best evidence available for each candidate.
    independent = [c for c in ranked if c.similarity >= floor and c.entity_hit]

    # The asymmetry that keeps this honest. Finding corroboration is valid
    # even when part of the search failed — evidence found is evidence found.
    # Finding *none* is only meaningful if we actually looked everywhere, so
    # a partial search that came back empty draws no conclusion at all.
    if not independent and outcome.failed:
        return unavailable(
            f'Searched for {", ".join(keywords) or "nothing usable"} and found no '
            f'match, but {" and ".join(sorted(set(outcome.failed)))} did not '
            f'answer. {outcome.detail} No rating is given: an incomplete search '
            'that finds nothing is not evidence that nothing is there.',
            f'{" and ".join(sorted(set(outcome.failed)))} did not answer and '
            'nothing was found, so the absence proves nothing.',
        )

    # --- Do the article's own statements hold up against what was read? ---
    evidence = [(c.domain, c.body) for c in independent if c.body]
    checkable = claim_check.extract_claims(item)
    if evidence:
        claim_check.verify_claims(checkable, evidence)
    claim_stats = claim_check.summarise(checkable if evidence else [])

    # --- The arithmetic ---
    strength = sum(c.similarity * (c.credibility or 0) / 100.0 for c in independent)
    # Diminishing returns: the second independent outlet is worth far more
    # than the ninth, which is usually just the story spreading.
    corroboration = 10.0 * (1.0 - math.exp(-0.8 * strength))
    source_component = source_credibility / 10.0

    # When nothing in the article is checkable, its share goes to corroboration
    # rather than being lost — otherwise an article with no figures in it could
    # not score above 7.5 however many outlets confirmed it.
    if claim_stats['checked']:
        w_source, w_corroboration, w_claims = 0.30, 0.45, 0.25
    else:
        w_source, w_corroboration, w_claims = 0.30, 0.70, 0.0

    agreement = (
        claim_stats['agreed'] / claim_stats['checked'] if claim_stats['checked'] else 0.0
    )
    # A concrete conflict outweighs a lot of vague agreement, and is capped so
    # one contested figure cannot on its own drive a story to zero.
    penalty = min(3.0, 1.5 * claim_stats['disputed'])

    from_source = w_source * source_component
    from_corroboration = w_corroboration * corroboration
    from_claims = w_claims * 10.0 * agreement

    score = round(max(0.0, min(10.0, from_source + from_corroboration + from_claims - penalty)), 1)
    verdict = _verdict_for(score, source_credibility, len(independent), claim_stats['disputed'])

    # --- The working, line by line ---
    reasons = [
        Reason(
            'Who published it',
            f'{own_domain} scores {source_credibility:.0f}/100 in Module 1.',
            round(from_source, 2),
        ),
    ]

    if independent:
        named = ', '.join(
            f'{c.domain} ({c.similarity:.0%}{", read in full" if c.read_in_full else ""})'
            for c in independent[:5]
        )
        reasons.append(Reason(
            'Who else reported it',
            f'{len(independent)} independent outlet(s) carried a matching story: '
            f'{named}{"…" if len(independent) > 5 else ""}.',
            round(from_corroboration, 2),
        ))
    else:
        reasons.append(Reason(
            'Who else reported it',
            'No other outlet in range carried a matching story. That can mean a '
            'scoop as easily as a fabrication, so it lowers the rating without '
            'condemning the article.',
            0.0,
        ))

    if claim_stats['checked']:
        reasons.append(Reason(
            'Do its details match',
            f'{claim_stats["detail"]} Checked against {outlets_read} article(s) '
            'read in full.',
            round(from_claims, 2),
        ))
        if claim_stats['disputed']:
            conflicts = '; '.join(
                f'this article says {c.display}, {c.disputed_by[0][0]} says '
                f'{c.disputed_by[0][1]}'
                for c in checkable if c.status == claim_check.DISPUTED
            )
            reasons.append(Reason(
                'Conflicting figures',
                f'{conflicts}. Which is right is not decided here — a later '
                'correction looks exactly like an error from the outside.',
                -round(penalty, 2),
            ))
    elif evidence:
        reasons.append(Reason(
            'Do its details match',
            'No figures, sums, or dates were found in this article to check '
            'against the other coverage.',
        ))
    else:
        reasons.append(Reason(
            'Do its details match',
            'Not checked: no corroborating article could be read in full, so '
            'there was nothing to compare the article\'s statements against.',
        ))

    # Placed before "Where we looked" so a fact-checker's ruling is not the
    # last line under a housekeeping note. It carries no `effect`, so it reads
    # as evidence to follow up rather than as part of the sum.
    reasons.append(fact_check_reason(fact_check))

    reasons.append(Reason(
        'Where we looked',
        f'{outcome.searched or "no index"}, for {", ".join(keywords) or "no usable terms"}. '
        f'{len(outcome.candidates)} candidate(s) considered, {outlets_read} fetched in full.'
        + (f' {fact_check.searched} also queried.' if fact_check.searched else ''),
    ))
    if outcome.failed or unreadable:
        notes = []
        if outcome.failed:
            notes.append(f'{" and ".join(sorted(set(outcome.failed)))} did not answer')
        if unreadable:
            notes.append(f'{len(unreadable)} page(s) could not be read')
        reasons.append(Reason(
            'Gaps in this check',
            f'{"; ".join(notes)}. {outcome.detail} Corroboration found is still '
            'valid; the search was narrower than intended.'.strip(),
        ))

    rationale = (
        f'{ENGINE_VERSION}: ' + ' '.join(
            f'{r.label} — {r.detail}' for r in reasons
        ) + ' Measures how well other outlets back this story up, and whether '
        'its stated details match theirs — not whether it is true.'
    )

    return CorroborationResult(
        score=score,
        verdict=verdict,
        headline=VERDICT_LABELS[verdict],
        rationale=rationale,
        keywords=keywords,
        matches=independent,
        sources_checked=len(outcome.candidates),
        independent_sources=len(independent),
        searched=outcome.searched,
        reasons=reasons,
        claims=checkable if evidence else [],
        claims_checked=claim_stats['checked'],
        claims_agreed=claim_stats['agreed'],
        claims_disputed=claim_stats['disputed'],
        outlets_read=outlets_read,
        fact_checks=fact_check.reviews,
        fact_check_status=fact_check.status,
        fact_check_detail=fact_check.detail,
    )
