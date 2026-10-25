"""Pulling checkable facts out of an article, and testing them against others.

The difference between this and `corroborate`
---------------------------------------------
Corroboration asks "did anyone else write about this?". That is a question
about *coverage*, and it is satisfied by a second outlet running a story on
the same subject even if the two disagree about every detail.

This module asks the narrower, harder question the analyst actually wants:
**does what this article states match what other outlets state?** It does that
by finding the article's checkable specifics - figures, sums of money,
percentages, dates, death tolls - and looking for those same specifics in the
text of other outlets' coverage of the same story.

What it can and cannot establish
--------------------------------
It can establish that four outlets independently print the same number, and
it can establish that one of them prints a different one. Those are real,
useful findings and neither of them requires a language model.

It cannot establish truth. Every outlet copying one wrong figure off one wire
report agrees perfectly. A disputed figure may be a correction rather than an
error, and the newer number may be the right one. So a claim is reported as
`agreed`, `disputed`, or `unverified`, and never as `true` or `false`.

Why it is deliberately reluctant to cry "disputed"
--------------------------------------------------
A false dispute is worse than a missed one: it accuses a correct article of
getting a fact wrong. So a dispute is only reported when the other outlet is
demonstrably talking about the same quantity: two or more of the claim's
context words present, the competing figure within a short span of them, the
same kind of quantity (money is never compared against percentages), the same
magnitude (a bare count is not a rival reading of "40 crore"), and - for bare
figures - the same counted noun, so that 47 *people* is only ever contradicted
by another count of people. Any doubt resolves to `unverified`.

Each of those guards is here because its absence produced a wrong answer on a
live article: "this article says 03, theguardian.com says 2022" and "this
article says 12, france24.com says 24" were both reported as factual conflicts
before the rules above existed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .pipeline import STOPWORDS

# Status of one checkable fact after looking at other outlets' coverage.
AGREED = 'agreed'
DISPUTED = 'disputed'
UNVERIFIED = 'unverified'

# Kind of quantity. Kept apart so "40%" is never checked against "$40".
MONEY = 'money'
PERCENT = 'percent'
QUANTITY = 'quantity'
DATE = 'date'

KIND_LABELS = {
    MONEY: 'sum of money',
    PERCENT: 'percentage',
    QUANTITY: 'figure',
    DATE: 'date',
}

MAX_CLAIMS = 8
# Characters of the article read for claims. The lede carries the story's
# actual assertions; paragraph forty is related coverage, and the page furniture
# below it is not the article at all.
LEDE_CHARS = 2000
# How close a competing figure must sit to a shared context word before it is
# treated as talking about the same thing.
DISPUTE_WINDOW = 140
# Context words that must co-occur before a difference counts as a dispute.
DISPUTE_MIN_CONTEXT = 2

CURRENCY = r'[$€£¥₹]|\b(?:USD|EUR|GBP|JPY|INR|Rs\.?|rupees?|dollars?|euros?|pounds?)\b'
MAGNITUDE = r'\b(?:hundred|thousand|lakh|lakhs|crore|crores|million|billion|trillion|bn|mn|m|k)\b'

_NUMBER = r'\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|\d+(?:\.\d+)?'

MONEY_RE = re.compile(
    rf'(?:(?:{CURRENCY})\s?({_NUMBER})(?:\s*({MAGNITUDE}))?'
    rf'|({_NUMBER})(?:\s*({MAGNITUDE}))?\s*(?:{CURRENCY}))',
    re.IGNORECASE,
)
PERCENT_RE = re.compile(rf'({_NUMBER})\s*(?:%|per\s?cent(?:age)?\b)', re.IGNORECASE)
# The colon in the guards is what keeps clock times out. Without it "03:12"
# yields two perfectly plausible-looking two-digit statistics, and a live
# check duly reported one of them as a figure another outlet disputed.
QUANTITY_RE = re.compile(
    rf'(?<![\w.,:])({_NUMBER})(?:\s*({MAGNITUDE}))?(?![\w.,:]*\d)', re.IGNORECASE
)

MONTHS = (
    'january|february|march|april|may|june|july|august|september|october|'
    'november|december|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec'
)
DATE_RE = re.compile(
    rf'\b(?:(\d{{1,2}})\s+({MONTHS})|({MONTHS})\s+(\d{{1,2}}))\b(?:,?\s*(\d{{4}}))?',
    re.IGNORECASE,
)
ISO_DATE_RE = re.compile(r'\b(\d{4})-(\d{2})-(\d{2})\b')

# A line break ends a sentence as surely as a full stop does. Headlines,
# standfirsts and captions rarely carry terminal punctuation, so splitting on
# punctuation alone glues them to the paragraph beneath - and then the word
# "after" a figure is the first word of the *next* line. That is how a story
# about 47 people came to record itself as counting 47 officials.
SENTENCE_RE = re.compile(r'(?<=[.!?])\s+|\n+')
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'-]{2,}")

# Numbers that are almost never the point of a sentence. Years are handled as
# dates; small counts ("one of two people") carry no checkable content; and a
# bare 2026 in a nav bar is not an assertion.
_TRIVIAL = {'0', '1', '2', '3', '4', '5', '6', '7', '8', '9', '10', '100'}

# Words that appear beside a figure in every newsroom and so identify nothing.
_WEAK_CONTEXT = frozenset({
    'said', 'says', 'reported', 'report', 'according', 'told', 'added',
    'news', 'article', 'story', 'read', 'more', 'also', 'last', 'first',
    'new', 'year', 'years', 'day', 'days', 'time', 'times', 'per',
    # Page chrome that survives extraction and sits right next to the
    # timestamp, so it is the "context" of every date on the page.
    'published', 'updated', 'edited', 'ago', 'gmt', 'utc', 'ist', 'est',
    'edt', 'bst', 'copyright', 'subscribe', 'newsletter', 'advertisement',
    'comments', 'share', 'min',
    # A month beside a date describes the date rather than what it is a date
    # *of*, so as context it is circular.
    'january', 'february', 'march', 'april', 'may', 'june', 'july', 'august',
    'september', 'october', 'november', 'december', 'monday', 'tuesday',
    'wednesday', 'thursday', 'friday', 'saturday', 'sunday',
})

# A date sitting beside any of these is the article's own byline timestamp,
# not something the article asserts. Checking a publication date against
# other outlets measures nothing: everything filed today says today.
_BYLINE_MARKERS = ('published', 'updated', 'last modified', 'posted', 'edited')

MONTH_NUMBERS = {
    'jan': 1, 'january': 1, 'feb': 2, 'february': 2, 'mar': 3, 'march': 3,
    'apr': 4, 'april': 4, 'may': 5, 'jun': 6, 'june': 6, 'jul': 7, 'july': 7,
    'aug': 8, 'august': 8, 'sep': 9, 'sept': 9, 'september': 9, 'oct': 10,
    'october': 10, 'nov': 11, 'november': 11, 'dec': 12, 'december': 12,
}


@dataclass
class Claim:
    """One checkable specific the article asserts."""

    text: str                       # the sentence it came from
    kind: str                       # MONEY / PERCENT / QUANTITY / DATE
    value: str                      # normalised, comparable across outlets
    display: str                    # as the article wrote it
    context: list[str] = field(default_factory=list)
    # What the figure counts, for quantities. Two figures are only rival
    # readings of each other if they are counting the same thing.
    counts: str = ''
    status: str = UNVERIFIED
    agreed_by: list[str] = field(default_factory=list)
    # (domain, the figure they printed instead)
    disputed_by: list[list[str]] = field(default_factory=list)

    @property
    def kind_label(self) -> str:
        return KIND_LABELS.get(self.kind, self.kind)

    def as_dict(self) -> dict:
        return {
            'text': self.text,
            'kind': self.kind,
            'kind_label': self.kind_label,
            'value': self.value,
            'display': self.display,
            'context': self.context,
            'status': self.status,
            'agreed_by': self.agreed_by,
            'disputed_by': self.disputed_by,
        }


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

def normalise_number(raw: str) -> str:
    """A comparable form of a figure, so 1,200 and 1200.0 are one value.

    Deliberately not converted to float: "88.20" and "88.2" must compare equal,
    but the rupee rate 88.20 and the year 2026 must not both collapse to
    something a float comparison would blur.
    """
    text = (raw or '').replace(',', '').strip()
    if '.' in text:
        text = text.rstrip('0').rstrip('.')
    # Leading zeros come from clock times and date fragments, and leaving them
    # on hides those from the checks below: "03" is not caught as the trivial
    # number 3, and so was reported as a disputed statistic.
    whole, _, fraction = text.partition('.')
    whole = whole.lstrip('0') or '0'
    return f'{whole}.{fraction}' if fraction else whole


def _is_year(value: str) -> bool:
    """Whether a bare figure is almost certainly a year.

    Years are dates wearing a number's clothes. Compared as quantities they
    match anything - a 2022 in one outlet's copy sits within a sentence of
    almost any figure in another's - which is how a live check came to report
    "this article says 03, the Guardian says 2022" as a factual conflict.
    """
    return value.isdigit() and len(value) == 4 and 1800 <= int(value) <= 2199


def _scale(magnitude: str) -> str:
    """Canonical magnitude word. Indian and Western scales are both kept as
    written - a crore is not a million and converting between them would
    invent precision the article did not have."""
    word = (magnitude or '').lower().strip().rstrip('s')
    return {
        'bn': 'billion', 'mn': 'million', 'm': 'million', 'k': 'thousand',
        'lakh': 'lakh', 'crore': 'crore',
    }.get(word, word)


def _value(number: str, magnitude: str = '') -> str:
    scale = _scale(magnitude)
    return f'{normalise_number(number)} {scale}'.strip()


def _context_words(sentence: str, around: int, span: int = 90) -> list[str]:
    """The words that say *what* a figure counts.

    Taken from a window either side of the number rather than the whole
    sentence: in a long sentence the far end is usually a different clause
    about a different quantity.
    """
    window = sentence[max(0, around - span): around + span]
    words, seen = [], set()
    for match in _WORD_RE.finditer(window):
        word = match.group(0)
        lowered = word.lower()
        if lowered in STOPWORDS or lowered in _WEAK_CONTEXT or lowered in seen:
            continue
        seen.add(lowered)
        # A capitalised word mid-sentence names something; it pins a figure to
        # a subject far better than a common noun does.
        weight = 2 if word[0].isupper() and match.start() > 0 else 1
        words.append((weight, len(lowered), lowered))
    words.sort(reverse=True)
    return [w for _, _, w in words[:6]]


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

def _iso(day: str, month: str, year: str) -> str:
    number = MONTH_NUMBERS.get((month or '').lower(), 0)
    if not number or not day:
        return ''
    stamp = f'{int(number):02d}-{int(day):02d}'
    return f'{year}-{stamp}' if year else stamp


def _claims_in(sentence: str) -> list[tuple[str, str, str, int, str]]:
    """(kind, value, display, position, counted noun) for each figure found."""
    found: list[tuple[str, str, str, int]] = []
    claimed: list[tuple[int, int]] = []

    def overlaps(start: int, end: int) -> bool:
        return any(start < e and s < end for s, e in claimed)

    # Order matters: a percentage and a sum of money both contain a bare
    # number, and whichever runs first should own those characters.
    for match in PERCENT_RE.finditer(sentence):
        claimed.append(match.span())
        found.append((PERCENT, normalise_number(match.group(1)), match.group(0).strip(), match.start(), ''))

    for match in MONEY_RE.finditer(sentence):
        if overlaps(*match.span()):
            continue
        claimed.append(match.span())
        number = match.group(1) or match.group(3) or ''
        magnitude = match.group(2) or match.group(4) or ''
        found.append((MONEY, _value(number, magnitude), match.group(0).strip(), match.start(), ''))

    for match in ISO_DATE_RE.finditer(sentence):
        if overlaps(*match.span()):
            continue
        claimed.append(match.span())
        found.append((DATE, match.group(0), match.group(0), match.start(), ''))

    for match in DATE_RE.finditer(sentence):
        if overlaps(*match.span()):
            continue
        day = match.group(1) or match.group(4) or ''
        month = match.group(2) or match.group(3) or ''
        value = _iso(day, month, match.group(5) or '')
        if not value:
            continue
        claimed.append(match.span())
        found.append((DATE, value, match.group(0).strip(), match.start(), ''))

    for match in QUANTITY_RE.finditer(sentence):
        if overlaps(*match.span()):
            continue
        number = normalise_number(match.group(1))
        magnitude = match.group(2) or ''
        if not _checkable_quantity(number, magnitude):
            continue
        if not _quantifies(sentence, match.end(), number, magnitude):
            continue
        claimed.append(match.span())
        found.append((
            QUANTITY, _value(match.group(1), magnitude), match.group(0).strip(),
            match.start(), _counted_noun(sentence, match.end()),
        ))

    return found


# Spaces and tabs only, never a newline: what a figure counts has to be on the
# same line as the figure.
_FOLLOWING_WORD_RE = re.compile(r'[ \t]*([A-Za-z][A-Za-z-]{2,})')


def _counted_noun(text: str, end: int) -> str:
    """The word a figure quantifies - the "people" in "47 people".

    This is what makes two figures comparable. Without it, any two-digit
    number in one article can be read as a rival reading of any two-digit
    number in another: a live check reported "this article says 12,
    france24.com says 24" as a factual conflict, when the two were counting
    entirely different things.
    """
    following = _FOLLOWING_WORD_RE.match(text, end)
    if not following:
        return ''
    word = following.group(1).lower()
    if word in STOPWORDS or word in _WEAK_CONTEXT:
        return ''
    # Crude singularisation, so "47 deaths" and "one death" compare.
    return word[:-1] if len(word) > 4 and word.endswith('s') else word


def _quantifies(sentence: str, end: int, number: str, magnitude: str) -> bool:
    """Whether a small bare figure actually counts something.

    "47 people died" states a fact. "the first half of my 20s" does not, and
    neither does a stray 12 left behind by a page's furniture. What separates
    them is whether the figure is followed by a word naming what there are 47
    of - which is how quantities are written in prose, and which costs one
    regex to check.

    Only bare one- and two-digit figures have to pass this. Anything with a
    magnitude word, a decimal, or three digits is specific enough on its own.
    """
    if magnitude or '.' in number or len(number.partition('.')[0]) >= 3:
        return True
    following = _FOLLOWING_WORD_RE.match(sentence, end)
    if not following:
        return False
    word = following.group(1).lower()
    return word not in STOPWORDS and word not in _WEAK_CONTEXT


def _checkable_quantity(number: str, magnitude: str) -> bool:
    """Whether a bare figure is an assertion worth testing against others.

    Single digits are excluded because they are everywhere and mean nothing in
    isolation, and years because a year is a date in disguise that matches
    almost any figure it is compared against.

    Two-digit figures are *not* excluded, though an earlier draft did exclude
    them. That draft threw away "47 people died" - a death toll is the single
    most valuable thing this module can check, and other outlets printing 42
    is precisely the finding worth surfacing. What separates that from a stray
    "12" in a page's furniture is not the number of digits, it is whether
    there are words around it saying what it counts, so bare figures are
    filtered on their context instead (see `extract_claims`).
    """
    if magnitude:
        return True
    if _is_year(number) or number in _TRIVIAL:
        return False
    return True


def extract_claims(item, limit: int = MAX_CLAIMS) -> list[Claim]:
    """The article's checkable specifics, most identifiable first.

    Read from the headline and the lede only. The tail of a scraped page is
    related-links, newsletter copy, and share counts, and treating "Read 2.3k
    times" as one of the article's assertions would be worse than useless.
    """
    head = (item.title or '').strip()
    lede = (item.summary or '') or (item.body or '')
    text = f'{head}. {lede[:LEDE_CHARS]}'

    claims: dict[tuple[str, str], Claim] = {}
    for sentence in SENTENCE_RE.split(text):
        sentence = ' '.join(sentence.split())
        if len(sentence) < 20:
            continue
        lowered = sentence.lower()
        byline = any(marker in lowered for marker in _BYLINE_MARKERS)
        for kind, value, display, position, counts in _claims_in(sentence):
            # A sum and a bare count of the same amount are one claim written
            # twice - the headline's "1,200 crore" and the body's "1,200 crore
            # rupees" - and counting both would double its weight in the score.
            key = ('amount', value) if kind in (MONEY, QUANTITY) else (kind, value)
            if key in claims:
                continue
            if kind == DATE and byline:
                continue          # the article's own timestamp, not a claim

            context = _context_words(sentence, position)
            if not context:
                # A figure with nothing around it to say what it counts cannot
                # be checked against anyone else's reporting.
                continue
            # A small bare figure needs more than one word of context before it
            # counts as an assertion. Without this, page furniture supplies an
            # endless stream of two-digit "claims".
            bare = kind == QUANTITY and ' ' not in value and len(value.partition('.')[0]) < 3
            if bare and len(context) < DISPUTE_MIN_CONTEXT:
                continue
            claims[key] = Claim(
                text=sentence[:400], kind=kind, value=value,
                display=display, context=context, counts=counts,
            )
            if len(claims) >= limit:
                return list(claims.values())
    return list(claims.values())


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

def _competing(kind: str, text: str, like: str = '') -> list[tuple[str, int, str]]:
    """Every figure of one kind in a text: (value, position, counted noun).

    `like` is the claim's own value, used to keep the comparison honest: a
    figure only competes with another of the same shape. A bare count is not a
    rival reading of "40 crore", and neither is a year.
    """
    values = []
    wanted_scale = like.partition(' ')[2] if ' ' in like else ''
    if kind == PERCENT:
        for match in PERCENT_RE.finditer(text):
            values.append((normalise_number(match.group(1)), match.start(), ''))
    elif kind == MONEY:
        for match in MONEY_RE.finditer(text):
            number = match.group(1) or match.group(3) or ''
            magnitude = match.group(2) or match.group(4) or ''
            values.append((_value(number, magnitude), match.start(), ''))
    elif kind == DATE:
        for match in ISO_DATE_RE.finditer(text):
            values.append((match.group(0), match.start(), ''))
        for match in DATE_RE.finditer(text):
            value = _iso(match.group(1) or match.group(4) or '',
                         match.group(2) or match.group(3) or '',
                         match.group(5) or '')
            if value:
                values.append((value, match.start(), ''))
    else:
        for match in QUANTITY_RE.finditer(text):
            number = normalise_number(match.group(1))
            magnitude = match.group(2) or ''
            if not _checkable_quantity(number, magnitude):
                continue
            if _scale(magnitude) != wanted_scale:
                continue
            # Held to the same standard as the claim itself: a loose number in
            # their page furniture must not be read as their rival figure.
            if not _quantifies(text, match.end(), number, magnitude):
                continue
            values.append((
                _value(match.group(1), magnitude), match.start(),
                _counted_noun(text, match.end()),
            ))
    return values


def _date_agrees(ours: str, theirs: str) -> bool:
    """A date written without a year still matches one written with it."""
    if ours == theirs:
        return True
    short, long_ = sorted((ours, theirs), key=len)
    return len(short) == 5 and long_.endswith(short)


def verify_claims(claims: list[Claim], evidence: list[tuple[str, str]]) -> list[Claim]:
    """Test each claim against the full text of other outlets' coverage.

    `evidence` is (domain, text) for each other article actually read. A claim
    only reaches `agreed` or `disputed` on the strength of a page we fetched
    and parsed - a headline is far too short to carry a figure reliably, and
    scoring an article down because a 9-word headline omitted its numbers
    would be measuring the headline, not the story.
    """
    for claim in claims:
        wanted = set(claim.context)
        for domain, text in evidence:
            if not text:
                continue
            lowered = text.lower()
            present = [w for w in wanted if w in lowered]
            if not present:
                continue

            others = _competing(claim.kind, text, like=claim.value)
            same = (
                _date_agrees if claim.kind == DATE
                else lambda a, b: a == b
            )
            if any(same(claim.value, value) for value, _, _ in others):
                if domain not in claim.agreed_by:
                    claim.agreed_by.append(domain)
                continue

            # Not present. Only a dispute if this outlet is demonstrably
            # discussing the same quantity: enough shared context, and a rival
            # figure sitting right beside one of those context words.
            if len(present) < DISPUTE_MIN_CONTEXT:
                continue
            anchors = [
                match.start()
                for word in present
                for match in re.finditer(re.escape(word), lowered)
            ]
            rival = next(
                (
                    value for value, position, counts in others
                    if any(abs(position - a) <= DISPUTE_WINDOW for a in anchors)
                    # A bare figure only contradicts another when both count
                    # the same thing. Without this, "12 <anything>" here and
                    # "24 <anything else>" there read as a factual conflict -
                    # which a live check duly reported, wrongly.
                    and (claim.kind != QUANTITY or counts == claim.counts)
                ),
                None,
            )
            if rival is not None and not any(d[0] == domain for d in claim.disputed_by):
                claim.disputed_by.append([domain, rival])

        if claim.agreed_by:
            # Agreement outranks disagreement: outlets legitimately carry both
            # an original figure and a later correction, and the fact that
            # someone printed ours is the finding worth surfacing.
            claim.status = AGREED
        elif claim.disputed_by:
            claim.status = DISPUTED
        else:
            claim.status = UNVERIFIED

    return claims


def summarise(claims: list[Claim]) -> dict:
    """Counts plus a sentence an analyst can read without expanding anything."""
    agreed = [c for c in claims if c.status == AGREED]
    disputed = [c for c in claims if c.status == DISPUTED]

    if not claims:
        detail = 'No checkable figures or dates were found in this article.'
    elif not agreed and not disputed:
        detail = (
            f'{len(claims)} checkable detail(s) found, none of which appeared in '
            'the other coverage that was read.'
        )
    else:
        parts = []
        if agreed:
            parts.append(f'{len(agreed)} of {len(claims)} matched other outlets')
        if disputed:
            first = disputed[0]
            parts.append(
                f'{len(disputed)} conflicted (this article says {first.display}, '
                f'{first.disputed_by[0][0]} says {first.disputed_by[0][1]})'
            )
        detail = '; '.join(parts) + '.'

    return {
        'checked': len(claims),
        'agreed': len(agreed),
        'disputed': len(disputed),
        'unverified': len(claims) - len(agreed) - len(disputed),
        'detail': detail,
    }
