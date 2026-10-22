from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import duckdb
import pytest
import yaml
from lakefix import write_full_bronze
from mlake import build, config


def test_round_only_touches_floats() -> None:
    assert build._round(1.23456) == 1.235
    assert build._round(1.23456, 1) == 1.2
    assert build._round(7) == 7
    assert build._round("x") == "x"
    assert build._round(None) is None


def test_load_container_od_without_cache_creates_empty_typed_table(lake: Path) -> None:
    con = duckdb.connect()
    build.load_container_od(con)
    assert con.execute("SELECT count(*) FROM silver_container_od").fetchone() == (0,)
    cols = [r[0] for r in con.execute("DESCRIBE silver_container_od").fetchall()]
    assert cols == ["origin", "dest", "scenario", "nm", "searoute_version"]


def test_load_container_od_reads_cache_when_present(lake: Path) -> None:
    config.SILVER.mkdir(parents=True)
    src = duckdb.connect()
    src.execute(
        f"COPY (SELECT 'a' origin, 'b' dest, 'open' scenario, 12.5 nm, 'v1' searoute_version) "
        f"TO '{config.SILVER / 'container_od.parquet'}' (FORMAT parquet)"
    )
    con = duckdb.connect()
    build.load_container_od(con)
    assert con.execute("SELECT origin, dest, scenario, nm FROM silver_container_od").fetchall() == [
        ("a", "b", "open", 12.5)
    ]


def test_load_curated_events_normalises_dates_and_optional_end(tmp_path: Path) -> None:
    path = tmp_path / "events.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "events": [
                    {
                        "id": "e1",
                        "title": "T",
                        "start": dt.date(2021, 3, 23),
                        "end": dt.date(2021, 3, 29),
                        "chokepoints": ["c1"],
                        "summary": "S",
                        "source_url": "https://example.test/e1",
                        "date_basis": "ignored",
                    },
                    {
                        "id": "e2",
                        "title": "Open-ended",
                        "start": dt.date(2023, 7, 30),
                        "chokepoints": ["c2"],
                        "summary": "S2",
                        "source_url": "https://example.test/e2",
                    },
                ]
            }
        )
    )
    e1, e2 = build.load_curated_events(path)
    assert e1 == {
        "id": "e1",
        "kind": "curated",
        "title": "T",
        "start": "2021-03-23",
        "end": "2021-03-29",
        "chokepoints": ["c1"],
        "summary": "S",
        "source_url": "https://example.test/e1",
    }
    assert e2["end"] is None and e2["start"] == "2023-07-30"


def test_export_gold_rejects_events_for_unknown_chokepoints(
    lake: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_full_bronze(config.BRONZE)
    monkeypatch.setattr(
        build,
        "load_curated_events",
        lambda: [{"id": "ghost", "chokepoints": ["chokepoint1", "nowhere"]}],
    )
    with pytest.raises(
        ValueError, match=r"event ghost references unknown chokepoints \['nowhere'\]"
    ):
        build.run()
    assert not (config.STATE / "build_state.json").exists()  # nothing recorded for a failed build


def test_write_state_rotates_current_into_previous(lake: Path) -> None:
    build.write_state({"silver_chokepoint_daily": 1})
    build.write_state({"silver_chokepoint_daily": 2})
    build.write_state({"silver_chokepoint_daily": 3})
    state = json.loads((config.STATE / "build_state.json").read_text())
    assert state == {
        "previous": {"silver_chokepoint_daily": 2},
        "current": {"silver_chokepoint_daily": 3},
    }


def test_build_silver_writes_partitioned_and_dimension_parquet(lake: Path) -> None:
    write_full_bronze(config.BRONZE)
    build.build_silver(duckdb.connect())
    silver = config.SILVER
    assert (silver / "chokepoint_daily" / "year=2024").is_dir()
    assert (silver / "port_daily" / "year=2024").is_dir()
    for name in ("dim_chokepoint", "disruptions", "dim_port", "container_port_rank"):
        assert (silver / f"{name}.parquet").exists(), name
    con = duckdb.connect()
    n = con.execute(
        "SELECT count(*) FROM read_parquet(?)", [str(silver / "dim_port.parquet")]
    ).fetchone()
    assert n == (8,)


def test_export_container_skips_od_json_when_cache_does_not_cover_ports(lake: Path) -> None:
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE gold_container_ports AS SELECT 'p1' portid, 'One' portname, 'A' country, "
        "1.0::DOUBLE lat, 2.0::DOUBLE lon, 1 rank, 10 calls_365d, "
        "3.14159::DOUBLE ma7_container, DATE '2026-01-02' latest_date"
    )
    con.execute(
        "CREATE TABLE gold_container_od (origin VARCHAR, dest VARCHAR, scenario VARCHAR, nm DOUBLE)"
    )
    out = lake / "gold"
    counts = build.export_container(con, out)
    assert counts == {"gold_container_ports": 1}
    assert not (out / "container_od.json").exists()
    (port,) = json.loads((out / "container_ports.json").read_text())
    assert port["latest_date"] == "2026-01-02"
    assert port["ma7_container"] == 3.142  # floats rounded to 3 dp in the JSON
