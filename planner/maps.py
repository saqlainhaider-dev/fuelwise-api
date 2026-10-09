"""Static SVG generated from saved route geometry and local Census boundaries."""

import json
from functools import lru_cache
from html import escape

from django.contrib.gis.geos import LineString

from planner.geography import states


@lru_cache(maxsize=1)
def background():
    result = []
    for name, boundary in states():
        geometry = boundary.simplify(0.02, preserve_topology=True)
        geometry.transform(5070)
        shape = json.loads(geometry.json)
        polygons = (
            shape["coordinates"] if shape["type"] == "MultiPolygon" else [shape["coordinates"]]
        )
        result.append((name, geometry.extent, polygons))
    return result


def render_map(trip):
    route = LineString(trip["route"]["geometry"]["coordinates"], srid=4326).transform(
        5070, clone=True
    )
    x0, y0, x1, y1 = route.extent
    pad = max(30000, max(x1 - x0, y1 - y0) * 0.15)
    x0, y0, x1, y1 = x0 - pad, y0 - pad, x1 + pad, y1 + pad
    scale = min(940 / (x1 - x0), 460 / (y1 - y0))
    ox, oy = (1000 - (x1 - x0) * scale) / 2, 110 + (460 - (y1 - y0) * scale) / 2

    def point(p):
        return (ox + (p[0] - x0) * scale, oy + (y1 - p[1]) * scale)

    def path(points):
        return "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in map(point, points))

    svg = [
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1000 640" role="img" aria-label="Route and fuel stops" font-family="sans-serif">',
        '<rect width="1000" height="640" fill="#f2f5f0"/>',
        '<text x="30" y="40" font-size="25" fill="#174632">Fuelwise · Route and fuel stops</text>',
        f'<text x="30" y="73" font-size="17">{trip["distance_miles"]:,.1f} miles · ${escape(trip["total_fuel_cost"])} fuel purchased · {len(trip["stops"])} stop(s)</text>',
        '<defs><clipPath id="map"><rect x="20" y="100" width="960" height="480" rx="8"/></clipPath></defs><g clip-path="url(#map)">',
    ]
    for name, extent, polygons in background():
        if extent[2] < x0 or extent[0] > x1 or extent[3] < y0 or extent[1] > y1:
            continue
        for polygon in polygons:
            svg.append(
                f'<path d="{" ".join(path(ring) + " Z" for ring in polygon)}" fill="#e0e8dc" stroke="#b2c2ac" stroke-width="1" fill-rule="evenodd"><title>{name}</title></path>'
            )
        x, y = point(((extent[0] + extent[2]) / 2, (extent[1] + extent[3]) / 2))
        svg.append(
            f'<text x="{x:.1f}" y="{y:.1f}" fill="#70816c" font-size="15" text-anchor="middle">{name}</text>'
        )
    svg.append(f'<path d="{path(route.coords)}" fill="none" stroke="#176341" stroke-width="4"/>')
    from django.contrib.gis.geos import Point

    markers = [("S", trip["start"]), ("F", trip["finish"])] + [
        (str(i + 1), s["access_location"]) for i, s in enumerate(trip["stops"])
    ]
    for label, location in markers:
        p = Point(location["lng"], location["lat"], srid=4326).transform(5070, clone=True)
        x, y = point(p.coords)
        description = label if not label.isdigit() else trip["stops"][int(label) - 1]["name"]
        svg.append(
            f'<g><title>{escape(description)}</title><circle cx="{x:.1f}" cy="{y:.1f}" r="12" fill="#b85b22"/><text x="{x:.1f}" y="{y + 4:.1f}" text-anchor="middle" fill="white" font-size="12">{label}</text></g>'
        )
    svg.extend(
        [
            "</g>",
            f'<text x="30" y="603" font-size="13">{escape(trip["start"]["label"][:65])} → {escape(trip["finish"]["label"][:65])}</text>',
            '<text x="30" y="625" font-size="11" fill="#52644e">S: start · F: finish · Numbers: selected stops | Route: HERE | Boundaries: U.S. Census Bureau 2024</text>',
            "</svg>",
        ]
    )
    return "".join(svg)
