import json
import re
import time
from pathlib import Path

from django.contrib.gis.geos import Point
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from planner.matching import assess
from planner.models import ProviderUsage, Station
from planner.provider import HereClient, PlanningError


class Command(BaseCommand):
    help = "Collect screened stations, with an explicit paid-service request cap."

    def add_arguments(self, parser):
        parser.add_argument("--target", type=int, default=10000)
        parser.add_argument("--max-requests", type=int, default=12000)

    def handle(self, *args, **options):
        target, cap = options["target"], options["max_requests"]
        if not 1 <= target <= 50000 or not 1 <= cap <= 50000:
            raise CommandError("Target must be 1..50000 and request cap 1..50000.")
        client = HereClient(max_requests=cap)
        # Prefer I-40 / I-10 corridors, round-robin by state to avoid consuming
        # the sample on one dense metropolitan area.
        order = "NC TN AR OK TX NM AZ CA".split()
        groups = {s: [] for s in order}
        others = []
        for station in Station.objects.filter(status="pending").order_by("opis_id"):
            if station.state in groups and re.search(r"I[ -]?(?:40|10)\b", station.address, re.I):
                groups[station.state].append(station)
            else:
                others.append(station)
        candidates = []
        while any(groups.values()):
            for state in order:
                if groups[state]:
                    candidates.append(groups[state].pop(0))
        candidates.extend(others)
        accepted = Station.objects.filter(status="accepted").count()
        stopped = None
        for station in candidates:
            if accepted >= target or client.requests >= cap:
                break
            try:
                items = client.discover(station)
            except PlanningError as exc:
                stopped = exc.message
                break
            ranked = sorted(
                [(assess(station, item), item) for item in items],
                key=lambda x: x[0][0],
                reverse=True,
            )
            evidence = [
                {"score": round(score, 4), "reason": reason, "candidate": item}
                for (score, reason), item in ranked
            ]
            station.status = "review"
            if (
                ranked
                and ranked[0][0][0] >= 0.8
                and (len(ranked) < 2 or ranked[0][0][0] - ranked[1][0][0] >= 0.1)
            ):
                item = ranked[0][1]
                pos = item["position"]
                access = (item.get("access") or [pos])[0]
                if 24 <= pos["lat"] <= 50 and -125 <= pos["lng"] <= -66:
                    station.location = Point(pos["lng"], pos["lat"], srid=4326)
                    station.access_location = Point(access["lng"], access["lat"], srid=4326)
                    station.here_id = item["id"]
                    # One provider POI cannot stand in for two CSV stations.
                    if not Station.objects.filter(status="accepted", here_id=item["id"]).exists():
                        station.status = "accepted"
                        accepted += 1
            station.match_evidence = {
                "method": "automated_identity_screen_v1",
                "independently_verified": False,
                "candidates": evidence,
            }
            station.geocoded_at = timezone.now()
            station.save()
            if client.requests % 100 == 0:
                self.stdout.write(
                    f"Calls: {client.requests}; accepted: {accepted}/{target}", ending="\n"
                )
            time.sleep(0.22)
        report = {
            "accepted": accepted,
            "target": target,
            "new_requests": client.requests,
            "review": Station.objects.filter(status="review").count(),
            "stopped": stopped,
            "usage": dict(ProviderUsage.objects.values_list("service", "requests")),
            "validation": "Automated name/locality/category screening; no independent entrance-coordinate ground truth.",
        }
        Path("reports/geocoding.json").write_text(json.dumps(report, indent=2))
        self.stdout.write(json.dumps(report, indent=2))
