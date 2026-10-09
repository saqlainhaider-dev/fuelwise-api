"""HERE adapter: persistent cache and counters, bounded requests, no retries."""

import hashlib
import json
import time
from datetime import timedelta

import flexpolyline
import requests
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from planner.models import ProviderCache, ProviderUsage


class PlanningError(Exception):
    def __init__(self, code, message, status=422):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


class HereClient:
    def __init__(self, max_requests=5):
        self.max_requests, self.requests = max_requests, 0
        self.started = time.monotonic()
        self.session = requests.Session()
        self.routing_calls = 0
        self.request_counts = {"routing": 0, "geocode": 0, "discover": 0}

    def get(self, service, url, params, days=7):
        key = hashlib.sha256(
            json.dumps([service, url, params], sort_keys=True).encode()
        ).hexdigest()
        cached = ProviderCache.objects.filter(pk=key, expires_at__gt=timezone.now()).first()
        if cached:
            return cached.payload
        if not settings.HERE_API_KEY:
            raise PlanningError("provider_not_configured", "HERE_API_KEY is missing.", 503)
        if service != "discover" and time.monotonic() - self.started > 150:
            raise PlanningError(
                "planning_timeout",
                "Route planning exceeded its time budget. Completed road lookups are cached for reuse.",
                503,
            )
        if self.requests >= self.max_requests:
            raise PlanningError(
                "request_budget_exceeded", "This operation reached its provider request cap.", 429
            )
        if service == "routing" and self.request_counts["routing"] >= 3:
            raise PlanningError(
                "routing_call_limit", "The three-call routing limit was reached.", 422
            )
        # Reserve and commit before network I/O, so even a killed worker counts
        # the attempted call. A row lock prevents concurrent cap overshoot.
        with transaction.atomic():
            usage, _ = ProviderUsage.objects.get_or_create(service=service)
            usage = ProviderUsage.objects.select_for_update().get(pk=usage.pk)
            cached = ProviderCache.objects.filter(pk=key, expires_at__gt=timezone.now()).first()
            if cached:
                return cached.payload
            if usage.requests >= settings.HERE_LIMITS[service]:
                raise PlanningError(
                    "provider_budget_exceeded",
                    f"The local {service} request allowance is exhausted.",
                    429,
                )
            usage.requests += 1
            usage.save(update_fields=["requests"])
            self.requests += 1
            self.request_counts[service] += 1
        try:
            response = self.session.get(
                url,
                params={**params, "apiKey": settings.HERE_API_KEY},
                timeout=(5, 20),
                allow_redirects=False,
            )
            if response.status_code != 200:
                raise PlanningError(
                    "provider_error",
                    f"HERE {service} returned HTTP {response.status_code}. No retry was attempted.",
                    503,
                )
            result = response.json()
            if not isinstance(result, dict):
                raise ValueError("Expected provider JSON object")
        except (requests.RequestException, ValueError) as exc:
            raise PlanningError(
                "provider_unavailable",
                "HERE could not complete the request. No retry was attempted.",
                503,
            ) from exc
        ProviderCache.objects.update_or_create(
            pk=key,
            defaults={"payload": result, "expires_at": timezone.now() + timedelta(days=days)},
        )
        return result

    def discover(self, station):
        # Exit descriptions are not street addresses and overwhelm Discover's
        # text matching. Include a source address only when it starts with a
        # street number; still check returned branch/address evidence below.
        import re

        street = f"{station.address}, " if re.match(r"^\d+\s", station.address) else ""
        return self.get(
            "discover",
            "https://discover.search.hereapi.com/v1/discover",
            {
                "q": f"{station.name}, {street}{station.city}, {station.state}, USA",
                "at": "39,-98",
                "in": "countryCode:USA",
                "limit": 3,
            },
            days=30,
        ).get("items", [])

    def geocode(self, text):
        items = self.get(
            "geocode",
            "https://geocode.search.hereapi.com/v1/geocode",
            {"q": text, "in": "countryCode:USA", "limit": 2},
        ).get("items", [])
        if not items:
            raise PlanningError("location_not_found", f"No US location found for {text}.", 400)
        item = items[0]
        from planner.cleaning import US_STATES

        if (
            item.get("address", {}).get("countryCode") != "USA"
            or item.get("address", {}).get("stateCode") not in US_STATES
        ):
            raise PlanningError(
                "unsupported_location", "Use a location in the contiguous United States.", 400
            )
        if item.get("resultType") not in (
            "houseNumber",
            "street",
            "locality",
            "place",
            "intersection",
        ):
            raise PlanningError(
                "imprecise_location", "Provide a city and state or a full street address.", 400
            )
        if (
            len(items) > 1
            and items[0].get("scoring", {}).get("queryScore", 0)
            - items[1].get("scoring", {}).get("queryScore", 0)
            < 0.05
        ):
            raise PlanningError(
                "ambiguous_location",
                "The location is ambiguous; include its state or full address.",
                400,
            )
        position = (item.get("access") or [item["position"]])[0]
        return {
            "label": item["address"].get("label", text),
            "lat": position["lat"],
            "lng": position["lng"],
        }

    def route(self, origin, destination, via=()):
        if self.routing_calls >= 3:
            raise PlanningError(
                "routing_call_limit", "The three-call routing limit was reached.", 422
            )
        self.routing_calls += 1
        params = {
            "origin": f"{origin[1]:.6f},{origin[0]:.6f}",
            "destination": f"{destination[1]:.6f},{destination[0]:.6f}",
            "transportMode": "car",
            "routingMode": "fast",
            "departureTime": "any",
            "return": "polyline,summary",
            "avoid[countryCodes]": "CAN,MEX",
        }
        if via:
            params["via"] = [f"{p[1]:.6f},{p[0]:.6f}" for p in via]
        data = self.get(
            "routing",
            "https://router.hereapi.com/v8/routes",
            params,
        )
        if not data.get("routes"):
            return None
        sections = data["routes"][0].get("sections", [])
        if not sections or any(
            n.get("severity") == "critical" for s in sections for n in s.get("notices", [])
        ):
            return None
        coordinates, meters, seconds = [], 0, 0
        for section in sections:
            meters += section["summary"]["length"]
            seconds += section["summary"]["duration"]
            coordinates.extend([[p[1], p[0]] for p in flexpolyline.decode(section["polyline"])])
        return {
            "meters": meters,
            "seconds": seconds,
            "coordinates": coordinates,
            "leg_meters": [s["summary"]["length"] for s in sections],
        }
