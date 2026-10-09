# Fuelwise API

Django REST Framework + PostgreSQL/PostGIS API for US fuel planning. Starts with 50 gallons, 500-mile range, 10 MPG; only fuel purchased en route contributes to cost.

## Run locally

Docker with Compose is required. For a fresh installation copy `.env.example` to `.env` and supply the HERE key.

```sh
docker compose up -d --build db
docker compose build web
docker compose run --rm web python manage.py migrate
docker compose run --rm web python manage.py import_stations
docker compose run --rm web python manage.py loaddata data/stations.json
docker compose up -d web
```

For normal restarts just run `docker compose up -d`. Database data survives restarts in the Compose volume. Do not delete that volume unless you intend to remove data and usage counters. Import/geocoding never runs automatically on startup.

Open http://localhost:8000 for endpoint information. The API is JSON-only, but every saved trip also has a per-trip interactive map page. No interactive map, tiles or Leaflet assets are served on planning requests, and the API itself never fetches map tiles.

## Plan a trip

Import `postman/Fuelwise.postman_collection.json` into Postman and run the **01 Demo** folder in order. The collection defaults to `http://localhost:8000`, needs no separate environment or API key, and saves trip/map variables automatically. Optional examples can spend additional provider allowance; automated response checks validate call limits and fuel accounting.

Coordinate inputs are recommended: at most **three total external calls**, all for routing. The local Census polygons validate that coordinates are in the contiguous US.

```sh
curl -X POST http://localhost:8000/api/trips/plan \
  -H 'Content-Type: application/json' \
  -d '{"start":{"lat":35.4676,"lng":-97.5164},"finish":{"lat":35.0844,"lng":-106.6504}}'
```

A start or finish can alternatively be a string such as `"Oklahoma City, OK"`. Each uncached address adds one geocoding request: **address inputs can use up to five external calls**, including up to three routing calls. This distinction is explicit in `provider_request_counts`.

The HTTP 201 response includes:

- `id`, `map_url`, `view_url`: saved trip, its static SVG map and its interactive map page.
- `route`: GeoJSON Feature, longitude/latitude coordinates.
- `stops`: ordered stations, prices, gallons purchased, cost and location.
- `legs`: actual road distances and departure/arrival fuel.
- `distance_miles`, `total_fuel_cost` (USD decimal string), `total_gallons_purchased`, `arrival_fuel_gallons`.
- `routing_calls`: routing method invocations, including cache hits.
- `provider_requests`, `provider_request_counts`: actual outgoing requests, excluding cache hits.

A selected waypoint can have zero purchase after exact fuel recalculation. Totals use unrounded Decimal costs; displayed stop cents can differ from the rounded total by a cent.

`GET /api/trips/<id>` loads saved JSON. `GET /trips/<id>/map` renders a static SVG with the road route, numbered stops, start/finish, cost and local state outlines. `GET /trips/<id>/view` renders an **interactive Leaflet map** of the same trip: open it in a browser and you get zoom/pan, the route drawn as one polyline per leg (hover a leg for real road distance and fuel used), numbered fuel-stop markers with price/gallons/cost popups, and start/finish markers. Leaflet 1.9.4 and the page script are vendored and served from `/static/leaflet/`, so the page needs no CDN. Raster tiles load in the viewer's browser only — CARTO's public raster basemaps first, OpenStreetMap's tile servers as an automatic fallback — which costs the API nothing. The page sends `Referrer-Policy: strict-origin-when-cross-origin` because cross-origin tile hosts identify apps by Referer and OSM's volunteer-run servers return 403 "Access blocked" when it is missing. **None of these endpoints makes any provider calls.** `GET /health/` checks database connectivity.

Invalid inputs return 400; coverage/validation failures 422; exhausted local allowances 429; provider failures 503. Planning errors use `{"error":{"code":"...","message":"..."}}`; serializer errors use field-name keys.

## How the call budget works

1. **Baseline, call 1:** HERE supplies actual route geometry and distance. Trips within 500 miles need no fuel purchase and finish here.
2. **Local optimization, no requests:** PostGIS finds all accepted sample stations within five miles of the baseline. Project their locations onto that route in a US projected coordinate system. A forward-graph fuel-state algorithm selects stops using estimated route progress, detour penalties and a 480-mile preliminary leg limit.
3. **Validate, call 2:** one HERE request contains every selected stop as a repeated `via` parameter. Its actual road sections must match the stop sequence, each leg must be within 500 miles, and total detour must be at most the greater of 20 miles or 5% of baseline distance.
4. **Optional correction, call 3:** if validation fails, select again using a 425-mile preliminary limit and a stronger detour penalty. Validate the different itinerary once. If it still fails, return a clear error. No fourth routing call is allowed.

