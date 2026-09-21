"""Turning a set of located events into a report an analyst can hand over.

A report here is a **document**, not a dump. The events are already available
as JSON and as a map; what a report adds is the reading of them — how many,
where, over what period, out of whose journalism, and how much of it is solid.

Three decisions worth stating, because each of them is a road not taken:

**The narrative is assembled, not generated.** Every sentence below comes from
a template filled with a counted figure. Nothing here writes prose about what
the events *mean*, because this module has no way to know that and a fluent
paragraph asserting it would be read as analysis. The analyst supplies the
meaning; this supplies the arithmetic, with its working attached.

**The longest section is the one listing what the report cannot show.** A map
with fourteen pins on it looks like fourteen things that happened, in a world
where nothing else did. It is actually fourteen places named in articles this
system happened to read, that a 383-row table happened to carry, that were
unambiguous enough to place. Every one of those filters removes real events,
and a report that does not say so is misleading in exactly the direction that
matters. `gaps` is a first-class output, not a footnote.

**It writes Markdown, not PDF.** No PDF library is installed and adding one to
produce a fixed-layout copy of text would buy nothing an analyst needs: this
output can be diffed against last week's, grepped, pasted into an email, and
read in a terminal. `Report.pdf` stays on the model for an analyst's own
export, attached by hand — it is not something this builder pretends to fill.

Swapping the builder is a settings change, same as every other engine in this
project: name a BaseReportBuilder subclass in REPORTGIS_REPORT_BUILDER.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from django.conf import settings
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from django.utils.module_loading import import_string

from . import gazetteer
from .geocode import haversine_km
from .models import ExtractionRun, GeoEvent

# low < medium < high. A scope asking for "medium" means medium *and better*,
# which is the only reading that makes sense for a quality floor.
BAND_RANK = {
    GeoEvent.Confidence.LOW: 0,
    GeoEvent.Confidence.MEDIUM: 1,
    GeoEvent.Confidence.HIGH: 2,
}

# What each band actually licenses you to believe. Printed in the report next
# to the count, because "medium" on its own is a word, not a caveat.
BAND_MEANING = {
    GeoEvent.Confidence.HIGH: (
        'Coordinates were stated in the text, or the place name was written '
        'with its country or region beside it.'
    ),
    GeoEvent.Confidence.MEDIUM: (
        'The name resolved to one place, either because the table carries it '
        'once or because the article named the country elsewhere. The table '
        'is small, so "only one match" is weaker than it sounds.'
    ),
    GeoEvent.Confidence.LOW: (
        'A country or region was named, nothing narrower. The pin is a point '
        'inside it, chosen to be on land — not the site of anything.'
    ),
}

MAX_EVENTS = 500

UNATTRIBUTED = 'Not attributed'


class ScopeError(ValueError):
    """The scope could not be understood. Reported to the caller verbatim."""


class EmptySelection(ValueError):
    """Nothing matched, so there is nothing to report.

    Deliberately an error rather than an empty report. A document headed with
    a scope and containing no events reads as "we looked here and nothing
    happened", which is a finding this module is in no position to make: it
    would be equally consistent with nobody having read anything. The place
    where "we looked and found nothing" is recorded honestly is the extraction
    log, and the caller is pointed at it.
    """


@dataclass
class BuiltReport:
    """Everything a Report row needs, before anything is written to the DB."""

    title: str
    summary: str
    document: str
    findings: dict = field(default_factory=dict)
    gaps: list[str] = field(default_factory=list)
    builder_version: str = ''
    event_ids: list[int] = field(default_factory=list)


def plural(count: int, singular: str, many: str = '') -> str:
    """`1 location` / `7 locations`.

    Worth the four lines. "7 location(s) in 3 country(ies)" is the house style
    of software nobody reads twice, and this document is meant to be handed to
    someone who did not run it.
    """
    return f'{count} {singular if count == 1 else (many or singular + "s")}'


def verb(count: int, singular: str, many: str) -> str:
    """Agreement for a count that is only known at generation time.

    The alternative is the passive voice everywhere, which is how a report
    ends up sounding like it is avoiding saying who did what.
    """
    return singular if count == 1 else many


def _country_name(value: str) -> str:
    """Accept 'LB' or 'Lebanon'; return the name a GeoEvent actually stores.

    Events carry the country's name, not its code — that is what
    reportgis.geocode writes into the row, and it is what an analyst typing a
    filter by hand will reach for. A code is accepted and translated anyway,
    because half the world writes AE and the other half writes United Arab
    Emirates, and neither should get an empty report for it.
    """
    text = value.strip()
    if len(text) == 2 and text.upper() in gazetteer.COUNTRY_NAMES:
        return gazetteer.COUNTRY_NAMES[text.upper()]
    folded = ' '.join(text.lower().split())
    for name in gazetteer.COUNTRY_NAMES.values():
        if name.lower() == folded:
            return name
    raise ScopeError(
        f'{value!r} is not a country this module knows. Use an ISO 3166-1 '
        f'alpha-2 code, or the country name as the gazetteer spells it.'
    )


def _parse_when(value, label: str):
    if not value:
        return None
    moment = parse_datetime(value)
    if moment is None:
        day = parse_date(value)
        if day is None:
            raise ScopeError(f'{label} is not a date: {value!r}.')
        moment = timezone.datetime.combine(day, timezone.datetime.min.time())
    if timezone.is_naive(moment):
        moment = timezone.make_aware(moment)
    return moment


def select_events(scope: dict | None) -> tuple[list[GeoEvent], list[str]]:
    """The events a scope picks out, and the scope written back in English.

    The English matters as much as the queryset. A report whose first section
    says "everything located" when the caller actually asked for high
    confidence in one country is a report that will be quoted out of context.
    """
    scope = dict(scope or {})
    queryset = GeoEvent.objects.select_related('content_item', 'content_item__source')
    described: list[str] = []

    band = str(scope.get('confidence') or '').strip().lower()
    if band:
        if band not in BAND_RANK:
            raise ScopeError(
                f'{band!r} is not a confidence band. Use low, medium, or high.'
            )
        floor = BAND_RANK[band]
        queryset = queryset.filter(
            confidence__in=[b for b, rank in BAND_RANK.items() if rank >= floor]
        )
        described.append(
            'every location, at any confidence'
            if band == GeoEvent.Confidence.LOW
            else f'locations placed with at least {band} confidence'
        )

    country = str(scope.get('country') or '').strip()
    if country:
        name = _country_name(country)
        queryset = queryset.filter(country__iexact=name)
        described.append(f'in {name}')

    event_type = str(scope.get('event_type') or '').strip()
    if event_type:
        queryset = queryset.filter(event_type__iexact=event_type)
        described.append(f'of type "{event_type}"')

    since = _parse_when(scope.get('since'), 'since')
    if since:
        queryset = queryset.filter(occurred_at__gte=since)
        described.append(f'reported on or after {since:%d %b %Y}')

    until = _parse_when(scope.get('until'), 'until')
    if until:
        queryset = queryset.filter(occurred_at__lte=until)
        described.append(f'reported on or before {until:%d %b %Y}')

    events = list(queryset[:MAX_EVENTS])

    near = str(scope.get('near') or '').strip()
    if near:
        try:
            lat_text, lon_text = near.split(',')
            lat, lon = float(lat_text), float(lon_text)
        except ValueError as exc:
            raise ScopeError(
                f'near must be "latitude,longitude"; got {near!r}.'
            ) from exc
        try:
            km = float(scope.get('within_km') or 100)
        except (TypeError, ValueError) as exc:
            raise ScopeError('within_km must be a number of kilometres.') from exc
        # Filtered after the slice rather than before it, unlike the events
        # endpoint's bounding box. A report's selection is small by
        # construction, and doing the exact distance here keeps one definition
        # of "near" instead of two that agree most of the time.
        events = [
            event for event in events
            if haversine_km(lat, lon, event.latitude, event.longitude) <= km
        ]
        described.append(f'within {km:g}km of {lat:.4f}, {lon:.4f}')

    if not described:
        described.append('every location this module has placed')

    return events, described


class BaseReportBuilder(ABC):
    """Contract: events in, a document out. No writes, no side effects."""

    builder_version = 'base'

    @abstractmethod
    def build(self, events: list[GeoEvent], *, title: str = '',
              scope_text: list[str] | None = None) -> BuiltReport:
        """Assemble a report, or raise EmptySelection when there is nothing."""


class NarrativeReportBuilder(BaseReportBuilder):
    """Counts the selection, states the counts, then states what they omit."""

    builder_version = 'narrative-v1'

    # Places listed by name in the summary before it stops naming them.
    named_places = 5

    def build(self, events, *, title='', scope_text=None):
        if not events:
            raise EmptySelection(
                'No located event matches this scope, so there is nothing to '
                'report. An empty report would read as a finding that nothing '
                'happened here; what the system can actually say is in the '
                'extraction log, which records the articles that were read '
                'and found to contain no placeable location.'
            )

        scope_text = list(scope_text or ['every location this module has placed'])
        findings = self._findings(events)
        gaps = self._gaps(events, findings)
        heading = title.strip() or self._title(findings)
        summary = self._summary(findings, scope_text)
        document = self._document(heading, summary, findings, gaps, scope_text, events)

        return BuiltReport(
            title=heading,
            summary=summary,
            document=document,
            findings=findings,
            gaps=gaps,
            builder_version=self.builder_version,
            event_ids=[event.pk for event in events],
        )

    # ------------------------------------------------------------------ data

    def _findings(self, events) -> dict:
        bands = Counter(event.confidence for event in events)
        methods = Counter(event.method for event in events)

        # GeoEvent.country holds the country's *name*, written there by the
        # geocoder. Grouping on it directly keeps one spelling of a country in
        # the system instead of two that have to agree.
        per_country: dict[str, list[GeoEvent]] = defaultdict(list)
        for event in events:
            per_country[event.country or ''].append(event)
        countries = sorted(
            (
                {
                    'name': name or UNATTRIBUTED,
                    'attributed': bool(name),
                    'events': len(rows),
                    'places': sorted({row.place_name for row in rows if row.place_name}),
                }
                for name, rows in per_country.items()
            ),
            key=lambda row: (-row['events'], row['name']),
        )

        dated = sorted(event.occurred_at for event in events if event.occurred_at)
        period = {
            'earliest': dated[0].isoformat() if dated else None,
            'latest': dated[-1].isoformat() if dated else None,
            'undated': sum(1 for event in events if not event.occurred_at),
        }

        items = {
            event.content_item_id: event.content_item
            for event in events if event.content_item_id
        }
        outlets = Counter(
            item.source.domain for item in items.values()
            if item and item.source_id
        )

        ranked = sorted(events, key=lambda event: (-event.mentions, event.place_name))

        return {
            'events': len(events),
            'by_confidence': {band: bands.get(band, 0) for band in BAND_RANK},
            'by_method': dict(methods),
            'countries': countries,
            'top_places': [
                {
                    'name': event.place_name,
                    'country': event.country,
                    'mentions': event.mentions,
                    'confidence': event.confidence,
                }
                for event in ranked[:self.named_places]
            ],
            'period': period,
            'articles': len(items),
            'outlets': [
                {'domain': domain, 'articles': count}
                for domain, count in outlets.most_common()
            ],
            'corroboration': self._corroboration(items.keys()),
            'not_placed': self._not_placed(items.keys()),
        }

    @staticmethod
    def _corroboration(item_ids) -> dict:
        """How well other outlets backed the articles these pins came from.

        Module 2 already answers this per article; the report's job is only to
        carry the answer forward rather than presenting geography as if the
        journalism underneath it were settled. Articles that were never
        checked are counted separately from articles that checked out badly —
        collapsing those two into one average is how an unexamined story ends
        up looking average.
        """
        from content.models import Corroboration

        ids = list(item_ids)
        if not ids:
            return {'checked': 0, 'unchecked': 0, 'rated': 0, 'mean': None, 'verdicts': {}}

        latest: dict[int, Corroboration] = {}
        for check in (
            Corroboration.objects.filter(content_item_id__in=ids).order_by('created_at')
        ):
            latest[check.content_item_id] = check

        scores = [c.score for c in latest.values() if c.score is not None]
        return {
            'checked': len(latest),
            'unchecked': len(ids) - len(latest),
            'rated': len(scores),
            'mean': round(sum(scores) / len(scores), 1) if scores else None,
            'verdicts': dict(Counter(c.verdict for c in latest.values())),
        }

    @staticmethod
    def _not_placed(item_ids) -> list[str]:
        """Names the geocoder saw in these same articles and refused to place.

        This is the part of the extraction that has no pin, which means it is
        also the part a map cannot show. Carried into the report so that the
        places the reader is not seeing are at least named.
        """
        ids = list(item_ids)
        if not ids:
            return []
        newest: dict[int, ExtractionRun] = {}
        for run in (
            ExtractionRun.objects
            .filter(content_item_id__in=ids)
            .order_by('content_item_id', '-created_at')
        ):
            newest.setdefault(run.content_item_id, run)

        seen: list[str] = []
        for run in newest.values():
            for note in run.notes or []:
                if note not in seen:
                    seen.append(note)
        return seen

    # --------------------------------------------------------------- periods

    @staticmethod
    def _bounds(period):
        if not period['earliest']:
            return None
        return parse_datetime(period['earliest']), parse_datetime(period['latest'])

    def _period_span(self, period) -> str:
        """Terse, for a title: '18 Sep 2026' or '18 to 20 Sep 2026'."""
        bounds = self._bounds(period)
        if not bounds:
            return ''
        first, last = bounds
        if first.date() == last.date():
            return f'{first:%d %b %Y}'
        if (first.year, first.month) == (last.year, last.month):
            return f'{first:%d} to {last:%d %b %Y}'
        return f'{first:%d %b %Y} to {last:%d %b %Y}'

    def _period_prose(self, period) -> str:
        """For a sentence: 'all on 18 Sep 2026' / 'from 18 to 20 Sep 2026'."""
        bounds = self._bounds(period)
        if not bounds:
            return ''
        first, last = bounds
        span = self._period_span(period)
        return f'all on {span}' if first.date() == last.date() else f'from {span}'

    # ----------------------------------------------------------------- prose

    def _title(self, findings) -> str:
        countries = [row for row in findings['countries'] if row['attributed']]
        where = (
            countries[0]['name'] if len(countries) == 1
            else plural(len(countries), 'country', 'countries') if countries
            else 'unattributed locations'
        )
        stem = f'{plural(findings["events"], "located event")} in {where}'
        span = self._period_span(findings['period'])
        return f'{stem}, {span}' if span else stem

    def _summary(self, findings, scope_text) -> str:
        """Five sentences at most, each one a figure straight from `findings`."""
        lines = []
        countries = [row for row in findings['countries'] if row['attributed']]
        lines.append(
            f'{plural(findings["events"], "location")} placed across '
            f'{plural(len(countries), "country", "countries")}, read out of '
            f'{plural(findings["articles"], "article")}.'
        )

        when = self._period_prose(findings['period'])
        undated = findings['period']['undated']
        if when:
            lines.append(
                f'Reported {when}'
                + (f', with {undated} carrying no date at all.' if undated else '.')
            )
        elif undated:
            lines.append(f'None of the {undated} carries a date.')

        bands = findings['by_confidence']
        lines.append(
            f'Precision: {bands[GeoEvent.Confidence.HIGH]} placed exactly, '
            f'{bands[GeoEvent.Confidence.MEDIUM]} to a single named place, '
            f'{bands[GeoEvent.Confidence.LOW]} no closer than a country or region.'
        )

        named = ', '.join(
            f'{row["name"]} ({row["mentions"]})' for row in findings['top_places']
        )
        if named:
            lines.append(f'Most mentioned: {named}.')

        corro = findings['corroboration']
        if corro['mean'] is not None:
            tail = (
                f', and {corro["unchecked"]} never checked at all'
                if corro['unchecked'] else ''
            )
            lines.append(
                f'The reporting behind them rates {corro["mean"]}/10 on average '
                f'across {plural(corro["rated"], "rated article")}{tail}.'
            )
        elif corro['unchecked']:
            lines.append(
                f'None of the {plural(corro["unchecked"], "article")} behind them '
                f'has been cross-referenced against another outlet.'
            )

        lines.append(f'Scope: {self._scope_sentence(scope_text)}.')
        return ' '.join(lines)

    @staticmethod
    def _scope_sentence(scope_text) -> str:
        return ', '.join(scope_text)

    def _gaps(self, events, findings) -> list[str]:
        """What this report is not evidence of. The section that earns it."""
        gaps: list[str] = []

        # The one that applies to every geographic report ever produced from a
        # corpus, and the one most likely to be forgotten by whoever is
        # looking at the map.
        gaps.append(
            'This is the geography of what was read, not of what happened. A '
            'country with no pin is a country nothing in this corpus '
            'mentioned; it is not a country where nothing occurred.'
        )

        low = findings['by_confidence'][GeoEvent.Confidence.LOW]
        if low:
            gaps.append(
                f'{low} of {findings["events"]} pins are country- or '
                f'region-level only. Those coordinates are a point inside a '
                f'border, not the site of an event, and distances should not '
                f'be measured from them.'
            )

        unplaced = findings['not_placed']
        if unplaced:
            # The notes are already full sentences; running them together
            # with their own full stops intact is what produces "..largest..".
            shown = ' '.join(note.rstrip('.') + '.' for note in unplaced[:4])
            more = (
                f' A further {len(unplaced) - 4} were refused for the same kind '
                f'of reason.' if len(unplaced) > 4 else ''
            )
            count = len(unplaced)
            gaps.append(
                f'{plural(count, "place name")} in these same articles '
                f'{verb(count, "was", "were")} found and deliberately not '
                f'placed, so {verb(count, "it appears", "they appear")} '
                f'nowhere on the map. {shown}{more}'
            )

        table = gazetteer.size()
        gaps.append(
            f'Place names are resolved against a table of {table["cities"]} '
            f'cities and {table["regions"]} regions compiled into this system. '
            f'Somewhere too small for that table produces no pin and, unless '
            f'the article stated coordinates, no trace at all.'
        )

        undated = findings['period']['undated']
        if undated:
            gaps.append(
                f'{plural(undated, "event")} '
                f'{verb(undated, "carries", "carry")} no date at all — in the '
                f'counts, and absent from the period above.'
            )
        if findings['period']['earliest']:
            gaps.append(
                'Dates are the publication dates of the articles, not the '
                'times the events occurred. A story published on the 5th about '
                'the 2nd is dated here as the 5th.'
            )

        corro = findings['corroboration']
        if corro['unchecked']:
            gaps.append(
                f'{corro["unchecked"]} of '
                f'{plural(findings["articles"], "article")} behind these pins '
                f'{verb(corro["unchecked"], "has", "have")} never been '
                f'cross-referenced against another outlet, so nothing here '
                f'says whether the underlying reporting is corroborated.'
            )

        if len(events) >= MAX_EVENTS:
            gaps.append(
                f'The selection was cut off at {MAX_EVENTS} events. More match '
                f'this scope than are reported here.'
            )

        return gaps

    # ------------------------------------------------------------- document

    def _document(self, heading, summary, findings, gaps, scope_text, events) -> str:
        now = timezone.localtime()
        out: list[str] = [
            f'# {heading}',
            '',
            f'*Generated {now:%d %b %Y %H:%M} by `{self.builder_version}`. '
            f'{plural(findings["events"], "located event")} from '
            f'{plural(findings["articles"], "article")}.*',
            '',
            '## What this covers',
            '',
            f'{self._scope_sentence(scope_text).capitalize()}.',
            '',
            '## Summary',
            '',
            summary,
            '',
            '## Where',
            '',
            '| Country | Events | Places |',
            '| --- | ---: | --- |',
        ]
        for row in findings['countries']:
            places = ', '.join(row['places'][:8]) or '—'
            if len(row['places']) > 8:
                places += f', +{len(row["places"]) - 8} more'
            out.append(f'| {row["name"]} | {row["events"]} | {places} |')

        out += [
            '',
            '## How certain each location is',
            '',
            '| Confidence | Events | What that means |',
            '| --- | ---: | --- |',
        ]
        for band in (GeoEvent.Confidence.HIGH, GeoEvent.Confidence.MEDIUM,
                     GeoEvent.Confidence.LOW):
            out.append(
                f'| {band.label} | {findings["by_confidence"][band]} | '
                f'{BAND_MEANING[band]} |'
            )

        out += ['', '## What it was read from', '']
        if findings['outlets']:
            out += ['| Outlet | Articles |', '| --- | ---: |']
            out += [
                f'| {row["domain"]} | {row["articles"]} |'
                for row in findings['outlets']
            ]
        else:
            out.append('No outlet is attached to these events.')

        out += ['', self._corroboration_sentence(findings['corroboration'],
                                                 findings['articles'])]

        out += ['', '## What this report does not show', '']
        out += [f'- {gap}' for gap in gaps]

        out += [
            '',
            '## Every event in this report',
            '',
            '| Place | Coordinates | Confidence | How | Mentions | Read from |',
            '| --- | --- | --- | --- | ---: | --- |',
        ]
        for event in events:
            title = (event.content_item.title if event.content_item else '') or '—'
            out.append(
                f'| {event.place_name or "—"} '
                f'| {event.latitude:.4f}, {event.longitude:.4f} '
                f'| {event.get_confidence_display()} '
                f'| {event.get_method_display()} '
                f'| {event.mentions} '
                f'| {title[:70]} |'
            )

        return '\n'.join(out) + '\n'

    @staticmethod
    def _corroboration_sentence(corro, articles) -> str:
        if not articles:
            return 'These events are not attached to any article.'
        if corro['mean'] is not None:
            verdicts = ', '.join(
                f'{count} {verdict.replace("_", " ")}'
                for verdict, count in sorted(corro['verdicts'].items())
            )
            unchecked = (
                f' {corro["unchecked"]} '
                f'{verb(corro["unchecked"], "has", "have")} never been checked.'
                if corro['unchecked'] else ''
            )
            return (
                f'Of {plural(articles, "article")}, {corro["rated"]} '
                f'{verb(corro["rated"], "carries", "carry")} a corroboration '
                f'rating, averaging {corro["mean"]}/10 ({verdicts}).{unchecked}'
            )
        return (
            f'None of these {plural(articles, "article")} carries a '
            f'corroboration rating, so the reporting behind every pin here is '
            f'unexamined.'
        )


def get_report_builder() -> BaseReportBuilder:
    """The configured builder. Same contract as every other engine here."""
    path = getattr(
        settings, 'REPORTGIS_REPORT_BUILDER',
        'reportgis.reporting.NarrativeReportBuilder',
    )
    return import_string(path)()
