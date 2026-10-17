from __future__ import annotations

import datetime as dt
import gzip
import json
from pathlib import Path

import duckdb
import pytest

from mlake.models import run_model

START = dt.date(2019, 1, 1)
END = dt.date(2024, 3, 15)


def value(d: dt.date) -> int:
    """Deterministic synthetic daily total: trend by year + weekday wiggle."""
    return 100 + (d.year - 2019) * 10 + d.weekday() * 3 + (d.timetuple().tm_yday % 5)


def make_silver(con, port="chokepointT", include=lambda d: True, fn=value):
    con.execute(
        """CREATE OR REPLACE TABLE silver_chokepoint_daily (
            date DATE, year INTEGER, portid VARCHAR, portname VARCHAR,
            n_container BIGINT, n_dry_bulk BIGINT, n_general_cargo BIGINT, n_roro BIGINT, n_tanker BIGINT,
            n_cargo BIGINT, n_total BIGINT,
            capacity_container BIGINT, capacity_dry_bulk BIGINT, capacity_general_cargo BIGINT,
            capacity_roro BIGINT, capacity_tanker BIGINT, capacity_cargo BIGINT, capacity BIGINT,
            ingested_run VARCHAR)"""
    )
    d = START
    rows = []
    while d <= END:
        if include(d):
            n = fn(d)
            rows.append((d, d.year, port, "Test Strait", n, 0, 0, 0, 0, n, n, n * 1000, 0, 0, 0, 0, n * 1000, n * 1000, "r"))
        d += dt.timedelta(days=1)
    con.executemany("INSERT INTO silver_chokepoint_daily VALUES (" + ",".join(["?"] * 19) + ")", rows)


@pytest.fixture(scope="module")
def gold():
    con = duckdb.connect()
    make_silver(con)
    con.execute(
        "CREATE TABLE silver_dim_chokepoint AS SELECT 'chokepointT' portid, 'Test Strait' portname, 'Test Strait' fullname, 1.5 lat, 2.5 lon"
    )
    run_model(con, "gold_chokepoint_daily")
    run_model(con, "gold_chokepoint_latest")
    return con


def row(con, d: dt.date):
    cur = con.execute("SELECT * FROM gold_chokepoint_daily WHERE date = ?", [d])
    cols = [c[0] for c in cur.description]
    return dict(zip(cols, cur.fetchone()))


def ma7(d):
    return sum(value(d - dt.timedelta(days=i)) for i in range(7)) / 7


def test_ma7_null_until_seven_days(gold):
    assert row(gold, dt.date(2019, 1, 6))["ma7_total"] is None
    assert row(gold, dt.date(2019, 1, 7))["ma7_total"] == pytest.approx(ma7(dt.date(2019, 1, 7)))
    assert row(gold, dt.date(2022, 6, 15))["ma7_total"] == pytest.approx(ma7(dt.date(2022, 6, 15)))
    # capacity moves with total in the fixture (x1000)
    assert row(gold, dt.date(2022, 6, 15))["ma7_capacity"] == pytest.approx(ma7(dt.date(2022, 6, 15)) * 1000)


def test_yoy(gold):
    d = dt.date(2023, 5, 10)
    expect = 100 * (ma7(d) / ma7(dt.date(2022, 5, 10)) - 1)
    assert row(gold, d)["yoy_pct"] == pytest.approx(expect)
    # no previous year available in 2019
    assert row(gold, dt.date(2019, 6, 1))["yoy_pct"] is None
    # Feb 29 2024 compares with Feb 28 2023
    expect_leap = 100 * (ma7(dt.date(2024, 2, 29)) / ma7(dt.date(2023, 2, 28)) - 1)
    assert row(gold, dt.date(2024, 2, 29))["yoy_pct"] == pytest.approx(expect_leap)


def test_baseline_is_mean_of_2019_2022_same_day(gold):
    d = dt.date(2024, 3, 1)
    years = [dt.date(y, 3, 1) for y in range(2019, 2023)]
    expect = sum(ma7(x) for x in years) / 4
    r = row(gold, d)
    assert r["baseline_ma7"] == pytest.approx(expect)
    assert r["baseline_lo"] == pytest.approx(min(ma7(x) for x in years))
    assert r["baseline_hi"] == pytest.approx(max(ma7(x) for x in years))
    assert r["vs_baseline_pct"] == pytest.approx(100 * (ma7(d) / expect - 1))


