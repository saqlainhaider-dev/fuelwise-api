"""Deterministic CSV normalization; never overwrite the supplied file."""

import csv
import hashlib
import re
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation

US_STATES = set(
    "AL AZ AR CA CO CT DE FL GA ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC".split()
)
REQUIRED = {
    "OPIS Truckstop ID",
    "Truckstop Name",
    "Address",
    "City",
    "State",
    "Retail Price",
    "Rack ID",
}


def normalize(value):
    return re.sub(r"\s+", " ", value.strip())


def clean_csv(path):
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    groups, rejected, seen = defaultdict(list), [], set()
    counts = Counter()
    with path.open(newline="", encoding="utf-8-sig") as source:
        reader = csv.DictReader(source)
        if not REQUIRED.issubset(reader.fieldnames or []):
            raise ValueError(
                "CSV is missing required columns: "
                + ", ".join(sorted(REQUIRED - set(reader.fieldnames or [])))
            )
        for line, raw in enumerate(reader, 2):
            counts["rows"] += 1
            row = {k: normalize(v or "") for k, v in raw.items() if k}
            reason = None
            try:
                station_id = int(row["OPIS Truckstop ID"])
                price = Decimal(row["Retail Price"])
                if station_id <= 0 or not price.is_finite() or not 0 < price < 100:
                    raise ValueError
                if price.as_tuple().exponent < -8:
                    raise ValueError
                if any(not row[k] for k in ("Truckstop Name", "Address", "City")):
                    raise ValueError
            except (ValueError, InvalidOperation):
                reason = "invalid_required_field_or_price"
            state = row["State"].upper()
            if state not in US_STATES:
                reason = "outside_supported_states"
            if reason:
                rejected.append({"line": line, "reason": reason})
                counts[reason] += 1
                continue
            fingerprint = tuple(sorted(row.items()))
            if fingerprint in seen:
                counts["exact_duplicates"] += 1
            seen.add(fingerprint)
            groups[station_id].append((line, row, price))
    stations = []
    for station_id, rows in sorted(groups.items()):
        line, first, price = rows[0]
        issues = []
        if len({r[2] for r in rows}) > 1:
            issues.append("conflicting_prices")
        for field in ("Truckstop Name", "Address", "City", "State"):
            if len({r[1][field].casefold() for r in rows}) > 1:
                issues.append("conflicting_" + field.lower().replace(" ", "_"))
        counts["conflicted_stations" if issues else "eligible_stations"] += 1
        stations.append(
            {
                "opis_id": station_id,
                "name": first["Truckstop Name"],
                "address": first["Address"],
                "city": first["City"],
                "state": first["State"].upper(),
                "price": price,
                "source_rows": [r[0] for r in rows],
                "issues": issues,
                "source_hash": digest,
            }
        )
    return stations, {
        "source_sha256": digest,
        "counts": dict(counts),
        "rejected_rows": rejected,
        "conflicts": [
            {"opis_id": s["opis_id"], "issues": s["issues"], "source_rows": s["source_rows"]}
            for s in stations
            if s["issues"]
        ],
    }
