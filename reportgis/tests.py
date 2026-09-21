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
from .models import ExtractionRun, GeoEvent
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
