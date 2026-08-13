"""Store + auth + share API tests. Needs DATABASE_URL."""

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
    assert extract_password("Bearer abc", None) == "abc"


@pytest.fixture(scope="module")
def database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL not set")
    return url


@pytest.fixture()
def store(database_url: str) -> Store:
    s = Store(database_url, min_size=1, max_size=2)
    yield s
    s.close()


def test_jar_and_mess_roundtrip(store: Store) -> None:
    name = f"demo-{uuid.uuid4().hex[:8]}"
    jar = store.create_jar(name, ["alice@cursor", "bob@claude"], password="jar-secret")
    assert jar.password == "jar-secret"
    assert store.get_jar_by_password("jar-secret") is not None

    mess = Mess.create(
        jar_id=jar.id,
        from_agent="alice@cursor",
        to_agent="bob@claude",
        body="openapi delta",
        kind=MessKind.question,
    )
    saved = store.send(mess)
    assert saved.seq == 1
    batch = store.check_jar("bob@claude", jar.name)
    assert batch[0]["messes"][0]["id"] == saved.id


def test_public_create_and_jar_auth(database_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MESSJAR_PASSWORD", raising=False)
    monkeypatch.delenv("MESSJAR_TOKEN", raising=False)
    store = Store(database_url, min_size=1, max_size=2)
    app = create_app(store)
    client = TestClient(app)

    assert client.get("/health").json()["status"] == "ok"
    assert client.get("/").status_code == 200

    name = f"web-{uuid.uuid4().hex[:8]}"
    created = client.post(
        "/api/jars",
        json={"name": name, "agents": ["a@cursor", "b@claude"], "password": "share-me"},
    )
    assert created.status_code == 200
    body = created.json()
    assert body["password"] == "share-me"
    assert "/j/" in body["share_url"]

    assert client.get("/jars").status_code == 401
    ok = client.get("/jars", headers={"Authorization": "Bearer share-me"})
    assert ok.status_code == 200
    assert ok.json()[0]["name"] == name

    bad = client.get(f"/j/{name}")
    assert bad.status_code == 401
    good = client.get(f"/j/{name}?p=share-me")
    assert good.status_code == 200
    assert "MCP" in good.text

    sent = client.post(
        "/send",
        headers={"Authorization": "Bearer share-me"},
        json={
            "jar": name,
            "from": "a@cursor",
            "to": "b@claude",
            "body": "hi",
            "kind": "question",
        },
    )
    assert sent.status_code == 200
    store.close()


def test_adapters_registered() -> None:
    from messjar.daemon.adapters import ADAPTERS

    assert set(ADAPTERS) >= {"claude_code", "cursor", "codex", "opencode"}
