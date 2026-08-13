"""Smoke tests for store + schema (no server required)."""

from __future__ import annotations

import tempfile
from pathlib import Path

from messjar.schema import Mess, MessKind
from messjar.store import Store


def test_jar_and_mess_roundtrip() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "t.db")
        jar = store.create_jar("demo", ["alice@cursor", "bob@claude"])
        assert jar.name == "demo"
        assert set(jar.agents) == {"alice@cursor", "bob@claude"}

        mess = Mess.create(
            jar_id=jar.id,
            from_agent="alice@cursor",
            to_agent="bob@claude",
            body="openapi delta attached",
            kind=MessKind.question,
            refs=["file:openapi.yaml"],
        )
        saved = store.send(mess)
        assert saved.seq == 1
        assert saved.reply_expected is True

        batch = store.check_jar("bob@claude", jar.name)
        assert len(batch) == 1
        assert batch[0]["messes"][0]["id"] == saved.id
        store.advance_cursor(jar.id, "bob@claude", batch[0]["cursor"])
        assert store.check_jar("bob@claude", jar.name) == []

        store.set_jar_paused(jar.name, True)
        try:
            store.send(
                Mess.create(
                    jar_id=jar.id,
                    from_agent="bob@claude",
                    to_agent="alice@cursor",
                    body="nope",
                    kind="answer",
                )
            )
            raise AssertionError("expected pause to block send")
        except RuntimeError:
            pass
        store.close()


def test_fyi_does_not_require_reply() -> None:
    m = Mess.create(
        jar_id="jar_x",
        from_agent="a",
        to_agent="b",
        body="heads up",
        kind="fyi",
    )
    assert m.reply_expected is False
    assert m.wakes_agent() is False
