from __future__ import annotations

import importlib
from pathlib import Path

import pytest
from mlake import config


def test_data_dir_defaults_to_repo_data() -> None:
    assert config.DATA == config.ROOT / "data"
    assert config.BRONZE == config.DATA / "bronze"
    assert config.SILVER == config.DATA / "silver"
    assert config.GOLD == config.DATA / "gold"
    assert config.STATE == config.DATA / "state"


def test_data_dir_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MLAKE_DATA_DIR", str(tmp_path))
    try:
        reloaded = importlib.reload(config)
        assert tmp_path == reloaded.DATA
        assert tmp_path / "gold" == reloaded.GOLD
        assert tmp_path != reloaded.ROOT  # code/sql locations are not relocated
    finally:
        monkeypatch.delenv("MLAKE_DATA_DIR")
        importlib.reload(config)
    assert config.DATA == config.ROOT / "data"


def test_repo_resources_exist() -> None:
    assert config.SQL_DIR.is_dir() and any(config.SQL_DIR.glob("*.sql"))
    assert config.EVENTS_YAML.is_file()


def test_ports_top_is_ingested_before_the_daily_ports_that_depend_on_it() -> None:
    names = list(config.SOURCES)
    assert names.index("ports_top") < names.index("Daily_Ports_Data")
    assert config.SOURCES["ports_top"]["mode"] == "statistics"
    assert config.SOURCES["Daily_Ports_Data"]["mode"] == "selected_ports"


def test_every_source_has_a_distinct_bronze_dir() -> None:
    bronze = [m["bronze"] for m in config.SOURCES.values()]
    assert len(bronze) == len(set(bronze))


def test_scenarios_and_presets() -> None:
    assert [s["id"] for s in config.SCENARIOS] == ["open", "red_sea_closed", "panama_closed"]
    assert all("northwest" in s["restrictions"] for s in config.SCENARIOS)
    assert len(config.PRESET_PORTS) == 5
