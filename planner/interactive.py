"""Interactive Leaflet map page rendered from a saved trip.

Tiles are fetched by the viewer's browser from OpenStreetMap; the API itself
makes no provider calls for this page, and Leaflet assets are served locally.
"""

import json
import mimetypes
from pathlib import Path

from django.http import Http404, HttpResponse

ASSET_DIR = Path(__file__).resolve().parent / "static" / "leaflet"
ALLOWED_ASSETS = {
    "leaflet.js",
    "leaflet.css",
    "trip-map.js",
    "images/marker-icon.png",
    "images/marker-icon-2x.png",
    "images/marker-shadow.png",
}


def leaflet_asset(request, name):
    if name not in ALLOWED_ASSETS:
        raise Http404("Unknown asset.")
    path = ASSET_DIR / name
    if not path.is_file():
        raise Http404("Unknown asset.")
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    response = HttpResponse(path.read_bytes(), content_type=content_type)
    response["Cache-Control"] = "public, max-age=604800"
    return response


def render_interactive_map(trip):
    """Return a self-contained HTML page (no template engine needed)."""
    payload = json.dumps(trip).replace("<", "\\u003c").replace("\u2028", "\\u2028").replace(
        "\u2029", "\\u2029"
    )
    return INTERACTIVE_PAGE.replace("__TRIP_JSON__", payload)


INTERACTIVE_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Fuelwise · Route and fuel stops</title>
<link rel="stylesheet" href="/static/leaflet/leaflet.css">
<style>
  html, body { margin: 0; height: 100%; font-family: sans-serif; }
  #map { position: absolute; inset: 0; }
  .panel {
    position: absolute; z-index: 1000; top: 12px; left: 52px;
    background: rgba(255,255,255,.95); border-radius: 8px;
    padding: 10px 14px; box-shadow: 0 1px 6px rgba(0,0,0,.3);
    max-width: 340px; font-size: 13px; color: #1c2b21;
  }
  .panel h1 { font-size: 16px; margin: 0 0 4px; color: #174632; }
  .panel .sub { color: #455a4b; }
  .legend { margin-top: 6px; }
  .legend span { display: inline-block; margin-right: 12px; white-space: nowrap; }
  .dot { display: inline-block; width: 11px; height: 11px; border-radius: 50%;
         margin-right: 4px; vertical-align: -1px; }
  .stop-label { font-weight: 700; }
  .leaflet-popup-content { font-size: 13px; line-height: 1.45; }
  .leaflet-popup-content b { color: #174632; }
</style>
</head>
<body>
<div id="map"></div>
<div class="panel">
  <h1>Fuelwise · Route and fuel stops</h1>
  <div class="sub" id="summary"></div>
  <div class="legend">
    <span><i class="dot" style="background:#2f8f4e"></i>Start</span>
    <span><i class="dot" style="background:#b85b22"></i>Fuel stop</span>
    <span><i class="dot" style="background:#c0392b"></i>Finish</span>
    <span><i class="dot" style="background:#176341"></i>Route leg</span>
  </div>
</div>
<script src="/static/leaflet/leaflet.js"></script>
<script type="application/json" id="trip-data">__TRIP_JSON__</script>
<script src="/static/leaflet/trip-map.js"></script>
</html>
"""
