"""Data-quality checks. Non-zero exit on failure."""
from __future__ import annotations

import glob
import json

import duckdb

from . import config

CAPACITY_ROUNDING_TOLERANCE = 5  # tonnes; components are rounded individually in the source


def checks(con: duckdb.DuckDBPyConnection) -> list[tuple[str, bool, str]]:
    res: list[tuple[str, bool, str]] = []

    dup = con.execute(
        "SELECT count(*) FROM (SELECT portid, date FROM silver GROUP BY 1, 2 HAVING count(*) > 1)"
    ).fetchone()[0]
    res.append(("no duplicate (portid, date)", dup == 0, f"{dup} duplicate keys"))

    gaps = con.execute(
        """
        SELECT portid, min(date), max(date), count(*), datediff('day', min(date), max(date)) + 1
        FROM silver GROUP BY portid
        HAVING count(*) != datediff('day', min(date), max(date)) + 1
        """
    ).fetchall()
    res.append(("no missing dates per chokepoint", not gaps, f"{len(gaps)} chokepoints with gaps: {gaps[:3]}"))

    bad_total = con.execute(
        "SELECT count(*) FROM silver WHERE n_total != n_container + n_dry_bulk + n_general_cargo + n_roro + n_tanker"
    ).fetchone()[0]
    res.append(("n_total = sum of vessel-type counts", bad_total == 0, f"{bad_total} violating rows"))

    bad_cargo = con.execute(
        "SELECT count(*) FROM silver WHERE n_cargo != n_container + n_dry_bulk + n_general_cargo + n_roro"
    ).fetchone()[0]
    res.append(("n_cargo = sum of non-tanker counts", bad_cargo == 0, f"{bad_cargo} violating rows"))

    # capacity components are rounded independently in the source -> allow a few tonnes
    bad_cap = con.execute(
        f"""SELECT count(*) FROM silver WHERE abs(capacity - (capacity_container + capacity_dry_bulk
            + capacity_general_cargo + capacity_roro + capacity_tanker)) > {CAPACITY_ROUNDING_TOLERANCE}"""
    ).fetchone()[0]
    res.append((f"capacity = sum of type capacities (+/-{CAPACITY_ROUNDING_TOLERANCE} t)", bad_cap == 0, f"{bad_cap} violating rows"))

    nulls = con.execute("SELECT count(*) FROM silver WHERE n_total IS NULL OR capacity IS NULL").fetchone()[0]
    res.append(("no null n_total / capacity", nulls == 0, f"{nulls} rows"))

    neg = con.execute("SELECT count(*) FROM silver WHERE n_total < 0 OR capacity < 0").fetchone()[0]
    res.append(("no negative counts", neg == 0, f"{neg} rows"))

    ends = con.execute("SELECT count(DISTINCT max_d) FROM (SELECT max(date) max_d FROM silver GROUP BY portid)").fetchone()[0]
    res.append(("all chokepoints end on the same date", ends == 1, f"{ends} distinct end dates"))

    n_ports = con.execute("SELECT count(DISTINCT portid) FROM silver").fetchone()[0]
    res.append(("28 chokepoints present", n_ports == 28, f"{n_ports} found"))

    state_path = config.STATE / "build_state.json"
    if state_path.exists():
        st = json.loads(state_path.read_text())
        cur, prev = st["current"], st.get("previous")
        if prev:
            delta = cur["silver_chokepoint_daily"] - prev["silver_chokepoint_daily"]
            res.append(
                ("silver row count did not shrink vs previous build", delta >= 0,
                 f"delta {delta:+d} ({prev['silver_chokepoint_daily']} -> {cur['silver_chokepoint_daily']})")
            )
        else:
            res.append(("row-count delta vs previous build", True, "first build, nothing to compare"))
        actual = con.execute("SELECT count(*) FROM silver").fetchone()[0]
        res.append(("silver on disk matches last build", actual == cur["silver_chokepoint_daily"], f"{actual} vs {cur['silver_chokepoint_daily']}"))
    else:
        res.append(("build state present", False, "run `mlake build` first"))
    return res


