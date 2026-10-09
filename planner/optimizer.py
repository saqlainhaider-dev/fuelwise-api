"""Minimum-cost dynamic program on a forward graph, with 0.1-gallon states.

Each edge rounds consumption UP to the next state. Runtime O(V*C + E*C),
where C=500. The graph and quantization bound the optimality claim.
"""

from decimal import ROUND_CEILING, Decimal

from planner.provider import PlanningError

METERS_PER_MILE = Decimal("1609.344")
CAPACITY = 500


def fuel_units(meters):
    return int((Decimal(str(meters)) / METERS_PER_MILE).to_integral_value(rounding=ROUND_CEILING))


def solve(prices, edges):
    n = len(prices)
    costs = [[None] * (CAPACITY + 1) for _ in range(n)]
    previous = {}
    costs[0][CAPACITY] = Decimal(0)
    outgoing = [[] for _ in range(n)]
    for (start, end), route in edges.items():
        if not 0 <= start < end < n:
            raise ValueError("Edges must form a forward DAG.")
        if route["meters"] < 0:
            raise ValueError("Negative distance.")
        outgoing[start].append((end, fuel_units(route["meters"])))
    for node in range(n):
        for fuel in range(CAPACITY + 1):
            cost = costs[node][fuel]
            if cost is None:
                continue
            if 0 < node < n - 1 and fuel < CAPACITY:
                purchase = cost + prices[node] / Decimal(10)
                if costs[node][fuel + 1] is None or purchase < costs[node][fuel + 1]:
                    costs[node][fuel + 1] = purchase
                    previous[node, fuel + 1] = (node, fuel)
            for end, used in outgoing[node]:
                if used > fuel:
                    continue
                remaining = fuel - used
                if costs[end][remaining] is None or cost < costs[end][remaining]:
                    costs[end][remaining] = cost
                    previous[end, remaining] = (node, fuel)
    finishes = [(cost, fuel) for fuel, cost in enumerate(costs[-1]) if cost is not None]
    if not finishes:
        raise PlanningError(
            "insufficient_station_coverage",
            "No feasible trip was found within the sampled stations and evaluated corridor. Try a shorter trip or expand the station dataset.",
        )
    cost, remaining = min(finishes)
    state, transitions = (n - 1, remaining), []
    while state != (0, CAPACITY):
        parent = previous[state]
        if parent[0] != state[0]:
            transitions.append({"start": parent[0], "end": state[0], "departure_units": parent[1]})
        state = parent
    return list(reversed(transitions)), cost


def fixed_itinerary_purchases(prices, leg_meters):
    """Exact continuous fuel optimum for a fixed, validated itinerary."""
    if len(leg_meters) != len(prices) - 1:
        raise PlanningError(
            "invalid_route_sections", "Road sections do not match the selected stops."
        )
    used = [Decimal(str(m)) / METERS_PER_MILE / 10 for m in leg_meters]
    if any(g < 0 or g > 50 for g in used):
        raise PlanningError(
            "unreachable_leg", "A validated road leg exceeds the 500-mile tank range."
        )
    fuel, results = Decimal(50), []
    for i, consumed in enumerate(used):
        arrival, bought = fuel, Decimal(0)
        if i:
            needed = Decimal(0)
            target = Decimal(50)
            for j in range(i + 1, len(prices)):
                needed += used[j - 1]
                if needed > 50:
                    break
                if j == len(prices) - 1 or prices[j] < prices[i]:
                    target = needed
                    break
            bought = max(Decimal(0), target - fuel)
            fuel += bought
        departure = fuel
        fuel -= consumed
        if fuel < 0 or departure > 50:
            raise PlanningError("fuel_validation_failed", "Physical fuel validation failed.", 500)
        results.append(
            {
                "arrival": arrival,
                "bought": bought,
                "departure": departure,
                "remaining": fuel,
                "consumed": consumed,
            }
        )
    return results
