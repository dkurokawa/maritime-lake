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
    run_model(con, "silver_dim_port", bronze_glob=_glob("ports"))
    run_model(con, "silver_container_port_rank", bronze_glob=_glob("ports_top"))
    run_model(con, "silver_port_daily", bronze_glob=_glob("port_daily"))
    out = config.SILVER
    out.mkdir(parents=True, exist_ok=True)
    con.execute(
        f"COPY (SELECT * FROM silver_chokepoint_daily) TO '{out / 'chokepoint_daily'}' "
        "(FORMAT parquet, PARTITION_BY (year), OVERWRITE_OR_IGNORE)"
    )
    con.execute(f"COPY silver_dim_chokepoint TO '{out / 'dim_chokepoint.parquet'}' (FORMAT parquet)")
    con.execute(f"COPY silver_disruptions TO '{out / 'disruptions.parquet'}' (FORMAT parquet)")
    con.execute(f"COPY silver_dim_port TO '{out / 'dim_port.parquet'}' (FORMAT parquet)")
    con.execute(f"COPY silver_container_port_rank TO '{out / 'container_port_rank.parquet'}' (FORMAT parquet)")
    con.execute(
        f"COPY (SELECT * FROM silver_port_daily) TO '{out / 'port_daily'}' "
        "(FORMAT parquet, PARTITION_BY (year), OVERWRITE_OR_IGNORE)"
    )


def build_gold_tables(con: duckdb.DuckDBPyConnection) -> None:
    run_model(con, "gold_chokepoint_daily")
    run_model(con, "gold_chokepoint_latest")
    run_model(
        con, "gold_container_port_selection",
        top_n=str(config.TOP_PORTS),
        preset_ids=", ".join(f"'{p}'" for p in config.PRESET_PORTS),
    )
    run_model(con, "gold_container_ports")
    load_container_od(con)
    run_model(con, "gold_container_od")


def load_container_od(con: duckdb.DuckDBPyConnection) -> None:
    """Expose the searoute cache (written by `mlake od`) as silver_container_od; empty if not computed yet."""
    path = config.SILVER / "container_od.parquet"
    if path.exists():
        con.execute(f"CREATE OR REPLACE TABLE silver_container_od AS SELECT * FROM read_parquet('{path}')")
    else:
        con.execute(
            "CREATE OR REPLACE TABLE silver_container_od (origin VARCHAR, dest VARCHAR, scenario VARCHAR, "
            "nm DOUBLE, searoute_version VARCHAR)"
        )


def _round(v, nd=3):
    return round(v, nd) if isinstance(v, float) else v


def _rows(con, sql: str) -> list[dict]:
    cur = con.execute(sql)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, (_round(x) for x in r))) for r in cur.fetchall()]


def export_container(con: duckdb.DuckDBPyConnection, out: Path | None = None) -> dict:
    """gold/container_ports.json (+ parquet) and, when the OD cache covers the ports, gold/container_od.json."""
    out = out or config.GOLD
    out.mkdir(parents=True, exist_ok=True)
    con.execute(f"COPY gold_container_ports TO '{out / 'container_ports.parquet'}' (FORMAT parquet)")
    ports = _rows(con, "SELECT * FROM gold_container_ports")
    for r in ports:
        for k in ("latest_date",):
            r[k] = str(r[k]) if r[k] is not None else None
    (out / "container_ports.json").write_text(json.dumps(ports, separators=(",", ":")))
    counts = {"gold_container_ports": len(ports)}

    ids = [r["portid"] for r in ports]
    od_rows = con.execute("SELECT origin, dest, scenario, nm FROM gold_container_od").fetchall()
    if od_rows:
        idx = {p: i for i, p in enumerate(ids)}
        nm: dict[str, list[list]] = {
            sc["id"]: [[None] * len(ids) for _ in ids] for sc in config.SCENARIOS
        }
        for o, d, sc, v in od_rows:
            if sc in nm and o in idx and d in idx:
                nm[sc][idx[o]][idx[d]] = None if v is None else round(v)
        doc = {
            "speed_kn": config.SPEED_KN,
            "ports": ids,
            "scenarios": config.SCENARIOS,
            "nm": nm,
        }
        (out / "container_od.json").write_text(json.dumps(doc, separators=(",", ":")))
        counts["gold_container_od"] = len(od_rows)
    return counts


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

    container = export_container(con, out)

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
        "silver_port_daily": con.execute("SELECT count(*) FROM silver_port_daily").fetchone()[0],
        "silver_dim_port": con.execute("SELECT count(*) FROM silver_dim_port").fetchone()[0],
        **container,
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