After validation, a continuous greedy fuel algorithm optimizes purchases **exactly for the chosen fixed itinerary**, using actual road distances. It buys enough to reach the next cheaper reachable stop, or fills only as much as capacity/remaining journey requires. Physical fuel is checked at every leg.

Stop selection remains approximate: projected progress and detour penalties are not road distances. These estimates never determine the final reported mileage or fuel bill. There is no claim of globally cheapest routing or full US coverage. Routing is passenger-car, planning mode, avoids Canada/Mexico, and does not account for truck restrictions, tolls, time cost or live fuel prices.

## Approach at a glance

Design decisions and why they were made:

| Decision | Rationale |
|---|---|
| **One provider, one job per call** | HERE supplies route geometry (1 call), address geocoding (only for string inputs) and station coordinates (only in the offline rebuild command). The call budget is a code constraint, not a convention: routing is hard-capped at 3 per plan and outgoing requests at 5, enforced before network I/O. |
| **Approximate selection, exact validation** | Stop selection runs entirely locally on estimated route progress and detour penalties. The final mileage, leg distances and fuel bill are computed only from the *validated* route's real road sections — estimates never reach the response. |
| **Numbers always come from the validated route** | If HERE's road sections through the chosen stops don't match the plan (too long, too much detour), the itinerary is rejected and re-selected once — never silently reported with inflated distance. |
| **Fuel model: 50-gal tank, 500-mi range, 10 MPG** | Consumption is rounded **up** (conservative). Cost counts fuel purchased *en route*; the starting tank is assumed full and free. Purchase optimization is exact for the fixed itinerary. |
| **PostGIS for geography** | Corridor search (`dwithin` on a `geography` column, indexed) and projection to EPSG:5070 for route-progress math. It costs a heavier install but makes station search a single indexed query, not a Python scan. |
| **DB-backed allowances and caching** | Usage counters are reserved in PostgreSQL *before* each request with a row lock, so concurrent requests or crashed workers cannot overshoot the budget. Cached responses make replays free and fast. |
| **Honest failure over fabricated plans** | Uncoverable corridors return `insufficient_station_coverage` with a clear message. No interpolated stations, no invented prices, no optimistic distance estimates. |
| **Two maps, zero map calls** | A static SVG (no JavaScript, strict `default-src 'none'` CSP) and an interactive Leaflet page. Tiles load only in the viewer's browser; the API never fetches one. |

## Data pipeline: from the supplied CSV to routable stations

The assignment's price list (`fuel-prices-for-be-assessment.csv`) is an OPIS truckstop extract: 8,151 rows of `OPIS Truckstop ID, Truckstop Name, Address, City, State, Rack ID, Retail Price`. It has **no coordinates**, which is the central problem — you cannot route to `"I-40, EXIT 267 & SR-379"`. The pipeline below turns it into 2,547 verified, routable stations with auditable provenance. Every stage is reproducible from its management command and writes a machine-readable report to `reports/`.

### Stage 1 — Ingest and clean (`import_stations`)

The source CSV is never modified; rows stream through a normalization pass (`planner/cleaning.py`):

- Whitespace collapsed, state codes uppercased, UTF-8 BOM handled.
- Required columns verified; station ID must be a positive integer; price must be finite, between 0 and 100, and carry at most 8 decimals; name, address and city must be non-empty. Violations reject the row, never the file.
- Rows outside the 48 contiguous states + DC are excluded.
- Exact duplicate rows are counted and collapsed.
- Rows are grouped by OPIS Truckstop ID. If any group disagrees on price, name, address, city or state, the **whole station is excluded** and reported — it is never "resolved" by silently choosing the cheapest row, because attaching a cheap price to the wrong physical station is precisely the error this API must not make.
- Every station keeps its source row numbers and a SHA-256 of the input file, so any figure quoted here is reproducible against the exact bytes imported.

**Outcome: 8,151 rows → 6,059 eligible unique stations** (620 non-US rows, 26 exact duplicates, 567 conflicted IDs excluded). Full report: `reports/import.json`.

### Stage 2 — Geocode (`geocode_stations`)

Coordinates come from HERE's Discover endpoint, one request per station, and the command is deliberately conservative:

- **Exit strings are not sent as street addresses.** `"I-40, EXIT 267"` is appended only as city/state context; a live trial showed that sending exit descriptions as street addresses substantially degraded match quality. A street address is included only when it starts with a number.
- Rate-limited (~0.22 s/request), budget-capped, and **resumable**: it only ever queries stations still marked `pending`, so an interrupted run re-sends nothing and re-spend nothing.
- The run spent 4,871 Discover requests inside a database-enforced allowance; usage is audited in `reports/geocoding.json`.

### Stage 3 — Identity screen (`planner/matching.py`)

