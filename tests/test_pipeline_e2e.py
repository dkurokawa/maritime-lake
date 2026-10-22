"""bronze -> silver -> gold -> od -> check on a tiny synthetic lake (no network)."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import duckdb
import pytest
from lakefix import PRESETS, cp_feat, write_full_bronze, write_run
from mlake import build, check, config, od


@pytest.fixture
def built(lake: Path) -> dict[str, int]:
    write_full_bronze(config.BRONZE)
    return build.run()


def test_build_row_counts_and_files(lake: Path, built: dict[str, int]) -> None:
    assert built["silver_chokepoint_daily"] == 28 * 20
    assert built["silver_dim_chokepoint"] == 28
    assert built["silver_disruptions"] == 2
    assert built["silver_dim_port"] == 8
    assert built["silver_port_daily"] == 8 * 20
    assert built["gold_chokepoint_daily"] == 28 * 20
    assert built["gold_container_ports"] == 8  # 8 ranked ports, presets all among them
    assert "gold_container_od" not in built  # no OD cache yet

    gold = config.GOLD
    for name in ("chokepoint_daily.parquet", "chokepoint_latest.parquet", "chokepoints.geojson"):
        assert (gold / name).exists()
    assert len(list((gold / "series").glob("*.json"))) == 28
    assert not (gold / "container_od.json").exists()

    series = json.loads((gold / "series" / "chokepoint1.json").read_text())
    assert series["name"] == "Strait chokepoint1"
    assert len(series["dates"]) == 20 and series["dates"][0] == "2024-01-01"
    assert set(series["n_by_type"]) == {"container", "dry_bulk", "general_cargo", "roro", "tanker"}

    geo = json.loads((gold / "chokepoints.geojson").read_text())
    assert len(geo["features"]) == 28
    assert geo["features"][0]["geometry"] == {"type": "Point", "coordinates": [2.0, 1.0]}
    assert {f["properties"]["date"] for f in geo["features"]} == {"2024-01-20"}


def test_build_events_merge_curated_and_disruptions(lake: Path, built: dict[str, int]) -> None:
    events = json.loads((config.GOLD / "events.json").read_text())
    curated = [e for e in events if e["kind"] == "curated"]
    disruptions = {e["id"]: e for e in events if e["kind"] == "disruption"}
    assert curated and built["events"] == len(curated) + 2
    # affected ports are filtered down to known chokepoints, sorted
    assert disruptions["pw-1"]["chokepoints"] == ["chokepoint1", "chokepoint3"]
    assert (
        disruptions["pw-1"]["start"] == "2024-01-05" and disruptions["pw-1"]["end"] == "2024-01-08"
    )
    assert disruptions["pw-2"]["chokepoints"] == [] and disruptions["pw-2"]["end"] is None
    assert disruptions["pw-1"]["event_type"] == "Cyclone"


def test_build_meta_and_state(lake: Path, built: dict[str, int]) -> None:
    meta = json.loads((config.GOLD / "meta.json").read_text())
    assert meta["data_max_date"] == "2024-01-20"
    assert meta["row_counts"] == built
    assert meta["attribution"] == config.ATTRIBUTION
    state = json.loads((config.STATE / "build_state.json").read_text())
    assert state == {"previous": None, "current": built}


def test_second_build_records_previous(lake: Path, built: dict[str, int]) -> None:
    build.run()
    state = json.loads((config.STATE / "build_state.json").read_text())
    assert state["previous"] == built and state["current"] == built


def test_later_run_overrides_earlier_run_in_silver(lake: Path) -> None:
    write_full_bronze(config.BRONZE)
    write_run(
        config.BRONZE,
        "chokepoint_daily",
        "20260111T000000Z",
        [cp_feat("chokepoint1", dt.date(2024, 1, 1), n=500)],
    )
    counts = build.run()
    assert counts["silver_chokepoint_daily"] == 28 * 20  # upsert, not append
    con = duckdb.connect()
    row = con.execute(
        "SELECT n_container, ingested_run FROM read_parquet(?, hive_partitioning=true) "
        "WHERE portid = 'chokepoint1' AND date = DATE '2024-01-01'",
        [str(config.SILVER / "chokepoint_daily" / "year=*" / "*.parquet")],
    ).fetchone()
    assert row == (500, "20260111T000000Z")


def test_od_then_check_passes_end_to_end(
    lake: Path,
    built: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # before OD: the container checks must fail loudly, not silently pass
    assert check.run() == 1
    assert "[FAIL] OD has every (origin, dest, scenario)" in capsys.readouterr().out

    calls: list[tuple[list[float], list[float], list[str]]] = []

    def fake_route(a: object, b: object, restrictions: list[str]) -> float:
        calls.append((list(a), list(b), restrictions))  # type: ignore[call-overload]
        return float("inf") if "panama" in restrictions else 1234.4

    real_compute = od.compute
    monkeypatch.setattr(
        od, "compute", lambda ports, todo: real_compute(ports, todo, route=fake_route)
    )
    out = od.run()
    n = 8
    assert out["computed"] == n * n * 3
    assert len(calls) == n * (n - 1) * 3  # the diagonal is never routed
    assert out["gold_container_od"] == n * n * 3

    doc = json.loads((config.GOLD / "container_od.json").read_text())
    assert len(doc["ports"]) == n and set(doc["nm"]) == {"open", "red_sea_closed", "panama_closed"}
    assert doc["nm"]["open"][0][0] == 0 and doc["nm"]["open"][0][1] == 1234
    assert doc["nm"]["panama_closed"][0][1] is None  # no path -> null

    # second run: everything is cached, nothing is recomputed
    calls.clear()
    assert od.run()["computed"] == 0 and calls == []

    assert check.run() == 0
    printed = capsys.readouterr().out
    assert "check: all passed" in printed and "[FAIL]" not in printed
    assert set(PRESETS) <= set(doc["ports"])
