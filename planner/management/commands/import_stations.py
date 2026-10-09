import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from planner.cleaning import clean_csv
from planner.models import Station


class Command(BaseCommand):
    help = "Validate/import CSV metadata without making provider calls."

    def add_arguments(self, parser):
        parser.add_argument("csv_path", nargs="?", default="fuel-prices-for-be-assessment.csv")

    def handle(self, *args, **options):
        try:
            rows, report = clean_csv(Path(options["csv_path"]))
        except (OSError, ValueError) as exc:
            raise CommandError(str(exc)) from exc
        with transaction.atomic():
            for values in rows:
                station, created = Station.objects.get_or_create(
                    opis_id=values["opis_id"], defaults=values
                )
                if not created and station.source_hash != values["source_hash"]:
                    for key, value in values.items():
                        setattr(station, key, value)
                    station.location = station.access_location = None
                    station.here_id = ""
                    station.match_evidence = {}
                    station.status = "pending"
                if station.issues:
                    station.status = "conflict"
                station.save()
            # Stations removed from the latest dataset must not remain routable.
            Station.objects.exclude(opis_id__in=[s["opis_id"] for s in rows]).update(
                status="retired"
            )
        Path("reports").mkdir(exist_ok=True)
        Path("reports/import.json").write_text(json.dumps(report, indent=2))
        self.stdout.write(json.dumps(report["counts"], indent=2))
