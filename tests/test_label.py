"""Task 2 — the label: proposal/approval flow, prompt injection, both route surfaces."""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from messjar.bus.server import create_app
from messjar.daemon.adapters.base import build_prompt
from messjar.schema import LABEL_MAX_BYTES, Mess, label_diff
from messjar.store import Store

# ---------------------------------------------------------------------------
# Pure unit — no DB
# ---------------------------------------------------------------------------


def test_build_prompt_includes_label_above_message() -> None:
    mess = {"kind": "question", "to": "b@claude", "from": "a@cursor", "body": "what's next?"}
    prompt = build_prompt(mess, label="## Agreed\n- use snake_case")
    label_idx = prompt.index("use snake_case")
    body_idx = prompt.index("what's next?")
    assert label_idx < body_idx
    assert "jar label" in prompt.lower()


def test_build_prompt_omits_label_block_when_none() -> None:
    mess = {"kind": "fyi", "to": "b@claude", "from": "a@cursor", "body": "fyi"}
    prompt = build_prompt(mess, label=None)
    assert "jar label" not in prompt.lower()


def test_label_diff_shows_added_and_removed_lines() -> None:
    diff = label_diff("## Agreed\n- old rule\n", "## Agreed\n- new rule\n")
    # Each source line already starts with "- " (markdown bullet); unified
    # diff prepends its own -/+ marker on top of that.
    assert "-- old rule" in diff  # removed line: diff '-' + bullet '-'
    assert "+- new rule" in diff  # added line: diff '+' + bullet '-'


# ---------------------------------------------------------------------------
# DB-backed
# ---------------------------------------------------------------------------


def _two_agent_jar(store: Store, *, prefix: str) -> object:
    name = f"{prefix}-{uuid.uuid4().hex[:8]}"
    return store.create_jar(name, ["a@cursor", "b@claude"], password=f"pw-{uuid.uuid4().hex}")


def test_propose_label_rejects_oversized_patch(store: Store) -> None:
    jar = _two_agent_jar(store, prefix="oversize")
    with pytest.raises(ValueError, match="exceeds"):
        store.propose_label(jar.id, proposed_by="a@cursor", patch="x" * (LABEL_MAX_BYTES + 1))


def test_propose_label_rejects_non_participant(store: Store) -> None:
    jar = _two_agent_jar(store, prefix="nonpart")
    with pytest.raises(PermissionError):
        store.propose_label(jar.id, proposed_by="stranger@cursor", patch="hi")


def test_two_agent_unanimous_accept_applies(store: Store) -> None:
    jar = _two_agent_jar(store, prefix="unanimous")
    proposal = store.propose_label(jar.id, proposed_by="a@cursor", patch="## Agreed\n- use snake_case")

    after_first = store.decide_label_proposal(proposal.id, participant="a@cursor", decision="accept")
    assert after_first.status == "pending"
    assert store.get_jar(jar.id).label is None

    after_second = store.decide_label_proposal(proposal.id, participant="b@claude", decision="accept")
    assert after_second.status == "applied"
    assert store.get_jar(jar.id).label == "## Agreed\n- use snake_case"


def test_reject_vetoes_regardless_of_other_accept(store: Store) -> None:
    jar = _two_agent_jar(store, prefix="veto")
    proposal = store.propose_label(jar.id, proposed_by="a@cursor", patch="## Agreed\n- x")
    store.decide_label_proposal(proposal.id, participant="a@cursor", decision="accept")
    rejected = store.decide_label_proposal(proposal.id, participant="b@claude", decision="reject")
    assert rejected.status == "rejected"
    assert store.get_jar(jar.id).label is None
    with pytest.raises(ValueError, match="already rejected"):
        store.decide_label_proposal(proposal.id, participant="a@cursor", decision="accept")


def test_edit_then_accept_resets_other_participants_accept(store: Store) -> None:
    jar = _two_agent_jar(store, prefix="edit")
    proposal = store.propose_label(jar.id, proposed_by="a@cursor", patch="## Agreed\n- draft one")
    store.decide_label_proposal(proposal.id, participant="b@claude", decision="accept")

    edited = store.edit_label_proposal(proposal.id, participant="a@cursor", patch="## Agreed\n- draft two")
    assert edited.status == "pending"  # only a@cursor has accepted the NEW text
    assert edited.patch == "## Agreed\n- draft two"
    assert store.get_jar(jar.id).label is None  # b@claude's stale accept doesn't count

    approvals = store.list_approvals(proposal.id)
    assert [a["participant"] for a in approvals if a["decision"] == "accept"] == ["a@cursor"]

    finished = store.decide_label_proposal(proposal.id, participant="b@claude", decision="accept")
    assert finished.status == "applied"
    assert store.get_jar(jar.id).label == "## Agreed\n- draft two"


def test_origin_mess_id_round_trips_and_is_validated(store: Store) -> None:
    jar = _two_agent_jar(store, prefix="origin")
    mess = store.send(
        Mess.create(jar_id=jar.id, from_agent="a@cursor", to_agent="b@claude", body="let's agree on this", kind="fyi")
    )
    proposal = store.propose_label(
        jar.id, proposed_by="a@cursor", patch="## Agreed\n- x", origin_mess_id=mess.id
    )
    assert proposal.origin_mess_id == mess.id

    with pytest.raises(ValueError, match="origin_mess_id"):
        store.propose_label(jar.id, proposed_by="a@cursor", patch="## Agreed\n- y", origin_mess_id="msg_nonexistent")


