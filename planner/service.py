"""Local candidate planning followed by at most two whole-itinerary validations."""

from decimal import ROUND_HALF_UP, Decimal

from django.contrib.gis.geos import LineString
from django.contrib.gis.measure import D

from planner.geography import resolve_location
from planner.models import Station
from planner.optimizer import METERS_PER_MILE, fixed_itinerary_purchases, solve
from planner.provider import HereClient, PlanningError

MILE = float(METERS_PER_MILE)

# Selection prefers itineraries with at most this many stops; beyond the hard
# cap below the request is rejected rather than sent.
MAX_VALIDATED_STOPS = 12
MAX_HARDCAP_STOPS = 20


def money(value):
    return str(value.quantize(Decimal(".01"), rounding=ROUND_HALF_UP))


def shortlist(route):
    """Keep the cheapest sampled station per 25-mile corridor bin.

    Bounding the candidate count keeps the stop-selection graph small on dense
    corridors; two stations within 25 miles of each other are near-substitutes
    for a 500-mile tank, so only the cheaper one matters.
    """
    line = LineString(route["coordinates"], srid=4326)
    projected = line.transform(5070, clone=True)
    if projected.length == 0:
        return []
    bins = {}
    for station in Station.objects.filter(
        status="accepted", access_location__isnull=False, location__dwithin=(line, D(mi=5))
    ):
        point = station.access_location.transform(5070, clone=True)
        station.progress_meters = projected.project(point) / projected.length * route["meters"]
        station.offset_meters = projected.distance(point)
        if 0 < station.progress_meters < route["meters"]:
            key = int(station.progress_meters // (25 * MILE))
            current = bins.get(key)
            if current is None or (station.price, station.opis_id) < (
                current.price,
                current.opis_id,
            ):
                bins[key] = station
    return sorted(bins.values(), key=lambda s: (s.progress_meters, s.opis_id))


def select_stops(baseline, candidates, correction=False):
    # Estimates select stops; only validated road distances determine final fuel.
    margin, detour = (425, 3) if correction else (480, 1.5)
    prices = [None] + [s.price for s in candidates] + [None]
    best = None
    # Prefer plans that stop less often: frequent tiny top-ups buy almost no
    # extra price advantage, and each stop adds detour risk at validation.
    for min_leg in (0, 200, 100):
        nodes = (
            [(0, 0)]
            + [(s.progress_meters, s.offset_meters) for s in candidates]
            + [(baseline["meters"], 0)]
        )
        edges = {}
        for i, (progress, offset) in enumerate(nodes[:-1]):
            for j in range(i + 1, len(nodes)):
                distance = nodes[j][0] - progress + detour * (offset + nodes[j][1])
                if distance < min_leg * MILE:
                    continue
                if 0 <= distance <= margin * MILE:
                    edges[i, j] = {"meters": distance}
        try:
            transitions, _ = solve(prices, edges)
        except PlanningError:
            continue
        stops = [candidates[step["end"] - 1] for step in transitions[:-1]]
        if best is None or len(stops) < len(best):
            best = stops
        if len(stops) <= MAX_VALIDATED_STOPS:
            return stops
    if best is None:
        raise PlanningError(
            "insufficient_station_coverage",
            "No feasible stop plan fits the sampled stations and evaluated corridor.",
        )
    return best


def guard_stop_count(selected):
    if len(selected) > MAX_HARDCAP_STOPS:
        raise PlanningError(
            "too_many_fuel_stops",
            "This corridor needs more than twenty fuel stops with the current station sample.",
        )


def validate_itinerary(route, selected, baseline):
    if route is None:
        raise PlanningError(
            "route_validation_failed", "HERE could not route through the selected stops."
        )
    if route["meters"] - baseline["meters"] > max(20 * MILE, 0.05 * baseline["meters"]):
        raise PlanningError("excessive_detour", "Selected stops require excessive road detours.")
    return fixed_itinerary_purchases(
        [None] + [s.price for s in selected] + [None], route["leg_meters"]
    )


def plan(start, finish, client=None):
    client = client or HereClient(max_requests=5)
    origin, destination = resolve_location(start, client), resolve_location(finish, client)
    a, b = (origin["lng"], origin["lat"]), (destination["lng"], destination["lat"])
    baseline = (
        {"meters": 0, "seconds": 0, "coordinates": [list(a), list(b)], "leg_meters": [0]}
        if a == b
        else client.route(a, b)
    )
    if baseline is None:
        raise PlanningError(
            "route_not_found", "No supported driving route connects these locations."
        )
    selected, candidates = [], []
    validated = baseline
    if baseline["meters"] <= 500 * MILE:
        validated = {**baseline, "leg_meters": [baseline["meters"]]}
        purchases = validate_itinerary(validated, selected, baseline)
    else:
        candidates = shortlist(baseline)
        selected = select_stops(baseline, candidates)
        guard_stop_count(selected)
        for attempt in range(2):
            validated = client.route(
                a, b, via=[(s.access_location.x, s.access_location.y) for s in selected]
            )
            try:
                purchases = validate_itinerary(validated, selected, baseline)
                break
            except PlanningError as exc:
                if attempt:
                    raise PlanningError(
                        "route_validation_failed",
                        "No safe itinerary was validated within three routing calls. Try different endpoints or expand station coverage.",
                    ) from exc
                revised = select_stops(baseline, candidates, correction=True)
                if [s.opis_id for s in revised] == [s.opis_id for s in selected]:
                    raise PlanningError(
                        "route_validation_failed",
                        "Road validation failed and no different corrective itinerary was found.",
                    ) from exc
                guard_stop_count(revised)
                selected = revised
    cost, purchased = Decimal(0), Decimal(0)
    stops, legs = [], []
    for i, fuel in enumerate(purchases):
        if i:
            station = selected[i - 1]
            stop_cost = fuel["bought"] * station.price
            cost += stop_cost
            purchased += fuel["bought"]
            stops.append(
                {
                    "station_id": station.opis_id,
                    "name": station.name,
                    "address": station.address,
                    "city": station.city,
                    "state": station.state,
                    "location": {"lat": station.location.y, "lng": station.location.x},
                    "access_location": {
                        "lat": station.access_location.y,
                        "lng": station.access_location.x,
                    },
                    "price_per_gallon": str(station.price),
                    "arrival_fuel_gallons": round(float(fuel["arrival"]), 6),
                    "gallons_purchased": round(float(fuel["bought"]), 6),
                    "cost": money(stop_cost),
                    "validation": "automated_identity_screen",
                }
            )
        legs.append(
            {
                "from_index": i,
                "to_index": i + 1,
                "distance_miles": round(validated["leg_meters"][i] / MILE, 3),
                "fuel_used_gallons": round(float(fuel["consumed"]), 6),
                "departure_fuel_gallons": round(float(fuel["departure"]), 6),
                "arrival_fuel_gallons": round(float(fuel["remaining"]), 6),
            }
        )
    return {
        "start": origin,
        "finish": destination,
        "distance_miles": round(validated["meters"] / MILE, 3),
        "baseline_distance_miles": round(baseline["meters"] / MILE, 3),
        "duration_seconds": validated["seconds"],
        "total_fuel_cost": money(cost),
        "total_gallons_purchased": round(float(purchased), 6),
        "arrival_fuel_gallons": round(float(purchases[-1]["remaining"]), 6),
        "currency": "USD",
        "stops": stops,
        "legs": legs,
        "route": {
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": validated["coordinates"]},
            "properties": {},
        },
        "provider_requests": client.requests,
        "provider_request_counts": client.request_counts,
        "routing_calls": client.routing_calls,
    }
