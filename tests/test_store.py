"""Store + join/agent-id tests. Needs DATABASE_URL."""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from messjar.agents import make_agent_id
from messjar.auth import extract_password, passwords_match
from messjar.bus.server import create_app
from messjar.repo import normalize_repo_key
from messjar.schema import Mess
from messjar.store import CircuitBreakerTripped, Store


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


def test_trigger_source_defaults_human() -> None:
    m = Mess.create(jar_id="jar_x", from_agent="a", to_agent="b", body="hi", kind="fyi")
    assert m.trigger_source == "human"


def test_wake_kinds_consistency() -> None:
    from messjar.schema import MessKind, WAKE_KINDS

    assert MessKind.question in WAKE_KINDS
    assert MessKind.handoff in WAKE_KINDS
    assert MessKind.answer in WAKE_KINDS
    assert MessKind.fyi not in WAKE_KINDS
    assert MessKind.artifact not in WAKE_KINDS
    for kind in MessKind:
        # answer/artifact require a verifiable ref as of Task 3 (refs.py) —
        # unrelated to WAKE_KINDS membership, just needed to construct one.
        refs = ["sha:abc123"] if kind in (MessKind.answer, MessKind.artifact) else []
        m = Mess.create(jar_id="jar_x", from_agent="a", to_agent="b", body="x", kind=kind, refs=refs)
        assert m.wakes_agent() == (kind in WAKE_KINDS)


def test_password_compare() -> None:
    assert passwords_match("secret", "secret")
    assert extract_password("Bearer abc", None) == "abc"


def test_empty_jar_and_attach(store: Store) -> None:
    name = f"empty-{uuid.uuid4().hex[:8]}"
    jar = store.create_jar(name, [], password="pw")
    assert jar.agents == []
    jar = store.attach_agent(jar.id, "alex@cursor")
    assert "alex@cursor" in jar.agents


def _two_agent_jar(store: Store, *, prefix: str, circuit: dict) -> object:
    name = f"{prefix}-{uuid.uuid4().hex[:8]}"
    # Unique password per jar — get_jar_by_password matches on this value
    # across the whole (persistent, shared-across-tests) database, so a
    # reused literal like "pw" would resolve to whichever jar happened to
    # claim it first.
    jar = store.create_jar(name, ["a@cursor", "b@claude"], password=f"pw-{uuid.uuid4().hex}", circuit=circuit)
    return jar


def _wake_mess(jar_id: str, *, hop: int) -> Mess:
    return Mess.create(
        jar_id=jar_id, from_agent="a@cursor", to_agent="b@claude", body="hi", kind="question", hop=hop
    )


def test_send_backward_compat_no_trigger_source(store: Store) -> None:
    jar = _two_agent_jar(store, prefix="compat", circuit={})
    mess = _wake_mess(jar.id, hop=0)
    sent = store.send(mess)
    assert sent.trigger_source == "human"


def test_circuit_breaker_trips_on_spawn_count(store: Store) -> None:
    jar = _two_agent_jar(store, prefix="spawn", circuit={"max_spawns": 2, "window_s": 60, "max_hop_depth": 100})
    store.send(_wake_mess(jar.id, hop=0))
    store.send(_wake_mess(jar.id, hop=0))
    with pytest.raises(CircuitBreakerTripped, match="spawn_count"):
        store.send(_wake_mess(jar.id, hop=0))

    tripped = store.get_jar(jar.id)
    assert tripped.paused is True
    assert tripped.paused_reason and "spawn_count" in tripped.paused_reason

    with pytest.raises(RuntimeError, match="is paused"):
        store.send(_wake_mess(jar.id, hop=0))


def test_circuit_breaker_trips_on_hop_depth(store: Store) -> None:
    jar = _two_agent_jar(store, prefix="hop", circuit={"max_spawns": 100, "window_s": 60, "max_hop_depth": 2})
    store.send(_wake_mess(jar.id, hop=0))
    store.send(_wake_mess(jar.id, hop=1))
    with pytest.raises(CircuitBreakerTripped, match="hop_depth"):
        store.send(_wake_mess(jar.id, hop=2))

    tripped = store.get_jar(jar.id)
    assert tripped.paused is True
    assert tripped.paused_reason and "hop_depth" in tripped.paused_reason


def test_fyi_exempt_from_circuit_breaker(store: Store) -> None:
    jar = _two_agent_jar(store, prefix="fyi", circuit={"max_spawns": 1, "window_s": 60, "max_hop_depth": 100})
    store.send(_wake_mess(jar.id, hop=0))  # uses the one spawn slot
    for _ in range(5):
        fyi = Mess.create(
            jar_id=jar.id, from_agent="a@cursor", to_agent="b@claude", body="fyi", kind="fyi"
        )
        store.send(fyi)  # fyi never counts against the breaker


def test_resume_clears_trip_and_state(store: Store) -> None:
    jar = _two_agent_jar(store, prefix="resume", circuit={"max_spawns": 1, "window_s": 60, "max_hop_depth": 100})
    store.send(_wake_mess(jar.id, hop=0))
    with pytest.raises(CircuitBreakerTripped):
        store.send(_wake_mess(jar.id, hop=0))

    resumed = store.set_jar_paused(jar.id, False)
    assert resumed.paused is False
    assert resumed.paused_reason is None

    # counters reset — a fresh send succeeds again
    store.send(_wake_mess(jar.id, hop=0))