def test_list_label_proposals_defaults_to_pending(store: Store) -> None:
    jar = _two_agent_jar(store, prefix="listing")
    p1 = store.propose_label(jar.id, proposed_by="a@cursor", patch="## Agreed\n- one")
    store.decide_label_proposal(p1.id, participant="a@cursor", decision="accept")
    store.decide_label_proposal(p1.id, participant="b@claude", decision="accept")  # applied, no longer pending
    p2 = store.propose_label(jar.id, proposed_by="b@claude", patch="## Agreed\n- two")

    pending = store.list_label_proposals(jar.id)
    assert [p.id for p in pending] == [p2.id]


# ---------------------------------------------------------------------------
# HTTP — both authenticated (CLI/agent-key) and public (web) surfaces
# ---------------------------------------------------------------------------


def test_authenticated_and_public_label_routes_agree(database_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MESSJAR_PASSWORD", raising=False)
    store = Store(database_url, min_size=1, max_size=2)
    app = create_app(store)
    with TestClient(app) as client:
        name = f"routes-{uuid.uuid4().hex[:8]}"
        password = f"pw-{uuid.uuid4().hex}"
        created = client.post(
            "/api/jars",
            json={"name": name, "password": password, "agents": ["a@cursor", "b@claude"]},
        )
        assert created.status_code == 200, created.text

        # Authenticated surface (CLI / agent-key), Bearer <jar password>.
        headers = {"Authorization": f"Bearer {password}"}
        proposed = client.post(
            f"/jars/{name}/label/propose",
            json={"agent": "a@cursor", "patch": "## Agreed\n- via cli"},
            headers=headers,
        )
        assert proposed.status_code == 200, proposed.text
        proposal_id = proposed.json()["id"]

        listed = client.get(f"/jars/{name}/label/proposals", headers=headers)
        assert listed.status_code == 200
        assert [p["id"] for p in listed.json()] == [proposal_id]

        # Public surface (web page), password in body.
        decided = client.post(
            "/api/jars/label/decide",
            json={"jar": name, "password": password, "proposal_id": proposal_id, "agent": "a@cursor", "decision": "accept"},
        )
        assert decided.status_code == 200, decided.text
        assert decided.json()["status"] == "pending"

        decided2 = client.post(
            f"/jars/{name}/label/proposals/{proposal_id}/decide",
            json={"agent": "b@claude", "decision": "accept"},
            headers=headers,
        )
        assert decided2.status_code == 200, decided2.text
        assert decided2.json()["status"] == "applied"

        final = client.get(f"/jars/{name}", headers=headers)
        assert final.json()["label"] == "## Agreed\n- via cli"
    store.close()


def test_public_propose_requires_correct_password(database_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MESSJAR_PASSWORD", raising=False)
    store = Store(database_url, min_size=1, max_size=2)
    app = create_app(store)
    with TestClient(app) as client:
        name = f"badpw-{uuid.uuid4().hex[:8]}"
        password = f"pw-{uuid.uuid4().hex}"
        client.post("/api/jars", json={"name": name, "password": password, "agents": ["a@cursor"]})
        res = client.post(
            "/api/jars/label/propose",
            json={"jar": name, "password": "wrong", "agent": "a@cursor", "patch": "x"},
        )
        assert res.status_code == 401
    store.close()


def test_mcp_update_label_creates_proposal_not_applying(database_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """The MCP tool creates a pending proposal; it must never apply immediately
    (that would let an agent propose-and-self-approve, defeating the human
    checkpoint — there is deliberately no MCP accept/reject/edit tool)."""
    monkeypatch.delenv("MESSJAR_PASSWORD", raising=False)
    store = Store(database_url, min_size=1, max_size=2)
    app = create_app(store)
    with TestClient(app) as client:
        name = f"mcp-{uuid.uuid4().hex[:8]}"
        password = f"pw-{uuid.uuid4().hex}"
        client.post("/api/jars", json={"name": name, "password": password, "agents": ["a@cursor"]})
        joined_token = store.upsert_agent_key("a@cursor")[1]

        res = client.post(
            "/rpc/call",
            headers={"Authorization": f"Bearer {joined_token}"},
            json={
                "name": "update_label",
                "arguments": {"jar": name, "agent": "a@cursor", "patch": "## Agreed\n- via mcp"},
            },
        )
        assert res.status_code == 200, res.text
        proposal = res.json()["content"][0]["json"]
        assert proposal["status"] == "pending"
        assert store.get_jar(name).label is None
    store.close()


# ---------------------------------------------------------------------------
# Acceptance test: a label decision survives a spawn with none of the
# intervening messages in context.
# ---------------------------------------------------------------------------


def test_label_respected_by_spawn_with_no_message_history(store: Store) -> None:
    jar = _two_agent_jar(store, prefix="acceptance")
    agreed_text = "## Agreed\n- use snake_case for python vars"
    proposal = store.propose_label(jar.id, proposed_by="a@cursor", patch=agreed_text)
    store.decide_label_proposal(proposal.id, participant="a@cursor", decision="accept")
    store.decide_label_proposal(proposal.id, participant="b@claude", decision="accept")
    jar = store.get_jar(jar.id)
    assert jar.label == agreed_text

    from messjar.daemon.adapters import get_adapter

    # A brand-new, unrelated message — nothing in it references the agreement,
    # and there is no history window; only the current label is available.
    fresh_mess = {
        "id": "msg_fresh",
        "jar_id": jar.id,
        "from": "a@cursor",
        "to": "b@claude",
        "body": "what should the retry timeout be?",
        "kind": "question",
        "reply_expected": True,
        "hop": 0,
        "refs": [],
        "trigger_source": "human",
    }
    adapter = get_adapter("claude_code")
    result = adapter.invoke(fresh_mess, workdir=".", session_id=None, dry_run=True, label=jar.label)
    prompt = " ".join(result.command)
    assert "use snake_case for python vars" in prompt
    assert "retry timeout" in prompt  # the actual new message is still there too
