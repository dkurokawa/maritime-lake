"""Origin-destination sailing distances (nautical miles) between the selected container ports.

searoute-py computes the shortest sea path on its Marnet graph; every ordered pair of ports is run
once per scenario and cached in data/silver/container_od.parquet. Only pairs missing from the cache
are computed, so re-runs are free and a changed port selection only costs the new pairs.
"""

from __future__ import annotations

import time
import warnings
from collections.abc import Callable, Sequence
from importlib.metadata import version
from typing import Any

import duckdb

from . import build, config
from .models import run_model

CACHE_COLUMNS = (
    "origin VARCHAR, dest VARCHAR, scenario VARCHAR, nm DOUBLE, searoute_version VARCHAR"
)


def selected_ports(con: duckdb.DuckDBPyConnection) -> list[tuple[str, float, float]]:
    rows = con.execute(
        "SELECT portid, lon, lat FROM gold_container_ports ORDER BY rank NULLS LAST, portid"
    ).fetchall()
    missing = [r[0] for r in rows if r[1] is None or r[2] is None]
    if missing:
        raise RuntimeError(f"selected ports without coordinates: {missing}")
    return rows


def missing_pairs(
    ports: list[tuple[str, float, float]], cached: set[tuple[str, str, str]]
) -> list[tuple[str, str, str]]:
    return [
        (o[0], d[0], sc["id"])
        for sc in config.SCENARIOS
        for o in ports
        for d in ports
        if (o[0], d[0], sc["id"]) not in cached
    ]


def compute(
    ports: list[tuple[str, float, float]],
    todo: list[tuple[str, str, str]],
    route: Callable[[Sequence[float], Sequence[float], list[str]], Any] | None = None,
    marnet: Any = None,
    ports_graph: Any = None,
) -> list[tuple[str, str, str, float | None, str]]:
    """Run searoute for each (origin, dest, scenario); nm is None when no path exists."""
    if route is None:
        import searoute
        from searoute.searoute import setup_M, setup_P

        # build the graph once, reuse it
        marnet, ports_graph = setup_M("networkx"), setup_P("networkx")

        def route(a: Sequence[float], b: Sequence[float], restrictions: list[str]) -> Any:
            f = searoute.searoute(
                a, b, units="naut", restrictions=restrictions, M=marnet, P=ports_graph
            )
            return f.properties["length"]

    ver = version("searoute")
    coord = {p[0]: [p[1], p[2]] for p in ports}
    restr = {sc["id"]: sc["restrictions"] for sc in config.SCENARIOS}
    out = []
    t0 = time.time()
    for i, (o, d, sc) in enumerate(todo, 1):
        if o == d:
            nm: float | None = 0.0
        else:
            with warnings.catch_warnings():
                # searoute warns on "no path"; we store NULL instead
                warnings.simplefilter("ignore")
                length = route(coord[o], coord[d], restr[sc])
            nm = (
                None
                if length is None or length == float("inf") or length != length
                else float(length)
            )
        out.append((o, d, sc, nm, ver))
        if i % 1000 == 0 or i == len(todo):
            print(f"  od {i}/{len(todo)} ({time.time() - t0:.0f}s)")
    return out


def run() -> dict[str, int]:
    gold_ports = config.GOLD / "container_ports.parquet"
    if not gold_ports.exists():
        raise SystemExit("od: no gold/container_ports.parquet; run `mlake build` first")
    con = duckdb.connect()
    con.execute(f"CREATE TABLE gold_container_ports AS SELECT * FROM read_parquet('{gold_ports}')")
    ports = selected_ports(con)

    cache_path = config.SILVER / "container_od.parquet"
    build.load_container_od(con)
    cached = {
        tuple(r)
        for r in con.execute("SELECT origin, dest, scenario FROM silver_container_od").fetchall()
    }
    todo = missing_pairs(ports, cached)
    print(
        f"od: {len(ports)} ports x {len(config.SCENARIOS)} scenarios, {len(todo)} pairs to "
        f"compute, {len(cached)} cached"
    )
    if todo:
        rows = compute(ports, todo)
        con.executemany("INSERT INTO silver_container_od VALUES (?, ?, ?, ?, ?)", rows)
        tmp = cache_path.with_suffix(".parquet.tmp")
        con.execute(
            f"COPY (SELECT * FROM silver_container_od ORDER BY scenario, origin, dest) TO "
            f"'{tmp}' (FORMAT parquet)"
        )
        tmp.replace(cache_path)
    run_model(con, "gold_container_od")
    counts = build.export_container(con)
    nopath = con.execute(
        "SELECT scenario, count(*) FILTER (WHERE nm IS NULL) FROM gold_container_od GROUP "
        "BY 1 ORDER BY 1"
    ).fetchall()
    print("od: pairs with no path:", dict(nopath), "| computed this run:", len(todo))
    return {"computed": len(todo), **counts}
