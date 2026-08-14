"""Daemon.tick(): local kill switch short-circuits before any network call,
and circuit-trip notifications fire once per trip (dedup), not once per poll.

Pure unit tests — BusClient never connects eagerly, so no real bus/DB needed.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

import messjar.localstate as localstate_module
from messjar.daemon.runner import Daemon


@pytest.fixture()
def killswitch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(localstate_module, "STATE_DIR", tmp_path)
    monkeypatch.setattr(localstate_module, "KILLSWITCH_FILE", tmp_path / "killswitch.json")
    return localstate_module


@pytest.fixture()
def daemon() -> Daemon:
    return Daemon(
        bus_url="http://127.0.0.1:9",  # never actually connected to in these tests
        agent_id="alex@cursor",
        adapter_name="cursor",
        workdir=".",
        dry_run=True,
    )


def test_tick_skips_poll_when_killswitch_engaged(daemon: Daemon, killswitch) -> None:
    killswitch.engage_killswitch("testing")
    with patch.object(daemon.bus, "check_jar") as check_jar, patch.object(
        daemon, "_check_circuit_trips"
    ) as check_trips:
        n = daemon.tick()
    assert n == 0
    check_jar.assert_not_called()
    check_trips.assert_not_called()


def test_tick_polls_when_killswitch_not_engaged(daemon: Daemon, killswitch) -> None:
    with patch.object(daemon.bus, "check_jar", return_value=[]) as check_jar, patch.object(
        daemon, "_check_circuit_trips"
    ) as check_trips:
        n = daemon.tick()
    assert n == 0
    check_jar.assert_called_once()
    check_trips.assert_called_once()


def _paused_jar(reason: str | None) -> dict:
    return {"id": "jar_1", "name": "demo", "paused": True, "paused_reason": reason}


def test_circuit_trip_notifies_once_per_trip(daemon: Daemon) -> None:
    daemon.jar_filter = "demo"
    with patch.object(daemon.bus, "get_jar", return_value=_paused_jar("hop_depth limit hit")), \
        patch("messjar.daemon.runner.notify_circuit_trip") as notify_trip:
        daemon._check_circuit_trips()
        daemon._check_circuit_trips()
    notify_trip.assert_called_once()


def test_circuit_trip_notifies_again_after_resume_and_retrip(daemon: Daemon) -> None:
    daemon.jar_filter = "demo"
    with patch("messjar.daemon.runner.notify_circuit_trip") as notify_trip:
        with patch.object(daemon.bus, "get_jar", return_value=_paused_jar("hop_depth limit hit")):
            daemon._check_circuit_trips()
        with patch.object(
            daemon.bus, "get_jar", return_value={"id": "jar_1", "name": "demo", "paused": False, "paused_reason": None}
        ):
            daemon._check_circuit_trips()
        with patch.object(daemon.bus, "get_jar", return_value=_paused_jar("spawn_count limit hit")):
            daemon._check_circuit_trips()
    assert notify_trip.call_count == 2


def test_plain_human_pause_does_not_notify(daemon: Daemon) -> None:
    daemon.jar_filter = "demo"
    with patch.object(daemon.bus, "get_jar", return_value=_paused_jar(None)), patch(
        "messjar.daemon.runner.notify_circuit_trip"
    ) as notify_trip:
        daemon._check_circuit_trips()
    notify_trip.assert_not_called()
