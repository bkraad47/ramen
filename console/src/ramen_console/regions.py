"""R1 / C7 (0.7.0): the static region list and the super admin's block list (`config/regions`)."""

import json
from pathlib import Path

PROVIDERS = ("gcp", "aws")
DOC = "regions"  # config/regions = {"blocked": {"gcp": [...], "aws": [...]}}
_DATA = json.loads(Path(__file__).with_name("regions.json").read_text())
NOTE: str = _DATA["note"]
REGIONS: dict[str, dict[str, list[str]]] = {p: _DATA[p] for p in PROVIDERS}


def zone_name(provider: str, region: str, letter: str) -> str:
    """GCP zones are `us-central1-a`, AWS availability zones `us-east-1a`."""
    return f"{region}-{letter}" if provider == "gcp" else f"{region}{letter}"


def zones(provider: str, blocked: list[str] | None = None) -> dict[str, list[str]]:
    """Region → its zone names, without the blocked regions. Unknown providers (`local`) have none."""
    skip = set(blocked or [])
    return {
        r: [zone_name(provider, r, z) for z in letters]
        for r, letters in REGIONS.get(provider, {}).items()
        if r not in skip
    }


def blocked_region(provider: str, value: str, blocked: dict[str, list[str]]) -> str | None:
    """The blocked region `value` (a region or a zone of it) falls in, or None. Matched by prefix on the region: GCP
    `us-west1-a` → `us-west1`, AWS `eu-west-1a` → `eu-west-1`; `us-west12-a` is not `us-west1`."""
    for r in blocked.get(provider, []) if provider in PROVIDERS else []:
        rest = value[len(r) :] if value.startswith(r) else None
        if rest == "" or (rest and (rest[0] == "-" if provider == "gcp" else rest.isalpha())):
            return r
    return None


def message(region: str) -> str:
    return f"region {region} is blocked by a super admin"


def clean(blocked: dict) -> dict[str, list[str]]:
    """Both providers always present, duplicates and blanks dropped; unknown regions raise ValueError."""
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
