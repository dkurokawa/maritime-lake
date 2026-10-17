"""Bronze ingestion: raw ArcGIS REST pages, gzipped, one directory per run."""
from __future__ import annotations

import datetime as dt
import glob
import gzip
import json
import time
from pathlib import Path

import duckdb
import httpx

from . import config
from .models import run_model


def _query_url(name: str) -> str:
    return f"{config.ARCGIS_BASE}/{name}/FeatureServer/0/query"


def _get(client: httpx.Client, url: str, params: dict, retries: int = 5) -> dict:
    delay = 2.0
    for attempt in range(retries):
        try:
            r = client.get(url, params=params, timeout=60)
            r.raise_for_status()
            body = r.json()
            if "error" in body:  # ArcGIS returns HTTP 200 with an error object
                raise RuntimeError(body["error"])
            return body
        except (httpx.HTTPError, RuntimeError, ValueError) as exc:
            if attempt == retries - 1:
                raise
            print(f"  retry {attempt + 1} after error: {exc!r}")
            time.sleep(delay)
            delay *= 2
    raise AssertionError("unreachable")


def fetch_statistics(client: httpx.Client, service: str, out_dir: Path, since: dt.date) -> int:
    """Container port calls per port since `since` (one server-side statistics query, one page)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    stats = [{"statisticType": "sum", "onStatisticField": "portcalls_container", "outStatisticFieldName": "calls"}]
    params = {
        "where": f"date >= DATE '{since.isoformat()}'",
        "outStatistics": json.dumps(stats),
        "groupByFieldsForStatistics": "portid,portname,country",
        "orderByFields": "calls DESC",
        "resultRecordCount": config.PAGE_SIZE,  # the top 1000 of ~2000 ports is far more than we use
        "f": "json",
    }
    body = _get(client, _query_url(service), params)
    feats = body.get("features", [])
    with gzip.open(out_dir / "page-0000.json.gz", "wt", encoding="utf-8") as fh:
        json.dump({"features": feats}, fh)
    return len(feats)


def source_max_date(client: httpx.Client, service: str) -> dt.date:
    stats = [{"statisticType": "max", "onStatisticField": "date", "outStatisticFieldName": "d"}]
    body = _get(client, _query_url(service), {"where": "1=1", "outStatistics": json.dumps(stats), "f": "json"})
    return dt.date.fromisoformat(body["features"][0]["attributes"]["d"][:10])


def select_ports(con: duckdb.DuckDBPyConnection) -> list[str]:
    """Selected container ports (top N by container calls + presets), via the SQL model gold uses."""
    run_model(
        con, "gold_container_port_selection",
        top_n=str(config.TOP_PORTS),
        preset_ids=", ".join(f"'{p}'" for p in config.PRESET_PORTS),
    )
    rows = con.execute("SELECT portid FROM gold_container_port_selection ORDER BY rank NULLS LAST, portid").fetchall()
    return [r[0] for r in rows]


def selected_ports_from_bronze(rank_dir: Path) -> list[str]:
    con = duckdb.connect()
    run_model(con, "silver_container_port_rank", bronze_glob=str(rank_dir / "run=*" / "page-*.json.gz"))
    return select_ports(con)


def silver_port_state() -> tuple[dt.date, set[str]] | None:
    files = glob.glob(str(config.SILVER / "port_daily" / "year=*" / "*.parquet"))
    if not files:
        return None
    con = duckdb.connect()
    d = con.execute("SELECT max(date) FROM read_parquet(?)", [files]).fetchone()[0]
    ids = {r[0] for r in con.execute("SELECT DISTINCT portid FROM read_parquet(?)", [files]).fetchall()}
    return d, ids


def fetch_source(client: httpx.Client, name: str, out_dir: Path, where: str = "1=1") -> int:
    """Paginate one source into out_dir/page-NNNN.json.gz. Returns feature count."""
    out_dir.mkdir(parents=True, exist_ok=True)
    offset, page, total = 0, 0, 0
    while True:
        params = {
            "where": where,
            "outFields": "*",
            "returnGeometry": "false",
            "orderByFields": "ObjectId",
            "resultOffset": offset,
            "resultRecordCount": config.PAGE_SIZE,
            "f": "json",
        }
        body = _get(client, _query_url(name), params)
        feats = body.get("features", [])
        with gzip.open(out_dir / f"page-{page:04d}.json.gz", "wt", encoding="utf-8") as fh:
            json.dump({"features": feats}, fh)
        total += len(feats)
        page += 1
        offset += len(feats)
        if not feats or not body.get("exceededTransferLimit"):
            break
        time.sleep(0.2)  # be polite
    return total


def silver_max_date() -> dt.date | None:
    files = glob.glob(str(config.SILVER / "chokepoint_daily" / "year=*" / "*.parquet"))
    if not files:
        return None
    con = duckdb.connect()
    val = con.execute("SELECT max(date) FROM read_parquet(?)", [files]).fetchone()[0]
    return val


def run(full: bool = False) -> dict[str, int]:
    run_id = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    max_date = None if full else silver_max_date()
    counts: dict[str, int] = {}
    headers = {"User-Agent": config.USER_AGENT}
    with httpx.Client(headers=headers, follow_redirects=True) as client:
        selected: list[str] = []
        for name, meta in config.SOURCES.items():
            where = "1=1"
            mode = "full"
            if meta.get("mode") == "statistics":
                service = meta["service"]
                since = source_max_date(client, service) - dt.timedelta(days=config.RANK_WINDOW_DAYS)
                out = config.BRONZE / meta["bronze"] / f"run={run_id}"
                print(f"ingest {name}: container calls since {since} (statistics query on {service})")
                counts[name] = fetch_statistics(client, service, out, since)
                print(f"  {counts[name]} ports -> {out}")
                selected = selected_ports_from_bronze(config.BRONZE / meta["bronze"])
                print(f"  selected {len(selected)} ports (top {config.TOP_PORTS} + presets)")
                continue
            if meta.get("mode") == "selected_ports":
                where = "portid IN (" + ", ".join(f"'{p}'" for p in selected) + ")"
                mode = f"{len(selected)} selected ports"
                have = None if full else silver_port_state()
                # incremental only when every selected port already has history in silver
                if have is not None and set(selected) <= have[1]:
                    since = have[0] - dt.timedelta(days=config.TRAILING_WINDOW_DAYS)
                    where += f" AND date >= DATE '{since.isoformat()}'"
                    mode += f", incremental since {since}"
                else:
                    mode += ", full history"
            elif meta["incremental_field"] and max_date is not None:
                since = max_date - dt.timedelta(days=config.TRAILING_WINDOW_DAYS)
                where = f"{meta['incremental_field']} >= DATE '{since.isoformat()}'"
                mode = f"incremental since {since}"
            out = config.BRONZE / meta["bronze"] / f"run={run_id}"
            print(f"ingest {name}: {mode}")
            n = fetch_source(client, name, out, where)
            print(f"  {n} rows -> {out}")
            counts[name] = n
    return counts
