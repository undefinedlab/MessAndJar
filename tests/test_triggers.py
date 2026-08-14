"""Task 5 — triggers: on_blocked prompt guidance, on_push/on_session_end/on_ci_fail
planners, and the full five-task tie-together (a triggered handoff still needs
a human's sign-off before it reaches anyone)."""

from __future__ import annotations

import subprocess
import uuid
from pathlib import Path

import pytest

from messjar import policy
from messjar.daemon.adapters.base import build_prompt
from messjar.daemon.triggers.on_blocked import BLOCKED_GUIDANCE
from messjar.daemon.triggers.on_ci_fail import plan_ci_fail_notice
from messjar.daemon.triggers.on_push import plan_push_notice
from messjar.daemon.triggers.on_session_end import git_session_changes, plan_session_end_notice
from messjar.schema import HeldMessage, Mess
from messjar.store import Store

OWNED_BY_LABEL = "## Owned by\n- b@claude: /src/billing\n"

# ---------------------------------------------------------------------------
# on_blocked — always in the prompt, unconditionally
# ---------------------------------------------------------------------------


def test_blocked_guidance_in_every_prompt() -> None:
    prompt = build_prompt({"kind": "fyi", "to": "a", "from": "b", "body": "hi"})
    assert BLOCKED_GUIDANCE in prompt


def test_blocked_guidance_present_even_with_label_and_warnings() -> None:
    prompt = build_prompt(
        {"kind": "answer", "to": "a", "from": "b", "body": "done", "refs": ["sha:bad"]},
        label="## Agreed\n- x",
        unverified_refs=["sha:bad"],
    )
    assert BLOCKED_GUIDANCE in prompt


# ---------------------------------------------------------------------------
# on_push — pure
# ---------------------------------------------------------------------------


def test_plan_push_notice_fans_out_excluding_sender() -> None:
    planned = plan_push_notice(
        agents=["a@cursor", "b@claude", "c@codex"], from_agent="a@cursor", sha="abc123def456", branch="main"
    )
    assert {p["to_agent"] for p in planned} == {"b@claude", "c@codex"}
    for p in planned:
        assert p["kind"] == "fyi"
        assert "sha:abc123def456" in p["refs"]
        assert "main" in p["body"]


def test_plan_push_notice_solo_jar_sends_nothing() -> None:
    assert plan_push_notice(agents=["a@cursor"], from_agent="a@cursor", sha="abc") == []


# ---------------------------------------------------------------------------
# on_session_end — pure planner + git I/O
# ---------------------------------------------------------------------------


def test_plan_session_end_notice_empty_changes_is_noop() -> None:
    assert (
        plan_session_end_notice(
            agents=["a@cursor", "b@claude"], from_agent="a@cursor", label=OWNED_BY_LABEL,
            changed_paths=[], summary="",
        )
        == []
    )


def test_plan_session_end_notice_targets_declared_owner() -> None:
    planned = plan_session_end_notice(
        agents=["a@cursor", "b@claude"], from_agent="a@cursor", label=OWNED_BY_LABEL,
        changed_paths=["/src/billing/invoices.py"], summary="1 file(s) changed",
    )
    assert len(planned) == 1
    assert planned[0]["to_agent"] == "b@claude"
    assert planned[0]["kind"] == "handoff"
    assert planned[0]["reply_expected"] is True
    assert "path:/src/billing/invoices.py" in planned[0]["refs"]


def test_plan_session_end_notice_self_owned_path_is_not_someone_elses_problem() -> None:
    label = "## Owned by\n- a@cursor: /src/api\n"
    planned = plan_session_end_notice(
        agents=["a@cursor", "b@claude"], from_agent="a@cursor", label=label,
        changed_paths=["/src/api/routes.py"], summary="1 file(s) changed",
    )
    # a@cursor owns the changed path themselves — falls through to the broadcast
    # fallback (no *other* agent's scope was matched), not a handoff to themselves.
    assert all(p["to_agent"] != "a@cursor" for p in planned)
    assert all(p["kind"] == "fyi" for p in planned)


