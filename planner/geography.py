"""Local Census polygons: coordinate validation and map background."""

import json
from functools import lru_cache

from django.conf import settings
from django.contrib.gis.geos import GEOSGeometry, Point

from planner.provider import PlanningError


@lru_cache(maxsize=1)
def states():
    data = json.loads((settings.BASE_DIR / "data/us_states.geojson").read_text())
    return [
        (f["properties"]["state"], GEOSGeometry(json.dumps(f["geometry"]), srid=4326))
        for f in data["features"]
    ]


def resolve_location(value, client):
    if isinstance(value, str):
        return client.geocode(value)
    point = Point(value["lng"], value["lat"], srid=4326)
    for state, boundary in states():
        if boundary.covers(point):
            return {
                "lat": value["lat"],
                "lng": value["lng"],
                "label": f"{value['lat']:.5f}, {value['lng']:.5f} ({state})",
            }
    raise PlanningError(
        "unsupported_location",
        "Coordinates must be within the supported contiguous US boundary. For coastal or boundary addresses, use a full address.",
        400,
    )
