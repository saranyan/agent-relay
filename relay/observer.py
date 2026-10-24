"""Read-only snapshot of the relay and the machine's agent sessions (dashboard M1).

Nothing here appends an event, starts a session or sends a prompt. Discovery
runs adapters' read-only `discover()` (e.g. `claude agents --json`).
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any

from .adapters import ADAPTERS, get_adapter
from .bus import Bus, summarize_pair
from .storage import RelayError, now_iso

TIMELINE_LIMIT = 200
ATTENTION = ("waiting_human", "delivery_uncertain", "failed", "disconnected", "untrusted")


class Discovery:
    """Background, rate-limited discovery of sessions on this machine.

    Each adapter is polled at most every `interval` seconds on a daemon thread
    that lives only as long as the foreground `relay serve` process.
    """

    def __init__(self, adapter_options: dict, adapters: tuple = ADAPTERS, interval: float = 10.0):
        self.options, self.adapters, self.interval = adapter_options, adapters, interval
        self.lock = threading.Lock()
        self.results: dict[str, dict] = {}
        self._stop = threading.Event()

    def refresh_once(self) -> None:
        for name in self.adapters:
            started = time.time()
            try:
                ad = get_adapter(name, self.options)
                if not ad.capabilities()["discover"]:
                    continue
                sessions = [dict(vars(s), adapter=name) for s in ad.discover()]
                res = {"sessions": sessions, "error": None, "verified_adapter": ad.verified,
                       "capabilities": ad.capabilities()}
            except RelayError as e:
                res = {"sessions": [], "error": str(e)}
            except Exception as e:  # discovery must never take the observer down
                res = {"sessions": [], "error": f"{type(e).__name__}: {e}"}
            res["checked_at"] = now_iso()
            res["took_ms"] = int((time.time() - started) * 1000)
            with self.lock:
                self.results[name] = res

    def start(self) -> None:
        def loop():
            while not self._stop.is_set():
                self.refresh_once()
                self._stop.wait(self.interval)
        threading.Thread(target=loop, name="relay-discovery", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def snapshot(self) -> dict:
        with self.lock:
            return {k: dict(v) for k, v in self.results.items()}


class Timeline:
    """Incremental reader of all event streams; re-reads only streams that grew."""

    def __init__(self, bus: Bus):
        self.bus = bus
        self.cache: dict[str, tuple[int, list[dict]]] = {}

    def events(self, limit: int = TIMELINE_LIMIT) -> tuple[list[dict], list[dict]]:
        out, errors = [], []
        for stream in self.bus.root.listdir("streams"):
            try:
                n = len(self.bus.root.listdir("streams", stream))
                cached = self.cache.get(stream)
                if cached is None or cached[0] != n:
                    evs = self.bus.root.read_stream(stream)
                    cached = (n, [_record(e) for e in evs])
                    self.cache[stream] = cached
                out.extend(cached[1])
            except RelayError as e:
                errors.append({"stream": stream, "error": str(e)})
        out.sort(key=lambda e: (e["at"], e["stream"], e["seq"]))
        return out[-limit:], errors


def _excerpt(text: str, n: int = 280) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _msg_view(m: dict) -> dict:
    return {"id": m["id"], "from": m["from"], "to": m["to"], "kind": m["kind"],
            "subject": m["subject"], "excerpt": _excerpt(m["body"]), "at": m["created_at"],
            "turn_id": m.get("turn_id"), "reply_to": m.get("reply_to")}


def _conversation(bus: Bus, thread_id: str) -> dict:
    """Task (first message) and the latest messages of a team's thread, for display."""
    try:
        msgs = bus.thread(thread_id)["messages"]
    except RelayError as e:
        return {"task": None, "recent": [], "count": 0, "error": str(e)}
    msgs = sorted(msgs, key=lambda m: (m["created_at"], m["id"]))
    return {"task": _msg_view(msgs[0]) if msgs else None,
            "recent": [_msg_view(m) for m in msgs[-3:]], "count": len(msgs)}


# Fields that act as credentials: they authorize state changes, so they are never
# shown. Each removal is listed by label in the record's `redacted` field.
REDACT = {"ctl": "controller ownership token", "token": "claim token",
          "token_sha256": "claim token hash", "takeover_from": "previous controller token"}


