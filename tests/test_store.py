"""Store + join/agent-id tests. Needs DATABASE_URL."""

from __future__ import annotations

import os
import uuid

import pytest
from fastapi.testclient import TestClient

from messjar.agents import make_agent_id
from messjar.auth import extract_password, passwords_match
from messjar.bus.server import create_app
from messjar.repo import normalize_repo_key
from messjar.schema import Mess
from messjar.store import Store


def test_normalize_repo_key() -> None:
    assert normalize_repo_key("https://github.com/Acme/ABC.git") == "github.com/acme/abc"


def test_make_agent_id() -> None:
    assert make_agent_id("Alex", "cursor") == "alex@cursor"
    assert make_agent_id("Alex", "claude_code") == "alex@claude"
    assert "@codex" in make_agent_id(None, "codex")


def test_fyi_does_not_require_reply() -> None:
    m = Mess.create(
        jar_id="jar_x", from_agent="a", to_agent="b", body="heads up", kind="fyi"
    )
    assert m.reply_expected is False


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


def test_empty_jar_and_attach(store: Store) -> None:
    name = f"empty-{uuid.uuid4().hex[:8]}"
    jar = store.create_jar(name, [], password="pw")
    assert jar.agents == []
    jar = store.attach_agent(jar.id, "alex@cursor")
    assert "alex@cursor" in jar.agents


def test_join_assigns_agent_id(database_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MESSJAR_PASSWORD", raising=False)
    store = Store(database_url, min_size=1, max_size=2)
    app = create_app(store)
    client = TestClient(app)

    name = f"web-{uuid.uuid4().hex[:8]}"
    repo = f"github.com/acme/{name}"
    created = client.post(
        "/api/jars",
        json={"name": name, "password": "share-me", "repos": [repo]},
    )
    assert created.status_code == 200
    assert created.json()["agents"] == []

    joined = client.post(
        "/api/join",
        json={
            "jar": name,
            "password": "share-me",
            "tool": "cursor",
            "display_name": "Alex",
        },
    )
    assert joined.status_code == 200
    assert joined.json()["agent_id"] == "alex@cursor"

    detail = client.get(f"/api/jars/{name}/detail", params={"p": "share-me"})
    assert detail.status_code == 200
    assert "alex@cursor" in detail.json()["agents"]

    added = client.post(
        "/api/jars/repos",
        json={"jar": name, "password": "share-me", "repos": [f"{repo}-ui"]},
    )
    assert added.status_code == 200
    assert f"{repo}-ui" in added.json()["repos"]

    page = client.get(f"/j/{name}?p=share-me")
    assert page.status_code == 200
    assert "Members" in page.text
    assert "Add repo key" in page.text
    assert "Recent messes" in page.text

    friend = client.post(
        "/api/join",
        json={"jar": name, "password": "share-me", "tool": "claude", "display_name": "Sam"},
    )
    assert friend.status_code == 200

    token = joined.json()["token"]
    sent = client.post(
        "/mcp/call",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "name": "send",
            "arguments": {
                "from": "alex@cursor",
                "to": "sam@claude",
                "body": "hi",
                "kind": "question",
                "repo": repo,
            },
        },
    )
    assert sent.status_code == 200, sent.text
    store.close()


def test_adapters_registered() -> None:
    from messjar.daemon.adapters import ADAPTERS

    assert set(ADAPTERS) >= {"claude_code", "cursor", "codex", "opencode"}
