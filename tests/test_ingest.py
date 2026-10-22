"""Bronze ingestion against a mocked ArcGIS server (httpx.MockTransport) - never the network."""

from __future__ import annotations

import datetime as dt
import gzip
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from lakefix import PRESETS, cp_feat, port_feat, write_run, write_silver_parquet
from mlake import config, ingest

Handler = Callable[[httpx.Request], httpx.Response]


def client_for(handler: Handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Replace time.sleep in the retry / politeness loops; returns the requested delays."""
    slept: list[float] = []
    monkeypatch.setattr(ingest.time, "sleep", slept.append)
    return slept


def read_page(path: Path) -> list[dict[str, Any]]:
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return json.load(fh)["features"]  # type: ignore[no-any-return]


# --- _get ------------------------------------------------------------------------------------


def test_get_returns_json_on_first_try(sleeps: list[float]) -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json={"features": [{"attributes": {"a": 1}}]})

    body = ingest._get(client_for(handler), "https://example.test/q", {"where": "1=1", "f": "json"})
    assert body == {"features": [{"attributes": {"a": 1}}]}
    assert len(seen) == 1 and sleeps == []
    assert seen[0].url.params["where"] == "1=1" and seen[0].url.params["f"] == "json"


def test_get_retries_http_errors_with_doubling_backoff(sleeps: list[float]) -> None:
    attempts = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts <= 3:
            return httpx.Response(503, text="busy")
        return httpx.Response(200, json={"features": []})

    body = ingest._get(client_for(handler), "https://example.test/q", {})
    assert body == {"features": []}
    assert attempts == 4
    assert sleeps == [2.0, 4.0, 8.0]


def test_get_treats_arcgis_error_object_on_http_200_as_failure(sleeps: list[float]) -> None:
    attempts = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(
                200, json={"error": {"code": 500, "message": "Unable to complete"}}
            )
        return httpx.Response(200, json={"features": [1]})

    assert ingest._get(client_for(handler), "https://example.test/q", {}) == {"features": [1]}
    assert attempts == 2 and sleeps == [2.0]


def test_get_retries_invalid_json(sleeps: list[float]) -> None:
    attempts = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return (
            httpx.Response(200, text="<html>gateway</html>")
            if attempts == 1
            else httpx.Response(200, json={"ok": True})
        )

    assert ingest._get(client_for(handler), "https://example.test/q", {}) == {"ok": True}
    assert attempts == 2


def test_get_gives_up_after_retries_and_reraises_http_error(sleeps: list[float]) -> None:
    attempts = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(500)

    with pytest.raises(httpx.HTTPStatusError):
        ingest._get(client_for(handler), "https://example.test/q", {}, retries=3)
    assert attempts == 3
    assert sleeps == [2.0, 4.0]  # no sleep after the final failed attempt


def test_get_gives_up_on_persistent_arcgis_error(sleeps: list[float]) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"error": {"message": "nope"}})

    with pytest.raises(RuntimeError, match="nope"):
        ingest._get(client_for(handler), "https://example.test/q", {}, retries=2)
    assert sleeps == [2.0]


def test_get_does_not_retry_transport_errors_silently(sleeps: list[float]) -> None:
    attempts = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ConnectError("boom", request=req)

    with pytest.raises(httpx.ConnectError):
        ingest._get(client_for(handler), "https://example.test/q", {}, retries=2)
    assert attempts == 2


# --- statistics / max date -------------------------------------------------------------------


def test_fetch_statistics_writes_one_page_and_asks_for_summed_container_calls(
    tmp_path: Path, sleeps: list[float]
) -> None:
    seen: list[httpx.Request] = []
    feats = [
        {"attributes": {"portid": "p1", "portname": "One", "country": "A", "calls": 900.0}},
        {"attributes": {"portid": "p2", "portname": "Two", "country": "B", "calls": 800.0}},
    ]

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json={"features": feats})

    out = tmp_path / "ports_top" / "run=x"
    n = ingest.fetch_statistics(client_for(handler), "Daily_Ports_Data", out, dt.date(2025, 3, 1))
    assert n == 2
    assert read_page(out / "page-0000.json.gz") == feats

    (req,) = seen
    assert req.url.path.endswith("/Daily_Ports_Data/FeatureServer/0/query")
    assert req.url.params["where"] == "date >= DATE '2025-03-01'"
    assert req.url.params["groupByFieldsForStatistics"] == "portid,portname,country"
    assert req.url.params["orderByFields"] == "calls DESC"
    stats = json.loads(req.url.params["outStatistics"])
    assert stats == [
        {
            "statisticType": "sum",
            "onStatisticField": "portcalls_container",
            "outStatisticFieldName": "calls",
        }
    ]


def test_source_max_date_parses_iso_prefix(sleeps: list[float]) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        stats = json.loads(req.url.params["outStatistics"])
        assert stats[0]["statisticType"] == "max" and stats[0]["onStatisticField"] == "date"
        return httpx.Response(
            200, json={"features": [{"attributes": {"d": "2026-03-02T00:00:00"}}]}
        )

    assert ingest.source_max_date(client_for(handler), "Daily_Ports_Data") == dt.date(2026, 3, 2)


# --- fetch_source pagination -----------------------------------------------------------------


def test_fetch_source_paginates_until_transfer_limit_clears(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    monkeypatch.setattr(config, "PAGE_SIZE", 2)
    pages = [
        {
            "features": [{"attributes": {"i": 0}}, {"attributes": {"i": 1}}],
            "exceededTransferLimit": True,
        },
        {
            "features": [{"attributes": {"i": 2}}, {"attributes": {"i": 3}}],
            "exceededTransferLimit": True,
        },
        {"features": [{"attributes": {"i": 4}}]},
    ]
    offsets: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        offsets.append(req.url.params["resultOffset"])
        assert req.url.params["where"] == "date >= DATE '2026-01-01'"
        assert req.url.params["resultRecordCount"] == "2"
        assert req.url.params["returnGeometry"] == "false"
        return httpx.Response(200, json=pages[len(offsets) - 1])

    out = tmp_path / "bronze" / "src" / "run=r"
    total = ingest.fetch_source(
        client_for(handler), "Daily_Chokepoints_Data", out, "date >= DATE '2026-01-01'"
    )
    assert total == 5
    assert offsets == ["0", "2", "4"]
    assert sorted(p.name for p in out.iterdir()) == [
        "page-0000.json.gz",
        "page-0001.json.gz",
        "page-0002.json.gz",
    ]
    assert [f["attributes"]["i"] for f in read_page(out / "page-0001.json.gz")] == [2, 3]
    assert sleeps == [0.2, 0.2]  # polite pause between pages only


def test_fetch_source_empty_result_still_writes_an_empty_page(
    tmp_path: Path, sleeps: list[float]
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"features": []})

    out = tmp_path / "run=r"
    assert ingest.fetch_source(client_for(handler), "S", out, prefix="inc-") == 0
    assert read_page(out / "page-inc-0000.json.gz") == []
    assert sleeps == []


def test_fetch_source_stops_when_server_flags_limit_but_returns_nothing(
    tmp_path: Path, sleeps: list[float]
) -> None:
    calls = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"features": [], "exceededTransferLimit": True})

    assert ingest.fetch_source(client_for(handler), "S", tmp_path / "r") == 0
    assert calls == 1  # no infinite loop


# --- port helpers ----------------------------------------------------------------------------


def test_in_builds_sql_in_list() -> None:
    assert ingest._in(["a", "b"]) == "portid IN ('a', 'b')"


def test_selected_ports_from_bronze_ranks_then_adds_presets(tmp_path: Path) -> None:
    rank_dir = tmp_path / "ports_top"
    write_run(
        rank_dir.parent,
        "ports_top",
        "20260101T000000Z",
        [{"portid": f"p{i}", "portname": "x", "country": "X", "calls": 100 - i} for i in range(3)],
    )
    ids = ingest.selected_ports_from_bronze(rank_dir)
    assert ids[:3] == ["p0", "p1", "p2"]
    assert set(ids[3:]) == set(PRESETS) and len(ids) == 3 + len(PRESETS)


def test_silver_state_helpers_empty_lake(lake: Path) -> None:
    assert ingest.silver_max_date() is None
    assert ingest.silver_port_state() is None


def test_silver_state_helpers_read_parquet(lake: Path) -> None:
    for table, rows in {
        "chokepoint_daily": "(DATE '2025-05-01', 'c1'), (DATE '2025-06-10', 'c1')",
        "port_daily": (
            "(DATE '2025-05-01', 'pA'), (DATE '2025-06-09', 'pB'), (DATE '2025-06-08', 'pA')"
        ),
    }.items():
        write_silver_parquet(table, 2025, rows)
    assert ingest.silver_max_date() == dt.date(2025, 6, 10)
    state = ingest.silver_port_state()
    assert state == (dt.date(2025, 6, 9), {"pA", "pB"})


# --- run() -----------------------------------------------------------------------------------


class FakeArcGIS:
    """Routes requests by service name, records them, serves tiny canned pages."""

    def __init__(self, ranking: list[str]) -> None:
        self.ranking = ranking
        self.requests: list[httpx.Request] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        service = req.url.path.split("/")[-4]
        params = req.url.params
        if service == "Daily_Ports_Data" and "outStatistics" in params:
            stat = json.loads(params["outStatistics"])[0]["statisticType"]
            if stat == "max":
                return httpx.Response(200, json={"features": [{"attributes": {"d": "2026-03-02"}}]})
            feats = [
                {"attributes": {"portid": p, "portname": p, "country": "X", "calls": 500 - i}}
                for i, p in enumerate(self.ranking)
            ]
            return httpx.Response(200, json={"features": feats})
        if service == "Daily_Ports_Data":
            return httpx.Response(
                200, json={"features": [{"attributes": port_feat("pX", dt.date(2026, 3, 1))}]}
            )
        if service == "Daily_Chokepoints_Data":
            return httpx.Response(
                200,
                json={"features": [{"attributes": cp_feat("chokepoint1", dt.date(2026, 3, 1))}]},
            )
        return httpx.Response(200, json={"features": [{"attributes": {"k": service}}]})

    def for_service(self, name: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path.split("/")[-4] == name]


@pytest.fixture
def fake_arcgis(monkeypatch: pytest.MonkeyPatch, sleeps: list[float]) -> FakeArcGIS:
    server = FakeArcGIS(ranking=["pA", "pB", "pC"])
    real_client = httpx.Client
    monkeypatch.setattr(
        ingest.httpx,
        "Client",
        lambda **kw: real_client(transport=httpx.MockTransport(server), **kw),
    )
    return server


def bronze_dirs(source: str) -> list[Path]:
    return sorted((config.BRONZE / source).glob("run=*"))


def test_run_full_loads_every_source(lake: Path, fake_arcgis: FakeArcGIS) -> None:
    counts = ingest.run(full=True)
    assert counts == {
        "Daily_Chokepoints_Data": 1,
        "PortWatch_chokepoints_database": 1,
        "portwatch_disruptions_database": 1,
        "PortWatch_ports_database": 1,
        "ports_top": 3,
        "Daily_Ports_Data": 1,
    }
    for source in (
        "chokepoint_daily",
        "chokepoints",
        "disruptions",
        "ports",
        "ports_top",
        "port_daily",
    ):
        assert len(bronze_dirs(source)) == 1, source

    # full run: chokepoint daily has no date filter
    (cp_req,) = fake_arcgis.for_service("Daily_Chokepoints_Data")
    assert cp_req.url.params["where"] == "1=1"
    assert "polite sequential client" in cp_req.headers["user-agent"]

    # statistics window: max date minus RANK_WINDOW_DAYS
    stats_reqs = [
        r for r in fake_arcgis.for_service("Daily_Ports_Data") if "outStatistics" in r.url.params
    ]
    assert stats_reqs[1].url.params["where"] == (
        f"date >= DATE '{dt.date(2026, 3, 2) - dt.timedelta(days=config.RANK_WINDOW_DAYS)}'"
    )

    # selected ports (ranking + presets) are all new -> one full-history request, "full-" pages
    (port_req,) = [
        r
        for r in fake_arcgis.for_service("Daily_Ports_Data")
        if "outStatistics" not in r.url.params
    ]
    where = port_req.url.params["where"]
    assert where.startswith("portid IN (") and "date >=" not in where
    for p in ["pA", "pB", "pC", *PRESETS]:
        assert f"'{p}'" in where
    (port_dir,) = bronze_dirs("port_daily")
    assert [p.name for p in port_dir.iterdir()] == ["page-full-0000.json.gz"]


def test_run_incremental_uses_trailing_window_and_splits_known_from_new_ports(
    lake: Path, fake_arcgis: FakeArcGIS
) -> None:
    for table, rows in {
        "chokepoint_daily": (
            "(DATE '2026-02-20', 'chokepoint1'), (DATE '2026-03-01', 'chokepoint1')"
        ),
        "port_daily": "(DATE '2026-02-25', 'pA'), (DATE '2026-02-28', 'pA')",
    }.items():
        write_silver_parquet(table, 2026, rows)

    ingest.run(full=False)

    window = config.TRAILING_WINDOW_DAYS
    (cp_req,) = fake_arcgis.for_service("Daily_Chokepoints_Data")
    assert cp_req.url.params["where"] == (
        f"date >= DATE '{dt.date(2026, 3, 1) - dt.timedelta(days=window)}'"
    )
    # sources without an incremental field are always reloaded in full
    (dim_req,) = fake_arcgis.for_service("PortWatch_chokepoints_database")
    assert dim_req.url.params["where"] == "1=1"

    port_reqs = [
        r
        for r in fake_arcgis.for_service("Daily_Ports_Data")
        if "outStatistics" not in r.url.params
    ]
    assert len(port_reqs) == 2
    known_where = port_reqs[0].url.params["where"]
    assert known_where == (
        f"portid IN ('pA') AND date >= DATE '{dt.date(2026, 2, 28) - dt.timedelta(days=window)}'"
    )
    new_where = port_reqs[1].url.params["where"]
    assert "'pA'" not in new_where and "'pB'" in new_where and "date >=" not in new_where
    (port_dir,) = bronze_dirs("port_daily")
    assert sorted(p.name for p in port_dir.iterdir()) == [
        "page-full-0000.json.gz",
        "page-inc-0000.json.gz",
    ]


def test_run_full_ignores_existing_silver(lake: Path, fake_arcgis: FakeArcGIS) -> None:
    write_silver_parquet("chokepoint_daily", 2026, "(DATE '2026-03-01', 'c')")
    ingest.run(full=True)
    (cp_req,) = fake_arcgis.for_service("Daily_Chokepoints_Data")
    assert cp_req.url.params["where"] == "1=1"


def test_run_propagates_persistent_server_failure(
    lake: Path, monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    real_client = httpx.Client
    monkeypatch.setattr(
        ingest.httpx,
        "Client",
        lambda **kw: real_client(
            transport=httpx.MockTransport(lambda r: httpx.Response(502)), **kw
        ),
    )
    with pytest.raises(httpx.HTTPStatusError):
        ingest.run(full=True)
    assert len(sleeps) == 6  # 7 attempts -> 6 backoff sleeps, none after the last
    assert not config.BRONZE.exists() or not any(config.BRONZE.rglob("*.json.gz"))
