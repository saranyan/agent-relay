"""Envelopes, the handoff contract and stream state machines (docs/PROTOCOL.md 4-8).

Fold functions turn a stream into a state; `*_transition` functions validate a
proposed event against that state and raise if it is illegal. They are pure.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .storage import (SCHEMA_VERSION, Blocked, Conflict, Event, InvalidInput,
                      canonical, check_name, sha256)

KINDS = ("task", "result", "question", "status", "review", "blocked", "approval_request")
ACTIONS = ("continue", "finish", "ask_human", "blocked")
MAX_BODY = 64 * 1024
MAX_SUBJECT = 200
MAX_SCOPE = 4 * 1024
MAX_REFS = 32
MAX_REF_LEN = 1024

ENVELOPE_FIELDS = {
    "schema_version", "id", "pair_id", "thread_id", "turn_id", "reply_to", "from", "to",
    "sender_session", "recipient_session", "created_at", "kind", "subject", "body",
    "scope", "approval_refs", "evidence_refs",
}
OPTIONAL_IDS = ("pair_id", "turn_id", "reply_to", "sender_session", "recipient_session")
TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$")


def safe_fold(fold, events, *args):
    """Fold a verified stream; a malformed payload makes the stream untrusted, never a crash."""
    from .storage import StreamUntrusted
    try:
        return fold(events, *args)
    except (KeyError, TypeError, ValueError, AttributeError) as e:
        stream = events[0].stream if events else "?"
        raise StreamUntrusted(f"stream {stream}: malformed event payload ({type(e).__name__}: {e})")


def _need(rec: Any, what: str, fields: dict) -> None:
    if not isinstance(rec, dict):
        raise InvalidInput(f"{what} is not a JSON object")
    for k, types in fields.items():
        if not isinstance(rec.get(k), types):
            raise InvalidInput(f"{what}: field {k!r} missing or wrong type")


def validate_session_record(rec: Any, endpoint_id: str) -> dict:
    _need(rec, f"session {endpoint_id}", {"schema_version": int, "id": str, "adapter": str,
                                          "client_session_id": str, "mode": str,
                                          "participant": str, "options": dict})
    if rec["schema_version"] != SCHEMA_VERSION or rec["id"] != endpoint_id:
        raise InvalidInput(f"session {endpoint_id}: unsupported version or mismatched id")
    if rec["mode"] not in ("attach", "launch"):
        raise InvalidInput(f"session {endpoint_id}: bad mode")
    return rec


def validate_pair_def(d: Any, pair_id: str) -> dict:
    _need(d, f"pair {pair_id}", {"schema_version": int, "id": str, "thread_id": str,
                                 "leader": dict, "follower": dict, "scope": str,
                                 "max_turns": int, "turn_timeout_s": int})
    if d["schema_version"] != SCHEMA_VERSION or d["id"] != pair_id:
        raise InvalidInput(f"pair {pair_id}: unsupported version or mismatched id")
    for role in ("leader", "follower"):
        _need(d[role], f"pair {pair_id} {role}", {"endpoint": str, "participant": str})
    return d

SECRET_PATTERNS = [
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("OpenAI/Anthropic-style API key", re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_-]{20,}")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}")),
    ("AWS access key id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("bearer token", re.compile(r"(?i)\bauthorization:\s*bearer\s+[A-Za-z0-9._~+/=-]{20,}")),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
]


def secret_warnings(*texts: str) -> list[str]:
    """Heuristic only: a clean result is NOT a guarantee of no sensitive data."""
    found = []
    for label, pat in SECRET_PATTERNS:
        if any(t and pat.search(t) for t in texts):
            found.append(label)
    return found


# ------------------------------------------------------------------ envelopes
def _check_refs(refs: Any, field: str) -> list:
    if not isinstance(refs, list):
        raise InvalidInput(f"{field} must be a list")
    if len(refs) > MAX_REFS:
        raise InvalidInput(f"{field}: more than {MAX_REFS} references")
    out = []
    for r in refs:
        if isinstance(r, str):
            r = {"ref": r}
        if not isinstance(r, dict) or set(r) - {"ref", "sha256"} or not isinstance(r.get("ref"), str):
            raise InvalidInput(f"{field}: each reference is a string or {{ref, sha256}}")
        if not r["ref"] or len(r["ref"]) > MAX_REF_LEN or "\n" in r["ref"] or "\x00" in r["ref"]:
            raise InvalidInput(f"{field}: reference empty, too long or multi-line")
        if "sha256" in r and not re.fullmatch(r"[0-9a-f]{64}", str(r["sha256"])):
            raise InvalidInput(f"{field}: sha256 must be 64 lowercase hex characters")
        out.append(r)
    return out


def validate_envelope(env: Any, participants: list[str] | None = None) -> dict:
    if not isinstance(env, dict):
        raise InvalidInput("message is not a JSON object")
    if env.get("schema_version") != SCHEMA_VERSION:
        raise InvalidInput(f"unsupported message schema_version {env.get('schema_version')!r}")
    extra = set(env) - ENVELOPE_FIELDS
    missing = ENVELOPE_FIELDS - set(env)  # the relay always writes every field
    if extra or missing:
        raise InvalidInput(f"envelope fields: unknown {sorted(extra)}, missing {sorted(missing)}")
    if not isinstance(env["created_at"], str) or not TS_RE.match(env["created_at"]):
        raise InvalidInput("created_at must be a UTC timestamp like 2026-09-24T06:00:00.000Z")
    check_name(env["id"], "message id")
    check_name(env["thread_id"], "thread id")
    for f in OPTIONAL_IDS:
        if env.get(f) is not None:
            check_name(env[f], f)
    for f in ("from", "to"):
        check_name(env[f], f)
        if participants is not None and env[f] not in participants:
            raise InvalidInput(f"unknown participant in {f!r}: {env[f]!r}")
    if env["from"] == env["to"]:
        raise InvalidInput("sender and recipient must differ")
    if env["kind"] not in KINDS:
        raise InvalidInput(f"kind must be one of {', '.join(KINDS)}")
    for f, limit in (("subject", MAX_SUBJECT), ("body", MAX_BODY), ("scope", MAX_SCOPE)):
        v = env.get(f, "")
        if not isinstance(v, str):
            raise InvalidInput(f"{f} must be a string")
        size = len(v.encode()) if f != "subject" else len(v)
        if size > limit:
            raise InvalidInput(f"{f} too large: {size} > {limit} ({'bytes' if f != 'subject' else 'chars'})")
    if "\n" in env["subject"]:
        raise InvalidInput("subject must be one line")
    env["approval_refs"] = _check_refs(env.get("approval_refs", []), "approval_refs")
    env["evidence_refs"] = _check_refs(env.get("evidence_refs", []), "evidence_refs")
    return env


def content_hash(env: dict) -> str:
    return sha256(canonical({k: v for k, v in env.items() if k not in ("id", "created_at")}))


# ----------------------------------------------------------- handoff contract
BLOCK_RE = re.compile(r"```relay[ \t]*\n(.*?)\n[ \t]*```", re.S)


class ControlError(InvalidInput):
    pass


def parse_response(text: str, turn_id: str) -> tuple[str, dict]:
    """Split a client's final reply into (body, control block). Never guesses."""
    if not isinstance(text, str):
        raise ControlError("response is not text")
    blocks = list(BLOCK_RE.finditer(text))
    if not blocks:
        raise ControlError("no ```relay control block")
    if len(blocks) > 1:
        raise ControlError("more than one ```relay control block")
    m = blocks[0]
    if text[m.end():].strip():
        raise ControlError("text after the ```relay control block")
    try:
        block = json.loads(m.group(1))
    except ValueError as e:
        raise ControlError(f"control block is not JSON: {e}")
    if not isinstance(block, dict) or block.get("relay") != 1:
        raise ControlError('control block must be an object with "relay": 1')
    if block.get("turn_id") != turn_id:
        raise ControlError(f"control block turn_id {block.get('turn_id')!r} != {turn_id!r}")
    if block.get("action") not in ACTIONS:
        raise ControlError(f"action must be one of {', '.join(ACTIONS)}")
    subject = block.get("subject", "")
    if not isinstance(subject, str) or len(subject) > MAX_SUBJECT or "\n" in subject:
        raise ControlError("subject must be one line of at most 200 characters")
    kind = block.get("kind")
    if kind is not None and kind not in KINDS:
        raise ControlError(f"kind must be one of {', '.join(KINDS)}")
    body = text[:m.start()].rstrip()
    # `progress` is optional and validated separately (validate_progress): an invalid one never
    # breaks a valid route.
    # `accepted` matters only to a review-loop reviewer's finish: exactly `true`.
    return body, {"action": block["action"], "subject": subject, "kind": kind,
                  "progress": block.get("progress"), "accepted": block.get("accepted") is True}


