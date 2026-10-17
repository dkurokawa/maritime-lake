"""bronze -> silver -> gold."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import duckdb
import yaml

from . import config
from .models import run_model

GOLD_SERIES_FIELDS = ["n_container", "n_dry_bulk", "n_general_cargo", "n_roro", "n_tanker"]


def _glob(source: str) -> str:
    return str(config.BRONZE / source / "run=*" / "page-*.json.gz")


def build_silver(con: duckdb.DuckDBPyConnection) -> None:
    run_model(con, "silver_chokepoint_daily", bronze_glob=_glob("chokepoint_daily"))
    run_model(con, "silver_dim_chokepoint", bronze_glob=_glob("chokepoints"))
    run_model(con, "silver_disruptions", bronze_glob=_glob("disruptions"))
    out = config.SILVER
    out.mkdir(parents=True, exist_ok=True)
    con.execute(
        f"COPY (SELECT * FROM silver_chokepoint_daily) TO '{out / 'chokepoint_daily'}' "
        "(FORMAT parquet, PARTITION_BY (year), OVERWRITE_OR_IGNORE)"
    )
    con.execute(f"COPY silver_dim_chokepoint TO '{out / 'dim_chokepoint.parquet'}' (FORMAT parquet)")
    con.execute(f"COPY silver_disruptions TO '{out / 'disruptions.parquet'}' (FORMAT parquet)")


def build_gold_tables(con: duckdb.DuckDBPyConnection) -> None:
    run_model(con, "gold_chokepoint_daily")
    run_model(con, "gold_chokepoint_latest")


def _round(v, nd=3):
    return round(v, nd) if isinstance(v, float) else v


def _rows(con, sql: str) -> list[dict]:
    cur = con.execute(sql)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, (_round(x) for x in r))) for r in cur.fetchall()]


def load_curated_events(path: Path = config.EVENTS_YAML) -> list[dict]:
    raw = yaml.safe_load(path.read_text())["events"]
    out = []
    for e in raw:
        out.append(
            {
                "id": e["id"],
                "kind": "curated",
                "title": e["title"],
                "start": str(e["start"]),
                "end": str(e["end"]) if e.get("end") else None,
                "chokepoints": e["chokepoints"],
                "summary": e["summary"],
                "source_url": e["source_url"],
            }
        )
    return out


def export_gold(con: duckdb.DuckDBPyConnection, out: Path | None = None) -> dict:
    out = out or config.GOLD
    (out / "series").mkdir(parents=True, exist_ok=True)

    # parquet for DuckDB-over-HTTPS consumers
    con.execute(f"COPY gold_chokepoint_daily TO '{out / 'chokepoint_daily.parquet'}' (FORMAT parquet)")
    con.execute(f"COPY gold_chokepoint_latest TO '{out / 'chokepoint_latest.parquet'}' (FORMAT parquet)")

    latest = _rows(con, "SELECT * FROM gold_chokepoint_latest")
    features = []
    for r in latest:
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [r["lon"], r["lat"]]},
                "properties": {
                    "portid": r["portid"],
                    "name": r["portname"],
                    "date": str(r["date"]),
                    "n_total": r["n_total"],
                    "ma7_total": r["ma7_total"],
                    "ma7_capacity": r["ma7_capacity"],
                    "yoy_pct": r["yoy_pct"],
                    "baseline_ma7": r["baseline_ma7"],
                    "vs_baseline_pct": r["vs_baseline_pct"],
                },
            }
        )
    (out / "chokepoints.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": features}))

    ports = [r[0] for r in con.execute("SELECT DISTINCT portid FROM gold_chokepoint_daily ORDER BY 1").fetchall()]
    for pid in ports:
        rows = _rows(
            con,
            f"SELECT * FROM gold_chokepoint_daily WHERE portid = '{pid}' ORDER BY date",
        )
        series = {
            "portid": pid,
            "name": rows[0]["portname"],
            "dates": [str(r["date"]) for r in rows],
            "n_total": [r["n_total"] for r in rows],
            "n_by_type": {f.removeprefix("n_"): [r[f] for r in rows] for f in GOLD_SERIES_FIELDS},
            "ma7_total": [r["ma7_total"] for r in rows],
            "baseline_ma7": [r["baseline_ma7"] for r in rows],
            "baseline_lo": [r["baseline_lo"] for r in rows],
            "baseline_hi": [r["baseline_hi"] for r in rows],
            "capacity": [r["capacity"] for r in rows],
        }
        (out / "series" / f"{pid}.json").write_text(json.dumps(series, separators=(",", ":")))

    events = load_curated_events()
    known = set(ports)
    for e in events:
        bad = [c for c in e["chokepoints"] if c not in known]
        if bad:
            raise ValueError(f"event {e['id']} references unknown chokepoints {bad}")
    for r in _rows(con, "SELECT * FROM silver_disruptions"):
        cps = sorted({p.strip() for p in (r["affectedports"] or "").split(",") if p.strip() in known})
        events.append(
            {
                "id": f"pw-{r['eventid']}",
                "kind": "disruption",
                "title": r["title"],
                "start": str(r["start_date"]),
                "end": str(r["end_date"]) if r["end_date"] else None,
                "chokepoints": cps,
                "summary": r["description"],
                "source_url": "https://portwatch.imf.org/",
                "event_type": r["eventtype"],
            }
        )
    (out / "events.json").write_text(json.dumps(events, separators=(",", ":")))

    counts = {
        "silver_chokepoint_daily": con.execute("SELECT count(*) FROM silver_chokepoint_daily").fetchone()[0],
        "silver_disruptions": con.execute("SELECT count(*) FROM silver_disruptions").fetchone()[0],
        "silver_dim_chokepoint": con.execute("SELECT count(*) FROM silver_dim_chokepoint").fetchone()[0],
        "gold_chokepoint_daily": con.execute("SELECT count(*) FROM gold_chokepoint_daily").fetchone()[0],
        "events": len(events),
    }
    data_max = con.execute("SELECT max(date) FROM silver_chokepoint_daily").fetchone()[0]
    meta = {
        "built_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "data_max_date": str(data_max),
        "row_counts": counts,
        "attribution": config.ATTRIBUTION,
        "note": "Values are modelled estimates derived from AIS data and may be revised.",
        "source": "https://portwatch.imf.org/",
    }
    (out / "meta.json").write_text(json.dumps(meta, indent=2))
    return counts


def write_state(counts: dict) -> None:
    config.STATE.mkdir(parents=True, exist_ok=True)
    path = config.STATE / "build_state.json"
    prev = None
    if path.exists():
        prev = json.loads(path.read_text()).get("current")
    path.write_text(json.dumps({"previous": prev, "current": counts}, indent=2))


def run() -> dict:
    con = duckdb.connect()
    build_silver(con)
    build_gold_tables(con)
    counts = export_gold(con)
    write_state(counts)
    print("build done:", counts)
    return counts
