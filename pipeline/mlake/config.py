from __future__ import annotations

import os
from pathlib import Path
from typing import Any

REPO_URL = (
    "https://github.com/maritime-lake/maritime-lake"  # placeholder until the repo is published
)
USER_AGENT = f"maritime-lake/0.1 (+{REPO_URL}; polite sequential client)"

ARCGIS_BASE = "https://services9.arcgis.com/weJ1QsnbMYJlCHdG/ArcGIS/rest/services"

# source name -> where clause column used for incremental loads (None = always full)
SOURCES: dict[str, dict[str, Any]] = {
    "Daily_Chokepoints_Data": {"incremental_field": "date", "bronze": "chokepoint_daily"},
    "PortWatch_chokepoints_database": {"incremental_field": None, "bronze": "chokepoints"},
    "portwatch_disruptions_database": {"incremental_field": None, "bronze": "disruptions"},
    # Phase 1c: container time map. Order matters: ports_top decides which ports
    # Daily_Ports_Data fetches.
    "PortWatch_ports_database": {"incremental_field": None, "bronze": "ports"},
    "ports_top": {"service": "Daily_Ports_Data", "mode": "statistics", "bronze": "ports_top"},
    "Daily_Ports_Data": {
        "incremental_field": "date",
        "bronze": "port_daily",
        "mode": "selected_ports",
    },
}

# Container time map
TOP_PORTS = 60
RANK_WINDOW_DAYS = 365
# The five preset origins are the only hand-picked ports; everything else is computed
# from the ranking.
PRESET_PORTS = {
    "port1188": "Shanghai",
    "port1201": "Singapore",
    "port1114": "Rotterdam",
    "port664": "Los Angeles-Long Beach",
    "port1305": "Tokyo",
}
SPEED_KN = 16
SCENARIOS: list[dict[str, Any]] = [
    {"id": "open", "label": "All canals open", "restrictions": ["northwest"]},
    {
        "id": "red_sea_closed",
        "label": "Red Sea / Suez closed (via Cape of Good Hope)",
        "restrictions": ["northwest", "suez", "babalmandab"],
    },
    {
        "id": "panama_closed",
        "label": "Panama Canal closed",
        "restrictions": ["northwest", "panama"],
    },
]

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