# Optional manager progress in the reply trailer. Plain text only; the
# relay derives who reported it, for which turn and when from its own record.
PROGRESS_MAX_DONE = 3
PROGRESS_MAX_ITEM = 240
PROGRESS_MAX_BYTES = 2048
PROGRESS_FRESH_S = 600  # S3: reported now/next count as current for 10 minutes


CHECK_FIELDS = ("status", "exit", "duration_s", "output_tail", "output_bytes", "truncated", "fingerprint",
                "reason", "config")


class ProgressError(ValueError):
    pass


def _progress_text(v: Any, what: str) -> str:
    if not isinstance(v, str):
        raise ProgressError(f"{what} must be a string")
    if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", v):
        raise ProgressError(f"{what} contains control characters")
    v = " ".join(v.split())  # plain text: one line
    if not v:
        raise ProgressError(f"{what} is empty")
    if len(v) > PROGRESS_MAX_ITEM:
        raise ProgressError(f"{what} is longer than {PROGRESS_MAX_ITEM} characters")
    return v


def validate_progress(raw: Any) -> dict:
    """{"done": [<=3 str], "now": str?, "next": str?}, each <=240 chars, <=2 KB in all."""
    if not isinstance(raw, dict):
        raise ProgressError("progress must be an object")
    extra = set(raw) - {"done", "now", "next"}
    if extra:
        raise ProgressError(f"progress has unknown fields {sorted(extra)}")
    out: dict[str, Any] = {"done": []}
    done = raw.get("done", [])
    if not isinstance(done, list) or len(done) > PROGRESS_MAX_DONE:
        raise ProgressError(f"progress.done must be a list of at most {PROGRESS_MAX_DONE} strings")
    out["done"] = [_progress_text(x, f"progress.done[{i}]") for i, x in enumerate(done)]
    for k in ("now", "next"):
        if raw.get(k) is not None:
            out[k] = _progress_text(raw[k], f"progress.{k}")
    if not out["done"] and "now" not in out and "next" not in out:
        raise ProgressError("progress is empty")
    if len(json.dumps(out, ensure_ascii=False).encode()) > PROGRESS_MAX_BYTES:
        raise ProgressError(f"progress is larger than {PROGRESS_MAX_BYTES} bytes")
    warns = secret_warnings(*out["done"], out.get("now", ""), out.get("next", ""))
    if warns:
        raise ProgressError(f"possible secret in progress ({', '.join(warns)})")
    return out