def test_plan_session_end_notice_matches_leading_slash_variance() -> None:
    """Regression: `## Owned by` is conventionally written /like/this, but
    git status --porcelain reports paths relative to the repo root with NO
    leading slash (found live during manual verification — a real change
    under a declared owner's path fell back to the broadcast instead of
    targeting them, because "src/billing/x".startswith("/src/billing") is
    False). Both sides must normalize the same way."""
    planned = plan_session_end_notice(
        agents=["a@cursor", "b@claude"], from_agent="a@cursor", label=OWNED_BY_LABEL,
        changed_paths=["src/billing/invoices.py"], summary="1 file(s) changed",  # no leading slash
    )
    assert len(planned) == 1
    assert planned[0]["to_agent"] == "b@claude"
    assert planned[0]["kind"] == "handoff"


def test_plan_session_end_notice_broadcasts_when_no_owner_matches() -> None:
    planned = plan_session_end_notice(
        agents=["a@cursor", "b@claude", "c@codex"], from_agent="a@cursor", label=None,
        changed_paths=["/misc/notes.md"], summary="1 file(s) changed",
    )
    assert {p["to_agent"] for p in planned} == {"b@claude", "c@codex"}
    assert all(p["kind"] == "fyi" for p in planned)


def test_git_session_changes_detects_a_real_change(tmp_path: Path) -> None:
    workdir = str(tmp_path)
    run = lambda *a: subprocess.run(a, cwd=workdir, check=True, capture_output=True)  # noqa: E731
    run("git", "init")
    run("git", "config", "user.email", "t@example.com")
    run("git", "config", "user.name", "t")
    (tmp_path / "f.txt").write_text("hello")
    run("git", "add", ".")
    run("git", "commit", "-m", "x")

    summary, paths = git_session_changes(workdir)
    assert summary == "" and paths == []  # clean tree, nothing to report

    (tmp_path / "f.txt").write_text("hello world")
    summary, paths = git_session_changes(workdir)
    assert paths == ["f.txt"]
    assert "f.txt" in summary


def test_git_session_changes_not_a_repo_fails_closed_to_empty(tmp_path: Path) -> None:
    assert git_session_changes(str(tmp_path)) == ("", [])


# ---------------------------------------------------------------------------
# on_ci_fail — pure
# ---------------------------------------------------------------------------


def test_plan_ci_fail_notice_targets_declared_owner() -> None:
    planned = plan_ci_fail_notice(
        agents=["a@cursor", "b@claude"], from_agent="a@cursor", label=OWNED_BY_LABEL,
        failing_paths=["/src/billing/invoices.py"], summary="tests failed", sha="deadbeef",
    )
    assert len(planned) == 1
    assert planned[0]["to_agent"] == "b@claude"
    assert planned[0]["kind"] == "question"
    assert "path:/src/billing/invoices.py" in planned[0]["refs"]
    assert "sha:deadbeef" in planned[0]["refs"]


def test_plan_ci_fail_notice_matches_leading_slash_variance() -> None:
    planned = plan_ci_fail_notice(
        agents=["a@cursor", "b@claude"], from_agent="a@cursor", label=OWNED_BY_LABEL,
        failing_paths=["src/billing/invoices.py"], summary="tests failed",  # no leading slash
    )
    assert len(planned) == 1
    assert planned[0]["to_agent"] == "b@claude"


def test_plan_ci_fail_notice_uses_fallback_when_no_match() -> None:
    planned = plan_ci_fail_notice(
        agents=["a@cursor", "b@claude"], from_agent="a@cursor", label=None,
        failing_paths=["/misc/x.py"], summary="tests failed", fallback_to="b@claude",
    )
    assert len(planned) == 1
    assert planned[0]["to_agent"] == "b@claude"


def test_plan_ci_fail_notice_no_match_no_fallback_sends_nothing() -> None:
    planned = plan_ci_fail_notice(
        agents=["a@cursor", "b@claude"], from_agent="a@cursor", label=None,
        failing_paths=["/misc/x.py"], summary="tests failed",
    )
    assert planned == []


def test_plan_ci_fail_notice_fallback_ignored_if_not_a_participant() -> None:
    planned = plan_ci_fail_notice(
        agents=["a@cursor", "b@claude"], from_agent="a@cursor", label=None,
        failing_paths=["/misc/x.py"], summary="tests failed", fallback_to="stranger@cursor",
    )
    assert planned == []


