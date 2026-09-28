"""pip install mcp/requirements.txt with the current interpreter; skipped when unchanged."""

import hashlib
import subprocess
import sys
from pathlib import Path

from .log import log


class DepsError(RuntimeError):
    pass


def install(bucket: Path) -> dict:
    req = Path(bucket) / "mcp" / "requirements.txt"
    if not req.is_file():
        return {"installed": False, "reason": "no requirements"}
    lines = [line.strip() for line in req.read_text(encoding="utf-8").splitlines()]
    pkgs = [line for line in lines if line and not line.startswith("#")]
    if not pkgs:
        return {"installed": False, "reason": "no requirements"}
    digest = hashlib.sha256("\n".join(pkgs).encode()).hexdigest()
    marker = req.with_name(".ramen_requirements.sha256")
    if marker.is_file() and marker.read_text().strip() == digest:
        return {"installed": False, "reason": "unchanged"}
    log("info", "pip install", requirements=str(req), packages=len(pkgs))
    p = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "-q", "-r", str(req)],
        capture_output=True,
        text=True,
    )
    if p.returncode != 0:
        raise DepsError(f"pip install failed: {p.stderr.strip()[-2000:]}")
    try:
        marker.write_text(digest)
    except OSError:
        pass
    return {"installed": True, "packages": len(pkgs)}