def route(role: str, action: str) -> str:
    """Routing decision for a captured response (docs/PROTOCOL.md 8)."""
    if action in ("ask_human", "blocked"):
        return "human"
    if role == "follower":
        return "to_leader"  # a follower can never end the exchange
    return "finish" if action == "finish" else "to_follower"


# ------------------------------------------------------------- control stream
def fold_control(events: list[Event]) -> dict:
    st = {"paused": False, "reason": None, "seq": len(events), "since": None, "gates": 0, "fences": {}}
    for e in events:
        if e.type == "paused":
            st.update(paused=True, reason=e.data.get("reason"), since=e.at)
        elif e.type == "resumed":
            st.update(paused=False, reason=None, since=e.at)
        elif e.type == "gate":
            st["gates"] += 1
        elif e.type == "fence":
            d = e.data
            st["fences"][d["key"]] = {"gen": d["gen"], "state": d["state"], "token": d.get("token"),
                                      "seq": e.seq}
    return st


def _check_fences(st: dict, data: dict) -> None:
    for key, want in (data.get("fences") or {}).items():
        cur = st["fences"].get(key)
        if not cur or cur["gen"] != want.get("gen") or cur["state"] != "relay_owned" \
                or cur.get("token") != want.get("token"):
            raise Blocked("conversation ownership changed since this dispatch was decided"
                          + (f" (now {cur['state']})" if cur else ""))


# Who may write a conversation (relay hand-off).
OWNERSHIP_STATES = ("native", "awaiting_exit", "relay_starting", "relay_owned", "returning",
                    "released", "blocked")
