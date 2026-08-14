"""Daemon: poll the bus, dispatch Messes through an adapter, optionally reply."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from messjar.client import BusClient
from messjar.daemon.adapters import get_adapter
from messjar.localstate import killswitch_state
from messjar.notify import notify_circuit_trip, notify_mess, notify_unverified_ref
from messjar.refs import has_verifiable_ref, verify_ref
from messjar.schema import WAKE_KINDS, MessKind

log = logging.getLogger("messjar.daemon")


class Daemon:
    def __init__(
        self,
        *,
        bus_url: str,
        agent_id: str,
        adapter_name: str,
        workdir: str | Path,
        jar: str | None = None,
        dry_run: bool = False,
        poll_s: float = 2.0,
        max_hops: int = 32,
        auto_reply: bool = True,
        password: str | None = None,
        notify: bool = True,
    ) -> None:
        self.bus = BusClient(bus_url, password=password)
        self.agent_id = agent_id
        self.adapter = get_adapter(adapter_name)
        self.workdir = str(Path(workdir).expanduser().resolve())
        self.jar_filter = jar
        self.dry_run = dry_run
        self.poll_s = poll_s
        self.max_hops = max_hops
        self.auto_reply = auto_reply
        self.notify = notify
        self._stop = False
        self._notified_trips: set[str] = set()

    def stop(self) -> None:
        self._stop = True

    def run_forever(self) -> None:
        log.info(
            "daemon start agent=%s adapter=%s workdir=%s dry_run=%s available=%s",
            self.agent_id,
            self.adapter.name,
            self.workdir,
            self.dry_run,
            self.adapter.available(),
        )
        while not self._stop:
            try:
                self.tick()
            except Exception:
                log.exception("poll failed")
            time.sleep(self.poll_s)

    def tick(self) -> int:
        """One loop iteration: local kill switch, then circuit-trip check, then poll.

        The kill switch is checked first and short-circuits before any
        network call — it must work with the bus completely unreachable.
        """
        ks = killswitch_state()
        if ks is not None:
            log.warning(
                "local kill switch engaged (%s); skipping poll",
                ks.get("reason") or "no reason given",
            )
            return 0
        self._check_circuit_trips()
        return self.poll_once()

    def _check_circuit_trips(self) -> None:
        try:
            if self.jar_filter:
                jars = [self.bus.get_jar(self.jar_filter)]
            else:
                jars = self.bus.list_jars(self.agent_id)
        except Exception:
            log.debug("circuit-trip check failed", exc_info=True)
            return
        for jar in jars:
            jid = jar.get("id")
            reason = jar.get("paused_reason")
            if jar.get("paused") and reason:
                if jid not in self._notified_trips:
                    notify_circuit_trip(jar)
                    self._notified_trips.add(jid)
                    log.warning("circuit breaker tripped jar=%s reason=%s", jar.get("name"), reason)
            else:
                self._notified_trips.discard(jid)

    def poll_once(self) -> int:
        batch = self.bus.check_jar(self.agent_id, self.jar_filter, ack=True)
        handled = 0
        for item in batch:
            jar = item["jar"]
            for mess in item["messes"]:
                self._handle(jar, mess)
                handled += 1
        return handled

    def _handle(self, jar: dict[str, Any], mess: dict[str, Any]) -> None:
        kind = mess.get("kind")
        hop = int(mess.get("hop") or 0)
        log.info(
            "recv jar=%s id=%s kind=%s from=%s hop=%s",
            jar.get("name"),
            mess.get("id"),
            kind,
            mess.get("from"),
            hop,
        )

        if self.notify:
            ok = notify_mess(jar, mess)
            log.info("desktop notify %s", "sent" if ok else "skipped")

        local = (jar.get("local") or {}).get(self.agent_id) or {}
        if jar.get("paused") or local.get("paused"):
            log.info("skip paused jar/agent")
            return

        if hop >= self.max_hops:
            log.warning("hop limit %s reached; not invoking", self.max_hops)
            return

        # fyi: notify already fired; queue without spawning an agent
        wakes = kind in (k.value for k in WAKE_KINDS)
        if not wakes and kind == MessKind.fyi.value:
            log.info("fyi queued (no wake); recording only")
            self.bus.update_local(
                jar["id"],
                self.agent_id,
                workdir=self.workdir,
                hop=hop,
            )
            return

        trigger_source = mess.get("trigger_source") or "human"
        readonly = trigger_source == "agent"
        if readonly and self.adapter.name == "opencode":
            log.warning(
                "agent-triggered spawn on opencode is unsupported (no sandboxing yet); "
                "skipping jar=%s mess=%s",
                jar.get("name"),
                mess.get("id"),
            )
            return

        session_id = local.get("session_id")
        workdir = local.get("workdir") or self.workdir

        # Refs are load-bearing for answer/artifact — Mess.create() already
        # required at least one verifiable-shaped ref to exist; here we check
        # whether it actually resolves in *this* workdir. A mismatch doesn't
        # block delivery (the human/agent still needs to see the content),
        # but it must not be silently trusted — surfaced in the prompt and
        # via desktop notification instead.
        unverified_refs: list[str] = []
        if kind in (MessKind.answer.value, MessKind.artifact.value):
            unverified_refs = [
                r for r in (mess.get("refs") or []) if verify_ref(r, workdir) is False
            ]
            if unverified_refs:
                log.warning("unverified refs on mess=%s: %s", mess.get("id"), unverified_refs)
                notify_unverified_ref(jar, mess, unverified_refs)

        result = self.adapter.invoke(
            mess,
            workdir=workdir,
            session_id=session_id,
            dry_run=self.dry_run or not self.adapter.available(),
            readonly=readonly,
            label=jar.get("label"),
            unverified_refs=unverified_refs or None,
        )
        if result.dry_run:
            log.info("dry-run cmd=%s", result.command)
        if not result.ok:
            log.error("adapter failed: %s", result.error)
            return

        new_session = result.session_id or session_id
        self.bus.update_local(
            jar["id"],
            self.agent_id,
            workdir=self.workdir,
            session_id=new_session,
            hop=hop,
        )

        if (
            self.auto_reply
            and mess.get("reply_expected")
            and result.reply_body
            and result.reply_kind
        ):
            reply_kind = result.reply_kind
            reply_refs = list(result.refs) if result.refs else []
            if reply_kind in ("answer", "artifact") and not has_verifiable_ref(reply_refs):
                # No adapter today populates InvokeResult.refs with real
                # evidence, so an auto-reply can't honestly claim to be an
                # answer — Mess.create() would reject it anyway. Keep the
                # thread alive as a question rather than going quiet.
                reply_kind = "question"
            reply = self.bus.send(
                jar["id"],
                from_agent=self.agent_id,
                to_agent=mess["from"],
                body=result.reply_body,
                kind=reply_kind,
                # Mirror the incoming Mess's reply_expected so a
                # question<->answer exchange can actually continue — this is
                # the one place in the codebase that autonomously generates
                # a Mess with no human at the keyboard, hence trigger_source.
                reply_expected=mess.get("reply_expected", False),
                hop=hop + 1,
                refs=reply_refs + [f"mess:{mess['id']}"],
                trigger_source="agent",
            )
            log.info("sent reply id=%s hop=%s kind=%s", reply.get("id"), hop + 1, reply_kind)
