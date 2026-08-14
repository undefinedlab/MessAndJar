"""Local kill switch — pure filesystem, no DB/network involved."""

from __future__ import annotations

from pathlib import Path

import pytest

import messjar.localstate as localstate_module


@pytest.fixture()
def localstate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(localstate_module, "STATE_DIR", tmp_path)
    monkeypatch.setattr(localstate_module, "KILLSWITCH_FILE", tmp_path / "killswitch.json")
    return localstate_module


def test_not_engaged_by_default(localstate) -> None:
    assert localstate.killswitch_state() is None


def test_engage_then_release(localstate) -> None:
    state = localstate.engage_killswitch("testing")
    assert state["reason"] == "testing"
    assert state["paused_at"]

    seen = localstate.killswitch_state()
    assert seen is not None
    assert seen["reason"] == "testing"

    assert localstate.release_killswitch() is True
    assert localstate.killswitch_state() is None
    assert localstate.release_killswitch() is False


def test_engage_with_no_reason(localstate) -> None:
    state = localstate.engage_killswitch()
    assert state["reason"] is None


def test_corrupt_file_fails_closed(localstate) -> None:
    localstate.STATE_DIR.mkdir(parents=True, exist_ok=True)
    localstate.KILLSWITCH_FILE.write_text("not json{{{")
    state = localstate.killswitch_state()
    assert state is not None  # fail closed: unreadable == engaged
    assert "unreadable" in state["reason"]
