"""Phase 1c: port selection, port silver upsert, OD cache reuse, gold JSON shape."""
from __future__ import annotations

import datetime as dt
import gzip
import json
from pathlib import Path

import duckdb
import pytest

from mlake import build, check, config, ingest, od
from mlake.models import run_model

PRESETS = list(config.PRESET_PORTS)


def write_pages(base: Path, run: str, feats: list[dict]) -> None:
    d = base / f"run={run}"
    d.mkdir(parents=True)
    with gzip.open(d / "page-0000.json.gz", "wt") as fh:
        json.dump({"features": [{"attributes": f} for f in feats]}, fh)


def rank_con(calls: dict[str, int], tmp_path: Path) -> duckdb.DuckDBPyConnection:
    write_pages(tmp_path, "20260101T000000Z", [
        {"portid": p, "portname": p.upper(), "country": "X", "calls": c} for p, c in calls.items()
    ])
    con = duckdb.connect()
    run_model(con, "silver_container_port_rank", bronze_glob=str(tmp_path / "run=*" / "page-*.json.gz"))
    return con


def test_selection_is_top_n_plus_presets(tmp_path, monkeypatch):
    # 70 ports, presets sit at the bottom of the ranking (and one preset has no row at all)
    calls = {f"p{i:03d}": 1000 - i for i in range(70)}
    calls["port1188"] = 5
    calls["port1201"] = 4
    calls["port1114"] = 3
    calls["port664"] = 2  # port1305 absent from the ranking
    con = rank_con(calls, tmp_path)
    ids = ingest.select_ports(con)
    top = [f"p{i:03d}" for i in range(config.TOP_PORTS)]
    assert set(top) <= set(ids)
    assert set(PRESETS) <= set(ids)
    assert len(ids) == config.TOP_PORTS + len(PRESETS)
    assert len(ids) == len(set(ids))  # deduplicated
    assert ids[: config.TOP_PORTS] == top  # ranked order first, preset-only ports after


def test_selection_dedupes_presets_already_in_top(tmp_path):
    calls = {f"p{i:03d}": 1000 - i for i in range(70)}
    calls["port1188"] = 5000  # preset also in the top 60
    con = rank_con(calls, tmp_path)
    ids = ingest.select_ports(con)
    assert ids.count("port1188") == 1
    assert ids[0] == "port1188"
    assert len(ids) == config.TOP_PORTS + len(PRESETS) - 1


def port_feat(date, portid="port1", container=10, **kw):
    f = dict(
        date=date, portid=portid, portname="Testport", country="Testland",
        portcalls_container=container, portcalls_dry_bulk=0, portcalls_general_cargo=0,
        portcalls_roro=0, portcalls_tanker=0, portcalls_cargo=container, portcalls=container,
    )
    f.update(kw)
    return f


def test_port_silver_upsert_latest_run_wins(tmp_path):
    write_pages(tmp_path, "20260101T000000Z", [port_feat("2025-12-30", container=10), port_feat("2025-12-31", container=11)])
    write_pages(tmp_path, "20260102T000000Z", [port_feat("2025-12-31", container=15), port_feat("2026-01-01", container=12)])
    con = duckdb.connect()
    run_model(con, "silver_port_daily", bronze_glob=str(tmp_path / "run=*" / "page-*.json.gz"))
    rows = con.execute("SELECT date, portcalls_container, ingested_run FROM silver_port_daily ORDER BY date").fetchall()
    assert [(str(r[0]), r[1]) for r in rows] == [("2025-12-30", 10), ("2025-12-31", 15), ("2026-01-01", 12)]
    assert rows[1][2] == "20260102T000000Z"