def _brief(data: dict) -> dict:
    """Event payload for display, without credential fields (see `REDACT`)."""
    return {k: v for k, v in data.items() if k not in REDACT}


def _record(e) -> dict:
    """A complete, display-safe event record: stream, sequence, id, actor and payload."""
    redacted = [f"{k} ({label})" for k, label in REDACT.items() if k in e.data]
    act = {k: v for k, v in (e.actor or {}).items() if k in ("kind", "pid", "host", "session")}
    if (e.actor or {}).get("id"):
        if str(e.actor["id"]).startswith("ctl_"):
            redacted.append("actor.id (controller ownership token)")
        else:
            act["id"] = e.actor["id"]
    return {"stream": e.stream, "seq": e.seq, "event_id": e.event_id, "type": e.type, "at": e.at,
            "actor": act, "data": _brief(e.data), "redacted": redacted}


def pair_command_hints(pid: str, att: dict | None) -> list[str]:
    if not att:
        return []
    reason = att.get("reason") or ""
    if "ask_human" in reason or "blocked" in reason or att.get("message"):
        return [f"relay pair answer {pid} --body-file answer.md"]
    if att["status"] == "delivery_uncertain":
        return [f"relay pair reconcile {pid} --outcome requeue --reason \"...\"",
                f"relay pair reconcile {pid} --outcome drop --reason \"...\""]
    if "budget" in reason:
        return [f"relay pair extend {pid} --add 2",
                f"relay pair reconcile {pid} --outcome continue --reason \"...\""]
    return [f"relay pair reconcile {pid} --outcome continue --reason \"...\"",
            f"relay pair answer {pid} --body-file answer.md"]


