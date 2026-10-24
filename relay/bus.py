"""Durable bus operations: messages, manual delivery, control, sessions, pairs.

Reads never append events. Nothing here runs message content or a model.
"""

from __future__ import annotations

import json
import secrets
import time
from typing import Any

from . import protocol as P
from .storage import (SCHEMA_VERSION, Blocked, Conflict, InvalidInput, NotFound, RelayError,
                      Root, StreamUntrusted, TimedOut, actor, canonical, check_name, new_id,
                      now_iso, sha256)

HUMAN = "human"  # pseudo-participant: may send a pair's initial request, never receives


class Bus:
    def __init__(self, root: Root):
        self.root = root

    @property
    def participants(self) -> list[str]:
        return list(self.root.config["participants"])

    @property
    def open_participants(self) -> bool:
        """`*` in config: any valid label is a participant (one label per session)."""
        return self.participants == ["*"]

    def is_participant(self, name: str) -> bool:
        if self.open_participants:
            try:
                check_name(name, "participant")
            except InvalidInput:
                return False
            return name != HUMAN
        return name in self.participants

    def _allowed(self) -> list[str] | None:
        return None if self.open_participants else self.participants + [HUMAN]

    # =========================================================== messages
    def send(self, *, sender: str, to: str, kind: str, subject: str, body: str,
             thread_id: str | None = None, reply_to: str | None = None, scope: str = "",
             approval_refs: list | None = None, evidence_refs: list | None = None,
             idempotency_key: str | None = None, pair_id: str | None = None,
             turn_id: str | None = None, sender_session: str | None = None,
             recipient_session: str | None = None) -> tuple[dict, bool, list[str]]:
        """Publish an immutable message. Returns (envelope, created, warnings)."""
        allowed_from = self._allowed()
        if sender != HUMAN and not self.is_participant(sender):
            raise InvalidInput(f"unknown sender {sender!r}; participants: {', '.join(self.participants)}")
        if not self.is_participant(to):
            raise InvalidInput(f"unknown recipient {to!r}; participants: {', '.join(self.participants)}")
        if reply_to is not None:
            parent = self.get_message(reply_to)
            thread_id = thread_id or parent["thread_id"]
        env = {
            "schema_version": SCHEMA_VERSION, "id": new_id("m"), "pair_id": pair_id,
            "thread_id": thread_id or new_id("t"), "turn_id": turn_id, "reply_to": reply_to,
            "from": sender, "to": to, "sender_session": sender_session,
            "recipient_session": recipient_session, "created_at": now_iso(), "kind": kind,
            "subject": subject, "body": body, "scope": scope,
            "approval_refs": approval_refs or [], "evidence_refs": evidence_refs or [],
        }
        P.validate_envelope(env, allowed_from)
        warnings = P.secret_warnings(subject, body, scope)
        if idempotency_key is None:
            self._publish_message(env)
            return env, True, warnings
        if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 256:
            raise InvalidInput("idempotency key must be 1-256 characters")
        # Hash what the caller supplied; a generated thread id is not part of the content.
        chash = P.content_hash(dict(env, thread_id=thread_id))
        key_name = sha256(canonical([sender, idempotency_key])) + ".json"
        record = {"schema_version": SCHEMA_VERSION, "from": sender, "content_sha256": chash,
                  "envelope": env}
        if self.root.publish(("idempotency", key_name), json.dumps(record, sort_keys=True).encode()):
            # A concurrent duplicate caller may publish this same envelope first; that is fine.
            self.root.publish(("messages", env["id"] + ".json"), self._encode(env))
            return env, True, warnings
        existing = self.root.read_json("idempotency", key_name)
        if existing.get("content_sha256") != chash:
            raise Conflict("idempotency key reused with different content",
                           existing_message=existing.get("envelope", {}).get("id"))
        env = P.validate_envelope(existing["envelope"], allowed_from)
        # A crash may have left the record without its message; finish the job.
        self.root.publish(("messages", env["id"] + ".json"), self._encode(env))
        return env, False, warnings

    def _encode(self, env: dict) -> bytes:
        return json.dumps(env, indent=2, sort_keys=True, ensure_ascii=False).encode()

    def _publish_message(self, env: dict) -> None:
        if not self.root.publish(("messages", env["id"] + ".json"), self._encode(env)):
            raise Conflict(f"message id collision: {env['id']}")

    def get_message(self, message_id: str) -> dict:
        check_name(message_id, "message id")
        env = self.root.read_json("messages", message_id + ".json")
        env = P.validate_envelope(env, self._allowed())
        if env["id"] != message_id:
            raise InvalidInput(f"message file {message_id} contains id {env['id']}")
        return env

    def all_messages(self) -> tuple[list[dict], list[dict]]:
        """Return (valid messages, errors). One bad file never hides the others."""
        valid, errors = [], []
        for name in self.root.listdir("messages"):
            if not name.endswith(".json"):
                continue
            mid = name[:-5]
            try:
                valid.append(self.get_message(mid))
            except RelayError as e:
                errors.append({"message": mid, "error": str(e)})
        return valid, errors

    # ===================================================== manual delivery
    def msg_state(self, message_id: str) -> dict:
        return P.safe_fold(P.fold_msg, self.root.read_stream(f"msg-{message_id}"))

    def control_state(self) -> dict:
        return P.safe_fold(P.fold_control, self.root.read_stream("control"))

    def inbox(self, participant: str, unread_only: bool = False) -> dict:
        check_name(participant, "participant")
        if not self.is_participant(participant):
            raise InvalidInput(f"unknown participant {participant!r}")
        msgs, errors = self.all_messages()
        items = []
        for m in msgs:
            if m["to"] != participant:
                continue
            if m["pair_id"]:
                state = "pair-routed"
            else:
                try:
                    state = self.msg_state(m["id"])["state"]
                except StreamUntrusted as e:
                    errors.append({"message": m["id"], "error": str(e)})
                    continue
            if unread_only and state != "unclaimed":
                continue
            items.append({"id": m["id"], "from": m["from"], "kind": m["kind"],
                          "subject": m["subject"], "thread_id": m["thread_id"],
                          "created_at": m["created_at"], "state": state, "pair_id": m["pair_id"]})
        items.sort(key=lambda i: (i["created_at"], i["id"]))
        return {"messages": items, "errors": errors}

    def _msg_append(self, message_id: str, etype: str, data: dict, who: dict):
        stream = f"msg-{message_id}"

        def validate(events):
            return P.msg_transition(P.safe_fold(P.fold_msg, events), etype, data)

        return self.root.append(stream, validate, who)

    def claim(self, message_id: str, as_: str, session: str | None = None) -> dict:
        m = self.get_message(message_id)
        if m["to"] != as_:
            raise Conflict(f"message is addressed to {m['to']}, not {as_}")
        if m["pair_id"]:
            raise Conflict(f"message belongs to pair {m['pair_id']}; its controller owns delivery")
        if session is not None:
            check_name(session, "session label")
        token = "k_" + secrets.token_hex(16)
        who = actor("participant", as_, session=session)
        st = self.msg_state(message_id)
        if st["state"] != "unclaimed":
            raise Conflict(f"message already {st['state']}" + (f" by {st['by']}" if st["by"] else ""))
        g = self.gate("claim", {"message": message_id, "token_sha256": sha256(token.encode())}, who)
        self._msg_append(message_id, "claimed",
                         {"token": token, "by": as_, "session": session, "gate_seq": g.seq}, who)
        return {"message": message_id, "token": token}

    def ack(self, message_id: str, token: str) -> dict:
        ev = self._msg_append(message_id, "acked", {"token": token, "state": "received"},
                              actor("participant"))
        return {"message": message_id, "state": "received", "duplicate": ev is None}

    def complete(self, message_id: str, token: str, result: str | None) -> dict:
        if result is not None:
            r = self.get_message(result)
            if r["reply_to"] != message_id:
                raise InvalidInput(f"result {result} is not a reply to {message_id}")
        self._msg_append(message_id, "completed", {"token": token, "result": result},
                         actor("participant"))
        return {"message": message_id, "state": "completed", "result": result,
                "note": "completion is the receiver's report, not independent proof"}

    def fail(self, message_id: str, token: str, reason: str) -> dict:
        self._msg_append(message_id, "failed", {"token": token, "reason": reason}, actor("participant"))
        return {"message": message_id, "state": "failed"}

    def release(self, message_id: str, token: str, reason: str) -> dict:
        self._msg_append(message_id, "released", {"token": token, "reason": reason}, actor("participant"))
        return {"message": message_id, "state": "unclaimed"}

    def recover(self, message_id: str, reason: str) -> dict:
        """Human-directed: abandon a stale claim. History is kept; nothing is replayed."""
        self.get_message(message_id)
        self._msg_append(message_id, "abandoned", {"reason": reason}, actor("human"))
        return {"message": message_id, "state": "unclaimed",
                "note": "the previous claimant may already have acted on this task"}

    def reply(self, message_id: str, sender: str, kind: str, body: str, subject: str | None = None,
              **kw) -> tuple[dict, bool, list[str]]:
        parent = self.get_message(message_id)
        if sender != parent["to"]:
            raise Conflict(f"only the recipient ({parent['to']}) can reply to {message_id}")
        return self.send(sender=sender, to=parent["from"] if parent["from"] != HUMAN else parent["to"],
                         kind=kind, subject=subject or f"Re: {parent['subject']}"[:P.MAX_SUBJECT],
                         body=body, reply_to=message_id, thread_id=parent["thread_id"],
                         pair_id=None, **kw)

    # ============================================================= control
    def _control(self, etype: str, data: dict, who: dict):
        return self.root.append("control", lambda ev: P.control_transition(
            P.safe_fold(P.fold_control, ev), etype, data), who)

    def pause(self, reason: str) -> dict:
        """Global pause. Independent of per-pair pauses; gates every new dispatch/claim."""
        self._control("paused", {"reason": reason}, actor("human"))
        return {"paused": True, "reason": reason,
                "note": "no new dispatch or claim is authorized; work already in flight continues"}

    def resume(self) -> dict:
        self._control("resumed", {}, actor("human"))
        return {"paused": False,
                "note": "pairs paused individually stay paused until `relay pair resume`"}

    def fence(self, key: str, *, expect_gen: int, state: str, who: dict, **data):
        """Conditionally move one conversation's ownership; the authoritative record."""
        return self._control("fence", dict(data, key=key, expect_gen=expect_gen, state=state), who)

    def commit_submit(self, pair_id: str, turn: str, fences: dict, who: dict):
        """Commit one submission to a handed-off team (see protocol `commit`)."""
        return self._control("commit", {"pair": pair_id, "turn": turn, "fences": fences}, who)

    def fences(self) -> dict:
        return self.control_state()["fences"]

    def gate(self, kind: str, data: dict, who: dict):
        """Append an authorization gate to the control stream (refused while paused)."""
        return self._control("gate", dict(data, kind=kind), who)

    # ============================================================ sessions
    def register_session(self, *, adapter: str, client_session_id: str, mode: str,
                         participant: str, cwd: str | None, options: dict | None = None,
                         info: dict | None = None, capabilities: dict | None = None) -> tuple[dict, bool]:
        check_name(adapter, "adapter")
        if mode not in ("attach", "launch"):
            raise InvalidInput("mode must be attach or launch")
        if not self.is_participant(participant):
            raise InvalidInput(f"unknown participant {participant!r}")
        if not isinstance(client_session_id, str) or not client_session_id or len(client_session_id) > 256:
            raise InvalidInput("client session id required")
        rec = {"schema_version": SCHEMA_VERSION, "id": new_id("s"), "adapter": adapter,
               "client_session_id": client_session_id, "mode": mode, "participant": participant,
               "cwd": cwd, "options": options or {}, "info": info or {},
               "capabilities": capabilities or {}, "registered_at": now_iso()}
        # The physical session identity is arbitrated atomically: exactly one endpoint
        # can ever represent it, so reservations cannot be bypassed through aliases.
        winner = self.claim_identity(adapter, options or {}, client_session_id, rec["id"], rec)
        if winner == rec["id"]:
            self.root.publish(("sessions", rec["id"] + ".json"),
                              json.dumps(rec, indent=2, sort_keys=True).encode())
            return self.get_session(rec["id"]), True
        s = self.get_session(winner)
        if s["participant"] != participant:
            raise Conflict(f"session already registered as {s['id']} for {s['participant']}")
        if s["state"]["state"] == "closed":
            raise Conflict(f"session was registered as {s['id']}, which is closed; a closed "
                           "session cannot be registered again")
        return s, False

    @staticmethod
    def identity_key(adapter: str, options: dict, client_session_id: str) -> str:
        from .adapters import get_adapter
        # The adapter resolves its namespace canonically, so differently spelled
        # options for the same physical client cannot create an alias.
        namespace = get_adapter(adapter, options).namespace()
        return sha256(canonical([adapter, namespace, client_session_id]))

    def claim_identity(self, adapter: str, options: dict, client_session_id: str,
                       endpoint_id: str, record: dict | None = None) -> str:
        """Bind a physical client session to one endpoint; return the winning endpoint."""
        self.root.ensure_dir("identities")
        name = self.identity_key(adapter, options, client_session_id) + ".json"
        body = {"schema_version": SCHEMA_VERSION, "adapter": adapter,
                "client_session_id": client_session_id, "endpoint": endpoint_id, "record": record}
        if self.root.publish(("identities", name), json.dumps(body, sort_keys=True).encode()):
            return endpoint_id
        existing = self.root.read_json("identities", name)
        winner = existing.get("endpoint")
        check_name(winner, "endpoint id")
        # Recover a registration interrupted between identity and session record.
        if existing.get("record") and not self.root.exists("sessions", winner + ".json"):
            self.root.publish(("sessions", winner + ".json"),
                              json.dumps(existing["record"], indent=2, sort_keys=True).encode())
        return winner

    def get_session(self, endpoint_id: str) -> dict:
        check_name(endpoint_id, "endpoint id")
        rec = self.root.read_json("sessions", endpoint_id + ".json")
        P.validate_session_record(rec, endpoint_id)
        rec["state"] = P.safe_fold(P.fold_session, self.root.read_stream(f"session-{endpoint_id}"))
        # Only a client report (identity_confirmed) makes an identity verified; the
        # registered/requested ID alone never does.
        rec["verified_client_session_id"] = rec["state"].get("client_session_id")
        rec["verified_cwd"] = rec["state"].get("cwd")
        return rec

    def sessions(self) -> list[dict]:
        out = []
        for name in self.root.listdir("sessions"):
            if name.endswith(".json"):
                try:
                    out.append(self.get_session(name[:-5]))
                except RelayError as e:
                    out.append({"id": name[:-5], "error": str(e), "adapter": None,
                                "client_session_id": None, "participant": None,
                                "state": {"state": "invalid"}})
        return out

    def session_event(self, endpoint_id: str, etype: str, data: dict, who: dict | None = None):
        def validate(events):
            return P.session_transition(P.safe_fold(P.fold_session, events), etype, data)

        return self.root.append(f"session-{endpoint_id}", validate, who or actor("controller"))

    # =============================================================== pairs
    def pair_ids(self) -> list[str]:
        return [n[:-5] for n in self.root.listdir("pairs") if n.endswith(".json")]

    def get_pair_def(self, pair_id: str) -> dict:
        check_name(pair_id, "pair id")
        return P.validate_pair_def(self.root.read_json("pairs", pair_id + ".json"), pair_id)

    def pair_state(self, pair_id: str) -> dict:
        d = self.get_pair_def(pair_id)
        events = self.root.read_stream(f"pair-{pair_id}")
        if not events:
            return {"pair_id": pair_id, "status": "incomplete", "definition": d}
        st = P.safe_fold(P.fold_pair, events, d)
        st["definition"] = d
        st["global_paused"] = self.control_state()["paused"]
        if st["global_paused"] and st["status"] in ("idle", "queued"):
            st["status"] = "paused"
        return st

    def authorize_dispatch(self, pair_id: str, data: dict, who: dict):
        """Gate on the control stream, then record the dispatch on the pair stream.

        The gate is the authorization point: once a global pause is committed no
        gate can follow it. A gate whose pair append is then refused (pair paused,
        budget, lost race) authorizes nothing and is harmless.
        """
        gate = {"pair": pair_id, "turn": data["turn"]}
        if data.get("fences"):
            gate["fences"] = data["fences"]
        g = self.gate("dispatch", gate, who)
        return self.pair_event(pair_id, "dispatch_authorized", dict(data, gate_seq=g.seq), who)

    def pair_event(self, pair_id: str, etype: str, data: dict, who: dict):
        d = self.get_pair_def(pair_id)

        def validate(events):
            if not events and etype != "created":
                raise Conflict("pair creation is incomplete")
            return P.pair_transition(P.safe_fold(P.fold_pair, events, d), etype, data)

        return self.root.append(f"pair-{pair_id}", validate, who)

    def create_pair(self, *, leader: str, follower: str, scope: str, subject: str = "",
                    max_turns: int | None = None, turn_timeout_s: int | None = None,
                    workflow: dict | None = None) -> dict:
        if leader == follower:
            raise InvalidInput("leader and follower must be different endpoints")
        ls, fs = self.get_session(leader), self.get_session(follower)
        if ls["participant"] == fs["participant"]:
            raise InvalidInput("leader and follower must be different participants")
        limits = self.root.config["limits"]
        max_turns = limits["max_turns"] if max_turns is None else max_turns
        turn_timeout_s = limits["turn_timeout_s"] if turn_timeout_s is None else turn_timeout_s
        if not (isinstance(max_turns, int) and 1 <= max_turns <= 1000):
            raise InvalidInput("max_turns must be 1-1000")
        if not (isinstance(turn_timeout_s, int) and 5 <= turn_timeout_s <= 86400):
            raise InvalidInput("turn timeout must be 5-86400 seconds")
        if len(scope.encode()) > P.MAX_SCOPE:
            raise InvalidInput("scope too large")
        d = {"schema_version": SCHEMA_VERSION, "id": new_id("p"), "thread_id": new_id("t"),
             "leader": {"endpoint": leader, "participant": ls["participant"]},
             "follower": {"endpoint": follower, "participant": fs["participant"]},
             "scope": scope, "subject": subject, "max_turns": max_turns,
             "turn_timeout_s": turn_timeout_s, "created_at": now_iso()}
        if workflow is not None:  # e.g. the review loop: fixed at creation, never edited
            d["workflow"] = workflow
        self.root.publish(("pairs", d["id"] + ".json"), json.dumps(d, indent=2, sort_keys=True).encode())
        who = actor("human")
        reserved = []
        try:
            for eid in sorted([leader, follower]):  # lock ordering: sessions first, sorted
                self.session_event(eid, "reserved", {"pair": d["id"]}, who)
                reserved.append(eid)
        except RelayError:
            for eid in reserved:
                self.session_event(eid, "released", {"pair": d["id"]}, who)
            raise
        self.pair_event(d["id"], "created", {"leader": leader, "follower": follower}, who)
        return d

    def role_of(self, d: dict, participant: str) -> str:
        return "leader" if d["leader"]["participant"] == participant else "follower"

    def start_pair(self, pair_id: str, *, body: str | None = None, subject: str | None = None,
                   message_id: str | None = None, to_leader: bool = False) -> dict:
        """Record Start with an explicit initial handoff. Never replays history."""
        d = self.get_pair_def(pair_id)
        leader, follower = d["leader"]["participant"], d["follower"]["participant"]
        who = actor("human")
        if message_id is not None:
            m = self.get_message(message_id)
            if m["from"] != leader or m["to"] != follower:
                raise InvalidInput(f"selected message must be from {leader} to {follower}")
            mode = "existing_message"
        elif body is not None:
            if to_leader:
                m, _, _ = self.send(sender=HUMAN, to=leader, kind="task",
                                    subject=subject or d["subject"] or "Initial request",
                                    body=body, thread_id=d["thread_id"], scope=d["scope"],
                                    pair_id=pair_id, idempotency_key=f"start:{pair_id}")
                mode = "leader_request"
            else:
                m, _, _ = self.send(sender=leader, to=follower, kind="task",
                                    subject=subject or d["subject"] or "Handoff",
                                    body=body, thread_id=d["thread_id"], scope=d["scope"],
                                    pair_id=pair_id, idempotency_key=f"start:{pair_id}")
                mode = "handoff"
        else:
            raise InvalidInput("provide a handoff body or --message")
        to = m["to"]
        st = self.pair_state(pair_id)
        if st.get("started"):
            if st.get("initial_message") == m["id"]:  # retry after a crash: already durable
                return {"pair": pair_id, "initial_message": m["id"], "mode": mode, "to": to,
                        "already_started": True}
            raise Conflict(f"pair already started with {st.get('initial_message')}")
        # One commit records Start and the initial queue entry together.
        self.pair_event(pair_id, "started", {"initial_message": m["id"], "mode": mode, "to": to,
                                             "role": self.role_of(d, to)}, who)
        return {"pair": pair_id, "initial_message": m["id"], "mode": mode, "to": to}

    # ============================================================ reading
    def thread(self, thread_id: str) -> dict:
        check_name(thread_id, "thread id")
        msgs, errors = self.all_messages()
        msgs = [m for m in msgs if m["thread_id"] == thread_id]
        known = {m["id"] for m in msgs}
        all_ids = {n[:-5] for n in self.root.listdir("messages") if n.endswith(".json")}
        children: dict[str | None, list[dict]] = {}
        for m in msgs:
            parent = m["reply_to"] if m["reply_to"] in known else None
            children.setdefault(parent, []).append(m)
        ordered = []

        def walk(parent, depth):
            for m in sorted(children.get(parent, []), key=lambda x: (x["created_at"], x["id"])):
                refs = []
                for r in m["approval_refs"] + m["evidence_refs"]:
                    ref = r["ref"]
                    if ref.startswith("m_"):
                        refs.append({"ref": ref, "resolved": ref in all_ids})
                    else:
                        refs.append({"ref": ref, "resolved": None})  # inert; never fetched
                item = dict(m, depth=depth, refs=refs,
                            reply_to_resolved=(m["reply_to"] is None or m["reply_to"] in known))
                ordered.append(item)
                walk(m["id"], depth + 1)

        walk(None, 0)
        return {"thread_id": thread_id, "messages": ordered, "errors": errors}

    def status(self) -> dict:
        out: dict[str, Any] = {"root": self.root.path, "participants": self.participants,
                               "errors": []}
        try:
            out["control"] = self.control_state()
        except StreamUntrusted as e:
            out["control"] = {"paused": True, "untrusted": str(e)}
        msgs, errs = self.all_messages()
        out["errors"] += errs
        counts: dict[str, int] = {}
        stale = []
        for m in msgs:
            if m["pair_id"]:
                counts["pair-routed"] = counts.get("pair-routed", 0) + 1
                continue
            try:
                st = self.msg_state(m["id"])
            except StreamUntrusted as e:
                out["errors"].append({"message": m["id"], "error": str(e)})
                continue
            counts[st["state"]] = counts.get(st["state"], 0) + 1
            if st["state"] in ("claimed", "received"):
                stale.append({"message": m["id"], "state": st["state"], "by": st["by"],
                              "session": st["session"], "since": st["claimed_at"]})
        out["messages"] = counts
        out["open_claims"] = stale
        out["sessions"] = [{"id": s["id"], "adapter": s["adapter"], "participant": s["participant"],
                            "client_session_id": s["client_session_id"], "mode": s.get("mode"),
                            "verified_client_session_id": s.get("verified_client_session_id"),
                            "cwd": s.get("cwd"), "verified_cwd": s.get("verified_cwd"),
                            "state": s["state"]} for s in self.sessions()]
        pairs = []
        for pid in self.pair_ids():
            try:
                st = self.pair_state(pid)
                pairs.append(summarize_pair(st))
            except RelayError as e:
                pairs.append({"pair": pid, "status": "untrusted", "error": str(e)})
        out["pairs"] = pairs
        return out

    def wait(self, participant: str, timeout: float, *, max_interval: float = 2.0) -> dict:
        """Bounded wait with backoff: exits on eligible work, pause or timeout."""
        deadline = time.monotonic() + timeout
        interval = 0.1
        while True:
            if self.control_state()["paused"]:
                return {"reason": "paused", "messages": []}
            ready = self.inbox(participant, unread_only=True)["messages"]
            ready = [m for m in ready if m["state"] == "unclaimed"]
            if ready:
                return {"reason": "message", "messages": ready}
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimedOut("no eligible message before timeout")
            time.sleep(min(interval, left))
            interval = min(interval * 2, max_interval)

    def rebuild_views(self) -> dict:
        """Regenerate disposable caches from authoritative streams."""
        st = self.status()
        self.root.write_view("control.json", st["control"])
        self.root.write_view("status.json", st)
        stale_tmp = []
        for name in self.root.listdir("tmp"):
            path = self.root.p("tmp", name)
            import os
            if time.time() - os.lstat(path).st_mtime > 3600:
                os.unlink(path)
                stale_tmp.append(name)
        return {"rebuilt": ["views/control.json", "views/status.json"], "removed_tmp": stale_tmp,
                "errors": st["errors"]}


def summarize_pair(st: dict) -> dict:
    d = st["definition"]
    if st.get("status") == "incomplete":
        return {"pair": d["id"], "status": "incomplete"}
    infl = st["inflight"]
    whose = None
    if infl:
        whose = infl["role"]
    elif st["queued"]:
        whose = st["queued"]["role"]
    return {
        "pair": d["id"], "status": st["status"], "paused": st["paused"],
        "global_paused": st.get("global_paused", False),
        "leader": d["leader"], "follower": d["follower"], "thread_id": d["thread_id"],
        "turns_used": st["used"], "max_turns": st["max_turns"],
        "turn_timeout_s": d["turn_timeout_s"], "whose_turn": whose,
        "inflight": infl and {k: infl.get(k) for k in ("turn", "role", "phase", "message",
                                                        "client_turn_id", "waiting")},
        "queued": st["queued"], "attention": st["attention"],
        "owner": st["owner"], "terminal": st["terminal"], "last_message": st["last_message"],
        "late_responses": st["late"],
    }
