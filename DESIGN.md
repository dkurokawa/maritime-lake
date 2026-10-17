# maritime-lake — design (frozen for Phase 1)

Working name. An open, reproducible data lakehouse for global shipping chokepoints,
with a public dashboard as its front page.

## Goal

- **Main deliverable is the pipeline, not the map.** Anyone can clone the repo and rebuild
  the same tables. Public Parquet files can be queried directly (DuckDB over HTTPS).
- **The dashboard's hook is "disruption impact"**: how traffic shifted when something happened
  (e.g. Red Sea attacks → Bab el-Mandeb down, Cape of Good Hope up).
- **Realtime layer** (Phase 2): live AIS positions around selected chokepoints.
- Audience: English-speaking data engineers and maritime analysts. All UI, README, code comments in English.

Differentiation vs. existing sites (World Monitor, straits.live): they publish dashboards but not
the code. We publish the code, the data model, and the derived data.

## Data sources (verified 2026-10-17)

ArcGIS REST, no key, `maxRecordCount` = 1000 → paginate with `resultOffset`.
Base: `https://services9.arcgis.com/weJ1QsnbMYJlCHdG/ArcGIS/rest/services/<name>/FeatureServer/0/query`

| name | rows | use |
|---|---|---|
| `Daily_Chokepoints_Data` | ~79.5k (28 chokepoints × daily since 2019-01-01) | core fact table. Fields: date, portid, portname, n_{container,dry_bulk,general_cargo,roro,tanker,cargo,total}, capacity_* |
| `PortWatch_chokepoints_database` | 28 | dimension (portid, portname, lat, lon, ...) |
| `portwatch_disruptions_database` | ~132 | natural-disaster events (GDACS-like: TC/EQ/FL…), affectedports. Not geopolitical |
| `Daily_Ports_Data` | ~5.86M | **Phase 1b**, not Phase 1 (backfill = ~5.9k requests; try an ArcGIS Hub CSV export first) |
| `PortWatch_ports_database` | 2065 | Phase 1b dimension |

- Data lags ~6 days (latest 2026-10-11 on 2026-10-17). Values are modelled estimates from AIS
  and can be revised → incremental loads must re-fetch a trailing window (last 30 days) and upsert.
- Attribution: "Source: IMF PortWatch". Check the PortWatch terms of use page and record the exact
  required wording + link in README. If redistribution of derived data is restricted, publish only
  aggregates that the terms allow and say so.

Realtime (Phase 2): AISStream.io WebSocket `wss://stream.aisstream.io/v0/stream`. API key required.
No direct browser connections. 3 connections per account. Bounding boxes required. 1 subscription
update/s per connection. No SLA.

## Architecture

```
GitHub Actions (daily cron)
  └─ Python (uv) + DuckDB
       bronze: raw JSON pages, gzip, per run      data/bronze/<source>/run=<ts>/page-*.json.gz
       silver: typed Parquet, deduped by (portid,date)  data/silver/chokepoint_daily/year=YYYY/*.parquet
       gold:   dashboard-ready aggregates           data/gold/*.json, data/gold/*.parquet
  └─ upload silver+gold to Cloudflare R2 (public read)   [Phase 1b: needs secrets]

Cloudflare Worker (static assets)  → dashboard (Vite + TS + MapLibre GL)
Cloudflare Durable Object          → AISStream relay + browser fan-out  [Phase 2]
```

Batch in Python/DuckDB on purpose: it is the lingua franca of the target audience, and the
SQL models are readable on GitHub. Serving and realtime stay on Workers.

## Phase 1 scope (implement now, all local, no secrets)

### pipeline/ (Python ≥3.12, uv, duckdb, httpx; no pandas required)
- `uv run mlake ingest` — fetch the 3 Phase-1 sources. Full load if no silver exists, else
  incremental from (max(date) − 30 days). Polite: sequential requests, retry with backoff, User-Agent with repo URL.
- `uv run mlake build` — bronze → silver → gold with SQL files in `pipeline/sql/` (one model per file).
- `uv run mlake check` — data quality: per chokepoint, no missing dates in silver between min and max;
  no duplicate (portid,date); n_total = sum of type counts where the source holds that invariant
  (verify first; if it does not hold, document instead of asserting); row-count delta vs previous build.
  Non-zero exit on failure.
- `uv run mlake all` = ingest + build + check.

### Gold models
- `chokepoint_daily`: silver + `ma7_total` (7-day trailing mean of n_total), `ma7_capacity`,
  `yoy_pct` (ma7_total vs same calendar date previous year), `baseline_ma7` (mean of ma7_total for the
  same day-of-year over 2019–2022; Feb 29 maps to Feb 28), `vs_baseline_pct`.
- `chokepoint_latest`: one row per chokepoint at the latest date with the above + lat/lon.
- Output for the web: `gold/chokepoints.geojson` (latest metrics as properties),
  `gold/series/<portid>.json` (date, n_total, n_by_type, ma7_total, baseline_ma7, capacity),
  `gold/events.json` (merged curated + disruptions), `gold/meta.json` (build time, data max date, row counts, source attribution).

### Curated events (`events/events.yaml`)
Geopolitical/structural events that PortWatch's disruption table does not hold. Each entry:
`id, title, start, end (nullable), chokepoints[], summary (1 sentence), source_url`.
**Include an event only if its date is verified against the source URL** (fetch it). Candidates:
Houthi attacks on Red Sea shipping (start Nov 2023), Panama Canal draft/transit restrictions due to
drought (2023–2024), Ever Given blocks Suez (Mar 2021). Prefer Reuters/AP/official canal authority
pages. Fewer verified events beat many unverified ones.

### web/ (Vite + TypeScript, no framework or a tiny one)
- MapLibre GL, basemap from OpenFreeMap (no API key). Dark-friendly style.
- Chokepoint markers: size = latest ma7_total, color = vs_baseline_pct (diverging, accessible palette).
- Click a chokepoint → side panel: long-term chart (2019→now) of ma7_total with baseline band and
  event markers; stacked by vessel type toggle. Chart lib: uPlot or Observable Plot.
- "Red Sea rerouting" story view: Bab el-Mandeb, Suez Canal, Cape of Good Hope normalized to the
  2019–2022 baseline on one chart with the Red Sea event marked.
- Header shows data freshness from meta.json and the attribution line.
- Loads gold JSON from `/data/` (dev: Vite serves `../data/gold` there).
- Mobile works (map full width, panel as bottom sheet).

### Repo hygiene
- `data/` gitignored. README (English): what/why, screenshot placeholder, architecture (mermaid),
  data sources + attribution, data model, quickstart (`uv run mlake all && cd web && npm run dev`),
  query-the-data example with DuckDB. License: MIT for code.
- `.github/workflows/daily.yml`: runs `mlake all` on cron; R2 upload step present but skipped unless
  secrets exist (`if: env.R2_ACCESS_KEY_ID != ''`).
- Tests: pytest for SQL models on a small fixture (ma7, yoy, baseline, Feb 29 mapping, dedupe/upsert).

## Phase 1b / Phase 2 (not now)
- 1b: R2 upload + public bucket/custom domain, deploy web Worker, ports table (Daily_Ports_Data), Iceberg via R2 catalog.
- 2: Durable Object holds the AISStream socket for bounding boxes of ~6 chokepoints, keeps last position per MMSI,
  fans out to browsers over WebSocket (hibernation API), live crossing counter, hourly snapshot to R2 bronze.

## Non-goals
- No accounts, no paid data, no scraping of commercial AIS sites.
- No claims beyond what the data shows (estimates are labelled as estimates).
