"""Tests for module 5.

The ones that matter most are in RefusalTests. Everything else here checks that
a correct answer comes out; those check that a *wrong* answer does not, which
is the harder property and the one a map has to have. A pin is read as a fact
by whoever looks at it, so the test suite treats "placed something it could not
justify" as the serious failure and "found nothing" as the acceptable one.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from content.models import ContentItem
from core.models import Source

from .gazetteer import lookup, size
from .geocode import GazetteerGeocoder, haversine_km
from .models import ExtractionRun, GeoEvent, Report
from .reporting import (
    EmptySelection,
    NarrativeReportBuilder,
    ScopeError,
    plural,
    select_events,
)
from .tasks import extract_locations, locate_content_item

ENGINE = GazetteerGeocoder()


def located(result, name):
    """The place named, or None. Names are compared case-insensitively."""
    for place in result.places:
        if place.name.lower() == name.lower():
            return place
    return None


def article(body, title='', source=None, **kwargs):
    source = source or Source.objects.create(domain=f'x{ContentItem.objects.count()}.test')
    return ContentItem.objects.create(
        source=source,
        url=f'https://x.test/{ContentItem.objects.count()}',
        title=title,
        body=body,
        status=ContentItem.Status.ANALYZED,
        **kwargs,
    )


class RefusalTests(TestCase):
    """What the geocoder declines to place, and whether it says so."""

    def test_an_ambiguous_name_produces_no_pin(self):
        result = ENGINE.locate('Officials in Springfield delayed the vote again.')
        self.assertEqual(result.places, [])

    def test_an_ambiguous_name_is_still_reported(self):
        # Silence would be indistinguishable from "no place was mentioned",
        # and an analyst would never know to look.
        result = ENGINE.locate('Officials in Springfield delayed the vote again.')
        self.assertEqual(len(result.notes), 1)
        self.assertIn('Springfield', result.notes[0])
        self.assertIn('3 entries', result.notes[0])

    def test_a_bare_washington_is_not_resolved_to_the_capital(self):
        result = ENGINE.locate('Washington imposed new sanctions on Tuesday.')
        self.assertIsNone(located(result, 'Washington'))
        self.assertTrue(any('Washington' in note for note in result.notes))

    def test_lowercase_words_that_happen_to_be_countries_are_not_places(self):
        # Without the capitalisation rule this sentence reports from four
        # countries and a French city.
        result = ENGINE.locate(
            'The turkey was nice, the china was chipped, and chad was reading.'
        )
        self.assertEqual(result.places, [])

    def test_an_institution_named_after_a_place_is_not_that_place(self):
        result = ENGINE.locate('Researchers at Georgia Tech released the data.')
        self.assertIsNone(located(result, 'Georgia'))

    def test_a_pair_of_ordinary_numbers_is_not_a_coordinate(self):
        # Two decimal places is a figure in a table. Three is a coordinate.
        result = ENGINE.locate('Inflation moved from 3.10, 2.40 over the quarter.')
        self.assertEqual(result.places, [])

    def test_an_out_of_range_coordinate_is_dropped(self):
        result = ENGINE.locate('Logged at 131.5017 N, 34.4668 E by the array.')
        self.assertEqual(result.places, [])


class CoordinateTests(TestCase):
    """Coordinates stated in the text. The only signal that stands alone."""

    def test_decimal_degrees_with_hemispheres(self):
        result = ENGINE.locate('The strike landed at 31.5017 N, 34.4668 E overnight.')
        place = result.places[0]
        self.assertEqual(place.confidence, 'high')
        self.assertEqual(place.method, 'coordinates')
        self.assertAlmostEqual(place.latitude, 31.5017, places=4)
        self.assertAlmostEqual(place.longitude, 34.4668, places=4)

    def test_southern_and_western_hemispheres_are_negative(self):
        result = ENGINE.locate('Recorded at 33.8688 S, 70.6693 W by the station.')
        place = result.places[0]
        self.assertLess(place.latitude, 0)
        self.assertLess(place.longitude, 0)

    def test_degrees_minutes_seconds(self):
        result = ENGINE.locate('Observed at 51°30′26″N 0°7′39″W.')
        place = result.places[0]
        self.assertAlmostEqual(place.latitude, 51.5072, places=3)
        self.assertAlmostEqual(place.longitude, -0.1275, places=3)

    def test_a_bare_pair_at_coordinate_precision(self):
        result = ENGINE.locate('Sensors logged 48.8566, 2.3522 before midnight.')
        place = result.places[0]
        self.assertEqual(place.confidence, 'high')
        self.assertAlmostEqual(place.latitude, 48.8566, places=4)

    def test_the_source_span_is_kept(self):
        result = ENGINE.locate('Sensors logged 48.8566, 2.3522 before midnight.')
        self.assertEqual(result.places[0].matched_text, '48.8566, 2.3522')


class DisambiguationTests(TestCase):
    """Two places with one name, and what settles it."""

    def test_a_written_qualifier_settles_it_and_earns_high(self):
        result = ENGINE.locate('A raid was reported in Tripoli, Lebanon on Tuesday.')
        place = located(result, 'Tripoli')
        self.assertEqual(place.confidence, 'high')
        # Lebanon's Tripoli, not Libya's. 34.44N/35.84E against 32.89N/13.19E.
        self.assertAlmostEqual(place.latitude, 34.44, places=2)

    def test_a_country_named_elsewhere_settles_it_and_earns_medium(self):
        result = ENGINE.locate(
            'A blast struck Tripoli early Monday. Libya has seen months of unrest.'
        )
        place = located(result, 'Tripoli')
        self.assertEqual(place.confidence, 'medium')
        self.assertAlmostEqual(place.latitude, 32.89, places=2)

    def test_a_us_state_qualifier_works_the_same_way(self):
        result = ENGINE.locate('Voters in Birmingham, Alabama turned out early.')
        place = located(result, 'Birmingham')
        self.assertEqual(place.confidence, 'high')
        self.assertAlmostEqual(place.latitude, 33.52, places=2)

    def test_the_english_birmingham_is_the_other_one(self):
        result = ENGINE.locate('Voters in Birmingham, England turned out early.')
        place = located(result, 'Birmingham')
        self.assertAlmostEqual(place.latitude, 52.49, places=2)

    def test_a_nationality_settles_it(self):
        # News prose writes "Libyan officials", not "Libya". Requiring the
        # literal country name refused stories that had plainly said where
        # they were.
        result = ENGINE.locate(
            'An explosion was heard in Tripoli on Monday, Libyan officials said.'
        )
        place = located(result, 'Tripoli')
        self.assertEqual(place.confidence, 'medium')
        self.assertAlmostEqual(place.latitude, 32.89, places=2)
        self.assertIn('Libyan', place.reasons[0]['label'])

    def test_a_nationality_never_places_a_pin_of_its_own(self):
        # "American" is not a report from the middle of Kansas.
        result = ENGINE.locate('American officials declined to comment.')
        self.assertEqual(result.places, [])

    def test_a_lowercase_nationality_is_not_evidence(self):
        result = ENGINE.locate('He admired the polish on the Tripoli brass.')
        self.assertIsNone(located(result, 'Tripoli'))

    def test_the_longest_name_wins(self):
        result = ENGINE.locate('Washington, D.C. announced the measure.')
        self.assertIsNotNone(located(result, 'Washington, D.C.'))


class ConfidenceTests(TestCase):
    """The band a hit lands in, which is the whole warning system."""

    def test_a_country_is_never_better_than_low(self):
        result = ENGINE.locate('Germany and France issued a joint statement.')
        for name in ('Germany', 'France'):
            self.assertEqual(located(result, name).confidence, 'low')

    def test_an_unambiguous_city_is_medium_not_high(self):
        # Unambiguous *in this table* is weaker than unambiguous. A second
        # Nairobi the table does not carry would never have shown up as a
        # conflict, so the band stays one below certain.
        result = ENGINE.locate('The summit opened in Nairobi on Thursday.')
        self.assertEqual(located(result, 'Nairobi').confidence, 'medium')

    def test_every_pin_carries_its_reasoning(self):
        result = ENGINE.locate('The summit opened in Nairobi, Kenya on Thursday.')
        for place in result.places:
            self.assertTrue(place.reasons)
            self.assertTrue(place.reasons[0]['label'])
            self.assertIn(place.reasons[0]['effect'], {'located', 'limits'})

    def test_a_country_pin_says_it_is_only_a_country_pin(self):
        result = ENGINE.locate('Unrest continued across Sudan this week.')
        label = located(result, 'Sudan').reasons[0]['label']
        self.assertIn('representative point', label)


class TableTests(TestCase):
    """The gazetteer itself."""

    def test_aliases_resolve_to_the_canonical_row(self):
        self.assertEqual(lookup('Kiev')[0].name, 'Kyiv')
        self.assertEqual(lookup('UK')[0].name, 'United Kingdom')
        self.assertEqual(lookup('Bombay')[0].name, 'Mumbai')

    def test_accents_do_not_have_to_be_typed(self):
        self.assertEqual(lookup('São Paulo'), lookup('Sao Paulo'))
        self.assertTrue(lookup('Bogotá'))

    def test_short_aliases_only_match_in_capitals(self):
        # "US" is the United States. "Us" opening a sentence is a pronoun, and
        # without this rule every one of them files a dispatch from Washington.
        self.assertIsNotNone(located(ENGINE.locate('The US said so.'), 'United States'))
        self.assertIsNone(located(ENGINE.locate('Us and them, he said.'), 'United States'))

    def test_a_name_the_table_lacks_is_simply_absent(self):
        # Not a near match, not the closest string. Absent.
        self.assertEqual(lookup('Nether Wallop'), [])
        self.assertEqual(ENGINE.locate('A fete was held in Nether Wallop.').places, [])

    def test_the_table_reports_its_own_size(self):
        counts = size()
        self.assertGreater(counts['cities'], 300)
        self.assertGreater(counts['countries'], 140)
        # The collisions are the point; a table with none is not being honest
        # about how place names work.
        self.assertGreater(counts['ambiguous_names'], 10)


class DistanceTests(TestCase):
    def test_a_known_distance(self):
        # London to Paris is about 343km great-circle.
        km = haversine_km(51.5072, -0.1276, 48.8566, 2.3522)
        self.assertAlmostEqual(km, 343, delta=3)

    def test_zero_distance(self):
        self.assertEqual(haversine_km(10.0, 20.0, 10.0, 20.0), 0.0)


class ExtractionTests(TestCase):
    """Turning a stored article into pins, repeatably."""

    def test_an_article_gets_pins_and_a_run(self):
        item = article('A blast struck Tripoli early Monday, officials in Libya said.')
        run = locate_content_item(item)

        self.assertEqual(run.events_found, GeoEvent.objects.filter(content_item=item).count())
        self.assertTrue(run.engine_version)
        self.assertTrue(GeoEvent.objects.filter(content_item=item, place_name='Tripoli').exists())

    def test_a_run_is_logged_even_when_nothing_is_found(self):
        # "No locations in this text" and "nobody has looked at this text" are
        # different states, and the console has to be able to tell them apart.
        item = article('The committee met to discuss procedural amendments.')
        run = locate_content_item(item)

        self.assertEqual(run.events_found, 0)
        self.assertEqual(ExtractionRun.objects.filter(content_item=item).count(), 1)

    def test_the_refusals_are_stored_with_the_run(self):
        item = article('Officials in Springfield delayed the vote again.')
        run = locate_content_item(item)

        self.assertEqual(run.events_found, 0)
        self.assertTrue(run.notes)
        self.assertIn('Springfield', run.notes[0])

    def test_re_running_updates_rather_than_duplicates(self):
        item = article('A blast struck Tripoli early Monday, officials in Libya said.')
        locate_content_item(item)
        first = GeoEvent.objects.filter(content_item=item).count()
        locate_content_item(item)

        self.assertEqual(GeoEvent.objects.filter(content_item=item).count(), first)
        self.assertEqual(ExtractionRun.objects.filter(content_item=item).count(), 2)

    def test_a_pin_the_engine_no_longer_supports_is_withdrawn(self):
        item = article('A blast struck Tripoli early Monday, officials in Libya said.')
        locate_content_item(item)

        item.body = 'The committee met to discuss procedural amendments.'
        item.save(update_fields=['body'])
        locate_content_item(item)

        self.assertFalse(GeoEvent.objects.filter(content_item=item).exists())

    def test_a_manual_pin_survives_a_re_run(self):
        # The engine withdraws its own conclusions, not an analyst's.
        item = article('The committee met to discuss procedural amendments.')
        GeoEvent.objects.create(
            content_item=item, latitude=1.0, longitude=2.0,
            place_name='Where the source was met',
            method=GeoEvent.Method.MANUAL,
        )
        locate_content_item(item)

        self.assertTrue(
            GeoEvent.objects.filter(content_item=item, method=GeoEvent.Method.MANUAL).exists()
        )

    def test_the_headline_is_read_too(self):
        item = article('No further detail was given.', title='Explosion reported in Beirut')
        locate_content_item(item)
        self.assertTrue(GeoEvent.objects.filter(content_item=item, place_name='Beirut').exists())

    def test_the_task_reports_what_it_did(self):
        item = article('A blast struck Tripoli early Monday, officials in Libya said.')
        outcome = extract_locations.run(item.pk)
        self.assertEqual(outcome['content_item'], item.pk)
        self.assertGreater(outcome['located'], 0)

    def test_the_task_survives_a_deleted_item(self):
        outcome = extract_locations.run(999_999)
        self.assertEqual(outcome['located'], 0)


class ApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(get_user_model().objects.create_user('analyst'))
        self.item = article(
            'A blast struck Tripoli early Monday, officials in Libya said.',
            published_at=timezone.now(),
        )

    def test_geojson_puts_longitude_first(self):
        locate_content_item(self.item)
        body = self.client.get('/api/reportgis/events/geojson/').json()

        self.assertEqual(body['type'], 'FeatureCollection')
        feature = next(f for f in body['features'] if f['properties']['place_name'] == 'Tripoli')
        lon, lat = feature['geometry']['coordinates']
        self.assertAlmostEqual(lon, 13.19, places=2)
        self.assertAlmostEqual(lat, 32.89, places=2)

    def test_geojson_carries_the_reasoning(self):
        locate_content_item(self.item)
        body = self.client.get('/api/reportgis/events/geojson/').json()
        self.assertTrue(body['features'][0]['properties']['reasons'])

    def test_a_radius_query_includes_and_excludes_correctly(self):
        GeoEvent.objects.create(latitude=51.5072, longitude=-0.1276, place_name='London')
        GeoEvent.objects.create(latitude=48.8566, longitude=2.3522, place_name='Paris')

        near_london = self.client.get(
            '/api/reportgis/events/', {'near': '51.5072,-0.1276', 'within_km': 100}
        ).json()
        names = {row['place_name'] for row in near_london['results']}
        self.assertEqual(names, {'London'})

        wider = self.client.get(
            '/api/reportgis/events/', {'near': '51.5072,-0.1276', 'within_km': 400}
        ).json()
        self.assertEqual(
            {row['place_name'] for row in wider['results']}, {'London', 'Paris'}
        )

    def test_a_radius_query_uses_distance_not_the_bounding_box(self):
        # A corner of the box around London at 400km is over 500km away. A
        # box-only filter would return it.
        GeoEvent.objects.create(latitude=54.9, longitude=5.0, place_name='Corner')
        body = self.client.get(
            '/api/reportgis/events/', {'near': '51.5072,-0.1276', 'within_km': 400}
        ).json()
        self.assertEqual(body['results'], [])

    def test_a_malformed_radius_query_returns_nothing_not_everything(self):
        GeoEvent.objects.create(latitude=51.5072, longitude=-0.1276, place_name='London')
        body = self.client.get('/api/reportgis/events/', {'near': 'over there'}).json()
        self.assertEqual(body['results'], [])

    def test_the_engine_endpoint_describes_the_table_and_the_limits(self):
        body = self.client.get('/api/reportgis/events/engine/').json()
        self.assertTrue(body['capabilities'])
        self.assertTrue(body['not_available'])
        self.assertTrue(body['refuses_to_guess'])
        self.assertGreater(body['gazetteer']['cities'], 300)

    def test_extract_runs_one_article_inline(self):
        response = self.client.post(
            '/api/reportgis/events/extract/', {'content_item': self.item.pk}
        )
        self.assertEqual(response.status_code, 200)
        self.assertGreater(response.json()['events_found'], 0)

    def test_extract_sweeps_what_has_not_been_read(self):
        body = self.client.post('/api/reportgis/events/extract/', {}).json()
        self.assertEqual(body['processed'], 1)
        # Nothing is left waiting, so a second sweep is a no-op and says so.
        again = self.client.post('/api/reportgis/events/extract/', {}).json()
        self.assertEqual(again['processed'], 0)
        self.assertIn('No analysed article', again['detail'])

    def test_extract_on_a_missing_item_is_a_404(self):
        response = self.client.post(
            '/api/reportgis/events/extract/', {'content_item': 999_999}
        )
        self.assertEqual(response.status_code, 404)

    def test_the_extraction_log_is_read_only(self):
        locate_content_item(self.item)
        run = ExtractionRun.objects.get()
        response = self.client.delete(f'/api/reportgis/extractions/{run.pk}/')
        self.assertEqual(response.status_code, 405)

    def test_the_module_is_reachable_without_a_spatial_database(self):
        # The whole point of the day's work: no GDAL, no GEOS, no PostGIS.
        overview = self.client.get('/api/overview/').json()
        self.assertTrue(overview['modules']['reportgis']['online'])


def pin(name, lat, lon, country='', confidence=GeoEvent.Confidence.MEDIUM, **kwargs):
    """A located event, without going through the geocoder to get one."""
    return GeoEvent.objects.create(
        place_name=name, latitude=lat, longitude=lon, country=country,
        confidence=confidence, **kwargs,
    )


class ScopeTests(TestCase):
    """Choosing which events a report is about."""

    def setUp(self):
        self.paris = pin('Paris', 48.86, 2.35, 'France')
        self.lyon = pin('Lyon', 45.76, 4.84, 'France',
                        confidence=GeoEvent.Confidence.HIGH)
        self.tripoli = pin('Tripoli', 32.89, 13.19, 'Libya',
                           confidence=GeoEvent.Confidence.LOW)

    def test_an_empty_scope_takes_everything_and_says_so(self):
        events, described = select_events({})
        self.assertEqual(len(events), 3)
        self.assertIn('every location', described[0])

    def test_a_confidence_floor_includes_the_bands_above_it(self):
        events, _ = select_events({'confidence': 'medium'})
        self.assertEqual({e.pk for e in events}, {self.paris.pk, self.lyon.pk})

    def test_an_unknown_band_is_refused_rather_than_ignored(self):
        # Silently dropping an unrecognised filter would widen the report
        # without telling anyone it had been widened.
        with self.assertRaises(ScopeError):
            select_events({'confidence': 'certain'})

    def test_a_country_is_accepted_by_name_or_by_code(self):
        by_name, _ = select_events({'country': 'France'})
        by_code, _ = select_events({'country': 'fr'})
        self.assertEqual(len(by_name), 2)
        self.assertEqual({e.pk for e in by_name}, {e.pk for e in by_code})

    def test_an_unknown_country_is_refused(self):
        with self.assertRaises(ScopeError):
            select_events({'country': 'Westeros'})

    def test_a_radius_scope_measures_distance_not_a_box(self):
        events, described = select_events({'near': '48.86,2.35', 'within_km': 500})
        self.assertEqual({e.pk for e in events}, {self.paris.pk, self.lyon.pk})
        self.assertIn('within 500km', described[0])

    def test_a_malformed_radius_is_refused(self):
        with self.assertRaises(ScopeError):
            select_events({'near': 'somewhere'})

    def test_a_date_window_filters_on_when_it_was_reported(self):
        recent = timezone.now()
        pin('Berlin', 52.52, 13.40, 'Germany', occurred_at=recent)
        events, described = select_events({'since': recent.date().isoformat()})
        self.assertEqual([e.place_name for e in events], ['Berlin'])
        self.assertIn('on or after', described[0])

    def test_an_unparseable_date_is_refused(self):
        with self.assertRaises(ScopeError):
            select_events({'since': 'last Tuesday'})


class ReportBuilderTests(TestCase):
    """What a generated report says, and what it admits it cannot say."""

    def setUp(self):
        self.builder = NarrativeReportBuilder()
        self.item = article(
            'A blast struck Tripoli, Libyan officials said. Reports from '
            'Springfield could not be confirmed.',
            title='Blast in Tripoli',
            published_at=timezone.now(),
        )
        locate_content_item(self.item)
        self.events = list(GeoEvent.objects.all())

    def build(self, events=None, **kwargs):
        return self.builder.build(events if events is not None else self.events, **kwargs)

    def test_nothing_to_report_is_a_refusal_not_an_empty_report(self):
        # An empty document under a scope heading reads as "we looked here and
        # nothing happened", which is a claim this module cannot make.
        with self.assertRaises(EmptySelection) as caught:
            self.build([])
        self.assertIn('extraction log', str(caught.exception))

    def test_the_document_carries_every_section_it_promises(self):
        built = self.build()
        for heading in (
            '## What this covers', '## Summary', '## Where',
            '## How certain each location is', '## What it was read from',
            '## What this report does not show', '## Every event in this report',
        ):
            self.assertIn(heading, built.document)

    def test_every_report_says_the_map_is_of_what_was_read(self):
        # The single caveat that applies no matter what the selection is.
        built = self.build()
        self.assertTrue(
            any('geography of what was read' in gap for gap in built.gaps)
        )

    def test_the_gaps_are_in_the_document_as_well_as_the_field(self):
        built = self.build()
        for gap in built.gaps:
            self.assertIn(gap, built.document)

    def test_names_the_geocoder_refused_are_named_in_the_report(self):
        # Springfield has no pin, so the map cannot show it. The report can.
        built = self.build()
        self.assertTrue(any('Springfield' in gap for gap in built.gaps))
        self.assertIn('Springfield', built.document)

    def test_an_unchecked_article_is_reported_as_unchecked(self):
        built = self.build()
        self.assertEqual(built.findings['corroboration']['rated'], 0)
        self.assertTrue(
            any('never been cross-referenced' in gap for gap in built.gaps)
        )

    def test_a_country_level_pin_is_flagged_as_not_a_site(self):
        built = self.build([pin('Chad', 15.45, 18.73, 'Chad',
                                confidence=GeoEvent.Confidence.LOW)])
        self.assertTrue(any('not the site of an event' in gap for gap in built.gaps))

    def test_the_findings_match_the_events_they_were_counted_from(self):
        built = self.build()
        self.assertEqual(built.findings['events'], len(self.events))
        self.assertEqual(
            sum(built.findings['by_confidence'].values()), len(self.events)
        )
        self.assertEqual(sorted(built.event_ids), sorted(e.pk for e in self.events))

    def test_a_given_title_is_kept_and_a_missing_one_is_written(self):
        self.assertEqual(self.build(title='Libya, week 38').title, 'Libya, week 38')
        self.assertIn('located event', self.build().title)

    def test_counts_are_written_as_english_not_as_parenthesised_esses(self):
        built = self.build([pin('Oslo', 59.91, 10.75, 'Norway')])
        self.assertIn('1 located event', built.title)
        self.assertNotIn('(s)', built.document)

    def test_the_scope_is_repeated_back_in_the_document(self):
        built = self.build(scope_text=['locations placed with at least high confidence'])
        self.assertIn('at least high confidence', built.document)

    def test_plural(self):
        self.assertEqual(plural(1, 'country', 'countries'), '1 country')
        self.assertEqual(plural(0, 'country', 'countries'), '0 countries')
        self.assertEqual(plural(2, 'article'), '2 articles')


class ReportApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(get_user_model().objects.create_user('analyst'))
        self.paris = pin('Paris', 48.86, 2.35, 'France')
        self.tripoli = pin('Tripoli', 32.89, 13.19, 'Libya')

    def generate(self, payload):
        return self.client.post('/api/reportgis/reports/generate/', payload, format='json')

    def test_generating_from_a_hand_picked_list(self):
        response = self.generate({'events': [self.paris.pk], 'title': 'One pin'})
        self.assertEqual(response.status_code, 201)
        report = Report.objects.get()
        self.assertEqual(report.title, 'One pin')
        self.assertEqual(list(report.geo_events.all()), [self.paris])
        self.assertEqual(report.scope, {'events': [self.paris.pk]})

    def test_generating_from_a_scope_stores_the_scope(self):
        response = self.generate({'scope': {'country': 'France'}})
        self.assertEqual(response.status_code, 201)
        report = Report.objects.get()
        self.assertEqual(report.scope, {'country': 'France'})
        self.assertEqual(report.geo_events.count(), 1)
        self.assertTrue(report.generated_at)
        self.assertTrue(report.builder_version)

    def test_a_scope_matching_nothing_is_a_refusal_with_a_reason(self):
        response = self.generate({'scope': {'country': 'Japan'}})
        self.assertEqual(response.status_code, 400)
        self.assertIn('extraction log', response.json()['detail'])
        self.assertEqual(Report.objects.count(), 0)

    def test_an_unknown_event_id_is_refused_rather_than_skipped(self):
        # Quietly dropping it would produce a report that is missing an event
        # the caller believes is in it.
        response = self.generate({'events': [self.paris.pk, 999_999]})
        self.assertEqual(response.status_code, 400)
        self.assertIn('999999', response.json()['detail'])

    def test_an_unknown_country_is_refused(self):
        response = self.generate({'scope': {'country': 'Westeros'}})
        self.assertEqual(response.status_code, 400)

    def test_preview_counts_without_writing_anything(self):
        body = self.client.post(
            '/api/reportgis/reports/preview/', {'scope': {}}, format='json'
        ).json()
        self.assertEqual(body['events'], 2)
        self.assertEqual(body['countries'], 2)
        self.assertEqual(Report.objects.count(), 0)

    def test_preview_says_plainly_when_a_scope_matches_nothing(self):
        body = self.client.post(
            '/api/reportgis/reports/preview/',
            {'scope': {'country': 'Japan'}}, format='json',
        ).json()
        self.assertEqual(body['events'], 0)
        self.assertIn('Nothing matches', body['detail'])

    def test_regenerating_reuses_the_row_and_picks_up_new_events(self):
        self.generate({'scope': {'country': 'France'}})
        report = Report.objects.get()
        pin('Lyon', 45.76, 4.84, 'France')

        response = self.client.post(f'/api/reportgis/reports/{report.pk}/regenerate/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Report.objects.count(), 1)
        report.refresh_from_db()
        self.assertEqual(report.geo_events.count(), 2)

    def test_regenerating_a_hand_picked_report_keeps_the_same_events(self):
        self.generate({'events': [self.paris.pk]})
        report = Report.objects.get()
        pin('Lyon', 45.76, 4.84, 'France')

        self.client.post(f'/api/reportgis/reports/{report.pk}/regenerate/')
        report.refresh_from_db()
        self.assertEqual(list(report.geo_events.all()), [self.paris])

    def test_download_serves_the_document_as_a_file(self):
        self.generate({'scope': {}, 'title': 'Weekly picture'})
        report = Report.objects.get()
        response = self.client.get(f'/api/reportgis/reports/{report.pk}/download/')

        self.assertEqual(response.status_code, 200)
        self.assertIn('text/markdown', response['Content-Type'])
        self.assertIn('weekly-picture.md', response['Content-Disposition'])
        self.assertIn(b'# Weekly picture', response.content)

    def test_download_of_an_ungenerated_report_is_a_404(self):
        report = Report.objects.create(title='Never generated')
        response = self.client.get(f'/api/reportgis/reports/{report.pk}/download/')
        self.assertEqual(response.status_code, 404)

    def test_the_index_leaves_the_document_out(self):
        self.generate({'scope': {}})
        row = self.client.get('/api/reportgis/reports/').json()['results'][0]
        self.assertNotIn('document', row)
        self.assertEqual(row['gap_count'], len(Report.objects.get().gaps))

    def test_the_detail_view_carries_the_document(self):
        self.generate({'scope': {}})
        report = Report.objects.get()
        body = self.client.get(f'/api/reportgis/reports/{report.pk}/').json()
        self.assertIn('## Summary', body['document'])

    def test_the_generated_prose_cannot_be_edited_in_place(self):
        # A hand-edited summary over machine-counted findings is a report that
        # contradicts its own appendix.
        self.generate({'scope': {}})
        report = Report.objects.get()
        response = self.client.patch(
            f'/api/reportgis/reports/{report.pk}/',
            {'summary': 'Everything is fine.'}, format='json',
        )
        self.assertEqual(response.status_code, 200)
        report.refresh_from_db()
        self.assertNotEqual(report.summary, 'Everything is fine.')

    def test_the_author_is_whoever_generated_it(self):
        self.generate({'scope': {}})
        self.assertEqual(Report.objects.get().author.username, 'analyst')


class SummaryTests(TestCase):
    """The module's headline counts, which must not be a page of a list."""

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(get_user_model().objects.create_user('analyst'))

    def test_counts_everything_not_just_the_first_page(self):
        source = Source.objects.create(domain='many.test')
        for n in range(30):
            pin(f'Place {n}', 10 + n * 0.1, 10, 'France')

        listed = self.client.get('/api/reportgis/events/').json()
        summary = self.client.get('/api/reportgis/events/summary/').json()

        self.assertEqual(len(listed['results']), 25)  # one page
        self.assertEqual(summary['events'], 30)
        self.assertEqual(summary['countries'], 1)
        self.assertFalse(source.content_items.exists())

    def test_refusals_are_not_double_counted_when_an_article_is_re_read(self):
        # The log keeps every pass. Summing all of them would make the refusal
        # count climb every time the same article was read again.
        item = article('Officials in Springfield delayed the vote again.')
        locate_content_item(item)
        locate_content_item(item)

        self.assertEqual(ExtractionRun.objects.count(), 2)
        summary = self.client.get('/api/reportgis/events/summary/').json()
        self.assertEqual(summary['articles_read'], 1)
        self.assertEqual(summary['not_placed'], 1)

    def test_the_bands_add_up_to_the_total(self):
        pin('Lyon', 45.76, 4.84, 'France', confidence=GeoEvent.Confidence.HIGH)
        pin('Chad', 15.45, 18.73, 'Chad', confidence=GeoEvent.Confidence.LOW)
        summary = self.client.get('/api/reportgis/events/summary/').json()
        self.assertEqual(sum(summary['by_confidence'].values()), summary['events'])
