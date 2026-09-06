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
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone as dt_timezone

from django.conf import settings
from django.utils.module_loading import import_string

from core.domains import registrable_domain

from .pipeline import STOPWORDS

logger = logging.getLogger(__name__)

ENGINE_VERSION = 'corroboration-v1'

DEFAULT_WINDOW_DAYS = 7
DEFAULT_MAX_QUERY_TERMS = 4
DEFAULT_MAX_CANDIDATES = 40
DEFAULT_MIN_SIMILARITY = 0.30
# Credibility assumed for an outlet we do not track. Neutral on purpose: a
# domain we have never scored is unknown, not suspect.
DEFAULT_UNKNOWN_CREDIBILITY = 50.0

_WORD_RE = re.compile(r"[a-z0-9][a-z0-9'-]{2,}")
# Words that survive stopword filtering but say nothing about a story.
NEWSROOM_NOISE = frozenset({
    'news', 'says', 'said', 'report', 'reports', 'reported', 'update',
    'updates', 'live', 'latest', 'video', 'photos', 'read', 'more',
    'published', 'story', 'article', 'today', 'yesterday', 'week',
})


class Verdict:
    """The tag shown next to the rating."""

    TRUSTWORTHY = 'trustworthy'
    LIKELY_RELIABLE = 'likely_reliable'
    PARTIALLY_CORROBORATED = 'partially_corroborated'
    UNCORROBORATED = 'uncorroborated'
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


def terms_for(item) -> list[Term]:
    """The search terms for one ContentItem, strongest first.

    Entities carry more weight than topic words because proper nouns are what
    make a story findable: "Mumbai" narrows a search, "flooding" does not.
    An entity that also appears in the headline outranks one buried in
    paragraph forty.
    """
    title_tokens = _tokens(item.title)
    terms: dict[str, Term] = {}

    for entity in item.entities.all()[:12]:
        text = entity.text.strip()
        if not text:
            continue
        key = text.lower()
        if key in STOPWORDS or key in NEWSROOM_NOISE:
            continue
        weight = 2.0 + min(2.0, (entity.mention_count or 1) / 5.0)
        if _tokens(text) & title_tokens:
            weight += 1.5
        terms[key] = Term(text=text, weight=weight, is_entity=True)

    for hit in item.content_topics.select_related('topic')[:8]:
        key = hit.topic.label.strip().lower()
        if not key or key in terms or key in STOPWORDS or key in NEWSROOM_NOISE:
            continue
        weight = 1.0 + min(1.0, (hit.weight or 0) * 20)
        if key in title_tokens:
            weight += 0.75
        terms[key] = Term(text=hit.topic.label, weight=weight, is_entity=False)

    if not terms:
        # Never analysed, or analysis found nothing. The headline is still a
        # perfectly good query.
        for word in sorted(title_tokens, key=len, reverse=True)[:5]:
            terms[word] = Term(text=word, weight=1.0, is_entity=False)

    return sorted(terms.values(), key=lambda t: -t.weight)