def build_state(bus: Bus, discovery: Discovery | None, timeline: Timeline) -> dict[str, Any]:
    status = bus.status()
    registered = {s["id"]: s for s in bus.sessions()}
    teams, needs, errors = [], [], list(status["errors"])

    for p in status["pairs"]:
        pid = p["pair"]
        if "leader" not in p:  # untrusted or incomplete
            teams.append({"id": pid, "status": p.get("status"), "error": p.get("error"),
                          "members": [], "edges": []})
            needs.append({"kind": "untrusted", "severity": "critical", "team": pid,
                          "title": f"Pair {pid} history cannot be trusted",
                          "detail": p.get("error") or p.get("status"), "commands": []})
            continue
        infl = p["inflight"] or {}
        try:
            full = bus.pair_state(pid)
            infl_since = (full.get("inflight") or {}).get("authorized_at")
            created_at = full["definition"].get("created_at")
            scope = full["definition"].get("scope")
        except RelayError:
            infl_since = created_at = scope = None
        members = [{"endpoint": p["leader"]["endpoint"], "role": "leader",
                    "participant": p["leader"]["participant"]},
                   {"endpoint": p["follower"]["endpoint"], "role": "follower",
                    "participant": p["follower"]["participant"]}]
        edge_state = p["status"]
        teams.append({
            "id": pid, "status": p["status"], "paused": p["paused"],
            "global_paused": p.get("global_paused"), "thread_id": p["thread_id"],
            "turns_used": p["turns_used"], "max_turns": p["max_turns"],
            "turn_timeout_s": p["turn_timeout_s"], "whose_turn": p["whose_turn"],
            "inflight": p["inflight"], "queued": p["queued"], "attention": p["attention"],
            "owner": p["owner"], "terminal": p["terminal"], "late": p["late_responses"],
            "inflight_since": infl_since, "created_at": created_at, "scope": scope,
            "conversation": _conversation(bus, p["thread_id"]),
            "members": members,
            "edges": [{"from": members[0]["endpoint"], "to": members[1]["endpoint"],
                       "label": "handoff", "active": infl.get("role") == "follower",
                       "state": edge_state},
                      {"from": members[1]["endpoint"], "to": members[0]["endpoint"],
                       "label": "result", "active": infl.get("role") == "leader",
                       "state": edge_state}],
        })
        att = p["attention"]
        if att:
            needs.append({"kind": att["status"], "team": pid,
                          "severity": "critical" if att["status"] in ("failed", "untrusted")
                          else "serious" if att["status"] == "delivery_uncertain" else "warning",
                          "title": {"waiting_human": "Waiting for you",
                                    "delivery_uncertain": "Delivery uncertain",
                                    "failed": "Turn failed",
                                    "disconnected": "Session disconnected"}.get(att["status"],
                                                                                att["status"]),
                          "detail": att.get("reason"), "message": att.get("message"),
                          "commands": pair_command_hints(pid, att)})
        if infl.get("waiting"):
            needs.append({"kind": "client_waiting", "team": pid, "severity": "warning",
                          "title": "Answer in the client's own window",
                          "detail": infl["waiting"], "commands": []})
        if p["late_responses"]:
            needs.append({"kind": "late_response", "team": pid, "severity": "warning",
                          "title": "Late response recorded, not routed",
                          "detail": ", ".join(x["message"] for x in p["late_responses"]),
                          "commands": [f"relay pair reconcile {pid} --outcome route-late "
                                       f"--message <id> --reason \"...\""]})
        if p.get("owner") is None and not p["terminal"] and p["status"] not in ("idle",):
            needs.append({"kind": "no_controller", "team": pid, "severity": "warning",
                          "title": "No controller is running this pair",
                          "detail": "nothing is routed until a controller runs it",
                          "commands": [f"relay pair run {pid}"]})

    in_team = {m["endpoint"]: t["id"] for t in teams for m in t["members"]}
    sessions = []
    for eid, s in registered.items():
        st = s.get("state", {})
        sessions.append({
            "id": eid, "managed": True, "adapter": s.get("adapter"),
            "client": _client(s.get("adapter")), "participant": s.get("participant"),
            "mode": s.get("mode"), "requested_client_session_id": s.get("client_session_id"),
            "verified_client_session_id": s.get("verified_client_session_id"),
            "cwd": s.get("verified_cwd") or s.get("cwd"),
            "cwd_verified": bool(s.get("verified_cwd")),
            "connection": st.get("state"), "team": in_team.get(eid) or st.get("pair"),
            "error": s.get("error"),
        })

    observed, discovery_status = [], {}
    if discovery is not None:
        known = {(s["adapter"], s["client_session_id"]) for s in registered.values()}
        known |= {(s["adapter"], s.get("verified_client_session_id")) for s in registered.values()}
        for name, res in discovery.snapshot().items():
            discovery_status[name] = {k: res.get(k) for k in ("error", "checked_at", "took_ms",
                                                              "verified_adapter")}
            for d in res.get("sessions", []):
                if (name, d["client_session_id"]) in known:
                    continue
                observed.append({
                    "adapter": name, "client": _client(name),
                    "client_session_id": d["client_session_id"], "cwd": d.get("cwd"),
                    "activity": d.get("activity"), "attachable": d.get("attachable"),
                    "limitations": d.get("limitations", []),
                    "name": (d.get("extra") or {}).get("name"),
                    "kind": (d.get("extra") or {}).get("kind"),
                })
                if d.get("activity") in ("waiting", "blocked"):
                    needs.append({"kind": "session_waiting", "severity": "warning",
                                  "title": f"{_client(name)} session waiting for you",
                                  "detail": f"{(d.get('extra') or {}).get('name') or d['client_session_id']}"
                                            f" in {d.get('cwd')}",
                                  "commands": []})

    if status["control"].get("paused"):
        needs.insert(0, {"kind": "global_pause", "severity": "warning",
                         "title": "Relay globally paused",
                         "detail": status["control"].get("reason"), "commands": ["relay resume"]})

    events, stream_errors = timeline.events()
    errors += stream_errors
    return {
        "generated_at": now_iso(), "root": status["root"], "pid": os.getpid(),
        "participants": status["participants"], "control": status["control"],
        "sessions": sessions, "observed": observed, "teams": teams, "needs_you": needs,
        "timeline": events, "discovery": discovery_status, "errors": errors,
        "open_claims": status["open_claims"], "read_only": True,
    }


def _client(adapter: str | None) -> str:
    if not adapter:
        return "unknown"
    if adapter.startswith("claude"):
        return "Claude"
    if adapter.startswith("codex"):
        return "Codex"
    return adapter.capitalize()
