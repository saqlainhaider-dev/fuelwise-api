import csv
import heapq
import random
import tempfile
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock, patch

from django.contrib.gis.geos import Point
from django.test import SimpleTestCase, TestCase, override_settings

from planner.cleaning import REQUIRED, clean_csv
from planner.matching import assess
from planner.models import ProviderCache, ProviderUsage, Station
from planner.optimizer import fuel_units, solve
from planner.provider import HereClient, PlanningError
from planner.service import MAX_VALIDATED_STOPS, plan, select_stops, shortlist


class OptimizerTests(SimpleTestCase):
    def test_full_tank_free_trip(self):
        steps, cost = solve([None, None], {(0, 1): {"meters": 500 * 1609.344}})
        self.assertEqual(cost, 0)
        self.assertEqual(steps[0]["departure_units"], 500)

    def test_just_over_range_is_infeasible(self):
        with self.assertRaises(PlanningError):
            solve([None, None], {(0, 1): {"meters": 500 * 1609.344 + 1}})

    def test_cheaper_station_ahead(self):
        edges = {
            (0, 1): {"meters": 400 * 1609.344},
            (1, 2): {"meters": 200 * 1609.344},
            (2, 3): {"meters": 300 * 1609.344},
        }
        steps, cost = solve([None, Decimal(5), Decimal(2), None], edges)
        self.assertEqual(cost, Decimal(110))  # 10 gallons at $5 + 30 at $2.

    def test_rounds_consumption_up(self):
        self.assertEqual(fuel_units(1609.345), 2)

    def test_multiple_purchases_required(self):
        edges = {
            (0, 1): {"meters": 643737.6},
            (1, 2): {"meters": 643737.6},
            (2, 3): {"meters": 643737.6},
        }
        steps, cost = solve([None, Decimal(5), Decimal(2), None], edges)
        self.assertEqual(cost, Decimal(230))
        self.assertEqual(len(steps), 3)

    def test_cheaper_detour_beats_shorter_route(self):
        edges = {
            (0, 1): {"meters": 643737.6},
            (1, 3): {"meters": 643737.6},
            (0, 2): {"meters": 724204.8},
            (2, 3): {"meters": 643737.6},
        }
        steps, cost = solve([None, Decimal(5), Decimal(2), None], edges)
        self.assertEqual(cost, Decimal(70))
        self.assertEqual(steps[0]["end"], 2)

    def test_random_graphs_against_independent_dijkstra(self):
        rng = random.Random(91)
        for _ in range(20):
            prices = [None, Decimal(rng.randrange(2, 8)), Decimal(rng.randrange(2, 8)), None]
            edges = {
                (i, j): {"meters": rng.randrange(100, 500) * 1609.344}
                for i in range(3)
                for j in range(i + 1, 4)
                if (i, j) != (0, 3)
            }
            queue, seen = [(Decimal(0), 0, 500)], {}
            expected = None
            while queue:
                cost, node, fuel = heapq.heappop(queue)
                if (node, fuel) in seen:
                    continue
                seen[node, fuel] = cost
                if node == 3:
                    expected = cost
                    break
                if node and fuel < 500:
                    heapq.heappush(queue, (cost + prices[node] / 10, node, fuel + 1))
                for (i, j), edge in edges.items():
                    used = fuel_units(edge["meters"])
                    if i == node and fuel >= used:
                        heapq.heappush(queue, (cost, j, fuel - used))
            self.assertEqual(solve(prices, edges)[1], expected)


