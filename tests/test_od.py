from __future__ import annotations

from pathlib import Path

import duckdb
import pytest
from mlake import od


def test_selected_ports_orders_by_rank_and_rejects_missing_coordinates() -> None:
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE gold_container_ports AS SELECT * FROM (VALUES "
        "('b', 2.0, 1.0, 2), ('a', 4.0, 3.0, 1), ('z', 0.0, 0.0, NULL::INTEGER)) "
        "t(portid, lon, lat, rank)"
    )
    assert od.selected_ports(con) == [("a", 4.0, 3.0), ("b", 2.0, 1.0), ("z", 0.0, 0.0)]

    con.execute("UPDATE gold_container_ports SET lat = NULL WHERE portid = 'b'")
    with pytest.raises(RuntimeError, match=r"without coordinates: \['b'\]"):
        od.selected_ports(con)


def test_compute_uses_real_searoute_graph_offline() -> None:
    # Rotterdam -> Singapore on the real Marnet graph (pure computation, no network)
    ports = [("rtm", 4.4, 51.9), ("sin", 103.8, 1.26)]
    todo = [("rtm", "sin", "open"), ("rtm", "sin", "red_sea_closed"), ("rtm", "rtm", "open")]
    rows = od.compute(ports, todo)
    by = {(o, d, s): nm for o, d, s, nm, _ in rows}
    assert 7500 < by[("rtm", "sin", "open")] < 9500
    assert by[("rtm", "sin", "red_sea_closed")] > by[("rtm", "sin", "open")] + 1500  # via the Cape
    assert by[("rtm", "rtm", "open")] == 0.0
    assert {r[4] for r in rows} == {rows[0][4]} and rows[0][4]  # searoute version recorded


def test_compute_stores_null_for_nan_none_and_inf() -> None:
    ports = [("a", 0.0, 0.0), ("b", 1.0, 1.0)]
    values = iter([float("nan"), None, float("inf"), 5.5])
    rows = od.compute(
        ports,
        [("a", "b", "open")] * 4,
        route=lambda a, b, r: next(values),
    )
    assert [r[3] for r in rows] == [None, None, None, 5.5]


def test_run_requires_build_first(lake: Path) -> None:
    with pytest.raises(SystemExit, match="run `mlake build` first"):
        od.run()