OWNERSHIP_TRANSITIONS = {
    "native": ("awaiting_exit",),
    "released": ("awaiting_exit", "native"),
    "awaiting_exit": ("relay_starting", "native", "blocked"),
    # Back to awaiting_exit only when the client refused the resume because another
    # writer still holds the conversation and the refused writer verifiably exited.
    "relay_starting": ("relay_owned", "blocked", "awaiting_exit"),
    "relay_owned": ("returning", "blocked"),
    "returning": ("released", "blocked"),
    # Leaving `blocked` is always a human decision, and only to a state with no relay writer.
    "blocked": ("released", "returning"),
}


def control_transition(st: dict, etype: str, data: dict) -> tuple[str, dict] | None:
    """The control stream is the single serialization point for global pause.

    Every pair dispatch and every manual claim first appends a `gate` here; a gate
    is refused once `paused` is committed, so no work is authorized after pause.
    """
    if etype == "gate":
        if st["paused"]:
            raise Blocked(f"relay is globally paused: {st['reason']}")
        # A dispatch to a handed-off conversation names the ownership generation and
        # token it was decided under. Any ownership change since then (return, block,
        # second writer) bumped the generation here first, so the gate is refused.
        _check_fences(st, data)
        return etype, data
    if etype == "commit":
        # The point of no return for one submission to a handed-off team, appended right
        # before the send. Ownership moves (fences) live in this same stream, so the two
        # are totally ordered across processes: a return fenced first refuses the commit
        # (nothing is sent); a commit first makes the turn in flight, and the return waits.
        if not data.get("fences"):
            raise InvalidInput("a commit names the ownership it relies on")
        _check_fences(st, data)
        return etype, data
    if etype == "fence":
        # Ownership transitions for one conversation are linearized here, in the same
        # stream as dispatch gates. `expect_gen` makes each transition conditional.
        key = data.get("key")
        if not isinstance(key, str) or not key or data.get("state") not in OWNERSHIP_STATES:
            raise InvalidInput("bad ownership fence")
        cur = st["fences"].get(key, {"gen": 0, "state": "native"})
        if data.get("expect_gen") != cur["gen"]:
            raise Conflict(f"ownership changed (generation {cur['gen']}, state {cur['state']})")
        allowed = OWNERSHIP_TRANSITIONS.get(cur["state"], ())
        if data["state"] not in allowed:
            raise Conflict(f"can't go from {cur['state']} to {data['state']}")
        out = {k: v for k, v in data.items() if k != "expect_gen"}
        out["gen"] = cur["gen"] + 1
        return etype, out
    if etype == "paused":
        return None if st["paused"] else (etype, data)
    if etype == "resumed":
        return (etype, data) if st["paused"] else None
    raise InvalidInput(f"unknown control event {etype}")


# ------------------------------------------------------------- message stream
MSG_TERMINAL = ("completed", "failed")


def fold_msg(events: list[Event]) -> dict:
    st: dict[str, Any] = {"state": "unclaimed", "token": None, "by": None, "session": None,
                          "claimed_at": None, "result": None, "reason": None, "history": []}
    for e in events:
        d = e.data
        st["history"].append({"seq": e.seq, "type": e.type, "at": e.at})
        if e.type == "claimed":
            st.update(state="claimed", token=d["token"], by=d.get("by"), session=d.get("session"),
                      claimed_at=e.at)
        elif e.type == "acked":
            st["state"] = "received"
        elif e.type == "completed":
            st.update(state="completed", result=d.get("result"))
        elif e.type == "failed":
            st.update(state="failed", reason=d.get("reason"))
        elif e.type in ("released", "abandoned"):
            st.update(state="unclaimed", token=None, by=None, session=None, claimed_at=None,
                      reason=d.get("reason"))
    return st


def msg_transition(st: dict, etype: str, data: dict) -> tuple[str, dict] | None:
    state = st["state"]
    if etype == "claimed":
        if state != "unclaimed":
            raise Conflict(f"message already {state}" + (f" by {st['by']}" if st["by"] else ""))
        return etype, data
    if etype == "abandoned":
        if state not in ("claimed", "received"):
            raise Conflict(f"nothing to recover: message is {state}")
        return etype, dict(data, prior_token=st["token"], prior_by=st["by"])
    if state in MSG_TERMINAL:
        raise Conflict(f"message already {state}; no further transitions")
    if state == "unclaimed":
        raise Conflict("message is not claimed")
    if data.get("token") != st["token"]:
        raise Conflict("claim token does not match the current claim")
    if etype == "acked":
        if state == "received":
            return None  # duplicate ack is a no-op
        return etype, data
    if etype in ("completed", "failed", "released"):
        return etype, data
    raise InvalidInput(f"unknown message event {etype}")