class CleaningTests(SimpleTestCase):
    def test_duplicates_conflicts_and_invalid_data(self):
        row = {
            "OPIS Truckstop ID": "7",
            "Truckstop Name": " Station  One ",
            "Address": "123 Main St",
            "City": "Town",
            "State": "TX",
            "Rack ID": "1",
            "Retail Price": "3.50000000",
        }
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "input.csv"
            with path.open("w") as f:
                writer = csv.DictWriter(f, fieldnames=sorted(REQUIRED))
                writer.writeheader()
                writer.writerows(
                    [
                        row,
                        row,
                        {**row, "Retail Price": "4"},
                        {**row, "OPIS Truckstop ID": "8", "State": "ON"},
                        {**row, "OPIS Truckstop ID": "9", "Retail Price": "NaN"},
                    ]
                )
            stations, report = clean_csv(path)
        self.assertEqual(len(stations), 1)
        self.assertEqual(stations[0]["name"], "Station One")
        self.assertEqual(stations[0]["issues"], ["conflicting_prices"])
        self.assertEqual(report["counts"]["exact_duplicates"], 1)
        self.assertEqual(len(report["rejected_rows"]), 2)


class MatchingTests(SimpleTestCase):
    def test_reject_wrong_branch_city_and_category(self):
        station = Mock(name="unused")
        station.name, station.city, station.state, station.address = (
            "Pilot #123",
            "Town",
            "TX",
            "I-40 EXIT 1",
        )
        item = {
            "title": "Pilot #123",
            "resultType": "place",
            "address": {"countryCode": "USA", "stateCode": "TX", "city": "Town"},
            "categories": [{"id": "700-7600-0116"}],
        }
        self.assertGreaterEqual(assess(station, item)[0], 0.8)
        self.assertEqual(assess(station, {**item, "title": "Pilot #999"})[0], 0)
        self.assertEqual(assess(station, {**item, "categories": []})[0], 0)

    def test_official_site_branch_conflict(self):
        station = Mock()
        station.name, station.city, station.state, station.address = (
            "LOVES #203",
            "Town",
            "OK",
            "I-40 EXIT 140",
        )
        item = {
            "title": "Love's",
            "resultType": "place",
            "address": {"city": "Town", "stateCode": "OK", "countryCode": "USA"},
            "categories": [{"id": "700-7600-0116"}],
            "contacts": [{"www": [{"value": "https://www.loves.com/locations/211"}]}],
        }
        self.assertEqual(assess(station, item), (0, "official_url_branch_conflict"))


@override_settings(HERE_API_KEY="test-key", HERE_LIMITS={"geocode": 2, "routing": 2, "discover": 2})
class ProviderTests(TestCase):
    @patch("planner.provider.requests.Session.get")
    def test_cache_and_budget(self, get):
        get.return_value = Mock(status_code=200)
        get.return_value.json.return_value = {"items": []}
        client = HereClient()
        for _ in range(2):
            client.get("geocode", "https://test.invalid", {"q": "a"})
        self.assertEqual(get.call_count, 1)
        self.assertEqual(ProviderUsage.objects.get(service="geocode").requests, 1)
        client.get("geocode", "https://test.invalid", {"q": "b"})
        with self.assertRaises(PlanningError):
            client.get("geocode", "https://test.invalid", {"q": "c"})
        self.assertEqual(get.call_count, 2)

    @patch("planner.provider.requests.Session.get")
    def test_http_error_consumes_allowance_without_retry(self, get):
        get.return_value = Mock(status_code=429)
        with self.assertRaises(PlanningError):
            HereClient().get("routing", "https://test.invalid", {})
        self.assertEqual(get.call_count, 1)
        self.assertEqual(ProviderUsage.objects.get(service="routing").requests, 1)
        self.assertFalse(ProviderCache.objects.exists())

    @patch("planner.provider.requests.Session.get")
    def test_timeout_is_sanitized(self, get):
        import requests

        get.side_effect = requests.Timeout("secret URL apiKey=do-not-leak")
        with self.assertRaises(PlanningError) as error:
            HereClient().get("routing", "https://test.invalid", {})
        self.assertNotIn("do-not-leak", str(error.exception))
        self.assertEqual(ProviderUsage.objects.get(service="routing").requests, 1)


