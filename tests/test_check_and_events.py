from __future__ import annotations

import datetime as dt
from pathlib import Path

import duckdb
from mlake import build, check
from test_models import make_silver

ROOT = Path(__file__).resolve().parents[1]


def silver_con(**kw):
    con = duckdb.connect()
    make_silver(con, **kw)
    con.execute("CREATE VIEW silver AS SELECT * FROM silver_chokepoint_daily")
    return con


def results(con):
    return {name: ok for name, ok, _ in check.checks(con)}


def test_check_detects_gap():
    con = silver_con(include=lambda d: d != dt.date(2020, 5, 5))
    assert results(con)["no missing dates per chokepoint"] is False


def test_check_detects_duplicate():
    con = silver_con()
    con.execute(
        "INSERT INTO silver_chokepoint_daily SELECT * FROM silver_chokepoint_daily WHERE "
        "date = DATE '2020-01-01'"
    )
    assert results(con)["no duplicate (portid, date)"] is False


def test_check_detects_broken_total_invariant():
    con = silver_con()
    con.execute(
        "UPDATE silver_chokepoint_daily SET n_total = n_total + 1 WHERE date = DATE '2020-01-01'"
    )
    assert results(con)["n_total = sum of vessel-type counts"] is False


def test_curated_events_are_valid():
    events = build.load_curated_events(ROOT / "events" / "events.yaml")
    assert events
    ids = [e["id"] for e in events]
    assert len(ids) == len(set(ids))
    for e in events:
        assert e["source_url"].startswith("https://")
        assert e["chokepoints"]
        assert dt.date.fromisoformat(e["start"])
        if e["end"]:
            assert e["end"] >= e["start"]
