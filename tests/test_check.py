from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest
from lakefix import write_full_bronze
from mlake import build, check, config
from test_models import make_silver

_TEMPLATE: dict[str, Path] = {}


@pytest.fixture(autouse=True, scope="module")
def _silver_template(tmp_path_factory: pytest.TempPathFactory) -> None:
    """make_silver inserts ~1900 rows one by one (slow); do it once and reload from parquet."""
    path = tmp_path_factory.mktemp("silver") / "template.parquet"
    src = duckdb.connect()
    make_silver(src)
    src.execute(f"COPY silver_chokepoint_daily TO '{path}' (FORMAT parquet)")
    _TEMPLATE["path"] = path


def silver_con() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute(
        f"CREATE TABLE silver_chokepoint_daily AS SELECT * FROM read_parquet('{_TEMPLATE['path']}')"
    )
    con.execute("CREATE VIEW silver AS SELECT * FROM silver_chokepoint_daily")
    return con


def by_name(con: duckdb.DuckDBPyConnection) -> dict[str, tuple[bool, str]]:
    return {name: (ok, detail) for name, ok, detail in check.checks(con)}


def n_rows(con: duckdb.DuckDBPyConnection) -> int:
    return int(con.execute("SELECT count(*) FROM silver").fetchone()[0])  # type: ignore[index]


def test_clean_synthetic_silver_passes_the_data_checks_but_not_28_chokepoints(lake: Path) -> None:
    con = silver_con()
    res = by_name(con)
    failing = {n for n, (ok, _) in res.items() if not ok}
    # one synthetic chokepoint, no build state: exactly these two are expected to fail
    assert failing == {"28 chokepoints present", "build state present"}
    assert res["28 chokepoints present"][1] == "1 found"


def test_detects_cargo_capacity_null_negative_and_end_date_problems(lake: Path) -> None:
    con = silver_con()
    con.execute(
        "UPDATE silver_chokepoint_daily SET n_cargo = n_cargo + 1 WHERE date = DATE '2020-01-01'"
    )
    con.execute(
        "UPDATE silver_chokepoint_daily SET capacity = capacity + 6 WHERE date = DATE '2020-01-02'"
    )
    con.execute("UPDATE silver_chokepoint_daily SET n_total = NULL WHERE date = DATE '2020-01-03'")
    res = by_name(con)
    assert res["n_cargo = sum of non-tanker counts"][0] is False
    assert res["no null n_total / capacity"] == (False, "1 rows")
    cap_name = next(n for n in res if n.startswith("capacity = sum of type capacities"))
    assert res[cap_name][0] is False  # off by more than the +/-5 t rounding tolerance

    con.execute("UPDATE silver_chokepoint_daily SET n_total = -1 WHERE date = DATE '2020-01-04'")
    assert by_name(con)["no negative counts"] == (False, "1 rows")


def test_capacity_rounding_within_tolerance_is_accepted(lake: Path) -> None:
    con = silver_con()
    con.execute("UPDATE silver_chokepoint_daily SET capacity = capacity + 5")
    cap_name = next(n for n in by_name(con) if n.startswith("capacity = sum of type capacities"))
    assert by_name(con)[cap_name][0] is True


def test_chokepoints_ending_on_different_dates_fail(lake: Path) -> None:
    con = silver_con()
    con.execute(
        "INSERT INTO silver_chokepoint_daily "
        "SELECT * REPLACE ('short' AS portid) FROM silver_chokepoint_daily "
        "WHERE date < DATE '2024-03-01'"
    )
    ok, detail = by_name(con)["all chokepoints end on the same date"]
    assert ok is False and detail == "2 distinct end dates"


def write_state(current: int, previous: int | None) -> None:
    config.STATE.mkdir(parents=True, exist_ok=True)
    prev = None if previous is None else {"silver_chokepoint_daily": previous}
    (config.STATE / "build_state.json").write_text(
        json.dumps({"previous": prev, "current": {"silver_chokepoint_daily": current}})
    )


def test_build_state_first_build(lake: Path) -> None:
    con = silver_con()
    write_state(current=n_rows(con), previous=None)
    res = by_name(con)
    assert res["row-count delta vs previous build"] == (True, "first build, nothing to compare")
    assert res["silver on disk matches last build"][0] is True


def test_build_state_shrink_and_mismatch_are_flagged(lake: Path) -> None:
    con = silver_con()
    rows = n_rows(con)
    write_state(current=rows - 10, previous=rows + 5)
    res = by_name(con)
    ok, detail = res["silver row count did not shrink vs previous build"]
    assert ok is False and detail == f"delta -15 ({rows + 5} -> {rows - 10})"
    assert res["silver on disk matches last build"] == (False, f"{rows} vs {rows - 10}")


def test_build_state_growth_passes(lake: Path) -> None:
    con = silver_con()
    rows = n_rows(con)
    write_state(current=rows, previous=rows - 7)
    ok, detail = by_name(con)["silver row count did not shrink vs previous build"]
    assert ok is True and detail.startswith("delta +7")


def test_run_without_silver_fails_with_hint(lake: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert check.run() == 1
    assert "no silver data; run `mlake build`" in capsys.readouterr().out


def test_run_reports_missing_container_data(lake: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_full_bronze(config.BRONZE)
    build.build_silver(duckdb.connect())  # silver only; no gold, so no container_ports.parquet
    build.write_state({"silver_chokepoint_daily": 28 * 20})
    assert check.run() == 1
    out = capsys.readouterr().out
    assert "[FAIL] container time-map data present -- run `mlake ingest` and `mlake build`" in out
    assert "[PASS] no duplicate (portid, date) --" in out
    assert "check: FAILED" in out


def test_run_without_od_cache_uses_empty_view_and_reports_all_pairs_missing(
    lake: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_full_bronze(config.BRONZE)
    build.run()  # builds silver + gold; the OD cache (silver/container_od.parquet) is absent
    assert not (config.SILVER / "container_od.parquet").exists()
    assert check.run() == 1
    out = capsys.readouterr().out
    assert (
        "[FAIL] OD has every (origin, dest, scenario) for the selected ports -- 192 of 192 missing"
        in out
    )
    assert "[PASS] every selected port has lat/lon" in out