class ApiTests(TestCase):
    def test_validation(self):
        response = self.client.post(
            "/api/trips/plan",
            data={"start": "", "finish": "Texas"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)

    @patch("planner.views.plan")
    def test_error_shape(self, plan_mock):
        plan_mock.side_effect = PlanningError(
            "insufficient_station_coverage", "Not enough stations"
        )
        response = self.client.post(
            "/api/trips/plan", data={"start": "A", "finish": "B"}, content_type="application/json"
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["code"], "insufficient_station_coverage")

    @patch("planner.views.plan")
    def test_trip_persistence_and_map(self, plan_mock):
        plan_mock.return_value = {
            "total_fuel_cost": "0.00",
            "stops": [],
            "distance_miles": 100,
            "start": {"lat": 35, "lng": -100, "label": "A"},
            "finish": {"lat": 35, "lng": -99, "label": "B"},
            "route": {"geometry": {"coordinates": [[-100, 35], [-99, 35]]}},
        }
        response = self.client.post(
            "/api/trips/plan", data={"start": "A", "finish": "B"}, content_type="application/json"
        )
        self.assertEqual(response.status_code, 201)
        trip = response.json()
        self.assertEqual(self.client.get("/api/trips/" + trip["id"]).json(), trip)
        with patch("planner.provider.requests.Session.get") as get:
            rendered = self.client.get(trip["map_url"])
            self.assertEqual(rendered.status_code, 200)
            self.assertEqual(rendered["Content-Type"], "image/svg+xml")
            self.assertNotIn(b"<script", rendered.content)
            get.assert_not_called()

    def test_removed_tile_and_asset_endpoints(self):
        self.assertEqual(self.client.get("/api/tiles/5/0/0.png").status_code, 404)
        self.assertEqual(self.client.get("/assets/leaflet.js").status_code, 404)
        self.assertFalse(ProviderUsage.objects.exists())

    @patch("planner.views.plan")
    def test_interactive_map_page(self, plan_mock):
        plan_mock.return_value = {
            "total_fuel_cost": "0.00",
            "stops": [],
            "distance_miles": 100,
            "start": {"lat": 35, "lng": -100, "label": "A"},
            "finish": {"lat": 35, "lng": -99, "label": "B"},
            "route": {"geometry": {"coordinates": [[-100, 35], [-99, 35]]}},
        }
        trip = self.client.post(
            "/api/trips/plan", data={"start": "A", "finish": "B"}, content_type="application/json"
        ).json()
        self.assertEqual(trip["view_url"], f"/trips/{trip['id']}/view")
        with patch("planner.provider.requests.Session.get") as get:
            page = self.client.get(trip["view_url"])
            get.assert_not_called()
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page["Content-Type"], "text/html")
        body = page.content.decode()
        self.assertIn("leaflet.js", body)
        self.assertIn("trip-map.js", body)
        self.assertIn('type="application/json" id="trip-data"', body)
        self.assertIn('"lat": 35', body)
        # Tiles are loaded client-side from the CSP img-src allowlist, with
        # CARTO first and OSM as the on-error fallback.
        csp = page["Content-Security-Policy"]
        self.assertIn("basemaps.cartocdn.com", csp)
        self.assertIn("tile.openstreetmap.org", csp)
        # Cross-origin tile hosts key on Referer; Django's default same-origin
        # policy strips it and OSM answers 403, so the page must relax it.
        self.assertEqual(page["Referrer-Policy"], "strict-origin-when-cross-origin")
        # No inline <script> bodies, so a strict script-src 'self' is enforceable.
        self.assertNotIn("<script>", body)
        self.assertIn("script-src 'self'", page["Content-Security-Policy"])
        for asset in ("leaflet.js", "leaflet.css", "trip-map.js"):
            served = self.client.get(f"/static/leaflet/{asset}")
            self.assertEqual(served.status_code, 200)
            self.assertTrue(served.content)
        self.assertEqual(self.client.get("/static/leaflet/nope.js").status_code, 404)
        self.assertFalse(ProviderUsage.objects.exists())


