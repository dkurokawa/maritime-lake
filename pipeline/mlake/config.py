from __future__ import annotations

import os
from pathlib import Path

REPO_URL = "https://github.com/maritime-lake/maritime-lake"  # placeholder until the repo is published
USER_AGENT = f"maritime-lake/0.1 (+{REPO_URL}; polite sequential client)"

ARCGIS_BASE = "https://services9.arcgis.com/weJ1QsnbMYJlCHdG/ArcGIS/rest/services"

# source name -> where clause column used for incremental loads (None = always full)
SOURCES = {
    "Daily_Chokepoints_Data": {"incremental_field": "date", "bronze": "chokepoint_daily"},
    "PortWatch_chokepoints_database": {"incremental_field": None, "bronze": "chokepoints"},
    "portwatch_disruptions_database": {"incremental_field": None, "bronze": "disruptions"},
}

TRAILING_WINDOW_DAYS = 30
PAGE_SIZE = 1000

ROOT = Path(__file__).resolve().parents[2]
DATA = Path(os.environ.get("MLAKE_DATA_DIR", ROOT / "data"))
BRONZE = DATA / "bronze"
SILVER = DATA / "silver"
GOLD = DATA / "gold"
STATE = DATA / "state"
SQL_DIR = ROOT / "pipeline" / "sql"
EVENTS_YAML = ROOT / "events" / "events.yaml"

ATTRIBUTION = "Sources: UN Global Platform; IMF PortWatch (portwatch.imf.org)."
