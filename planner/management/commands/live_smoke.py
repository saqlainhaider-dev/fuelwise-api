"""Opt-in live smoke check. Never runs as part of the offline test suite."""

import json
import time
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.test import Client

from planner.models import ProviderUsage


class Command(BaseCommand):
    help = "Plan and replay one live trip, checking fuel accounting and provider-cache reuse."

    def add_arguments(self, parser):
        parser.add_argument("--start", default="Oklahoma City, OK")
        parser.add_argument("--finish", default="Albuquerque, NM")
        parser.add_argument("--coordinate-demo", action="store_true")

    def handle(self, *args, **options):
        client = Client()
        payload = {"start": options["start"], "finish": options["finish"]}
        if options["coordinate_demo"]:
            payload = {
                "start": {"lat": 35.4676, "lng": -97.5164},
                "finish": {"lat": 35.0844, "lng": -106.6504},
            }
        started = time.perf_counter()
        response = client.post("/api/trips/plan", data=payload, content_type="application/json")
        first_seconds = time.perf_counter() - started
        data = response.json()
        if response.status_code != 201:
            raise CommandError(json.dumps(data))
        before = dict(
            ProviderUsage.objects.filter(service__in=["routing", "geocode"]).values_list(
                "service", "requests"
            )
        )
        started = time.perf_counter()
        replay = client.post("/api/trips/plan", data=payload, content_type="application/json")
        replay_seconds = time.perf_counter() - started
        after = dict(
            ProviderUsage.objects.filter(service__in=["routing", "geocode"]).values_list(
                "service", "requests"
            )
        )
        if replay.status_code != 201 or before != after:
            raise CommandError("Replay failed or used unexpected provider requests.")
        for leg in data["legs"]:
            if (
                leg["distance_miles"] > 500
                or not 0 <= leg["arrival_fuel_gallons"] <= 50
                or leg["departure_fuel_gallons"] > 50
            ):
                raise CommandError("Physical fuel/range validation failed.")
        expected = 50 + data["total_gallons_purchased"] - data["distance_miles"] / 10
        if abs(expected - data["arrival_fuel_gallons"]) > 0.001:
            raise CommandError("Fuel conservation failed.")
        data["smoke_test"] = {
            "first_response_seconds": round(first_seconds, 3),
            "cached_response_seconds": round(replay_seconds, 3),
            "cached_replay_requests": 0,
            "physical_validation": "passed",
            "usage": after,
        }
        Path("reports/live-trip.json").write_text(json.dumps(data, indent=2))
        self.stdout.write(
            json.dumps(
                {
                    k: data[k]
                    for k in (
                        "id",
                        "map_url",
                        "distance_miles",
                        "total_fuel_cost",
                        "provider_requests",
                        "provider_request_counts",
                        "routing_calls",
                        "smoke_test",
                    )
                },
                indent=2,
            )
        )
