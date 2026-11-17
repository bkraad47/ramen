"""R1 / C7 (0.7.0): the static region list and the super admin's block list (`config/regions`)."""

import json
from pathlib import Path

PROVIDERS = ("gcp", "aws", "azure")
DOC = "regions"  # config/regions = {"blocked": {"gcp": [...], "aws": [...], "azure": [...]}}
_DATA = json.loads(Path(__file__).with_name("regions.json").read_text())
NOTE: str = _DATA["note"]
REGIONS: dict[str, dict[str, list[str]]] = {p: _DATA[p] for p in PROVIDERS}


def zone_name(provider: str, region: str, letter: str) -> str:
    """GCP zones are `us-central1-a`, AWS availability zones `us-east-1a`, Azure availability zones `eastus-1`."""
    return f"{region}{letter}" if provider == "aws" else f"{region}-{letter}"


def zones(provider: str, blocked: list[str] | None = None) -> dict[str, list[str]]:
    """Region → its zone names, without the blocked regions. Unknown providers (`local`) have none."""
    skip = set(blocked or [])
    return {
        r: [zone_name(provider, r, z) for z in letters]
        for r, letters in REGIONS.get(provider, {}).items()
        if r not in skip
    }


CONTINENTS = ("Americas", "Europe", "Asia Pacific", "Middle East & Africa")
_PREFIX = {  # 1.0.0c: GCP and AWS name the continent in the region's first word; Azure in a substring
    "gcp": {
        "us": 0,
        "northamerica": 0,
        "southamerica": 0,
        "europe": 1,
        "asia": 2,
        "australia": 2,
        "africa": 3,
        "me": 3,
    },
    "aws": {"us": 0, "ca": 0, "sa": 0, "mx": 0, "eu": 1, "ap": 2, "af": 3, "me": 3, "il": 3},
}
_AZURE = (
    (0, ("us", "canada", "brazil", "mexico", "chile")),
    (
        1,
        ("europe", "uk", "france", "germany", "norway", "sweden", "switzerland", "poland", "italy", "spain", "austria"),
    ),
    (1, ("belgium", "denmark", "finland", "greece")),
    (2, ("asia", "japan", "korea", "india", "australia", "newzealand", "indonesia", "malaysia", "taiwan")),
    (3, ("uae", "qatar", "israel", "southafrica", "saudi")),
)


def continent(provider: str, region: str) -> str:
    """The continent a region sits in (one of CONTINENTS), `"Other"` for a name the tables do not know."""
    if provider in _PREFIX:
        i = _PREFIX[provider].get(region.split("-", 1)[0])
    else:
        i = next((i for i, words in _AZURE if any(w in region for w in words)), None)
    return CONTINENTS[i] if i is not None else "Other"


def grouped(provider: str, blocked: list[str] | None = None) -> list[tuple[str, list[str]]]:
    """`[(continent, [region, ...]), ...]` in CONTINENTS order (then "Other"), the blocked regions left out."""
    by: dict[str, list[str]] = {}
    for r in zones(provider, blocked):
        by.setdefault(continent(provider, r), []).append(r)
    return [(c, by[c]) for c in (*CONTINENTS, "Other") if c in by]


def blocked_region(provider: str, value: str, blocked: dict[str, list[str]]) -> str | None:
    """The blocked region `value` (a region or a zone of it) falls in, or None. Matched by prefix on the region: GCP
    `us-west1-a` → `us-west1`, AWS `eu-west-1a` → `eu-west-1`, Azure `eastus-1` → `eastus`; `us-west12-a` is not
    `us-west1` and `eastus2-1` is not `eastus`."""
    for r in blocked.get(provider, []) if provider in PROVIDERS else []:
        rest = value[len(r) :] if value.startswith(r) else None
        if rest == "" or (rest and (rest.isalpha() if provider == "aws" else rest[0] == "-")):
            return r
    return None


def message(region: str) -> str:
    return f"region {region} is blocked by a super admin"


# --- §28 satellite clusters: config/regions.clusters = [{provider, region, name, host, project?, account?,
# resource_group?}]; the home cluster is implicit (`Cloud.home_region()`) ---------------------------------------------
CLUSTER_EXTRAS = ("project", "account", "resource_group")


def cluster_for(provider: str, region: str, clusters: list[dict], home: str | None):
    """The cluster that hosts `region` (a region or a zone of it): `"home"`, a satellite entry, or None."""
    if home and blocked_region(provider, region, {provider: [home]}):
        return "home"
    for c in clusters:
        if c.get("provider") == provider and blocked_region(provider, region, {provider: [c.get("region", "")]}):
            return c
    return None


def clean_clusters(items) -> list[dict]:
    """Known provider and region, non-empty name and host (a bare hostname), one cluster per region and provider;
    ValueError otherwise."""
    out: list[dict] = []
    for c in items:
        provider = str(c.get("provider") or "").strip()
        if provider not in PROVIDERS:
            raise ValueError(f"Unknown provider {provider!r}")
        region = str(c.get("region") or "").strip()
        if region not in REGIONS[provider]:
            raise ValueError(f"Unknown {provider} region {region!r}")
        name = str(c.get("name") or "").strip()
        host = str(c.get("host") or "").strip().removeprefix("https://").removeprefix("http://").strip("/")
        if not name:
            raise ValueError("A satellite cluster needs a name")
        if not host:
            raise ValueError("A satellite cluster needs a host")
        if any(o["provider"] == provider and o["region"] == region for o in out):
            raise ValueError(f"Two clusters in {provider} region {region}")
        entry = {"provider": provider, "region": region, "name": name, "host": host}
        entry.update({k: str(c[k]).strip() for k in CLUSTER_EXTRAS if c.get(k)})
        out.append(entry)
    return out


def clean(blocked: dict) -> dict[str, list[str]]:
    """Every provider always present, duplicates and blanks dropped; unknown regions raise ValueError."""
    out = {}
    for p in PROVIDERS:
        seen = []
        for r in blocked.get(p) or []:
            r = str(r).strip()
            if r and r not in seen:
                seen.append(r)
        unknown = [r for r in seen if r not in REGIONS[p]]
        if unknown:
            raise ValueError(f"Unknown {p} regions: {', '.join(unknown)}")
        out[p] = seen
    return out
