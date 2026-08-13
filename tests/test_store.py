"""Store + auth tests. Integration tests need DATABASE_URL (see docker-compose)."""

from __future__ import annotations

import os
import uuid

import pytest
from fastapi.testclient import TestClient

from messjar.auth import extract_password, passwords_match
from messjar.bus.server import create_app
from messjar.schema import Mess, MessKind
from messjar.store import Store


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


def test_password_compare() -> None:
    assert passwords_match("secret", "secret")
    assert not passwords_match("nope", "secret")
    assert not passwords_match(None, "secret")
    assert extract_password("Bearer abc", None) == "abc"
    assert extract_password(None, "from-header") == "from-header"


@pytest.fixture(scope="module")
def database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL not set — start docker compose db or export a Postgres URL")
    return url


@pytest.fixture()
def store(database_url: str) -> Store:
    s = Store(database_url, min_size=1, max_size=2)
    yield s
    s.close()


def test_jar_and_mess_roundtrip(store: Store) -> None:
    name = f"demo-{uuid.uuid4().hex[:8]}"
    jar = store.create_jar(name, ["alice@cursor", "bob@claude"])
    assert jar.name == name
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
    with pytest.raises(RuntimeError):
        store.send(
            Mess.create(
                jar_id=jar.id,
                from_agent="bob@claude",
                to_agent="alice@cursor",
                body="nope",
                kind="answer",
            )
        )


def test_auth_gate(database_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MESSJAR_PASSWORD", "shared-secret")
    store = Store(database_url, min_size=1, max_size=2)
    app = create_app(store)
    client = TestClient(app)

    assert client.get("/health").status_code == 200
    assert client.get("/jars").status_code == 401
    assert client.get("/mcp/tools").status_code == 401

    ok = client.get("/jars", headers={"Authorization": "Bearer shared-secret"})
    assert ok.status_code == 200

    name = f"auth-{uuid.uuid4().hex[:8]}"
    created = client.post(
        "/jars",
        headers={"X-MessJar-Password": "shared-secret"},
        json={"name": name, "agents": ["a@x", "b@y"]},
    )
    assert created.status_code == 200
    store.close()
