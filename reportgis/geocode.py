"""Finding where a story happened, from the story.

The contract is the same shape as the rest of the platform: an ABC, a
dependency-free default, and a settings-named dotted path. Point
REPORTGIS_GEOCODER at a subclass and nothing that calls this changes.

What the default implementation does, in order of how much it is worth:

1. **Explicit coordinates in the text.** "31.5017° N, 34.4668° E" is not a
   guess about anything. When an article states coordinates, that is the
   location, and it is the only thing here that earns HIGH confidence on its
   own.

2. **A gazetteer name with a qualifier attached.** "Tripoli, Lebanon" and
   "Birmingham, Alabama" say which one they mean. Also HIGH.

3. **A gazetteer name that is unambiguous, or disambiguated by something else
   in the same text.** An article that says "Tripoli" and later "Libya" — or
   just "Libyan officials", which is how news prose actually writes it — has
   told you. MEDIUM: the qualifier is inferred rather than stated, and the
   table is small enough that "unambiguous here" is weaker than "unambiguous".

4. **A country or a region.** LOW, always. A country is not a place an event
   happens; the coordinate is a pin dropped somewhere inside it so the map has
   something to draw, and the confidence band is the warning label.

**A name it cannot resolve is not emitted.** If the text says "Springfield"
with nothing to say which of three Springfields, this module records that it
saw an ambiguous name and moves on. Picking the largest would produce a pin on
a map that an analyst would reasonably believe. That is the failure mode this
project keeps refusing: no location is better than a confident wrong one.

Two matching rules worth knowing about, because they are the difference between
a usable extractor and a joke:

- **A candidate must be capitalised in the source text.** Otherwise "turkey",
  "china", "nice", "mali" and "chad" turn every recipe and every mild adjective
  into a foreign dispatch. Headlines in full caps still match.
- **A candidate followed by an institution word is not a place.** "Georgia
  Tech" is not the country and "the Iraq War" is not a dateline. The list in
  _INSTITUTION_TAIL is short and catches only the obvious cases.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from math import asin, cos, radians, sin, sqrt

from django.conf import settings
from django.utils.module_loading import import_string

from . import gazetteer
from .gazetteer import CITY, COUNTRY, REGION

# Mirrors reportgis.models.GeoEvent.Confidence. Kept as plain strings so this
# module can be imported and exercised without Django models loaded.
HIGH = 'high'
MEDIUM = 'medium'
LOW = 'low'

# Decimal degrees with a hemisphere letter: 31.5017° N, 34.4668° E
_DECIMAL_HEMI = re.compile(
    r'(?<![\d.])(\d{1,3}(?:\.\d+)?)\s*°?\s*([NS])\s*[, ]\s*'
    r'(\d{1,3}(?:\.\d+)?)\s*°?\s*([EW])\b',
    re.IGNORECASE,
)

# Degrees/minutes/seconds: 31°30'06"N 34°28'00"E
_DMS = re.compile(
    r"(\d{1,3})\s*°\s*(\d{1,2})\s*['′]\s*(?:(\d{1,2}(?:\.\d+)?)\s*[\"″])?\s*([NS])"
    r"\s*[, ]\s*"
    r"(\d{1,3})\s*°\s*(\d{1,2})\s*['′]\s*(?:(\d{1,2}(?:\.\d+)?)\s*[\"″])?\s*([EW])",
    re.IGNORECASE,
)

# A bare signed pair: 31.5017, 34.4668
#
# Three decimal places minimum, on both numbers, deliberately. Any two numbers
# separated by a comma look like this; "1.5, 2.0" in a table of percentages is
# not a location. Requiring metre-ish precision on both halves is the cheapest
# rule that tells a coordinate from a pair of figures, and it is still only a
# heuristic — it will miss "51.5, -0.1", which is a real coordinate.
_BARE_PAIR = re.compile(
    r'(?<![\d.\-])(-?\d{1,3}\.\d{3,})\s*,\s*(-?\d{1,3}\.\d{3,})(?![\d.])'
)

# Follows a place name when the text is saying which one: ", Lebanon".
_QUALIFIER_GAP = re.compile(r'^\s*,?\s+(?:in\s+)?')

# Words that make a following capitalised name a demonym or an institution
# rather than a location. "the Iraq War" is not a report from Iraq, and
# "Georgia Tech" is not the country.
_INSTITUTION_TAIL = re.compile(
    r'^\s+(?:University|College|Tech|Institute|Street|Road|Avenue|Square|'
    r'Bridge|Airport|Station|Hospital|Times|Post|Herald|Journal|Review|War|'
    r'Summit|Treaty|Accord|Protocol|Agreement|Conference)\b'
)


# Every nationality the table knows, in one alternation. Longest first so
# "Nigerien" is not eaten by a prefix of "Niger"-something.
_DEMONYM_RE = re.compile(
    r'\b(?:' + '|'.join(
        re.escape(word) for word in sorted(gazetteer.DEMONYMS, key=len, reverse=True)
    ) + r')\b',
    re.IGNORECASE,
)


@dataclass
class LocatedPlace:
    """One resolved location, with the working that produced it."""

    name: str
    latitude: float
    longitude: float
    confidence: str
    method: str
    # The exact span of source text this came from, so an analyst can go and
    # read the sentence rather than trusting the pin.
    matched_text: str
    country: str = ''
    region: str = ''
    kind: str = CITY
    mentions: int = 1
    reasons: list[dict] = field(default_factory=list)


@dataclass
class GeocodeResult:
    places: list[LocatedPlace]
    # Names that were found and deliberately not resolved, and why. This is
    # the honest half of the output and the UI shows it.
    notes: list[str]
    engine_version: str


class BaseGeocoder(ABC):
    """Contract for turning text into locations."""

    engine_version: str = 'base'

    @abstractmethod
    def locate(self, text: str) -> GeocodeResult:
        ...


class GazetteerGeocoder(BaseGeocoder):
    """Coordinate patterns plus a compiled-in place table. No network."""

    engine_version = 'gazetteer-v1'

    # How far past a name to look for ", Lebanon".
    qualifier_window = 40
    max_places = 40

    def locate(self, text: str) -> GeocodeResult:
        text = text or ''
        notes: list[str] = []
        found: dict[tuple, LocatedPlace] = {}

        for place in self._explicit_coordinates(text):
            found.setdefault(self._key(place), place)

        names = self._scan_names(text)
        context = self._context(names, text)

        for name, spans in names.items():
            candidates = gazetteer.lookup(name)
            if not candidates:
                continue
            resolved, reason, confidence = self._resolve(name, candidates, text, spans, context)
            if resolved is None:
                notes.append(reason)
                continue

            located = LocatedPlace(
                name=resolved.name,
                latitude=resolved.lat,
                longitude=resolved.lon,
                confidence=confidence,
                method='gazetteer',
                matched_text=spans[0][2],
                country=resolved.country_name,
                region=resolved.region,
                kind=resolved.kind,
                mentions=len(spans),
                reasons=[reason],
            )
            key = self._key(located)
            if key in found:
                found[key].mentions += located.mentions
            else:
                found[key] = located

        places = sorted(
            found.values(),
            key=lambda p: (_CONFIDENCE_RANK[p.confidence], p.mentions),
            reverse=True,
        )
        if len(places) > self.max_places:
            notes.append(
                f'{len(places)} places resolved; the {self.max_places} best-'
                f'supported are kept and the rest dropped.'
            )
            places = places[: self.max_places]

        return GeocodeResult(places=places, notes=notes, engine_version=self.engine_version)

    # -- coordinates -------------------------------------------------------

    def _explicit_coordinates(self, text: str):
        seen = set()

        for match in _DECIMAL_HEMI.finditer(text):
            lat = float(match.group(1)) * (-1 if match.group(2).upper() == 'S' else 1)
            lon = float(match.group(3)) * (-1 if match.group(4).upper() == 'W' else 1)
            place = self._coordinate(lat, lon, match.group(0), 'stated with hemispheres')
            if place and place.matched_text not in seen:
                seen.add(place.matched_text)
                yield place

        for match in _DMS.finditer(text):
            lat = _dms(match.group(1), match.group(2), match.group(3))
            lon = _dms(match.group(5), match.group(6), match.group(7))
            if match.group(4).upper() == 'S':
                lat = -lat
            if match.group(8).upper() == 'W':
                lon = -lon
            place = self._coordinate(lat, lon, match.group(0), 'stated in degrees/minutes/seconds')
            if place and place.matched_text not in seen:
                seen.add(place.matched_text)
                yield place

        for match in _BARE_PAIR.finditer(text):
            if match.group(0) in seen:
                continue
            place = self._coordinate(
                float(match.group(1)), float(match.group(2)), match.group(0),
                'a bare decimal pair at coordinate precision',
            )
            if place:
                seen.add(match.group(0))
                yield place

    @staticmethod
    def _coordinate(lat: float, lon: float, matched: str, how: str) -> LocatedPlace | None:
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            return None
        return LocatedPlace(
            name=f'{lat:.4f}, {lon:.4f}',
            latitude=lat,
            longitude=lon,
            confidence=HIGH,
            method='coordinates',
            matched_text=matched.strip(),
            kind='point',
            reasons=[{
                'label': (
                    f'The text states coordinates directly ({matched.strip()}), '
                    f'{how}. Nothing was inferred; this is where the article '
                    f'says the thing is.'
                ),
                'effect': 'located',
            }],
        )

    # -- names -------------------------------------------------------------

    def _scan_names(self, text: str) -> dict[str, list[tuple[int, int, str]]]:
        """Every gazetteer name present, with the spans where it occurs.

        Longest name first, and spans already claimed by a longer name are not
        offered to a shorter one — so "Washington, D.C." is not also counted as
        a hit on some other Washington.
        """
        claimed: list[tuple[int, int]] = []
        hits: dict[str, list[tuple[int, int, str]]] = {}

        for name in gazetteer.NAMES_BY_LENGTH:
            pattern = _name_pattern(name)
            for match in pattern.finditer(text):
                start, end = match.span()
                if any(s < end and start < e for s, e in claimed):
                    continue
                surface = match.group(0)
                if not self._looks_like_a_place(text, start, end, surface):
                    continue
                claimed.append((start, end))
                hits.setdefault(name, []).append((start, end, surface))

        return hits

    @staticmethod
    def _looks_like_a_place(text: str, start: int, end: int, surface: str) -> bool:
        # A place name in running prose is capitalised. Lowercase "turkey",
        # "china", "nice", "mali", "chad" and "reading" are not datelines, and
        # without this rule they are most of the output.
        if not surface[0].isupper():
            return False
        # Two- and three-letter forms are only ever abbreviations here (US,
        # UK, UAE, DRC). Required in full caps, or "Us" opening a sentence
        # becomes the United States.
        if len(surface.replace('.', '')) <= 3 and not surface.isupper():
            return False
        if _INSTITUTION_TAIL.match(text[end:end + 30]):
            return False
        return True

    def _context(self, names: dict, text: str) -> dict[str, str]:
        """Which countries and regions the text points at, and what said so.

        This is what lets an article that writes "Tripoli" in the headline and
        "Libya" in the third paragraph resolve its own headline. Nationalities
        count too, and have to: news prose says "Libyan officials" far more
        often than it repeats the country's name.

        The value is the phrase that supplied the evidence, so the pin's
        reasons can name it rather than claiming the country was stated.
        """
        context: dict[str, str] = {}

        def note(place, written):
            context.setdefault(place.country, written)
            if place.kind == REGION:
                context.setdefault(f'{place.country}/{place.region}', written)

        for name, spans in names.items():
            for place in gazetteer.lookup(name):
                if place.kind in (COUNTRY, REGION):
                    note(place, spans[0][2])

        for match in _DEMONYM_RE.finditer(text):
            written = match.group(0)
            # Same capitalisation rule as place names: "the polish on the
            # table" is not a dispatch from Warsaw.
            if not written[0].isupper():
                continue
            for place in gazetteer.demonym_places(written):
                note(place, written)

        return context

    def _resolve(self, name, candidates, text, spans, context):
        """Pick one candidate, or refuse. Returns (place, reason, confidence)."""
        first = candidates[0]

        if len(candidates) == 1:
            if first.kind in (COUNTRY, REGION):
                what = 'A country' if first.kind == COUNTRY else 'A region'
                return first, {
                    'label': (
                        f'{what} was named ({first.name}). The pin is a '
                        f'representative point inside it, not the site of '
                        f'anything — read it as "somewhere in {first.name}".'
                    ),
                    'effect': 'limits',
                }, LOW
            return first, {
                'label': (
                    f'"{first.name}" appears in the gazetteer exactly once, so '
                    f'nothing had to be chosen between. Confidence is held at '
                    f'medium because the table is small — a second {first.name} '
                    f'it does not carry would not have shown up as a conflict.'
                ),
                'effect': 'located',
            }, MEDIUM

        # Ambiguous. Look for a qualifier written right after one of the spans.
        qualified = self._by_qualifier(candidates, text, spans)
        if qualified is not None:
            place, qualifier = qualified
            return place, {
                'label': (
                    f'"{name}" is ambiguous — the gazetteer holds '
                    f'{len(candidates)} of them — and the text resolves it '
                    f'itself by writing "{qualifier}".'
                ),
                'effect': 'located',
            }, HIGH

        # Failing that, look for a containing country or region mentioned
        # anywhere else in the document.
        supported = [
            c for c in candidates
            if c.country in context or (c.region and f'{c.country}/{c.region}' in context)
        ]
        if len(supported) == 1:
            place = supported[0]
            key = (
                f'{place.country}/{place.region}'
                if place.region and f'{place.country}/{place.region}' in context
                else place.country
            )
            evidence = context[key]
            return place, {
                'label': (
                    f'"{name}" is ambiguous ({len(candidates)} in the table). '
                    f'The word "{evidence}" elsewhere in the same text points '
                    f'at {place.region or place.country_name}, and only one '
                    f'candidate sits there, so that is the one taken. The '
                    f'qualifier was inferred from context, not stated.'
                ),
                'effect': 'located',
            }, MEDIUM

        others = ' / '.join(c.label for c in candidates)
        return None, (
            f'"{name}" was found but not placed: it matches {len(candidates)} '
            f'entries ({others}) and nothing in the text says which. No pin is '
            f'recorded rather than guessing at the largest.'
        ), None

    def _by_qualifier(self, candidates, text, spans):
        for start, end, _surface in spans:
            tail = text[end:end + self.qualifier_window]
            gap = _QUALIFIER_GAP.match(tail)
            if not gap:
                continue
            rest = tail[gap.end():]
            for qualifier_name in gazetteer.NAMES_BY_LENGTH:
                if qualifier_name not in gazetteer.QUALIFIERS:
                    continue
                pattern = _name_pattern(qualifier_name)
                match = pattern.match(rest)
                if not match:
                    continue
                written = match.group(0)
                for place in gazetteer.lookup(qualifier_name):
                    if place.kind == COUNTRY:
                        hit = [c for c in candidates if c.country == place.country]
                    elif place.kind == REGION:
                        hit = [
                            c for c in candidates
                            if c.region.lower() == place.region.lower()
                            and c.country == place.country
                        ]
                    else:
                        continue
                    if len(hit) == 1:
                        return hit[0], written
        return None

    @staticmethod
    def _key(place: LocatedPlace):
        return (round(place.latitude, 3), round(place.longitude, 3))


_CONFIDENCE_RANK = {HIGH: 3, MEDIUM: 2, LOW: 1}

# Compiled name patterns, built once. The table has ~450 names and a scan
# compiles each one per call otherwise.
_PATTERN_CACHE: dict[str, re.Pattern] = {}


def _name_pattern(name: str) -> re.Pattern:
    """Word-bounded, case-insensitive, tolerant of runs of whitespace.

    Case-insensitive here and capitalisation enforced separately, because
    "SANAA" in a headline is a place and "sanaa" in a URL slug is not — the
    pattern finds both and _looks_like_a_place decides.
    """
    pattern = _PATTERN_CACHE.get(name)
    if pattern is None:
        body = r'\s+'.join(re.escape(word) for word in name.split())
        # (?!\w) rather than \b for the tail. \b after a full stop demands a
        # word character next, so "Washington, D.C." matched only when the
        # sentence ran straight on into another word — which is to say, in
        # real text, never.
        pattern = re.compile(rf'(?<!\w){body}(?!\w)', re.IGNORECASE)
        _PATTERN_CACHE[name] = pattern
    return pattern


def _dms(degrees, minutes, seconds) -> float:
    return float(degrees) + float(minutes) / 60 + (float(seconds or 0)) / 3600


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in kilometres.

    This is the one thing PostGIS was going to be needed for, and at the scale
    this module works at — a few thousand events filtered in a request — doing
    it in Python costs nothing measurable. It uses a spherical earth, so it is
    off by up to about 0.5% against the ellipsoid; for "events within 50km"
    that is a rounding error, and for anything where it is not, this module is
    the wrong tool anyway.
    """
    r = 6371.0088
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = (
        sin(dlat / 2) ** 2
        + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    )
    return 2 * r * asin(sqrt(a))


def get_geocoder() -> BaseGeocoder:
    path = getattr(settings, 'REPORTGIS_GEOCODER', 'reportgis.geocode.GazetteerGeocoder')
    return import_string(path)()