def container_checks(con: duckdb.DuckDBPyConnection) -> list[tuple[str, bool, str]]:
    """Time-map checks. Expects views silver_port_daily, gold_container_ports, silver_container_od."""
    res: list[tuple[str, bool, str]] = []
    dup = con.execute(
        "SELECT count(*) FROM (SELECT portid, date FROM silver_port_daily GROUP BY 1, 2 HAVING count(*) > 1)"
    ).fetchone()[0]
    res.append(("no duplicate (portid, date) in silver_port_daily", dup == 0, f"{dup} duplicate keys"))

    no_ll = con.execute("SELECT count(*) FROM gold_container_ports WHERE lat IS NULL OR lon IS NULL").fetchone()[0]
    res.append(("every selected port has lat/lon", no_ll == 0, f"{no_ll} ports without coordinates"))

    n_ports = con.execute("SELECT count(*) FROM gold_container_ports").fetchone()[0]
    no_daily = con.execute(
        "SELECT count(*) FROM gold_container_ports WHERE latest_date IS NULL"
    ).fetchone()[0]
    res.append(("every selected port has daily port calls", no_daily == 0, f"{no_daily} of {n_ports} ports without data"))

    expected = n_ports * n_ports * len(config.SCENARIOS)
    missing = con.execute(
        """
        SELECT count(*) FROM (
            SELECT o.portid AS origin, d.portid AS dest, s.scenario
            FROM gold_container_ports o CROSS JOIN gold_container_ports d
            CROSS JOIN (SELECT unnest(?::VARCHAR[]) AS scenario) s
        ) x
        LEFT JOIN silver_container_od c USING (origin, dest, scenario)
        WHERE c.origin IS NULL
        """,
        [[sc["id"] for sc in config.SCENARIOS]],
    ).fetchone()[0]
    nopath = con.execute(
        """
        SELECT c.scenario, count(*) FROM silver_container_od c
        JOIN gold_container_ports o ON o.portid = c.origin JOIN gold_container_ports d ON d.portid = c.dest
        WHERE c.nm IS NULL GROUP BY 1 ORDER BY 1
        """
    ).fetchall()
    res.append(
        ("OD has every (origin, dest, scenario) for the selected ports", missing == 0,
         f"{missing} of {expected} missing; pairs with no path (NULL, allowed): {dict(nopath) or 'none'}")
    )
    return res


def run() -> int:
    files = glob.glob(str(config.SILVER / "chokepoint_daily" / "year=*" / "*.parquet"))
    if not files:
        print("check: no silver data; run `mlake build`")
        return 1
    con = duckdb.connect()
    con.execute(
        f"CREATE VIEW silver AS SELECT * FROM read_parquet('{config.SILVER}/chokepoint_daily/year=*/*.parquet', hive_partitioning=true)"
    )
    all_checks = checks(con)
    port_files = glob.glob(str(config.SILVER / "port_daily" / "year=*" / "*.parquet"))
    ports_pq = config.GOLD / "container_ports.parquet"
    if port_files and ports_pq.exists():
        con.execute(
            f"CREATE VIEW silver_port_daily AS SELECT * FROM read_parquet('{config.SILVER}/port_daily/year=*/*.parquet', hive_partitioning=true)"
        )
        con.execute(f"CREATE VIEW gold_container_ports AS SELECT * FROM read_parquet('{ports_pq}')")
        od_pq = config.SILVER / "container_od.parquet"
        if od_pq.exists():
            con.execute(f"CREATE VIEW silver_container_od AS SELECT * FROM read_parquet('{od_pq}')")
        else:
            con.execute(
                "CREATE VIEW silver_container_od AS SELECT ''::VARCHAR origin, ''::VARCHAR dest, ''::VARCHAR scenario, 0.0::DOUBLE nm WHERE false"
            )
        all_checks += container_checks(con)
    else:
        all_checks.append(("container time-map data present", False, "run `mlake ingest` and `mlake build`"))
    failed = 0
    for name, ok, detail in all_checks:
        print(f"[{'PASS' if ok else 'FAIL'}] {name} -- {detail}")
        failed += 0 if ok else 1
    print("check:", "FAILED" if failed else "all passed")
    return 1 if failed else 0
