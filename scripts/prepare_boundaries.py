"""One-time download of public-domain Census boundaries; no runtime map calls."""

import json
import tempfile
import zipfile
from pathlib import Path

import requests
from django.contrib.gis.gdal import DataSource

URL = "https://www2.census.gov/geo/tiger/GENZ2024/shp/cb_2024_us_state_500k.zip"
STATES = set(
    "AL AZ AR CA CO CT DE FL GA ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC".split()
)
with tempfile.TemporaryDirectory() as directory:
    archive = Path(directory) / "states.zip"
    response = requests.get(URL, timeout=60)
    response.raise_for_status()
    archive.write_bytes(response.content)
    with zipfile.ZipFile(archive) as zipped:
        zipped.extractall(
            directory, members=[n for n in zipped.namelist() if "/" not in n and "\\" not in n]
        )
    features = []
    for feature in DataSource(str(next(Path(directory).glob("*.shp"))))[0]:
        if feature["STUSPS"].value not in STATES:
            continue
        geometry = feature.geom
        geometry.transform(4326)
        features.append(
            {
                "type": "Feature",
                "properties": {"state": feature["STUSPS"].value},
                "geometry": json.loads(geometry.json),
            }
        )
    Path("data").mkdir(exist_ok=True)
    Path("data/us_states.geojson").write_text(
        json.dumps(
            {"type": "FeatureCollection", "source": URL, "features": features},
            separators=(",", ":"),
        )
    )
    print(f"Saved {len(features)} state boundaries.")
