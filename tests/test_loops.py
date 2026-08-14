"""Task 3 — loop tracking, verified refs, and mj digest."""

from __future__ import annotations

import subprocess
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import messjar.store as store_module
from messjar.daemon.adapters.base import InvokeResult, build_prompt
from messjar.daemon.runner import Daemon
from messjar.refs import has_verifiable_ref, verify_ref
from messjar.schema import Mess
from messjar.store import Store

# ---------------------------------------------------------------------------
# Pure unit — refs
# ---------------------------------------------------------------------------


def _git_repo(tmp_path: Path) -> tuple[str, str]:
    workdir = str(tmp_path)
    run = lambda *args: subprocess.run(args, cwd=workdir, check=True, capture_output=True)  # noqa: E731
    run("git", "init")
    run("git", "config", "user.email", "t@example.com")
    run("git", "config", "user.name", "t")
    (tmp_path / "f.txt").write_text("hello")
    run("git", "add", ".")
    run("git", "commit", "-m", "x")
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=workdir, check=True, capture_output=True, text=True
    ).stdout.strip()
    return workdir, sha


def test_has_verifiable_ref() -> None:
    assert has_verifiable_ref(["sha:abc123"])
    assert has_verifiable_ref(["file:README.md"])
    assert has_verifiable_ref(["test:3 passed"])
    assert not has_verifiable_ref([])
    assert not has_verifiable_ref(["mess:msg_1"])


def test_verify_ref_sha_resolves_against_real_repo(tmp_path: Path) -> None:
    workdir, sha = _git_repo(tmp_path)
    assert verify_ref(f"sha:{sha}", workdir) is True
    assert verify_ref("sha:0000000000000000000000000000000000000000", workdir) is False


def test_verify_ref_sha_false_when_not_a_repo(tmp_path: Path) -> None:
    assert verify_ref("sha:deadbeefdeadbeefdeadbeefdeadbeefdeadbeef", str(tmp_path)) is False


def test_verify_ref_file(tmp_path: Path) -> None:
    (tmp_path / "exists.txt").write_text("x")
    assert verify_ref("file:exists.txt", str(tmp_path)) is True
    assert verify_ref("file:missing.txt", str(tmp_path)) is False


def test_verify_ref_non_resolvable_types_return_none(tmp_path: Path) -> None:
    assert verify_ref("test:42 passed", str(tmp_path)) is None
    assert verify_ref("mess:msg_1", str(tmp_path)) is None
    assert verify_ref("whatever", str(tmp_path)) is None


def test_mess_create_requires_verifiable_ref_for_answer() -> None:
    with pytest.raises(ValueError, match="verifiable ref"):
        Mess.create(jar_id="jar_x", from_agent="a", to_agent="b", body="done", kind="answer")
    m = Mess.create(
        jar_id="jar_x", from_agent="a", to_agent="b", body="done", kind="answer", refs=["sha:abc"]
    )
    assert m.kind.value == "answer"


def test_mess_create_requires_verifiable_ref_for_artifact() -> None:
    with pytest.raises(ValueError, match="verifiable ref"):
        Mess.create(jar_id="jar_x", from_agent="a", to_agent="b", body="see attached", kind="artifact")


def test_mess_create_question_never_requires_refs() -> None:
    m = Mess.create(jar_id="jar_x", from_agent="a", to_agent="b", body="?", kind="question")
    assert m.refs == []


def test_build_prompt_shows_unverified_ref_warning() -> None:
    mess = {"kind": "answer", "to": "b", "from": "a", "body": "fixed it"}
    prompt = build_prompt(mess, unverified_refs=["sha:deadbeef"])
    assert "UNVERIFIED" in prompt
    assert "sha:deadbeef" in prompt


def test_build_prompt_no_warning_when_verified() -> None:
    mess = {"kind": "answer", "to": "b", "from": "a", "body": "fixed it"}
    prompt = build_prompt(mess, unverified_refs=None)
    assert "UNVERIFIED" not in prompt


# ---------------------------------------------------------------------------
# DB-backed — loop lifecycle
# ---------------------------------------------------------------------------


def _two_agent_jar(store: Store, *, prefix: str) -> object:
    name = f"{prefix}-{uuid.uuid4().hex[:8]}"
    return store.create_jar(name, ["a@cursor", "b@claude"], password=f"pw-{uuid.uuid4().hex}")