class ServiceTests(TestCase):
    def station(self, **extra):
        fields = {
            "opis_id": 1,
            "name": "A",
            "address": "Road",
            "city": "Town",
            "state": "TX",
            "price": Decimal("3"),
            "source_hash": "a",
            "status": "accepted",
            "location": Point(-100, 35, srid=4326),
            "access_location": Point(-100, 35, srid=4326),
        }
        fields.update(extra)
        return Station.objects.create(**fields)

    def test_postgis_corridor(self):
        self.station()
        route = {"coordinates": [[-101, 35], [-99, 35]], "meters": 200000}
        self.assertEqual(len(shortlist(route)), 1)
        Station.objects.update(status="review")
        self.assertEqual(shortlist(route), [])

    def test_shortlist_keeps_cheapest_per_25_mile_bin(self):
        route = {"coordinates": [[-101, 35], [-99, 35]], "meters": 293500}
        expensive = self.station(opis_id=1, price=Decimal("3.50"), location=Point(-100.9, 35.0))
        cheap = self.station(
            opis_id=2, price=Decimal("3.00"), location=Point(-100.85, 35.01)
        )
        next_bin = self.station(
            opis_id=3, price=Decimal("3.20"), location=Point(-100.5, 35.0)
        )
        for station in (expensive, cheap, next_bin):
            station.access_location = station.location
            station.save()
        kept = shortlist(route)
        self.assertEqual([s.opis_id for s in kept], [2, 3])
        self.assertTrue(all(s.progress_meters > 0 for s in kept))

    def test_stop_selection_limits_stop_frequency(self):
        from types import SimpleNamespace

        # Price dips every 60 miles over 2,000 miles: an unconstrained optimum
        # would stop at each dip; selection must prefer a sparser itinerary.
        baseline = {"meters": 2000 * 1609.344}
        candidates = [
            SimpleNamespace(
                progress_meters=(i * 60 + 30) * 1609.344,
                offset_meters=0,
                price=Decimal("2.99") if i % 2 else Decimal("4.49"),
                opis_id=i,
            )
            for i in range(33)
        ]
        stops = select_stops(baseline, candidates)
        self.assertGreater(len(stops), 0)
        self.assertLessEqual(len(stops), MAX_VALIDATED_STOPS)

    @patch("planner.service.shortlist")
    def test_physical_fuel_and_cost_reconciliation(self, shortlist_mock):
        station = self.station()
        station.progress_meters, station.offset_meters = 400 * 1609.344, 0
        shortlist_mock.return_value = [station]
        client = Mock(requests=2, routing_calls=2, request_counts={"routing": 2, "geocode": 0})
        client.geocode.side_effect = [
            {"lat": 35, "lng": -104, "label": "A"},
            {"lat": 35, "lng": -96, "label": "B"},
        ]
        client.route.side_effect = [
            {
                "meters": 800 * 1609.344,
                "seconds": 100,
                "coordinates": [[-104, 35], [-96, 35]],
                "leg_meters": [800 * 1609.344],
            },
            {
                "meters": 800.4 * 1609.344,
                "seconds": 160,
                "coordinates": [[-104, 35], [-100, 35], [-96, 35]],
                "leg_meters": [400.2 * 1609.344, 400.2 * 1609.344],
            },
        ]
        result = plan("A", "B", client)
        self.assertAlmostEqual(
            50 + result["total_gallons_purchased"] - result["distance_miles"] / 10,
            result["arrival_fuel_gallons"],
            places=5,
        )
        self.assertTrue(
            all(
                0 <= leg["arrival_fuel_gallons"] <= 50 and leg["departure_fuel_gallons"] <= 50
                for leg in result["legs"]
            )
        )
        self.assertEqual(result["total_fuel_cost"], result["stops"][0]["cost"])
