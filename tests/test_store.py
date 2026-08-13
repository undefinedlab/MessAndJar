"""Store + auth + repo→jar tests. Needs DATABASE_URL."""

from __future__ import annotations

import os
import uuid

import pytest
from fastapi.testclient import TestClient

from messjar.auth import extract_password, passwords_match
from messjar.bus.server import create_app
from messjar.repo import normalize_repo_key
from messjar.schema import Mess, MessKind
from messjar.store import Store


def test_normalize_repo_key() -> None:
    assert normalize_repo_key("https://github.com/Acme/ABC.git") == "github.com/acme/abc"
    assert normalize_repo_key("git@github.com:Acme/ABC.git") == "github.com/acme/abc"
    assert normalize_repo_key("ABC") == "abc"


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


def test_repo_resolve_and_agent_key(store: Store) -> None:
    suffix = uuid.uuid4().hex[:8]
    a = store.create_jar(
        f"abc-{suffix}",
        ["alice@cursor", "bob@claude"],
        password=f"pw-a-{suffix}",
        repos=["github.com/acme/abc"],
    )
    store.create_jar(
        f"billing-{suffix}",
        ["alice@cursor", "bob@claude"],
        password=f"pw-b-{suffix}",
        repos=["github.com/acme/billing"],
    )
    found = store.resolve_jar(
        agent_id="alice@cursor",
        repo="https://github.com/acme/abc.git",
    )
    assert found.id == a.id

    agent_id, token = store.upsert_agent_key("alice@cursor")
    assert store.get_agent_by_token(token) == agent_id
    # second claim returns same token
    _, token2 = store.upsert_agent_key("alice@cursor")
    assert token2 == token


def test_public_create_claim_and_which_jar(
    database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MESSJAR_PASSWORD", raising=False)
    store = Store(database_url, min_size=1, max_size=2)
    app = create_app(store)
    client = TestClient(app)

    name = f"web-{uuid.uuid4().hex[:8]}"
    created = client.post(
        "/api/jars",
        json={
            "name": name,
            "agents": ["a@cursor", "b@claude"],
            "password": "share-me",
            "repos": ["github.com/acme/web"],
        },
    )
    assert created.status_code == 200
    assert "github.com/acme/web" in created.json()["repos"]

    claimed = client.post(
        "/api/agent-key",
        json={"agent_id": "a@cursor", "jar": name, "password": "share-me"},
    )
    assert claimed.status_code == 200
    token = claimed.json()["token"]
    assert token.startswith("mj_")

    which = client.post(
        "/mcp/call",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "name": "which_jar",
            "arguments": {
                "agent": "a@cursor",
                "repo": "github.com/acme/web",
            },
        },
    )
    assert which.status_code == 200
    assert which.json()["content"][0]["json"]["jar"] == name

    sent = client.post(
        "/mcp/call",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "name": "send",
            "arguments": {
                "from": "a@cursor",
                "to": "b@claude",
                "body": "hi from repo context",
                "kind": "question",
                "repo": "github.com/acme/web",
            },
        },
    )
    assert sent.status_code == 200
    store.close()


def test_adapters_registered() -> None:
    from messjar.daemon.adapters import ADAPTERS

    assert set(ADAPTERS) >= {"claude_code", "cursor", "codex", "opencode"}
