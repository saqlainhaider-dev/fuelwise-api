from decimal import Decimal
from unittest.mock import patch

import flexpolyline
from django.contrib.gis.geos import Point
from django.test import TestCase, override_settings

from planner.models import Station
from planner.optimizer import fixed_itinerary_purchases
from planner.provider import HereClient, PlanningError
from planner.service import plan

START, FINISH = {"lat": 35.4676, "lng": -97.5164}, {"lat": 35.0844, "lng": -106.6504}


def response(legs):
    return {
        "routes": [
            {
                "sections": [
                    {
                        "summary": {"length": m * 1609.344, "duration": 100},
                        "polyline": flexpolyline.encode([(35, -97), (35, -106)]),
                    }
                    for m in legs
                ]
            }
        ]
    }


@override_settings(HERE_API_KEY="fake", HERE_LIMITS={"routing": 100, "geocode": 20, "discover": 20})
class CallBudgetTests(TestCase):
    def setUp(self):
        self.station = Station.objects.create(
            opis_id=1,
            name="Fuel",
            address="1 Road",
            city="Town",
            state="TX",
            price=Decimal("3"),
            source_hash="a",
            status="accepted",
            location=Point(-100, 35, srid=4326),
            access_location=Point(-100, 35, srid=4326),
        )

    @patch("planner.provider.requests.Session.get")
    def test_uncached_short_trip_one_call_and_cached_replay_zero(self, get):
        get.return_value.status_code = 200
        get.return_value.json.return_value = response([100])
        first = plan(START, FINISH)
        self.assertEqual(
            first["provider_request_counts"], {"routing": 1, "geocode": 0, "discover": 0}
        )
        self.assertEqual(plan(START, FINISH)["provider_requests"], 0)
        self.assertEqual(get.call_count, 1)

    @patch("planner.service.select_stops")
    @patch("planner.service.shortlist")
    @patch("planner.provider.requests.Session.get")
    def test_uncached_fuel_trip_two_calls_and_actual_distance_cost(self, get, shortlist, select):
        shortlist.return_value = [self.station]
        select.return_value = [self.station]
        get.return_value.status_code = 200
        get.return_value.json.side_effect = [response([600]), response([400, 200])]
        result = plan(START, FINISH)
        self.assertEqual(get.call_count, 2)
        self.assertEqual(result["total_fuel_cost"], "30.00")
        self.assertEqual(result["provider_request_counts"]["geocode"], 0)
        self.assertEqual(get.call_args.kwargs["params"]["via"], ["35.000000,-100.000000"])

    def correction(self, get, shortlist, select, succeeds):
        alternate = Station(
            opis_id=2,
            name="Other",
            address="Road",
            city="Town",
            state="TX",
            price=Decimal(2),
            location=Point(-101, 35),
            access_location=Point(-101, 35),
        )
        shortlist.return_value = [self.station, alternate]
        select.side_effect = [[self.station], [alternate]]
        get.return_value.status_code = 200
        get.return_value.json.side_effect = [
            response([600]),
            response([510, 90]),
            response([300, 300] if succeeds else [510, 90]),
        ]
        if succeeds:
            result = plan(START, FINISH)
            self.assertEqual(result["provider_requests"], 3)
            self.assertEqual(result["total_fuel_cost"], "20.00")
        else:
            with self.assertRaises(PlanningError) as error:
                plan(START, FINISH)
            self.assertEqual(error.exception.code, "route_validation_failed")
        self.assertEqual(get.call_count, 3)

    @patch("planner.service.select_stops")
    @patch("planner.service.shortlist")
    @patch("planner.provider.requests.Session.get")
    def test_correction_uses_at_most_three_calls(self, *args):
        self.correction(*args, True)

    @patch("planner.service.select_stops")
    @patch("planner.service.shortlist")
    @patch("planner.provider.requests.Session.get")
    def test_failed_correction_never_tries_fourth_call(self, *args):
        self.correction(*args, False)

    @patch("planner.provider.requests.Session.get")
    def test_adapter_hard_limit_on_uncached_calls(self, get):
        get.return_value.status_code = 200
        get.return_value.json.return_value = response([10])
        client = HereClient()
        for i in range(3):
            client.route((-97, 35), (-100 - i, 35))
        with self.assertRaises(PlanningError):
            client.route((-97, 35), (-104, 35))
        self.assertEqual(get.call_count, 3)

    @patch("planner.provider.requests.Session.get")
    def test_invalid_coordinates_spend_no_quota(self, get):
        response = self.client.post(
            "/api/trips/plan",
            {"start": {"lat": True, "lng": -100}, "finish": FINISH},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        with self.assertRaises(PlanningError):
            plan({"lat": 51.5, "lng": -0.12}, FINISH)
        get.assert_not_called()

    def test_exact_fixed_path_fuel_and_capacity(self):
        legs = [400.2 * 1609.344, 400.2 * 1609.344]
        result = fixed_itinerary_purchases([None, Decimal(3), None], legs)
        self.assertAlmostEqual(float(result[1]["bought"]), 30.04)
        self.assertAlmostEqual(float(result[-1]["remaining"]), 0)
        self.assertTrue(all(0 <= r["departure"] <= 50 and r["remaining"] >= 0 for r in result))