# ------------------------------------------------------------- session stream
def fold_session(events: list[Event]) -> dict:
    st: dict[str, Any] = {"state": "registered", "pair": None, "connection": None, "closed": None}
    for e in events:
        d = e.data
        if e.type == "reserved":
            st.update(state="reserved", pair=d["pair"])
        elif e.type == "released":
            st.update(state="registered", pair=None, connection=None)
        elif e.type == "connected":
            st.update(state="connected", connection=d.get("connection"))
        elif e.type == "identity_confirmed":
            st.update(client_session_id=d["client_session_id"], cwd=d.get("cwd"))
        elif e.type == "disconnected":
            if st["state"] == "connected":
                st.update(state="reserved" if st["pair"] else "registered", connection=None)
        elif e.type == "closed":
            st.update(state="closed", closed=d.get("reason"), connection=None)
    return st


def session_transition(st: dict, etype: str, data: dict) -> tuple[str, dict] | None:
    if st["state"] == "closed":
        raise Conflict("session endpoint is closed")
    if etype == "reserved":
        if st["pair"] == data["pair"]:
            return None
        if st["pair"]:
            raise Conflict(f"session already reserved by pair {st['pair']}")
        return etype, data
    if etype == "released":
        if st["pair"] != data.get("pair"):
            return None
        return etype, data
    if etype == "identity_confirmed":
        prior = st.get("client_session_id")
        if prior and prior != data.get("client_session_id"):
            raise Conflict(f"session already confirmed as {prior}")
        return None if prior == data.get("client_session_id") and st.get("cwd") == data.get("cwd") \
            else (etype, data)
    if etype in ("connected", "disconnected", "closed"):
        return etype, data
    raise InvalidInput(f"unknown session event {etype}")


# ---------------------------------------------------------------- pair stream
TURN_END = ("response_captured", "turn_failed", "delivery_uncertain", "turn_timed_out")
ABANDON_STATEMENT = "End management and give back; this turn may have run and will not be resent."


def unknown_turns(st: dict) -> list[dict]:
    """Turns whose outcome nobody knows and no human has disposed of: uncertain or timed out,
    or still in flight on an ended team (nothing will ever settle it). Confirmed writer exit
    is a separate fact: it never answers what a turn did."""
    out = []
    for t in st["turns"]:
        if t["turn"] in st["disposed"]:
            continue
        if t["phase"] in ("uncertain", "timed_out") or \
                (st["terminal"] and t["phase"] in ("submitting", "accepted")):  # "authorized": never sent
            out.append(t)
    return out