def test_gold_container_ports_ma7_and_shape(tmp_path):
    # port1 has 7 contiguous days; port2 only 6
    base = dt.date(2026, 1, 1)
    feats = [port_feat((base + dt.timedelta(days=i)).isoformat(), "port1", container=i + 1) for i in range(7)]
    feats += [port_feat((base + dt.timedelta(days=i)).isoformat(), "port2", container=3) for i in range(6)]
    write_pages(tmp_path, "20260105T000000Z", feats)
    con = duckdb.connect()
    run_model(con, "silver_port_daily", bronze_glob=str(tmp_path / "run=*" / "page-*.json.gz"))
    con.execute("CREATE TABLE silver_dim_port AS SELECT * FROM (VALUES ('port1','One','A','AAA','One',1.0::DOUBLE,2.0::DOUBLE),"
                "('port2','Two','B','BBB','Two',3.0::DOUBLE,4.0::DOUBLE)) t(portid, portname, country, iso3, fullname, lat, lon)")
    con.execute("CREATE TABLE silver_container_port_rank AS SELECT * FROM (VALUES ('port1','One','A',500,1),"
                "('port2','Two','B',400,2)) t(portid, portname, country, calls_365d, rank)")
    run_model(con, "gold_container_port_selection", top_n="2", preset_ids="'port1'")
    run_model(con, "gold_container_ports")
    rows = {r[0]: r for r in con.execute("SELECT portid, ma7_container, rank, calls_365d, lat, lon FROM gold_container_ports").fetchall()}
    assert rows["port1"][1] == pytest.approx(4.0)  # mean of 1..7
    assert rows["port2"][1] is None  # fewer than 7 observations
    assert rows["port1"][2:] == (1, 500, 1.0, 2.0)

    # gold JSON shape
    con.execute("CREATE TABLE silver_container_od AS SELECT * FROM (VALUES "
                "('port1','port2','open',100.4,'v'),('port2','port1','open',101.0,'v'),"
                "('port1','port1','open',0.0,'v'),('port2','port2','open',0.0,'v'),"
                "('port1','port2','panama_closed',NULL::DOUBLE,'v')) t(origin, dest, scenario, nm, searoute_version)")
    run_model(con, "gold_container_od")
    out = tmp_path / "gold"
    build.export_container(con, out)
    ports = json.loads((out / "container_ports.json").read_text())
    assert [p["portid"] for p in ports] == ["port1", "port2"]
    assert set(ports[0]) == {"portid", "portname", "country", "lat", "lon", "rank", "calls_365d", "ma7_container", "latest_date"}
    doc = json.loads((out / "container_od.json").read_text())
    assert doc["speed_kn"] == 16 and doc["ports"] == ["port1", "port2"]
    assert [s["id"] for s in doc["scenarios"]] == ["open", "red_sea_closed", "panama_closed"]
    assert doc["scenarios"][1]["restrictions"] == ["northwest", "suez", "babalmandab"]
    assert doc["nm"]["open"] == [[0, 100], [101, 0]]  # integers
    assert doc["nm"]["panama_closed"][0][1] is None  # no path -> null
    assert set(doc["nm"]) == {"open", "red_sea_closed", "panama_closed"}


def test_od_cache_reuse_second_run_computes_nothing(tmp_path, monkeypatch):
    ports = [("a", 0.0, 0.0), ("b", 10.0, 10.0), ("c", 20.0, 0.0)]
    calls = []

    def fake_route(a, b, restrictions):
        calls.append((tuple(a), tuple(b), tuple(restrictions)))
        # pretend: panama_closed has no path to c
        return float("inf") if "panama" in restrictions and b == [20.0, 0.0] else 100.0

    todo = od.missing_pairs(ports, set())
    assert len(todo) == 3 * 3 * 3
    rows = od.compute(ports, todo, route=fake_route)
    assert len(calls) == 3 * 2 * 3  # the diagonal is 0 and never routed
    assert {r[3] for r in rows if r[0] == r[1]} == {0.0}
    assert [r for r in rows if r[2] == "panama_closed" and r[1] == "c" and r[0] != "c"][0][3] is None

    cached = {(r[0], r[1], r[2]) for r in rows}
    assert od.missing_pairs(ports, cached) == []  # second run computes 0 pairs
    # a new port only costs its own pairs
    extra = od.missing_pairs(ports + [("d", 5.0, 5.0)], cached)
    assert len(extra) == (4 * 4 - 3 * 3) * 3


def test_container_checks(tmp_path):
    con = duckdb.connect()
    con.execute("CREATE TABLE silver_port_daily AS SELECT 'p1' portid, DATE '2026-01-01' date")
    con.execute("CREATE TABLE gold_container_ports AS SELECT * FROM (VALUES ('p1', 1.0, 2.0, DATE '2026-01-01'),"
                "('p2', NULL::DOUBLE, NULL::DOUBLE, DATE '2026-01-01')) t(portid, lat, lon, latest_date)")
    con.execute("CREATE TABLE silver_container_od AS SELECT 'p1' origin, 'p1' dest, 'open' scenario, 0.0 nm")
    res = {n: ok for n, ok, _ in check.container_checks(con)}
    assert res["no duplicate (portid, date) in silver_port_daily"] is True
    assert res["every selected port has lat/lon"] is False
    assert res["OD has every (origin, dest, scenario) for the selected ports"] is False
