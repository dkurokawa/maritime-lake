from __future__ import annotations

from pathlib import Path

import pytest
from lakefix import point_lake_at


@pytest.fixture
def lake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty lake root (bronze/silver/gold/state) isolated from the repo's data/."""
    root = tmp_path / "lake"
    point_lake_at(monkeypatch, root)
    return root