def test_human_pause_has_no_reason(store: Store) -> None:
    jar = _two_agent_jar(store, prefix="humanpause", circuit={})
    paused = store.set_jar_paused(jar.id, True)
    assert paused.paused_reason is None


def test_circuit_config_endpoint_enforced(store: Store) -> None:
    jar = _two_agent_jar(store, prefix="cfg", circuit={})
    store.set_jar_circuit(jar.id, {"max_spawns": 1, "window_s": 60, "max_hop_depth": 100})
    store.send(_wake_mess(jar.id, hop=0))
    with pytest.raises(CircuitBreakerTripped):
        store.send(_wake_mess(jar.id, hop=0))


def test_reply_loop_halts_within_window(store: Store) -> None:
    """Acceptance test: two real Daemon instances (dry_run=True, no real CLI
    needed) exchanging question/answer messages via alternating tick() calls.
    The circuit breaker must halt the exchange within the configured window;
    dry_run guarantees zero filesystem writes occur anywhere.
    """
    from unittest.mock import patch

    from messjar.daemon.runner import Daemon

    jar = _two_agent_jar(
        store, prefix="loop", circuit={"max_spawns": 100, "window_s": 300, "max_hop_depth": 5}
    )

    daemon_a = Daemon(
        bus_url="http://unused", agent_id="a@cursor", adapter_name="cursor",
        workdir=".", jar=jar.name, dry_run=True, notify=False, auto_reply=True,
    )
    daemon_b = Daemon(
        bus_url="http://unused", agent_id="b@claude", adapter_name="claude_code",
        workdir=".", jar=jar.name, dry_run=True, notify=False, auto_reply=True,
    )
    def fake_check_jar(agent, jar_filter, ack=True):
        # Mirror what the real check_jar MCP tool does: fetch, then advance
        # each jar's cursor so the next poll only sees genuinely new messes.
        batch = store.check_jar(agent, jar_filter)
        if ack:
            for item in batch:
                store.advance_cursor(item["jar"]["id"], agent, item["cursor"])
        return batch

    def fake_send(jar_id, **k):
        return store.send(Mess.create(jar_id=jar_id, **k)).model_dump(by_alias=True)

    for d in (daemon_a, daemon_b):
        d.bus.check_jar = fake_check_jar
        d.bus.update_local = lambda *a, **k: None
        d.bus.send = fake_send

    # Seed the loop: a human asks a question expecting a reply.
    seed = Mess.create(
        jar_id=jar.id, from_agent="a@cursor", to_agent="b@claude", body="start",
        kind="question", reply_expected=True, hop=0, trigger_source="human",
    )
    store.send(seed)

    tripped = False
    with patch("messjar.daemon.runner.notify_circuit_trip"):
        for _ in range(20):  # bounded iteration guard against a runaway test
            for d in (daemon_a, daemon_b):
                try:
                    d.poll_once()
                except CircuitBreakerTripped:
                    pass
            j = store.get_jar(jar.id)
            if j.paused:
                tripped = True
                break

    assert tripped, "circuit breaker should have tripped within the iteration bound"
    j = store.get_jar(jar.id)
    assert j.paused_reason and "hop_depth" in j.paused_reason


def test_send_rest_backward_compat_and_circuit_429(
    database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A v1-shaped /send payload (no trigger_source) still works, and a
    tripped breaker surfaces as 429 through the real REST route — not just
    when calling Store.send() directly."""
    monkeypatch.delenv("MESSJAR_PASSWORD", raising=False)
    store = Store(database_url, min_size=1, max_size=2)
    app = create_app(store)
    with TestClient(app) as client:
        name = f"rest-{uuid.uuid4().hex[:8]}"
        password = f"pw-{uuid.uuid4().hex}"
        created = client.post(
            "/api/jars",
            json={
                "name": name,
                "password": password,
                "agents": ["a@cursor", "b@claude"],
                "circuit": {"max_spawns": 1, "window_s": 60, "max_hop_depth": 100},
            },
        )
        assert created.status_code == 200, created.text

        headers = {"Authorization": f"Bearer {password}"}
        payload = {
            "jar": name,
            "from": "a@cursor",
            "to": "b@claude",
            "body": "hi",
            "kind": "question",
        }
        first = client.post("/send", json=payload, headers=headers)
        assert first.status_code == 200, first.text
        assert first.json()["trigger_source"] == "human"

        second = client.post("/send", json=payload, headers=headers)
        assert second.status_code == 429, second.text

        cfg = client.post(
            f"/jars/{name}/circuit", json={"max_spawns": 50}, headers=headers
        )
        assert cfg.status_code == 200, cfg.text
        assert cfg.json()["circuit"]["max_spawns"] == 50
    store.close()


def test_join_assigns_agent_id(database_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MESSJAR_PASSWORD", raising=False)
    store = Store(database_url, min_size=1, max_size=2)
    app = create_app(store)
    with TestClient(app) as client:
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
            "/rpc/call",
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

        # Streamable HTTP MCP initialize (what Cursor expects at /mcp)
        init = client.post(
            "/mcp",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "0"},
                },
            },
        )
        assert init.status_code == 200, init.text
        payload = init.json()
        assert payload.get("result", {}).get("serverInfo", {}).get("name") == "messjar"
    store.close()


def test_adapters_registered() -> None:
    from messjar.daemon.adapters import ADAPTERS

    assert set(ADAPTERS) >= {"claude_code", "cursor", "codex", "opencode"}