def test_feb29_maps_to_feb28_and_does_not_pollute_baseline(gold):
    feb28_years = [dt.date(y, 2, 28) for y in range(2019, 2023)]
    expect = sum(ma7(x) for x in feb28_years) / 4  # 2020-02-29 must not be averaged in
    assert row(gold, dt.date(2024, 2, 28))["baseline_ma7"] == pytest.approx(expect)
    assert row(gold, dt.date(2024, 2, 29))["baseline_ma7"] == pytest.approx(expect)
    # and the Feb 29 row itself exists in the baseline years (2020) but is not a baseline key
    assert row(gold, dt.date(2020, 2, 29))["baseline_ma7"] == pytest.approx(expect)


def test_baseline_early_january_excludes_incomplete_ma7(gold):
    # Jan 1-6 2019 have NULL ma7, so their baseline averages only 2020-2022
    d = dt.date(2024, 1, 3)
    expect = sum(ma7(dt.date(y, 1, 3)) for y in range(2020, 2023)) / 3
    assert row(gold, d)["baseline_ma7"] == pytest.approx(expect)


def test_latest_one_row_per_chokepoint(gold):
    rows = gold.execute("SELECT portid, date, lat, lon, ma7_total FROM gold_chokepoint_latest").fetchall()
    assert len(rows) == 1
    assert rows[0][1] == END and rows[0][2] == 1.5 and rows[0][3] == 2.5
    assert rows[0][4] == pytest.approx(ma7(END))


def test_ma7_uses_calendar_window_when_dates_missing():
    con = duckdb.connect()
    # drop one day: window of 7 calendar days then has only 6 observations -> NULL
    missing = dt.date(2019, 3, 10)
    make_silver(con, include=lambda d: d != missing)
    con.execute("CREATE TABLE silver_dim_chokepoint AS SELECT 'chokepointT' portid, 'x' portname, 'x' fullname, 0.0 lat, 0.0 lon")
    run_model(con, "gold_chokepoint_daily")
    assert row(con, dt.date(2019, 3, 12))["ma7_total"] is None
    assert row(con, dt.date(2019, 3, 17))["ma7_total"] is not None


# ---------- silver: dedupe / upsert from bronze ----------

def write_page(base: Path, run: str, feats: list[dict]) -> None:
    d = base / f"run={run}"
    d.mkdir(parents=True)
    with gzip.open(d / "page-0000.json.gz", "wt") as fh:
        json.dump({"features": [{"attributes": f} for f in feats]}, fh)


def feat(date, portid="chokepoint1", n_total=10, **kw):
    base = dict(
        date=date, portid=portid, portname="Suez Canal",
        n_container=n_total, n_dry_bulk=0, n_general_cargo=0, n_roro=0, n_tanker=0,
        n_cargo=n_total, n_total=n_total,
        capacity_container=1, capacity_dry_bulk=0, capacity_general_cargo=0, capacity_roro=0,
        capacity_tanker=0, capacity_cargo=1, capacity=1,
    )
    base.update(kw)
    return base


def test_silver_upsert_latest_run_wins_and_dedupes(tmp_path):
    write_page(tmp_path, "20260101T000000Z", [feat("2025-12-30", n_total=10), feat("2025-12-31", n_total=11)])
    # later incremental run revises 12-31 and adds 2026-01-01
    write_page(tmp_path, "20260102T000000Z", [feat("2025-12-31", n_total=15), feat("2026-01-01", n_total=12)])
    con = duckdb.connect()
    run_model(con, "silver_chokepoint_daily", bronze_glob=str(tmp_path / "run=*" / "page-*.json.gz"))
    rows = con.execute("SELECT date, n_total, ingested_run FROM silver_chokepoint_daily ORDER BY date").fetchall()
    assert [(str(r[0]), r[1]) for r in rows] == [("2025-12-30", 10), ("2025-12-31", 15), ("2026-01-01", 12)]
    assert rows[1][2] == "20260102T000000Z"
    assert con.execute("SELECT count(*) FROM silver_chokepoint_daily").fetchone()[0] == 3


def test_silver_types(tmp_path):
    write_page(tmp_path, "20260101T000000Z", [feat("2025-12-30")])
    con = duckdb.connect()
    run_model(con, "silver_chokepoint_daily", bronze_glob=str(tmp_path / "run=*" / "page-*.json.gz"))
    types = dict(con.execute("SELECT column_name, data_type FROM information_schema.columns WHERE table_name='silver_chokepoint_daily'").fetchall())
    assert types["date"] == "DATE" and types["year"] in ("BIGINT", "INTEGER")
    assert types["n_total"] == "BIGINT"