# ---------------------------------------------------------------------------
# DB-backed: triggered sends go through the exact same policy gate
# ---------------------------------------------------------------------------


def _two_agent_jar(store: Store, *, prefix: str, label: str | None = None) -> object:
    name = f"{prefix}-{uuid.uuid4().hex[:8]}"
    jar = store.create_jar(name, ["a@cursor", "b@claude"], password=f"pw-{uuid.uuid4().hex}")
    if label is not None:
        with store._pool.connection() as conn:
            conn.execute("UPDATE jars SET label = %s WHERE id = %s", (label, jar.id))
            conn.commit()
        jar = store.get_jar(jar.id)
    return jar


def test_triggered_push_fyi_delivers_immediately(store: Store) -> None:
    jar = _two_agent_jar(store, prefix="trigpush")
    planned = plan_push_notice(agents=jar.agents, from_agent="a@cursor", sha="abc123def456")
    for p in planned:
        mess = Mess.create(
            jar_id=jar.id, from_agent="a@cursor", to_agent=p["to_agent"], body=p["body"],
            kind=p["kind"], refs=p["refs"], trigger_source="agent",
        )
        result = policy.send(store, mess)
        assert isinstance(result, Mess)
        assert result.seq is not None


def test_triggered_session_end_handoff_is_held_not_delivered(store: Store) -> None:
    jar = _two_agent_jar(store, prefix="trigsession", label=OWNED_BY_LABEL)
    planned = plan_session_end_notice(
        agents=jar.agents, from_agent="a@cursor", label=jar.label,
        changed_paths=["/src/billing/invoices.py"], summary="1 file(s) changed",
    )
    assert len(planned) == 1 and planned[0]["kind"] == "handoff"
    p = planned[0]
    mess = Mess.create(
        jar_id=jar.id, from_agent="a@cursor", to_agent=p["to_agent"], body=p["body"],
        kind=p["kind"], reply_expected=p["reply_expected"], refs=p["refs"], trigger_source="agent",
    )
    result = policy.send(store, mess)
    assert isinstance(result, HeldMessage)  # Task 4's gate applies to trigger-sourced messages too
    assert store.list_messes(jar.id) == []


# ---------------------------------------------------------------------------
# Acceptance test — the five-task tie-together
# ---------------------------------------------------------------------------


def test_acceptance_session_end_handoff_needs_human_approval_then_shows_in_digest(store: Store) -> None:
    """'your agent finishes, notices the next step isn't yours, and hands it
    off before you close the laptop' — without that handoff ever bypassing
    a human. Label (Task 2) declares ownership; loop tracking (Task 3)
    reflects it in digest; the policy gate (Task 4) holds it; the trigger
    (Task 5) is what generated it in the first place."""
    jar = _two_agent_jar(store, prefix="acceptance")
    proposal = store.propose_label(jar.id, proposed_by="a@cursor", patch=OWNED_BY_LABEL)
    store.decide_label_proposal(proposal.id, participant="a@cursor", decision="accept")
    store.decide_label_proposal(proposal.id, participant="b@claude", decision="accept")
    jar = store.get_jar(jar.id)
    assert jar.label == OWNED_BY_LABEL

    summary, changed_paths = "1 file(s) changed: invoices.py", ["/src/billing/invoices.py"]
    planned = plan_session_end_notice(
        agents=jar.agents, from_agent="a@cursor", label=jar.label,
        changed_paths=changed_paths, summary=summary,
    )
    assert len(planned) == 1
    p = planned[0]
    mess = Mess.create(
        jar_id=jar.id, from_agent="a@cursor", to_agent=p["to_agent"], body=p["body"],
        kind=p["kind"], reply_expected=p["reply_expected"], refs=p["refs"], trigger_source="agent",
    )

    held = policy.send(store, mess)
    assert isinstance(held, HeldMessage)
    # not yet visible to the recipient at all
    digest_before = store.digest(agent_id="b@claude", jar_id_or_name=jar.id)
    assert digest_before[0]["waiting_on_you"] == []

    sent = store.approve_held_message(held.id, approved_by="a@cursor")
    assert sent.to_agent == "b@claude"

    digest_after = store.digest(agent_id="b@claude", jar_id_or_name=jar.id)
    assert [m["id"] for m in digest_after[0]["waiting_on_you"]] == [sent.id]
