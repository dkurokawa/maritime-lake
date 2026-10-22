from __future__ import annotations

import pytest
from mlake import build, check, cli, ingest, od


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, object]]:
    log: list[tuple[str, object]] = []
    monkeypatch.setattr(ingest, "run", lambda full=False: log.append(("ingest", full)) or {})
    monkeypatch.setattr(build, "run", lambda: log.append(("build", None)) or {})
    monkeypatch.setattr(od, "run", lambda: log.append(("od", None)) or {})

    def fake_check() -> int:
        log.append(("check", None))
        return 0

    monkeypatch.setattr(check, "run", fake_check)
    return log


def test_ingest_dispatch_defaults_to_incremental(calls: list[tuple[str, object]]) -> None:
    assert cli.main(["ingest"]) == 0
    assert calls == [("ingest", False)]


def test_ingest_full_flag(calls: list[tuple[str, object]]) -> None:
    assert cli.main(["ingest", "--full"]) == 0
    assert calls == [("ingest", True)]


@pytest.mark.parametrize("cmd", ["build", "od", "check"])
def test_single_command_runs_only_that_step(calls: list[tuple[str, object]], cmd: str) -> None:
    assert cli.main([cmd]) == 0
    assert [name for name, _ in calls] == [cmd]


def test_all_runs_every_step_in_order_and_passes_full(calls: list[tuple[str, object]]) -> None:
    assert cli.main(["all", "--full"]) == 0
    assert calls == [("ingest", True), ("build", None), ("od", None), ("check", None)]


def test_check_failure_becomes_exit_code(
    calls: list[tuple[str, object]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(check, "run", lambda: 1)
    assert cli.main(["check"]) == 1
    assert cli.main(["all"]) == 1


def test_all_stops_before_check_when_an_earlier_step_raises(
    calls: list[tuple[str, object]], monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom() -> None:
        raise RuntimeError("build failed")

    monkeypatch.setattr(build, "run", boom)
    with pytest.raises(RuntimeError, match="build failed"):
        cli.main(["all"])
    assert [name for name, _ in calls] == ["ingest"]


def test_command_is_required(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main([])
    assert exc.value.code == 2
    assert "required" in capsys.readouterr().err


def test_unknown_command_is_rejected() -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["nonsense"])
    assert exc.value.code == 2