def test_question_opens_a_loop(store: Store) -> None:
    jar = _two_agent_jar(store, prefix="open")
    q = store.send(
        Mess.create(jar_id=jar.id, from_agent="a@cursor", to_agent="b@claude", body="?", kind="question")
    )
    assert q.loop_id == q.id
    assert q.loop_state == "open"


def test_answer_with_ref_closes_the_loop(store: Store) -> None:
    jar = _two_agent_jar(store, prefix="close")
    q = store.send(
        Mess.create(jar_id=jar.id, from_agent="a@cursor", to_agent="b@claude", body="?", kind="question")
    )
    store.send(
        Mess.create(
            jar_id=jar.id, from_agent="b@claude", to_agent="a@cursor", body="done",
            kind="answer", refs=[f"mess:{q.id}", "sha:abc123"],
        )
    )
    reloaded = store.get_mess(q.id)
    assert reloaded.loop_state == "answered"


def test_answer_referencing_unrelated_loop_is_a_noop(store: Store) -> None:
    jar = _two_agent_jar(store, prefix="noop")
    q = store.send(
        Mess.create(jar_id=jar.id, from_agent="a@cursor", to_agent="b@claude", body="?", kind="question")
    )
    store.send(
        Mess.create(
            jar_id=jar.id, from_agent="b@claude", to_agent="a@cursor", body="done",
            kind="answer", refs=["mess:msg_nonexistent", "sha:abc123"],
        )
    )
    reloaded = store.get_mess(q.id)
    assert reloaded.loop_state == "open"  # untouched