A geocoder returns *a* match, not necessarily the *right* match — and a wrong match attaches the wrong price to the wrong location. Every candidate passes a multi-rule screen that rejects rather than guesses:

- Must be a `place` result, in the USA, in the **same state** and **same normalized city** as the CSV row, and categorized as a fuel/truck-stop station.
- **Branch-number conflicts reject**: a CSV station named `#247` is discarded if HERE's title says `#251`. Known Love's store numbers are additionally cross-checked against Love's own official website URLs in the returned contact data — one provider POI can never stand in for two CSV stations.
- Name similarity is scored with digit-stripped string matching; distinctive brand words boost the score, but **generic names ("TRAVEL CENTER") are rejected outright** as unidentifiable.
- Street-number conflicts reject. The top candidate must score ≥ 0.8 **and** beat the runner-up by ≥ 0.1 — ambiguous cases are refused, not coin-flipped.
- Accepted coordinates must fall inside the contiguous-US bounding box.

Every candidate, score and rejection reason is stored on the station as `match_evidence`, so any accepted match can be audited later.

**Outcome: 2,547 accepted stations — 42% of eligible.** The other 3,512 were screened out and stay out. Accepted matches are *automatically screened*, **not independently surveyed coordinates**.

### Known gap, stated honestly

The source CSV contains only **29 Oregon stations**, so I-5 through Oregon cannot be planned and returns a clear `insufficient_station_coverage` error. This is a limitation of the supplied data, not of the pipeline — spending more geocoding budget cannot close it.

### Query-time use

Accepted stations are stored as PostGIS `geography` points. At plan time, an indexed corridor query finds stations within 5 miles of the route, projects them onto it in EPSG:5070, and keeps the **cheapest station per 25-mile bin** — two stations within 25 miles are near-substitutes for a 500-mile tank. This bounds the selection graph on dense corridors: `select_stops` prefers itineraries with twelve stops or fewer (hard cap twenty) so a cost-optimal plan doesn't stop at every price dip. These are cost/latency heuristics, not API limits — HERE is bounded by request length, roughly a hundred waypoints — and planning stays under a second even with the full dataset.

### Rebuilding the dataset

Loading the bundled fixture (`loaddata data/stations.json`) never calls HERE. To rebuild from scratch:

```sh
docker compose exec web python manage.py import_stations
docker compose exec web python manage.py geocode_stations --target 10000 --max-requests 12000
docker compose exec web python manage.py export_stations
```

Accepted matches and their evidence are retained under the account holder's permission; the exported fixture contains no API keys. The historical CSV is not a current pump-price feed — prices are the assignment's snapshot, and every response says so.

## Local static map

`data/us_states.geojson` is public-domain [2024 Census cartographic boundary data](https://www2.census.gov/geo/tiger/GENZ2024/shp/), transformed to WGS84. Map rendering projects and simplifies it locally. Coordinate validation uses the unsimplified cartographic polygons; these are generalized boundaries, so coastal/boundary locations can require a full address instead.

The data is already bundled. `scripts/prepare_boundaries.py` reproduces the one-time download; it never runs on requests or server startup. Map attribution identifies HERE route geometry and Census boundaries.

## Safety limits and caching

Default lifetime local allowances: Discover 1,500; Geocode 30; Routing 180. They persist in PostgreSQL and do not reset automatically. These are safeguards, **not the account's remaining HERE billing balance**; historical benchmark calls predate them.

Each planning client permits at most three routing calls and five outgoing calls total. Failed attempts consume allowance, reserved in the database before network I/O. Requests have bounded timeouts; no retries, redirects, pagination or details calls. Cached searches last 30 days; routes/geocodes seven days. Old tile counters/cache records may remain as audit history but no code can fetch tiles.

The app binds only to localhost and includes anonymous throttling. Before public deployment add authentication, shared throttling, proper secrets and HTTPS; saved trip UUIDs are not authorization.

## Tests and live demonstration

```sh
docker compose exec web python manage.py test planner.tests -v 2
docker compose exec web python manage.py check
```

Automated tests mock all networking and spend no quota. Tests exercise uncached one/two/three-call paths, a failed correction with no fourth call, cache reuse, fuel conservation/capacity, price optimization, PostGIS filtering, import/match checks, static maps, the interactive map page and its locally served Leaflet assets, and removed endpoints.

An opt-in live check spends limited routing allowance, times the response and verifies a zero-request replay:
```sh
docker compose exec web python manage.py live_smoke --coordinate-demo
```
Its result is saved to `reports/live-trip.json`. The script's HTTP client executes the real Django view in-process; timings include application/database/provider work, but not client network latency.

Installed dependencies are reproducibly pinned in `requirements.lock`; declared ranges are in `requirements.txt`. No code has been committed or pushed.
