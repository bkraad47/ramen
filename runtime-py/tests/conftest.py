import shutil
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def demo_bucket(tmp_path: Path) -> Path:
    dst = tmp_path / "demo"
    shutil.copytree(FIXTURES / "demo", dst)
    return dst


def write_pkg(bucket: Path, kind: str, name: str, proto: dict, code: str = "", stem: str | None = None) -> Path:
    d = bucket / "mcp" / kind / name
    d.mkdir(parents=True, exist_ok=True)
    import json

    (d / f"{stem or name}.json").write_text(json.dumps(proto))
    if code:
        (d / f"{stem or name}.py").write_text(code)
    return d
