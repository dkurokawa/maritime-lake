"""Shared fixture data for the pipeline tests: tiny ArcGIS-style bronze pages."""

from __future__ import annotations

import datetime as dt
import gzip
import json
from pathlib import Path
from typing import Any

import duckdb
import pytest
from mlake import config

PRESETS = list(config.PRESET_PORTS)
EXTRA_PORTS = ["portA", "portB", "portC"]
ALL_PORTS = PRESETS + EXTRA_PORTS
RUN = "20260110T000000Z"


def write_run(bronze: Path, source: str, run: str, feats: list[dict[str, Any]]) -> Path:
    """One gzipped page under bronze/<source>/run=<run>/ (ArcGIS features[].attributes shape)."""
    d = bronze / source / f"run={run}"
    d.mkdir(parents=True, exist_ok=True)
    with gzip.open(d / "page-0000.json.gz", "wt", encoding="utf-8") as fh:
        json.dump({"features": [{"attributes": f} for f in feats]}, fh)
    return d


def cp_feat(portid: str, date: dt.date, n: int = 10) -> dict[str, Any]:
    return {
        "date": date.isoformat(),
        "portid": portid,
        "portname": f"Strait {portid}",
        "n_container": n,
        "n_dry_bulk": 1,
        "n_general_cargo": 1,
        "n_roro": 1,
        "n_tanker": 2,
        "n_cargo": n + 3,
        "n_total": n + 5,
        "capacity_container": n * 1000,
        "capacity_dry_bulk": 100,
        "capacity_general_cargo": 100,
        "capacity_roro": 100,
        "capacity_tanker": 200,
        "capacity_cargo": n * 1000 + 300,
        "capacity": n * 1000 + 500,
    }


def port_feat(portid: str, date: dt.date, container: int = 10) -> dict[str, Any]:
    return {
        "date": date.isoformat(),
        "portid": portid,
        "portname": f"Port {portid}",
        "country": "Testland",
        "portcalls_container": container,
        "portcalls_dry_bulk": 0,
        "portcalls_general_cargo": 0,
        "portcalls_roro": 0,
        "portcalls_tanker": 0,
        "portcalls_cargo": container,
        "portcalls": container,
    }


def write_full_bronze(bronze: Path, run: str = RUN, days: int = 20) -> None:
    """A complete, consistent bronze layer: 28 chokepoints, 8 container ports, 2 disruptions."""
    start = dt.date(2024, 1, 1)
    cps = [f"chokepoint{i}" for i in range(1, 29)]
    write_run(
        bronze,
        "chokepoint_daily",
        run,
        [cp_feat(p, start + dt.timedelta(days=i), 10 + i) for p in cps for i in range(days)],
    )
    write_run(
        bronze,
        "chokepoints",
        run,
        [
            {
                "portid": p,
                "portname": f"Strait {p}",
                "fullname": f"Full {p}",
                "lat": 1.0,
                "lon": 2.0,
            }
            for p in cps
        ],
    )
    ms = int(dt.datetime(2024, 1, 5, tzinfo=dt.UTC).timestamp() * 1000)
    write_run(
        bronze,
        "disruptions",
        run,
        [
            {
                "eventid": 1,
                "eventtype": "Cyclone",
                "eventname": "c1",
                "htmlname": "Cyclone one",
                "htmldescription": "desc",
                "alertlevel": "RED",
                "country": "X",
                "fromdate": ms,
                "todate": ms + 3 * 86400 * 1000,
                "affectedports": "chokepoint1, chokepoint3, port999",
                "n_affectedports": 3,
                "lat": 0.0,
                "long": 0.0,
            },
            {
                "eventid": 2,
                "eventtype": "Flood",
                "eventname": "f1",
                "htmlname": "Flood two",
                "htmldescription": "desc",
                "alertlevel": "ORANGE",
                "country": "Y",
                "fromdate": ms,
                "todate": None,
                "affectedports": None,
                "n_affectedports": 0,
                "lat": 0.0,
                "long": 0.0,
            },
        ],
    )
    write_run(
        bronze,
        "ports",
        run,
        [
            {
                "portid": p,
                "portname": f"Port {p}",
                "country": "Testland",
                "ISO3": "TST",
                "fullname": f"Port {p}, Testland",
                "lat": 10.0 + i,
                "lon": 20.0 + i,
            }
            for i, p in enumerate(ALL_PORTS)
        ],
    )
    write_run(
        bronze,
        "ports_top",
        run,
        [
            {"portid": p, "portname": f"Port {p}", "country": "Testland", "calls": 1000 - i}
            for i, p in enumerate(ALL_PORTS)
        ],
    )
    write_run(
        bronze,
        "port_daily",
        run,
        [port_feat(p, start + dt.timedelta(days=i), 5 + i) for p in ALL_PORTS for i in range(days)],
    )


def point_lake_at(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    """Redirect every pipeline path into `root` so tests never touch the repo's data/."""
    monkeypatch.setattr(config, "DATA", root)
    monkeypatch.setattr(config, "BRONZE", root / "bronze")
    monkeypatch.setattr(config, "SILVER", root / "silver")
    monkeypatch.setattr(config, "GOLD", root / "gold")
    monkeypatch.setattr(config, "STATE", root / "state")


def write_silver_parquet(table: str, year: int, rows: str) -> None:
    """Minimal silver partition (date, portid) at silver/<table>/year=<year>/d.parquet."""
    part = config.SILVER / table / f"year={year}"
    part.mkdir(parents=True)
    duckdb.connect().execute(
        f"COPY (SELECT * FROM (VALUES {rows}) t(date, portid)) "
        f"TO '{part / 'd.parquet'}' (FORMAT parquet)"
    )