def test_loop_flips_to_stale_after_timeout(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    jar = _two_agent_jar(store, prefix="stale")
    q = store.send(
        Mess.create(jar_id=jar.id, from_agent="a@cursor", to_agent="b@claude", body="?", kind="question")
    )
    monkeypatch.setattr(store_module, "LOOP_TIMEOUT_S", -1)  # any open loop is instantly overdue
    loops = store.list_open_loops(jar.id)
    assert [m.id for m in loops] == [q.id]
    assert loops[0].loop_state == "stale"
    # persisted, not just computed for this call
    assert store.get_mess(q.id).loop_state == "stale"


def test_digest_across_all_jars_for_agent(store: Store) -> None:
    jar1 = _two_agent_jar(store, prefix="digest1")
    jar2 = _two_agent_jar(store, prefix="digest2")

    # jar1: one open loop waiting on b, one answered (shouldn't show up)
    waiting = store.send(
        Mess.create(jar_id=jar1.id, from_agent="a@cursor", to_agent="b@claude", body="q1", kind="question")
    )
    answered = store.send(
        Mess.create(jar_id=jar1.id, from_agent="a@cursor", to_agent="b@claude", body="q2", kind="question")
    )
    store.send(
        Mess.create(
            jar_id=jar1.id, from_agent="b@claude", to_agent="a@cursor", body="done",
            kind="answer", refs=[f"mess:{answered.id}", "sha:abc"],
        )
    )

    # jar2: one loop old enough to be stale. Backdate its ts directly rather
    # than monkeypatching LOOP_TIMEOUT_S — that constant is global, so
    # patching it would also stale jar1's still-fresh "waiting" loop above,
    # which is exactly the open-vs-stale split this test wants to tell apart.
    stale_open = store.send(
        Mess.create(jar_id=jar2.id, from_agent="a@cursor", to_agent="b@claude", body="q3", kind="question")
    )
    with store._pool.connection() as conn:
        conn.execute(
            "UPDATE messes SET ts = now() - interval '2 days' WHERE id = %s", (stale_open.id,)
        )
        conn.commit()

    entries = store.digest(agent_id="b@claude", jar_id_or_name=None)
    by_name = {e["jar"]: e for e in entries}
    assert jar1.name in by_name and jar2.name in by_name

    e1 = by_name[jar1.name]
    assert [m["id"] for m in e1["open_loops"]] == [waiting.id]
    assert [m["id"] for m in e1["waiting_on_you"]] == [waiting.id]
    assert e1["stale_loops"] == []
    assert len(e1["recent"]) >= 1

    e2 = by_name[jar2.name]
    assert e2["open_loops"] == []
    assert [m["id"] for m in e2["stale_loops"]] == [stale_open.id]


def test_digest_single_jar_filter(store: Store) -> None:
    jar = _two_agent_jar(store, prefix="digestone")
    store.send(
        Mess.create(jar_id=jar.id, from_agent="a@cursor", to_agent="b@claude", body="?", kind="question")
    )
    entries = store.digest(agent_id=None, jar_id_or_name=jar.id)
    assert len(entries) == 1
    assert entries[0]["jar"] == jar.name


# ---------------------------------------------------------------------------
# Daemon — unresolved-ref warning and answer→question downgrade
# ---------------------------------------------------------------------------


def _bare_daemon(workdir: Path) -> Daemon:
    d = Daemon(
        bus_url="http://unused", agent_id="b@claude", adapter_name="claude_code",
        workdir=str(workdir), dry_run=True, notify=False,
    )
    d.bus.update_local = MagicMock()
    return d


def test_unresolved_ref_notifies_and_flags_adapter(tmp_path: Path) -> None:
    daemon = _bare_daemon(tmp_path)
    daemon.adapter.invoke = MagicMock(return_value=InvokeResult(ok=True))
    jar = {"id": "jar_1", "name": "demo", "local": {}}
    mess = {
        "id": "msg_1", "from": "a@cursor", "to": "b@claude", "kind": "answer",
        "body": "fixed it", "refs": ["sha:deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"],
        "reply_expected": False, "hop": 0,
    }
    with patch("messjar.daemon.runner.notify_unverified_ref") as notify_mock:
        daemon._handle(jar, mess)
    notify_mock.assert_called_once()
    kwargs = daemon.adapter.invoke.call_args.kwargs
    assert kwargs["unverified_refs"] == ["sha:deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"]


def test_resolving_ref_does_not_notify(tmp_path: Path) -> None:
    workdir, sha = _git_repo(tmp_path)
    daemon = _bare_daemon(Path(workdir))
    daemon.adapter.invoke = MagicMock(return_value=InvokeResult(ok=True))
    jar = {"id": "jar_1", "name": "demo", "local": {}}
    mess = {
        "id": "msg_1", "from": "a@cursor", "to": "b@claude", "kind": "answer",
        "body": "fixed it", "refs": [f"sha:{sha}"], "reply_expected": False, "hop": 0,
    }
    with patch("messjar.daemon.runner.notify_unverified_ref") as notify_mock:
        daemon._handle(jar, mess)
    notify_mock.assert_not_called()
    kwargs = daemon.adapter.invoke.call_args.kwargs
    assert kwargs["unverified_refs"] is None


def test_auto_reply_downgrades_unevidenced_answer_to_question(tmp_path: Path) -> None:
    daemon = _bare_daemon(tmp_path)
    daemon.adapter.invoke = MagicMock(
        return_value=InvokeResult(ok=True, reply_body="done", reply_kind="answer", refs=[])
    )
    daemon.bus.send = MagicMock(return_value={"id": "msg_reply"})
    jar = {"id": "jar_1", "name": "demo", "local": {}}
    mess = {
        "id": "msg_1", "from": "a@cursor", "to": "b@claude", "kind": "question",
        "body": "?", "refs": [], "reply_expected": True, "hop": 0,
    }
    daemon._handle(jar, mess)
    kwargs = daemon.bus.send.call_args.kwargs
    assert kwargs["kind"] == "question"
    assert "mess:msg_1" in kwargs["refs"]


def test_auto_reply_keeps_answer_when_evidenced(tmp_path: Path) -> None:
    daemon = _bare_daemon(tmp_path)
    daemon.adapter.invoke = MagicMock(
        return_value=InvokeResult(ok=True, reply_body="done", reply_kind="answer", refs=["file:README.md"])
    )
    daemon.bus.send = MagicMock(return_value={"id": "msg_reply"})
    jar = {"id": "jar_1", "name": "demo", "local": {}}
    mess = {
        "id": "msg_1", "from": "a@cursor", "to": "b@claude", "kind": "question",
        "body": "?", "refs": [], "reply_expected": True, "hop": 0,
    }
    daemon._handle(jar, mess)
    kwargs = daemon.bus.send.call_args.kwargs
    assert kwargs["kind"] == "answer"
    assert "file:README.md" in kwargs["refs"]
    assert "mess:msg_1" in kwargs["refs"]
