from pathlib import Path

from django.core import serializers
from django.core.management.base import BaseCommand

from planner.models import Station


class Command(BaseCommand):
    help = "Export accepted stations as a portable Django fixture without credentials or cached routes."

    def handle(self, *args, **options):
        Path("data").mkdir(exist_ok=True)
        stations = Station.objects.filter(status="accepted").order_by("opis_id")
        with Path("data/stations.json").open("w") as output:
            serializers.serialize("json", stations, stream=output, indent=2)
        self.stdout.write(f"Exported {stations.count()} accepted stations to data/stations.json")