def fold_pair(events: list[Event], definition: dict) -> dict:
    st: dict[str, Any] = {
        "pair_id": definition["id"], "owner": None, "started": False, "paused": False,
        "pause_reason": None, "terminal": None, "terminal_reason": None,
        "max_turns": definition["max_turns"], "used": 0, "queued": None, "inflight": None,
        "attention": None, "turns": [], "last_message": None, "late": [], "seq": len(events),
        "detached": [], "pending_route": None, "initial_message": None,
        "disposed": [],  # turns a human reconciled or explicitly abandoned (unknown outcome)
        "progress": [],  # manager progress reports, attributed from the relay record
        "progress_rejected": [], "updated_at": None,
        # Review loop: relay-run checks per implementer revision, and acceptance.
        "checks": [], "check_running": None, "accepted_work": None,
    }
    turns: dict[str, dict] = {}
    for e in events:
        d, t = e.data, e.type
        st["updated_at"] = e.at
        if t == "controller_acquired":
            st["owner"] = {"ctl": d["ctl"], "pid": d.get("pid"), "host": d.get("host"), "at": e.at}
        elif t == "controller_released":
            st["owner"] = None
        elif t == "started":
            st["started"] = True
            st["initial_message"] = d["initial_message"]
            st["queued"] = {"message": d["initial_message"], "to": d["to"], "role": d["role"]}
        elif t == "queued":
            st["queued"] = {"message": d["message"], "to": d["to"], "role": d["role"]}
            st["pending_route"] = None
        elif t == "dispatch_authorized":
            st["used"] += 1
            turn = {"turn": d["turn"], "message": d["message"], "to": d["to"], "role": d["role"],
                    "phase": "authorized", "request_id": d["request_id"], "authorized_at": e.at,
                    "authorized_seq": e.seq, "timeout_at": d.get("timeout_at")}
            turns[d["turn"]] = turn
            st["turns"].append(turn)
            st["inflight"] = turn
            st["queued"] = None
        elif t == "submit_attempted":
            turns[d["turn"]]["phase"] = "submitting"
        elif t == "progress_reported":
            turn = turns[d["turn"]]
            st["progress"].append({"turn": d["turn"], "role": turn["role"], "message": turn.get("response"),
                                   "at": e.at, "seq": e.seq, "done": list(d["done"]),
                                   "now": d.get("now"), "next": d.get("next")})
        elif t == "check_started":
            st["check_running"] = {"revision": d["revision"], "at": e.at, "seq": e.seq}
        elif t == "check_finished":
            st["check_running"] = None
            st["checks"].append(dict({k: d.get(k) for k in CHECK_FIELDS}, revision=d["revision"],
                                     at=e.at, seq=e.seq))
        elif t == "accepted_work":
            st["accepted_work"] = {"revision": d["revision"], "check_seq": d["check_seq"],
                                   "fingerprint": d["fingerprint"], "at": e.at}
        elif t == "progress_rejected":
            st["progress_rejected"].append({"turn": d["turn"], "reason": d.get("reason"), "at": e.at})
        elif t == "accepted":
            turns[d["turn"]].update(phase="accepted", client_turn_id=d.get("client_turn_id"))
        elif t in TURN_END:
            turn = turns[d["turn"]]
            turn["phase"] = {"response_captured": "completed", "turn_failed": "failed",
                             "delivery_uncertain": "uncertain", "turn_timed_out": "timed_out"}[t]
            turn["reason"] = d.get("reason")
            if t == "response_captured":
                turn.update(response=d["message"], action=d["action"], accepted=bool(d.get("accepted")))
                st["last_message"] = d["message"]
                st["pending_route"] = {"turn": d["turn"], "message": d["message"],
                                       "action": d["action"], "role": turn["role"],
                                       "accepted": bool(d.get("accepted"))}
            st["inflight"] = None
            if t == "turn_failed" and d.get("requeue"):
                # Never sent: the same message waits again and the turn isn't counted.
                st["queued"] = {"message": turn["message"], "to": turn["to"], "role": turn["role"]}
                st["used"] -= 1
            elif t == "turn_failed":
                st["attention"] = {"status": d.get("status", "failed"), "reason": d.get("reason"),
                                   "turn": d["turn"], "evidence": d.get("evidence")}
            elif t == "delivery_uncertain":
                st["attention"] = {"status": "delivery_uncertain", "reason": d.get("reason"),
                                   "turn": d["turn"]}
            elif t == "turn_timed_out":
                st["attention"] = {"status": "waiting_human", "reason": "turn timed out; the client "
                                   "may still be working", "turn": d["turn"]}
        elif t == "client_waiting":
            if st["inflight"] and st["inflight"]["turn"] == d.get("turn"):
                st["inflight"]["waiting"] = d.get("reason")
                # The client's own request id (each approval is its own decision).
                st["inflight"]["waiting_request"] = d.get("request") if d.get("reason") else None
                st["inflight"]["waiting_seq"] = e.seq if d.get("reason") else None
                st["inflight"]["waiting_at"] = e.at if d.get("reason") else None
        elif t == "late_response":
            st["late"].append({"turn": d["turn"], "message": d["message"], "action": d.get("action")})
        elif t == "human_needed":
            st["attention"] = {"status": d.get("status", "waiting_human"), "reason": d.get("reason"),
                               "turn": d.get("turn"), "message": d.get("message")}
            if d.get("routed"):
                st["pending_route"] = None
        elif t == "finished":
            st.update(terminal="finished", terminal_reason=d.get("message"), pending_route=None)
        elif t == "stopped":
            st.update(terminal="stopped", terminal_reason=d.get("reason"), pending_route=None)
        elif t == "paused":
            st.update(paused=True, pause_reason=d.get("reason"))
        elif t == "resumed":
            st.update(paused=False, pause_reason=None)
        elif t == "budget_extended":
            st["max_turns"] += int(d["add"])
        elif t == "outcome_abandoned":
            st["disposed"].append(d["turn"])
            turns[d["turn"]]["abandoned"] = d["statement"]  # its outcome stays unknown in history
        elif t == "reconciled":
            if d.get("turn"):
                st["disposed"].append(d["turn"])
            st["attention"] = None
            if d.get("drop_queue") or d.get("requeue"):
                st["pending_route"] = None
            if d.get("drop_queue"):
                st["queued"] = None
            if d.get("requeue"):
                st["queued"] = d["requeue"]
        elif t == "detached":
            st["detached"].append(d.get("endpoint"))
        # Where each attention began: stamped once, when it is set.
        if st["attention"] is not None and "seq" not in st["attention"]:
            st["attention"].update(seq=e.seq, at=e.at)
    st["status"] = pair_status(st)
    return st