def query_terms(terms: list[Term], limit: int | None = None) -> list[str]:
    """The handful of terms actually sent to a search index.

    Kept short on purpose: search backends AND their terms, so a long query
    returns nothing at all rather than returning something imperfect.
    """
    limit = limit or getattr(settings, 'CORROBORATION_MAX_QUERY_TERMS', DEFAULT_MAX_QUERY_TERMS)
    chosen: list[str] = []
    entities = [t for t in terms if t.is_entity]
    topics = [t for t in terms if not t.is_entity]
    for pool, share in ((entities, max(1, limit // 2)), (topics, limit)):
        for term in pool:
            if len(chosen) >= share or len(chosen) >= limit:
                break
            lowered = term.text.lower()
            if any(lowered in c.lower() or c.lower() in lowered for c in chosen):
                continue          # skip a term already implied by another
            chosen.append(term.text)
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
    """
    haystack = f'{candidate.title} {candidate.snippet}'.lower()
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
    engine_version: str = ENGINE_VERSION


VERDICT_LABELS = {
    Verdict.TRUSTWORTHY: 'TRUSTWORTHY',
    Verdict.LIKELY_RELIABLE: 'LIKELY RELIABLE',
    Verdict.PARTIALLY_CORROBORATED: 'PARTIALLY CORROBORATED',
    Verdict.UNCORROBORATED: 'UNCORROBORATED',
    Verdict.DO_NOT_TRUST: 'DO NOT TRUST',
    Verdict.UNAVAILABLE: 'COULD NOT CHECK',
}


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


def _verdict_for(score: float, source_credibility: float, independent: int) -> str:
    """Turn the number into a tag, with two guards that matter.

    Being first to report something is not dishonesty, so an outlet Module 1
    rates well is never tagged DO NOT TRUST merely for standing alone. And a
    source Module 1 has already condemned does not get rehabilitated by a
    handful of keyword matches.
    """
    if source_credibility < 30:
        return Verdict.DO_NOT_TRUST
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


def assess(item) -> CorroborationResult:
    """Rate one ContentItem out of 10 by how well other outlets corroborate it."""
    window = timedelta(days=getattr(settings, 'CORROBORATION_WINDOW_DAYS', DEFAULT_WINDOW_DAYS))
    floor = getattr(settings, 'CORROBORATION_MIN_SIMILARITY', DEFAULT_MIN_SIMILARITY)

    terms = terms_for(item)
    keywords = query_terms(terms)
    own_domain = item.source.domain

    latest = item.source.credibility_scores.first()
    source_credibility = latest.credibility_score if latest else DEFAULT_UNKNOWN_CREDIBILITY

    outcome = get_search().search(keywords, item.published_at, own_domain)

    def unavailable(reason: str) -> CorroborationResult:
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
        )

    if not outcome.available:
        return unavailable(
            f'Searched for {", ".join(keywords) or "nothing usable"} but the '
            f'lookup did not complete. {outcome.detail} No rating is given: '
            'a failed search is not evidence that nobody else reported this.'
        )

    # Score every candidate, keeping only real subject overlap.
    scored: list[Candidate] = []
    for candidate in outcome.candidates:
        if not candidate.url or candidate.domain == own_domain:
            continue
        if (item.published_at and candidate.published_at
                and abs(candidate.published_at - item.published_at) > window):
            # Same words, different year. The 2005 Mumbai floods do not
            # corroborate a report of flooding this week.
            continue
        similarity, matched, entity_hit = similarity_of(candidate, terms)
        if similarity < floor or not entity_hit:
            continue
        candidate.similarity = round(similarity, 4)
        candidate.matched_terms = matched
        scored.append(candidate)

    credibility = _credibility_for({c.domain for c in scored})
    for candidate in scored:
        candidate.credibility = credibility.get(candidate.domain, DEFAULT_UNKNOWN_CREDIBILITY)

    # Collapse syndicated copies: forty mastheads running one wire story is
    # one independent confirmation, not forty.
    groups: dict[str, Candidate] = {}
    for candidate in sorted(scored, key=lambda c: (-c.similarity, -(c.credibility or 0))):
        key = _normalised_headline(candidate.title) or candidate.domain
        if key not in groups:
            groups[key] = candidate
    independent = list(groups.values())
    # A single outlet publishing five follow-ups is also one voice.
    by_domain: dict[str, Candidate] = {}
    for candidate in independent:
        by_domain.setdefault(candidate.domain, candidate)
    independent = list(by_domain.values())

    # The asymmetry that keeps this honest. Finding corroboration is valid
    # even when part of the search failed — evidence found is evidence found.
    # Finding *none* is only meaningful if we actually looked everywhere, so
    # a partial search that came back empty draws no conclusion at all.
    if not independent and outcome.failed:
        return unavailable(
            f'Searched for {", ".join(keywords) or "nothing usable"} and found no '
            f'match, but {" and ".join(sorted(set(outcome.failed)))} did not '
            f'answer. {outcome.detail} No rating is given: an incomplete search '
            'that finds nothing is not evidence that nothing is there.'
        )

    strength = sum(c.similarity * (c.credibility or 0) / 100.0 for c in independent)
    # Diminishing returns: the second independent outlet is worth far more
    # than the ninth, which is usually just the story spreading.
    corroboration = 10.0 * (1.0 - math.exp(-0.8 * strength))
    source_component = source_credibility / 10.0

    score = round(0.45 * source_component + 0.55 * corroboration, 1)
    verdict = _verdict_for(score, source_credibility, len(independent))

    if independent:
        found = ', '.join(f'{c.domain} ({c.similarity:.0%})' for c in independent[:5])
        corroboration_line = (
            f'{len(independent)} independent outlet(s) carried a matching story: {found}'
            + ('…' if len(independent) > 5 else '')
        )
    else:
        corroboration_line = (
            'No other outlet in range carried a matching story. That can mean a '
            'scoop as easily as a fabrication'
        )

    rationale = (
        f'{ENGINE_VERSION}: searched {outcome.searched or "no index"} for '
        f'{", ".join(keywords) or "no usable terms"}. {corroboration_line}. '
        f'Source credibility {source_credibility:.0f}/100 contributes '
        f'{0.45 * source_component:.1f}, corroboration contributes '
        f'{0.55 * corroboration:.1f}. Measures how well other outlets back this '
        'story up — not whether it is true.'
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
    )
