"""Conservative identity screening, not an independent coordinate audit."""

import re
from difflib import SequenceMatcher
from urllib.parse import urlparse


def words(value):
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def assess(station, item):
    address = item.get("address", {})
    if (
        item.get("resultType") != "place"
        or address.get("countryCode") != "USA"
        or address.get("stateCode") != station.state
    ):
        return 0, "wrong_type_country_or_state"
    if words(address.get("city", "")) != words(station.city):
        return 0, "different_city"
    categories = item.get("categories", [])
    if not any(
        c.get("id", "").startswith("700-7600")
        or any(w in c.get("name", "").lower() for w in ("gas station", "fuel", "truck stop"))
        for c in categories
    ):
        return 0, "not_fuel_station"
    name, title = words(station.name), words(item.get("title", ""))
    branch = re.search(r"#\s*(\d+)", station.name)
    other = re.search(r"#\s*(\d+)", item.get("title", ""))
    if branch and other and branch[1] != other[1]:
        return 0, "branch_conflict"
    # HERE may omit a chain's store number in its title but include it in the
    # official website URL. Never attach another branch's price to this POI.
    linked_branches = set()
    for contact in item.get("contacts", []):
        for site in contact.get("www", []):
            parsed = urlparse(site.get("value", ""))
            if parsed.hostname in ("loves.com", "www.loves.com"):
                match = re.match(r"/locations/(\d+)(?:/|$)", parsed.path)
                if match:
                    linked_branches.add(int(match[1]))
    if branch and linked_branches and int(branch[1]) not in linked_branches:
        return 0, "official_url_branch_conflict"
    score = SequenceMatcher(None, re.sub(r"\d+", "", name), re.sub(r"\d+", "", title)).ratio()
    distinctive = set(name.split()) - {
        "travel",
        "center",
        "truck",
        "stop",
        "station",
        "fuel",
        "gas",
        "the",
        "of",
        "and",
    }
    distinctive = {w for w in distinctive if not w.isdigit()}
    if distinctive and distinctive.issubset(set(title.split())):
        score = max(score, 0.9)
    if not distinctive:
        return 0, "generic_name"
    street_number = re.match(r"^(\d+)\s", station.address)
    if street_number and address.get("houseNumber") and street_number[1] != address["houseNumber"]:
        return 0, "street_number_conflict"
    if branch and linked_branches == {int(branch[1])}:
        score = max(score, 0.99)
    return score, "name_city_state_category_agree"
