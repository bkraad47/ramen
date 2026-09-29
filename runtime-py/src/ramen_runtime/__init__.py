"""Version comes from one place (CONTRACTS §14, W9): the installed distribution's metadata, which uv builds from
`pyproject.toml`, falling back to the repo `VERSION` file for an editable checkout. Nothing here is typed by hand,
so a release bump cannot leave a stale string in the sidebar or in `/healthz`."""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _dist_version
from pathlib import Path


def _version() -> str:
    try:
        return _dist_version("ramen-runtime")
    except PackageNotFoundError:  # pragma: no cover - only outside an installed environment
        for p in (Path(__file__).resolve().parents[3] / "VERSION", Path("/app/VERSION")):
            if p.exists():
                return p.read_text().strip()
        return "0+unknown"


__version__ = _version()
