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
        for name, meta in config.SOURCES.items():
            where = "1=1"
            mode = "full"
            if meta["incremental_field"] and max_date is not None:
                since = max_date - dt.timedelta(days=config.TRAILING_WINDOW_DAYS)
                where = f"{meta['incremental_field']} >= DATE '{since.isoformat()}'"
                mode = f"incremental since {since}"
            out = config.BRONZE / meta["bronze"] / f"run={run_id}"
            print(f"ingest {name}: {mode}")
            n = fetch_source(client, name, out, where)
            print(f"  {n} rows -> {out}")
            counts[name] = n
    return counts