def pair_status(st: dict) -> str:
    if st["terminal"]:
        return st["terminal"]
    if st["attention"]:
        return st["attention"]["status"]
    if st["inflight"]:
        if st["inflight"].get("waiting"):
            return "waiting_human"
        return "working" if st["inflight"]["phase"] == "accepted" else "delivering"
    if st["paused"]:
        return "paused"
    if st["queued"] or st.get("pending_route"):
        return "queued"
    return "idle"


def pair_transition(st: dict, etype: str, data: dict) -> tuple[str, dict] | None:
    term = st["terminal"]
    infl = st["inflight"]

    def need_owner():
        if not st["owner"] or st["owner"]["ctl"] != data.get("ctl"):
            raise Conflict("this controller does not own the pair")

    def need_turn(*phases):
        if not infl or infl["turn"] != data.get("turn"):
            raise Conflict(f"turn {data.get('turn')} is not the pair's in-flight turn")
        if phases and infl["phase"] not in phases:
            raise Conflict(f"turn {infl['turn']} is {infl['phase']}, expected {'/'.join(phases)}")

    if etype == "created":
        if st["seq"]:
            raise Conflict("pair already created")
        return etype, data
    if etype == "controller_acquired":
        if st["owner"] and st["owner"]["ctl"] == data["ctl"]:
            return None
        if st["owner"] and not data.get("takeover_from") == st["owner"]["ctl"]:
            raise Conflict(f"pair is owned by controller {st['owner']['ctl']} "
                           f"(pid {st['owner'].get('pid')} on {st['owner'].get('host')})")
        return etype, data
    if etype == "outcome_abandoned":
        if not term:
            raise Conflict("end management first: an unknown outcome is abandoned only on an ended team")
        if data.get("turn") not in [t["turn"] for t in unknown_turns(st)]:
            raise Conflict(f"turn {data.get('turn')} has no unacknowledged unknown outcome")
        if data.get("statement") != ABANDON_STATEMENT:
            raise InvalidInput("the abandonment statement must be recorded verbatim")
        return etype, data
    if etype == "check_started":
        # The relay's own check of the implementer's latest revision, before it goes to review.
        need_owner()
        pr = st["pending_route"]
        if not pr or pr["role"] != "follower" or pr["turn"] != data.get("revision"):
            raise Conflict("a check belongs to the implementer's reply waiting to be routed")
        if st["check_running"]:
            raise Conflict("a check is already running")
        if any(c["revision"] == data["revision"] for c in st["checks"]):
            return None
        return etype, {"revision": data["revision"], "ctl": data.get("ctl")}
    if etype == "check_finished":
        need_owner()
        run = st["check_running"]
        if not run or run["revision"] != data.get("revision"):
            raise Conflict("no check is running for that revision")
        if data.get("status") not in ("passed", "failed", "timed_out", "unknown", "unavailable"):
            raise InvalidInput("unknown check status")
        return etype, dict({k: data.get(k) for k in CHECK_FIELDS}, revision=data["revision"], ctl=data.get("ctl"))
    if etype == "accepted_work":
        need_owner()
        pr = st["pending_route"]
        if not pr or pr["role"] != "leader" or pr["action"] != "finish" or not pr.get("accepted"):
            raise Conflict("only the reviewer's finish with accepted: true can accept the work")
        chk = next((c for c in st["checks"] if c["seq"] == data.get("check_seq")), None)
        if not chk or chk["status"] != "passed" or chk["revision"] != data.get("revision") \
                or chk["fingerprint"] != data.get("fingerprint") or not chk["fingerprint"]:
            raise Conflict("acceptance needs the passing check of that exact revision")
        return etype, {k: data.get(k) for k in ("revision", "check_seq", "fingerprint", "ctl")}
    if etype in ("progress_reported", "progress_rejected"):
        # Recorded right after the response it came with; never affects routing.
        need_owner()
        turn = next((t for t in st["turns"] if t["turn"] == data.get("turn")), None)
        if not turn or turn["phase"] != "completed":
            raise Conflict("progress belongs to a completed turn")
        if any(p["turn"] == turn["turn"] for p in st["progress"] + st["progress_rejected"]):
            return None  # already recorded
        if etype == "progress_reported":
            if turn["role"] != "leader":
                raise Conflict("only the manager reports progress")
            ok = validate_progress({k: data.get(k) for k in ("done", "now", "next") if k in data})
            return etype, dict(ok, turn=turn["turn"], ctl=data.get("ctl"))
        return etype, {"turn": turn["turn"], "reason": str(data.get("reason") or "")[:300], "ctl": data.get("ctl")}
    if etype == "controller_released":
        if not st["owner"] or st["owner"]["ctl"] != data["ctl"]:
            return None
        return etype, data
    if etype in ("paused", "resumed", "stopped", "budget_extended", "detached", "human_needed",
                 "late_response", "reconciled") and term and etype not in ("detached", "late_response"):
        raise Conflict(f"pair is {term}")
    if etype == "paused":
        return None if st["paused"] else (etype, data)
    if etype == "resumed":
        return (etype, data) if st["paused"] else None
    if etype == "stopped":
        return etype, data
    if etype == "budget_extended":
        if not isinstance(data.get("add"), int) or data["add"] < 1:
            raise InvalidInput("budget extension must be a positive integer")
        return etype, data
    if etype in ("detached", "late_response", "human_needed"):
        return etype, data
    if etype == "client_waiting":
        need_owner()
        need_turn()
        return etype, data
    if etype == "reconciled":
        if not st["attention"]:
            raise Conflict("nothing to reconcile")
        if infl:
            raise Conflict("a turn is still in flight")
        return etype, data
    if term:
        raise Conflict(f"pair is {term}")
    if etype == "started":
        if st["started"]:
            raise Conflict("pair already started")
        if not all(isinstance(data.get(k), str) for k in ("initial_message", "to", "role")):
            raise InvalidInput("started needs initial_message, to and role")
        return etype, data
    if etype == "queued":
        if st["queued"] and st["queued"]["message"] == data.get("message"):
            return None
        if infl or st["queued"]:
            raise Conflict("pair already has a queued or in-flight handoff")
        if not st["started"]:
            raise Conflict("pair not started")
        return etype, data
    if etype == "dispatch_authorized":
        need_owner()
        if not isinstance(data.get("gate_seq"), int):
            raise Conflict("dispatch needs a control-stream gate")
        if st["paused"]:
            raise Blocked("pair is paused")
        if st["attention"]:
            raise Blocked(f"pair needs human attention: {st['attention']['reason']}")
        if infl:
            raise Conflict("a turn is already in flight")
        if not st["queued"] or st["queued"]["message"] != data["message"]:
            raise Conflict("message is not the queued handoff")
        if st["used"] >= st["max_turns"]:
            raise Blocked(f"turn budget exhausted ({st['used']}/{st['max_turns']})")
        return etype, data
    if etype == "submit_attempted":
        need_owner()
        need_turn("authorized")
        return etype, data
    if etype == "accepted":
        need_owner()
        if infl and infl["turn"] == data.get("turn") and infl["phase"] == "accepted":
            return None  # duplicate lifecycle event
        need_turn("submitting")
        return etype, data
    if etype == "response_captured":
        need_owner()
        need_turn("submitting", "accepted")
        return etype, data
    if etype == "turn_failed" and data.get("requeue"):
        # Only a turn that provably never reached the client (refused before any submit
        # was attempted) may go back on the queue; anything later needs a human.
        need_owner()
        need_turn("authorized")
        return etype, data
    if etype in ("turn_failed", "delivery_uncertain", "turn_timed_out"):
        need_owner()
        need_turn()
        return etype, data
    if etype == "finished":
        return etype, data
    raise InvalidInput(f"unknown pair event {etype}")
