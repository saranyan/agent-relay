"""Session console service: the engine behind `relay serve`'s simple UI.

Plain-language model for the page:
* a **session** is any Claude/Codex session the relay can see;
* it is **managed by you** (default) or **managed by AI**: another session (the
  manager) leads it, and it follows;
* **manage** starts that, **intervene** pauses it so you can speak, **say** sends
  your message and hands back, **resume** hands back without a message, and
  **release** ends AI management.

Every action goes through the same bus operations as the CLI, recorded with
actor `human (console)`. Controllers for teams started here run on threads inside
the foreground `relay serve` process and stop with it.
"""

from __future__ import annotations

import hashlib
import os
import re
import shlex
import socket
import threading
import time
import uuid
from typing import Any

from . import personalities as PERS
from . import notify as NOTIFY
from . import verify as V
from .context import STALE_AFTER_S, ContextStore, context_key, valid_start
from .adapters import get_adapter
from .bus import HUMAN, Bus
from .controller import Controller, abandon_unknown, answer, reconcile
from .handoff import CLAIMS_LOCK, Handoffs, resume_command, team_holds, unknown_hold
from .protocol import ABANDON_STATEMENT, PROGRESS_FRESH_S, unknown_turns
from .observer import Discovery, _client
from .storage import Blocked, Conflict, InvalidInput, NotFound, RelayError, Unsupported, actor, pid_alive

TERMINAL = ("finished", "stopped")
STEPPED_IN = "You stepped in"
RETURNING = "Returning to the terminal"
HANDOFF_BLOCKED = "Hand-off blocked"
CONSOLE_STOPPED = "The console stopped"
WHO = actor("human", "console")
CLIENT_ADAPTERS = {"claude": "claude-stream", "codex": "codex-app-server"}


def _slug(text: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", text or "").strip("-.").lower()
    return (s or "session")[:40]


def _epoch(iso: str) -> float:
    """UTC ISO timestamp → epoch seconds."""
    import calendar
    try:
        return calendar.timegm(time.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S"))
    except (ValueError, TypeError):
        return time.time()


def _exit_hint(client: str) -> str:
    return "type /exit" if (client or "").lower() == "claude" else "press Ctrl-C twice or type /quit"


def _age_s(iso: str | None, now: float | None = None) -> float | None:
    """Seconds since a relay timestamp (YYYY-MM-DDTHH:MM:SS.mmmZ), or None."""
    if not iso:
        return None
    try:
        import calendar
        base, _, frac = iso.rstrip("Z").partition(".")
        t = calendar.timegm(time.strptime(base, "%Y-%m-%dT%H:%M:%S")) + (float("0." + frac) if frac else 0)
    except ValueError:
        return None
    return max(0.0, (now if now is not None else time.time()) - t)


def _trim(text: str, n: int) -> str:
    t = " ".join((text or "").split())
    return t if len(t) <= n else t[: n - 1] + "…"


class Host:
    def __init__(self, bus: Bus, discovery: Discovery | None, *,
                 new_session_adapters: dict | None = None, adapter_options: dict | None = None,
                 read_only: bool = False, check_runner=None, notify_config: "NOTIFY.Config | None" = None):
        self.bus, self.discovery = bus, discovery
        self.check_runner = check_runner  # review loop checks; default: the isolated runner
        # Decision notifications: off until you configure and verify a channel.
        self.notify_config = notify_config or NOTIFY.Config(os.environ.get("AGENT_RELAY_NOTIFY_CONFIG") or None)
        self.notifier = NOTIFY.Notifier(bus.root, self.notify_config, self.open_decisions)
        self._notify_stop = threading.Event()
        self._notify_thread: threading.Thread | None = None
        self.new_adapters = new_session_adapters or dict(CLIENT_ADAPTERS)
        self.options = adapter_options or {}
        self.read_only = read_only
        self.lock = threading.RLock()
        self.hosted: dict[str, dict] = {}  # pair id -> {thread, stop, error}
        self.context = ContextStore(bus)
        self.labels: dict[str, str] = {}   # endpoint id -> display label (from the last catalog)
        self.handoffs = Handoffs(bus, self.options, self._discover_one)
        self._ticker: threading.Thread | None = None
        self._ticker_stop = threading.Event()

    # ================================================================ reading
    def _teams(self) -> dict[str, dict]:
        out = {}
        for pid in self.bus.pair_ids():
            try:
                out[pid] = self.bus.pair_state(pid)
            except RelayError as e:
                out[pid] = {"pair_id": pid, "status": "untrusted", "error": str(e)}
        return out

    def _active_team_of(self, eid: str, teams: dict) -> tuple[str, dict, str] | None:
        for pid, st in teams.items():
            d = st.get("definition")
            if not d or st.get("status") in TERMINAL or st.get("terminal"):
                continue
            for role in ("leader", "follower"):
                if d[role]["endpoint"] == eid:
                    return pid, st, role
        return None

    def _last_team_of(self, eid: str, teams: dict) -> tuple[str, dict, str] | None:
        best = None
        for pid, st in teams.items():
            d = st.get("definition")
            if not d:
                continue
            for role in ("leader", "follower"):
                if d[role]["endpoint"] == eid and (best is None or d["created_at"] > best[1]["definition"]["created_at"]):
                    best = (pid, st, role)
        return best

    def _discovered(self) -> list[dict]:
        out = []
        for name, res in (self.discovery.snapshot() if self.discovery else {}).items():
            for d in res.get("sessions", []):
                out.append(dict(d, adapter=name))
        return out

    @staticmethod
    def _client_of(adapter: str, info: dict | None) -> str:
        c = (info or {}).get("client")
        return c if c else _client(adapter)

    def _name_of(self, info: dict | None, cwd: str | None, fallback: str) -> str:
        n = (info or {}).get("name")
        if n:
            return n
        return os.path.basename(cwd.rstrip("/")) if cwd else fallback

    def catalog(self) -> dict[str, Any]:
        teams = self._teams()
        registered = self.bus.sessions()
        found = self._discovered()
        found_by = {(d["adapter"], d["client_session_id"]): d for d in found}
        names = {}
        sessions = []

        for s in registered:
            if "error" in s:
                continue
            disc = found_by.pop((s["adapter"], s["client_session_id"]), None)
            if s.get("verified_client_session_id"):
                found_by.pop((s["adapter"], s["verified_client_session_id"]), None)
            info = dict(s.get("info") or {})
            info.update({k: v for k, v in ((disc or {}).get("extra") or {}).items() if v not in (None, "")})
            launch_opts = (s.get("options") or {}).get("launch") or {}
            info.setdefault("client", launch_opts.get("client"))
            info.setdefault("name", launch_opts.get("name"))
            cwd = s.get("verified_cwd") or s.get("cwd") or (disc or {}).get("cwd")
            item = {
                "key": s["id"], "endpoint": s["id"], "adapter": s["adapter"],
                "client": self._client_of(s["adapter"], info), "cwd": cwd,
                "name": self._name_of(info, cwd, s["participant"]),
                "personality": (s.get("options") or {}).get("personality"),
                "started_by_relay": s["mode"] == "launch", "connection": s["state"]["state"],
                "activity": (disc or {}).get("activity") or ("offline" if s["state"]["state"] == "closed" else "unknown"),
                "client_session_id": s.get("verified_client_session_id") or s["client_session_id"],
                "meta": self._meta(info), "proc": {"pid": info.get("pid"), "started_ms": info.get("started_ms")},
                "handoff": bool((s.get("options") or {}).get("handoff")),
            }
            names[s["id"]] = item["name"]
            sessions.append(item)

        for d in found_by.values():
            extra = d.get("extra") or {}
            sessions.append({
                "key": f"d:{d['adapter']}:{d['client_session_id']}", "endpoint": None,
                "adapter": d["adapter"], "client": self._client_of(d["adapter"], extra),
                "cwd": d.get("cwd"), "name": self._name_of(extra, d.get("cwd"), d["client_session_id"][:8]),
                "personality": None, "started_by_relay": False, "connection": "not connected",
                "activity": d.get("activity") or "unknown", "attachable": d.get("attachable"),
                "limitations": d.get("limitations", []),
                "client_session_id": d["client_session_id"], "meta": self._meta(extra),
                "proc": {"pid": extra.get("pid"), "started_ms": extra.get("started_ms")},
                "interactive": bool(extra.get("interactive")),
            })

        self._disambiguate(sessions, names)
        for item in sessions:
            self._ownership(item)
            self._describe(item, teams, names)
            self._attach_context(item, teams)
            own = (item.get("ownership") or {}).get("state")
            if own in ("awaiting_exit", "relay_starting", "relay_owned", "returning", "blocked") \
                    and not item.get("team") and item.get("endpoint"):
                last = self._last_team_of(item["endpoint"], teams)
                if last:  # its team ended, but the relay still has it: link back to give it back
                    item["handoff_team"] = last[0]
                    item["why_not"] = ("The relay still has this conversation from a finished team. "
                                       "Open it to give it back to your terminal.")
        # A manager the relay started is gone once its job is over; don't list it.
        sessions = [x for x in sessions if not (x["started_by_relay"] and x["state"] == "offline")]
        order = {"needs_you": 0, "working": 1, "waiting": 2, "idle": 3, "unknown": 3, "done": 4, "offline": 5}
        sessions.sort(key=lambda x: (order.get(x["state"], 3), x["name"].lower()))
        self.labels = {x["endpoint"]: x["name"] for x in sessions if x["endpoint"]}
        return {"sessions": sessions, "personalities": [
            {"id": k, "name": v["name"], "blurb": v["blurb"]} for k, v in PERS.PERSONALITIES.items()],
            "new_session_clients": [c for c in ("claude", "codex") if c in self.new_adapters],
            "read_only": self.read_only, "paused": self.bus.control_state()["paused"],
            "fake_mode": bool(self.new_adapters) and all(a == "fake" for a in self.new_adapters.values())}

    @staticmethod
    def _meta(info: dict) -> dict:
        """Context the client itself reports, discovered read-only (never prompts)."""
        m = {"_identity": info.get("identity") or "exact", "_match": info.get("match"),
             "_candidate": info.get("candidate_thread")}
        if info.get("title"):
            m["title"] = {"text": info["title"], "source": info.get("title_source") or "client metadata",
                          "at": info.get("title_updated")}
        if info.get("summary"):  # e.g. fake sessions' --summary; real clients don't provide one
            m["summary"] = {"text": info["summary"], "source": info.get("summary_source") or "client metadata",
                            "at": None}
        return m

    @staticmethod
    def _disambiguate(sessions: list, names: dict) -> None:
        """Two sessions in one folder get the same name; add the client, then a title or id."""
        from collections import Counter
        n = Counter(x["name"] for x in sessions)
        for x in sessions:
            if n[x["name"]] > 1:
                x["name"] = f"{x['name']} · {x['client']}"
        n = Counter(x["name"] for x in sessions)
        for x in sessions:
            if n[x["name"]] > 1:
                x["name"] = f"{x['name']} · {(x.get('client_session_id') or '')[:6]}"
        for x in sessions:
            if x["endpoint"]:
                names[x["endpoint"]] = x["name"]

    def _attach_context(self, item: dict, teams: dict) -> None:
        """Title, work summary and managed goal, each with where it came from and when.

        Only a conversation with a confirmed identity gets a context key: nothing is
        ever saved against a guessed conversation. Your values stay pinned per field
        until cleared; reports and client metadata are alternatives in Details.
        """
        meta = item.pop("meta", {}) or {}
        proc = item.pop("proc", {}) or {}
        item["identity"] = meta.pop("_identity", "exact")
        item["identity_note"] = meta.pop("_match", None)
        candidate = meta.pop("_candidate", None)
        item["context_key"] = None
        if item["identity"] != "exact" and candidate:
            # A likely conversation is confirmed only by a report about that conversation
            # whose process ancestry reaches this very process (pid and start time).
            ckey = context_key(item["client"], candidate)
            rep = self.context.get(ckey).get("report") or {}
            for field in ("summary", "title"):
                if rep.get(field) and self._judge_report(item, proc, rep[field]) == "the session itself":
                    item.update(identity="exact", identity_note="confirmed by its own report",
                                client_session_id=candidate)
                    break
        if item["identity"] == "exact":
            try:
                item["context_key"] = context_key(item["client"], item.get("client_session_id") or "")
            except InvalidInput:
                pass
        stored = self.context.get(item["context_key"]) if item["context_key"] else {}
        cands = {"title": [], "summary": []}
        for field in cands:
            mine = (stored.get("manual") or {}).get(field)
            if mine and mine["text"]:
                cands[field].append({"text": mine["text"], "source": "you", "at": mine["at"], "kind": "manual"})
            rep = (stored.get("report") or {}).get(field)
            if rep and rep["text"]:
                verdict = self._judge_report(item, proc, rep)
                if verdict:
                    cands[field].append({"text": rep["text"], "source": verdict, "at": rep["at"], "kind": "report"})
            if field in meta:
                cands[field].append(dict(meta[field], kind="client"))
        now = time.time()
        for field in cands:
            for c in cands[field]:
                c["stale"] = bool(c.get("at") and now - _epoch(c["at"]) > STALE_AFTER_S)
        item["title"] = cands["title"][0] if cands["title"] else None
        item["summary"] = cands["summary"][0] if cands["summary"] else None
        item["managed_goal"] = self._relay_summary(item, teams)  # shown separately, never replaces yours
        # Everything else known, for Details: nothing is hidden, only ranked.
        item["context_sources"] = ([dict(c, field="title") for c in cands["title"][1:]] +
                                   [dict(c, field="summary") for c in cands["summary"][1:]])
        item["folder"] = item.get("cwd")

    @staticmethod
    def _judge_report(item: dict, proc: dict, rep: dict) -> str | None:
        """How a report may be shown, or None if it doesn't attach to this session.

        Same exact conversation identity (the stream key) and the same canonical
        folder establish an association; process ancestry that reaches this session's
        own process corroborates it. Neither grants anything."""
        if not item.get("cwd") or not rep.get("cwd") or \
                os.path.realpath(item["cwd"]) != os.path.realpath(rep["cwd"]):
            return None
        chain = (rep.get("reporter") or {}).get("ancestry") or []
        pid, started = proc.get("pid"), proc.get("started_ms")
        # Corroboration needs the same pid AND a valid start time on both sides that agree;
        # missing or invalid start evidence can't rule out pid reuse, so it never corroborates.
        if pid and valid_start(started):
            for a in chain:
                if a.get("pid") == pid and valid_start(a.get("started_ms")) \
                        and abs(a["started_ms"] - started) < 2000:
                    return "the session itself"
        return "reported context; reporter not verified"

    def _relay_summary(self, item: dict, teams: dict) -> dict | None:
        pid = item.get("team")
        st = teams.get(pid) if pid else None
        if not st or not st.get("definition"):
            return None
        d = st["definition"]
        goal = _trim(d.get("subject") or "", 120)
        text = goal if item.get("role") == "follower" else f"Managing: {goal}"
        return {"text": text, "source": "the relay (the goal it is managing)", "at": d.get("created_at"),
                "kind": "relay", "stale": False}

    def set_context(self, key: str, *, title: str | None = None, summary: str | None = None) -> dict:
        """Your title/summary for a listed session. Describes it; grants no control.
        Sessions whose conversation isn't confirmed have no key, so nothing is ever
        saved against a guessed conversation."""
        self._check_writable()
        listed = {x.get("context_key") for x in self.catalog()["sessions"]} - {None}
        if not key or key not in listed:
            raise NotFound("that session isn't in the list, or its conversation isn't confirmed")
        self.context.save(key, title=title, summary=summary, source="manual", by=WHO)
        return {"ok": True}

    def _describe(self, item: dict, teams: dict, names: dict) -> None:
        """Fill managed_by / state / doing / can_manage in plain words."""
        eid = item["endpoint"]
        active = self._active_team_of(eid, teams) if eid else None
        item["team"] = None
        if active:
            pid, st, role = active
            d = st["definition"]
            other = d["follower" if role == "leader" else "leader"]["endpoint"]
            other_name = names.get(other, "another session")
            goal = self._goal(st)
            infl = st.get("inflight") or {}
            item["team"] = pid
            item["role"] = role
            if role == "follower":
                if self._stepped_in(st):  # you explicitly took control with Intervene
                    item["managed_by"] = {"kind": "you", "manager": other_name}
                else:
                    item["managed_by"] = {"kind": "ai", "manager": other_name, "manager_endpoint": other}
            else:
                item["managed_by"] = {"kind": "manager", "managing": other_name}
            status = st["status"]
            if role == "leader":  # the manager: describe the team, but the question belongs to the session
                kind = self.attention_kind(st)
                if infl.get("role") == "leader":
                    item["state"], item["doing"] = "working", f"Reviewing {other_name}'s work"
                elif kind and kind not in ("approval",):
                    item["state"], item["doing"] = "waiting", f"Managing {other_name} · waiting for you"
                else:
                    item["state"], item["doing"] = "waiting", f"Managing {other_name}: {goal}"
            elif status == "waiting_human" or infl.get("waiting"):
                item["state"] = "needs_you"
                item["doing"] = self._question(st) or "Waiting for you"
            elif status == "paused" or st.get("paused"):
                item["state"] = "waiting"
                item["doing"] = "Paused — you stepped in" if self._stepped_in(st) else "Paused"
            elif infl.get("role") == role:
                item["state"] = "working"
                item["doing"] = (f"Working on: {goal}" if role == "follower" else f"Reviewing {other_name}'s work")
            elif infl:
                item["state"] = "waiting"
                item["doing"] = (f"Waiting for {other_name} to review" if role == "follower"
                                 else f"Managing {other_name}: {goal}")
            elif status in ("delivery_uncertain", "failed", "disconnected", "untrusted"):
                item["state"] = "needs_you"
                item["doing"] = (st.get("attention") or {}).get("reason") or "Needs your attention"
            else:
                item["state"] = "waiting"
                item["doing"] = f"Managing {other_name}" if role == "leader" else f"Managed by {other_name}"
            item["can_manage"], item["why_not"] = False, None
            item["hosted_error"] = (self.hosted.get(pid) or {}).get("error")
            return

        # No team: nobody has been assigned. "Managed by you" needs an explicit assignment.
        item["managed_by"] = {"kind": "none"}
        last = self._last_team_of(eid, teams) if eid else None
        reported = ((item.get("meta") or {}).get("summary") or {}).get("text")
        summary = _trim(reported or "", 90)  # only what the client itself reported
        act = item["activity"]
        if act in ("busy", "working"):
            item["state"], item["doing"] = "working", f"Working: {summary}" if summary else "Working"
        elif act in ("waiting", "blocked"):
            item["state"], item["doing"] = "needs_you", "Waiting for you in its own window"
        elif act in ("offline", "exited", "disconnected") or item.get("connection") == "closed":
            item["state"], item["doing"] = "offline", "Not running"
        elif act == "idle":
            if last and last[1].get("status") == "finished":
                item["state"], item["doing"] = "done", f"Done: {self._goal(last[1])}"
            else:
                item["state"], item["doing"] = "idle", f"Idle · last: {summary}" if summary else "Idle"
        else:  # no evidence either way
            item["state"], item["doing"] = "unknown", "Activity unknown"
        item["can_manage"], item["why_not"] = self._manageable(item)

    def _manageable(self, item: dict) -> tuple[bool, str | None]:
        if item["state"] == "offline":
            return False, "This session isn't running."
        if item.get("route") == "handoff":
            own = item.get("ownership") or {}
            if own.get("state") not in ("native", "released"):
                return False, own.get("reason") or "This conversation is already being handed over."
            return True, None
        try:
            caps = get_adapter(item["adapter"], self.options).capabilities()
        except RelayError as e:
            return False, str(e)
        if item.get("interactive") and not item["endpoint"] and caps.get("attach_live"):
            return False, item.get("handoff_unavailable") or "It's open in a terminal the relay can't drive."
        if caps.get("attach_live") and not caps.get("exclusive_input") and not item["endpoint"]:
            # An idle check followed by a submit isn't a lock. Without a client
            # that serializes its own input, someone typing at that moment would be merged in.
            return False, ("The relay can't stop someone typing into this session at the same moment, "
                           "so it can't manage it automatically yet.")
        if not caps.get("attach_live") and not item["endpoint"]:
            if item["client"] == "Claude":
                return False, ("It's open in your terminal, and Claude doesn't let another program "
                               "drive a session that's already open.")
            return False, "The relay can see this session but can't drive it."
        if item["endpoint"] and item.get("started_by_relay") and item.get("connection") == "closed":
            return False, "This session has ended."
        return True, None

    def _goal(self, st: dict) -> str:
        d = st.get("definition") or {}
        return _trim(d.get("subject") or "", 90) or "its goal"

    def _question(self, st: dict) -> str | None:
        att = st.get("attention") or {}
        if att.get("message"):
            try:
                m = self.bus.get_message(att["message"])
                return _trim(m["body"], 200)
            except RelayError:
                return None
        if att.get("reason") == STEPPED_IN:
            return None
        return att.get("reason")

    def _stepped_in(self, st: dict) -> bool:
        return (st.get("attention") or {}).get("reason") == STEPPED_IN

    @staticmethod
    def attention_kind(st: dict) -> str | None:
        """What kind of human resolution the team needs, if any.

        Only `stepped_in` (an ordinary Intervene) may be handed back as is; every
        other kind has its own resolution.
        """
        att = st.get("attention") or {}
        infl = st.get("inflight") or {}
        if att:
            reason = att.get("reason") or ""
            if reason == STEPPED_IN:
                return "stepped_in"
            if reason.startswith("Time limit reached"):
                return "deadline"
            if reason.startswith("Not accepted:"):  # carries the reviewer's message; not a question
                return "acceptance_refused"
            if att.get("message"):
                return "question"
            if att.get("status") == "delivery_uncertain":
                return "uncertain"
            if "budget" in reason:
                return "budget"
            if "timed out" in reason:
                return "timeout"
            if att.get("status") in ("failed", "disconnected", "untrusted") or "identity" in reason:
                return "failed"
            return "other"
        if infl.get("waiting"):
            return "approval"
        return None

    # ------------------------------------------------------- decisions
    NOTIFY_KINDS = ("question", "budget", "deadline", "timeout", "uncertain", "failed", "other", "acceptance_refused")

    def open_decisions(self) -> list[dict]:
        """Every unresolved actionable decision, with a durable identity. A status poll
        never makes a new one; the same decision always has the same id."""
        import calendar
        out = []

        def since(iso):
            try:
                base, _, frac = (iso or "").rstrip("Z").partition(".")
                return calendar.timegm(time.strptime(base, "%Y-%m-%dT%H:%M:%S")) + (float("0." + frac) if frac else 0)
            except ValueError:
                return None
        for pid, st in self._teams().items():
            if st.get("status") in ("untrusted", "incomplete") or "definition" not in st:
                continue
            terminal = st.get("terminal")
            kind = self.attention_kind(st)
            att = st.get("attention") or {}
            if not terminal and kind in self.NOTIFY_KINDS and att.get("seq"):
                q = None
                if kind == "question":
                    try:
                        q = self.bus.get_message(att["message"])["body"]
                    except (RelayError, KeyError):
                        q = None
                out.append({"id": f"{pid}:att:{att['seq']}", "team": pid, "kind": kind, "question": q,
                            "since": since(att.get("at"))})
            infl = st.get("inflight") or {}
            if not terminal and kind == "approval" and infl.get("waiting"):
                rid = infl.get("waiting_request") or f"seq{infl.get('waiting_seq')}"
                out.append({"id": f"{pid}:approval:{infl['turn']}:{rid}", "team": pid, "kind": "approval",
                            "question": None, "since": since(infl.get("waiting_at"))})
            if terminal:
                for t in unknown_turns(st):  # survives stopping: it still needs your acknowledgement
                    out.append({"id": f"{pid}:unknown:{t['turn']}", "team": pid, "kind": "unknown_outcome",
                                "question": None, "since": None})
            try:
                members = self._handoff_members(pid)
            except RelayError:
                members = {}
            for _eid, (_a, _c, key) in members.items():
                own = self.handoffs.own.get(key)
                ep = (own.get("detail") or {}).get("episode") or ""
                if own["state"] == "blocked":
                    out.append({"id": f"{pid}:{key[:24]}:blocked:{own['gen']}:{ep}", "team": pid,
                                "kind": "handoff_blocked", "question": None, "since": None})
                elif terminal and own["state"] == "relay_owned":
                    out.append({"id": f"{pid}:{key[:24]}:held:{own['gen']}:{ep}", "team": pid,
                                "kind": "give_back", "question": None, "since": None})
        return out

    def notify_tick(self) -> None:
        if self.notify_config.active():
            self.notifier.tick()

    def start_notifier(self, every_s: float = 5.0) -> None:
        """Background delivery for a running console; nothing happens until a channel is on."""
        if self._notify_thread and self._notify_thread.is_alive():
            return

        def loop():
            while not self._notify_stop.wait(every_s):
                try:
                    self.notify_tick()
                except Exception:  # pragma: no cover - durable outbox; try again next round
                    pass
        self._notify_thread = threading.Thread(target=loop, name="relay-notify", daemon=True)
        self._notify_thread.start()

    def notify_settings(self) -> dict:
        return self.notify_config.public()

    def notify_action(self, body: dict) -> dict:
        """The settings page's actions. A real send happens only for an explicit verification
        code or once a verified channel is on."""
        self._check_writable()
        a, ch = body.get("action"), body.get("channel")
        if a == "set_destination":
            return self.notify_config.set_destination(ch, body.get("destination") or {})
        if a == "send_code":
            return self.notify_config.start_verify(ch)
        if a == "confirm":
            return self.notify_config.confirm_verify(ch, str(body.get("code", "")))
        if a in ("enable", "disable"):
            return self.notify_config.set_enabled(ch, a == "enable")
        if a == "options":
            return self.notify_config.set_options(**{k: body[k] for k in ("display_label", "question_text", "link_base",
                                                                         "reminder") if k in body})
        if a == "resend":
            self.notifier.resend(str(body.get("key", "")), acknowledge_duplicate=bool(body.get("acknowledge_duplicate")))
            return {"ok": True}
        raise InvalidInput("unknown notification action")

    def resolve_decision_ref(self, ref: str) -> dict:
        """The opaque reference in a notification link -> the team page, behind the console's
        normal sign-in. Only a decision that is still open resolves."""
        if not re.fullmatch(r"[0-9a-f]{24}", ref or ""):
            raise InvalidInput("that link isn't a Relay decision link")
        key = self.notify_config.link_key()
        for d in self.open_decisions():
            if NOTIFY.decision_ref(d["id"], key) == ref:
                return {"team": d["team"], "kind": d["kind"]}
        raise NotFound("that decision isn't open any more")

    def _team_notices(self, pid: str) -> list[dict]:
        if not self.notify_config.load()["channels"]:
            return []
        out = []
        for d in self.open_decisions():
            if d["team"] == pid:
                out += self.notifier.status(d["id"])
        return out

    # ------------------------------------------------------------- console
    def console(self, pid: str) -> dict[str, Any]:
        st = self.bus.pair_state(pid)
        d = st["definition"]
        names, clients, cwds, pers = {}, {}, {}, None
        for role in ("leader", "follower"):
            try:
                s = self.bus.get_session(d[role]["endpoint"])
                info = dict(s.get("info") or {}, **((s.get("options") or {}).get("launch") or {}))
                cwd = s.get("verified_cwd") or s.get("cwd")
                cwds[role] = cwd
                names[role] = self.labels.get(s["id"]) or self._name_of(info, cwd, d[role]["participant"])
                clients[role] = self._client_of(s["adapter"], info)
                if role == "leader":
                    pers = (s.get("options") or {}).get("personality")
            except RelayError:
                names[role], clients[role] = d[role]["participant"], "?"
        by_participant = {d["leader"]["participant"]: "leader", d["follower"]["participant"]: "follower"}
        msgs = []
        # Causal order (reply links), not timestamps.
        for m in self.bus.thread(d["thread_id"])["messages"]:
            role = "you" if m["from"] == HUMAN else by_participant.get(m["from"], "?")
            first = m["from"] == HUMAN and m["to"] == d["leader"]["participant"] and not m.get("reply_to")
            msgs.append({"id": m["id"], "role": role, "kind": m["kind"], "subject": m["subject"],
                         "body": m["body"], "at": m["created_at"], "brief": first})
        infl = st.get("inflight") or {}
        status = st["status"]
        kind = self.attention_kind(st)
        att = st.get("attention") or {}
        asker = None
        if kind == "question":
            try:
                q = self.bus.get_message(att["message"])
                role = by_participant.get(q["from"])
                asker = {"role": role, "name": names.get(role, q["from"]), "text": _trim(q["body"], 400)}
            except RelayError:
                asker = {"role": None, "name": "The session", "text": att.get("reason") or ""}
        working_name = names.get(infl.get("role")) if infl else None
        if status in TERMINAL:
            line = "Done." if status == "finished" else "Stopped."
        elif kind == "stepped_in" and infl:
            line = f"Finishing the current turn — {working_name} is wrapping up…"
        elif kind == "stepped_in":
            line = "You're in control."
        elif kind == "question":
            line = "Waiting for your answer."
        elif kind == "deadline":
            line = "Time limit reached."
        elif kind == "acceptance_refused":
            line = "The work wasn't accepted."
        elif kind == "approval":
            line = f"{working_name} is waiting for an approval in its own window."
        elif kind == "uncertain":
            line = "The last message may not have arrived."
        elif kind == "budget":
            line = f"Used all {st.get('max_turns')} turns."
        elif kind == "timeout":
            line = "The last turn took too long."
        elif kind in ("failed", "other"):
            line = att.get("reason") or "Something went wrong."
        elif st.get("paused") or st.get("global_paused"):
            line = "Paused."
        elif infl.get("role") == "follower":
            line = f"{names['follower']} is working…"
        elif infl.get("role") == "leader":
            line = f"{names['leader']} is reviewing…"
        else:
            line = "Starting…"
        hosted = self.hosted.get(pid) or {}
        actions = {  # what the page may offer for this state
            None: ["intervene"], "approval": ["intervene"],
            "stepped_in": ["send", "hand_back"], "question": ["answer"], "acceptance_refused": ["send"],
            "deadline": [],
            "uncertain": ["send_again"], "timeout": ["send_again"], "budget": ["more_turns"],
            "failed": [], "other": [],
        }.get(kind, [])
        if status in TERMINAL:
            actions = []
        elif kind not in (None, "approval"):
            actions = actions + ["stop"]
        handoff = self._console_handoff(pid, d, names, clients, status in TERMINAL)
        return {
            "id": pid, "status": status, "line": line, "goal": d.get("subject"), "attention": kind,
            "summary": self._summary(pid, st, d, names, clients, cwds, kind, asker, line, handoff),
            "reason": att.get("reason"),
            "manager": {"name": names["leader"], "client": clients["leader"],
                        "personality": PERS.PERSONALITIES.get(pers, {}).get("name") if pers else None},
            "session": {"name": names["follower"], "client": clients["follower"]},
            "turns": [st.get("used", 0), st.get("max_turns", 0)],
            "working": infl.get("role"), "working_since": infl.get("authorized_at"),
            "asker": asker, "actions": actions,
            "in_control": kind in ("stepped_in", "question"),  # you hold the turn
            "finishing_turn": bool(kind == "stepped_in" and infl),
            "can_send": bool(kind in ("stepped_in", "question", "acceptance_refused") and not infl
                             and status not in TERMINAL),
            "review": self._review_view(d, st),
            "notices": self._team_notices(pid),
            "messages": msgs, "terminal": status in TERMINAL,
            "controller": bool(st.get("owner")), "error": hosted.get("error"),
            **handoff,
        }

    def _review_view(self, d: dict, st: dict) -> dict | None:
        wf = d.get("workflow") or {}
        if wf.get("kind") != "review_loop":
            return None
        impl = [t for t in st["turns"] if t["role"] == "follower"]
        latest = impl[-1]["turn"] if impl else None
        checks = st.get("checks") or []
        chk = next((c for c in reversed(checks) if c["revision"] == latest), None) if latest else None
        acc = st.get("accepted_work")
        if acc:
            fact = "Accepted by the reviewer, with the relay's passing check of the latest revision."
        elif wf.get("runner_unavailable"):
            fact = f"Relay checks are unavailable here ({wf['runner_unavailable']}), so this can't be accepted."
        elif not latest:
            fact = "No implementation yet; the relay checks each revision before review."
        elif impl[-1]["phase"] != "completed":
            fact = "The implementer's latest turn hasn't completed; nothing is checked yet."
        elif not chk:
            fact = "The relay hasn't checked the latest revision yet."
        else:
            fact = f"Relay check of the latest revision: {chk['status'].replace('_', ' ')}."
        left = float(wf["deadline_ts"]) - time.time()
        return {
            "fact": fact, "done_when": wf["done_when"], "accepted": bool(acc),
            "deadline_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(wf["deadline_ts"]))),
            "minutes_left": max(0, int(left // 60)), "past_deadline": left <= 0,
            "handover": {"sha256": wf["handover"]["sha256"], "bytes": wf["handover"]["bytes"],
                         "text": wf["handover"]["text"], "sources": wf["handover"].get("sources") or []},
            "check": {"argv": wf["check"]["argv"], "cwd": wf["check"]["cwd"], "timeout_s": wf["check"]["timeout_s"],
                      "runner": wf.get("runner"), "unavailable": wf.get("runner_unavailable")},
            "checks": [{"revision_n": next((i + 1 for i, t in enumerate(impl) if t["turn"] == c["revision"]), None),
                        "status": c["status"], "exit": c.get("exit"), "duration_s": c.get("duration_s"),
                        "reason": c.get("reason"), "at": c["at"], "latest": c["revision"] == latest}
                       for c in checks],
        }

    def _summary(self, pid: str, st: dict, d: dict, names: dict, clients: dict, cwds: dict,
                 kind: str | None, asker: dict | None, line: str, handoff: dict) -> dict:
        """The plain-language summary. Authoritative lifecycle facts come from the
        relay; anything the manager reported is attributed and never presented as fact, and
        lifecycle, ownership and attention always win over a reported "now"."""
        terminal = st["status"] in TERMINAL
        now = time.time()
        mgr = {"name": names.get("leader"), "client": clients.get("leader")}

        def reported(text: str | None, rep: dict) -> dict | None:
            if not text:
                return None
            age = _age_s(rep["at"], now)
            return {"text": text, "by": mgr, "at": rep["at"], "turn": rep["turn"],
                    "stale": age is None or age > PROGRESS_FRESH_S}

        reps = st.get("progress") or []
        latest = reps[-1] if reps else None
        with_done = next((r for r in reversed(reps) if r["done"]), None)
        happened = [{"text": t, "by": mgr, "at": with_done["at"], "turn": with_done["turn"]}
                    for t in with_done["done"]] if with_done else []
        facts = [f"{st.get('used', 0)} of {st.get('max_turns', 0)} turns used."]
        rv = self._review_view(d, st)
        if rv:  # relay facts, not claims: what the relay itself checked and recorded
            facts.insert(0, rv["fact"])
        if st["status"] == "finished":
            facts.insert(0, f"{names.get('leader')} ended the exchange.")
        elif st["status"] == "stopped":
            facts.insert(0, f"Stopped: {st.get('terminal_reason') or 'by you'}.")

        writers = handoff.get("writers") or []
        blocked = [w for w in writers if w.get("state") == "blocked"]
        held = [w for w in writers if w.get("state") in ("relay_owned", "blocked")]
        att = st.get("attention") or {}
        decision = None
        if handoff.get("unknown_outcome"):
            decision = {"kind": "unknown_outcome", "text": "A turn may have run, and the relay doesn't know "
                        "what it did.", "how": "Confirm below before the conversations go back."}
        elif blocked:
            decision = {"kind": "handoff_blocked", "text": "; ".join(f"{w['name']}: {w.get('reason') or w['words']}"
                                                                    for w in blocked),
                        "how": "Resolve it below."}
        elif kind == "question" and asker:
            decision = {"kind": "question", "who": asker["name"], "text": asker["text"], "how": "Answer below."}
        elif kind == "approval":
            decision = {"kind": "approval", "text": line, "how": "Approve or decline it in that session's own window."}
        elif kind == "budget":
            decision = {"kind": "budget", "text": line, "how": "Allow more turns below, or stop managing."}
        elif kind in ("uncertain", "timeout"):
            decision = {"kind": kind, "text": line, "how": "Send it again below, or stop managing."}
        elif kind == "acceptance_refused":
            decision = {"kind": kind, "text": att.get("reason") or line,
                        "how": "Tell the reviewer or the implementer what to do next below, or stop managing."}
        elif kind == "deadline":
            decision = {"kind": kind, "text": "The team used its time limit. Nothing more is sent, checked or retried.",
                        "how": "Stop managing below."}
        elif kind in ("failed", "other"):
            decision = {"kind": kind, "text": att.get("reason") or line, "how": "Stop managing below."}
        elif terminal and held:
            decision = {"kind": "give_back", "text": f"The team has ended; the relay still has "
                        f"{' and '.join(w['name'] for w in held)}.", "how": "Give it back below when you're ready."}

        # Present tense: only while the team is live and nothing overrides the report.
        live = not terminal and kind is None and not handoff.get("handoff_line") \
            and not st.get("paused") and not st.get("global_paused")
        now_rep = reported(latest.get("now"), latest) if (latest and live) else None
        next_rep = reported(latest.get("next"), latest) if (latest and live) else None
        runs = []
        follower = d["follower"]["endpoint"]
        for other in self.bus.pair_ids():
            try:
                od = self.bus.get_pair_def(other)
            except RelayError:
                continue
            if od["follower"]["endpoint"] == follower:  # by confirmed session, never by folder
                runs.append({"id": other, "goal": od.get("subject"), "current": other == pid})
        runs.sort(key=lambda r: r["id"])
        return {
            "task": d.get("subject"), "folder": cwds.get("follower"),
            "sessions": [{"role": r, "name": names.get(r), "client": clients.get(r), "folder": cwds.get(r)}
                         for r in ("follower", "leader")],
            "happened": happened, "facts": facts,
            "now": {"relay": handoff.get("handoff_line") or line, "reported": now_rep},
            "next": next_rep, "decision": decision,
            "reported": bool(reps), "rejected": len(st.get("progress_rejected") or []),
            "updated_at": st.get("updated_at"), "fresh_s": PROGRESS_FRESH_S, "runs": runs,
        }

    WRITER_WORDS = {
        "awaiting_exit": "waiting for you to close it in its terminal",
        "relay_starting": "the relay is taking it over",
        "relay_owned": "the relay is driving it",
        "returning": "going back to your terminal",
        "released": "back in your terminal",
        "blocked": "stopped — needs you",
        "native": "in your terminal",
    }

    def _console_handoff(self, pid: str, d: dict, names: dict, clients: dict, terminal: bool) -> dict:
        """Who writes each conversation, in plain words, and the hand-off actions that apply."""
        try:
            members = self._handoff_members(pid)
        except RelayError:
            return {}
        if not members:
            return {}
        writers, actions = [], []
        for role in ("leader", "follower"):
            eid = d[role]["endpoint"]
            if eid not in members:
                writers.append({"role": role, "name": names[role], "client": clients[role],
                                "endpoint": eid, "state": None, "words": "run by the relay"})
                continue
            adapter, conv, _key = members[eid]
            own = self.handoffs.state(adapter, conv)
            w = {"role": role, "name": names[role], "client": clients[role], "endpoint": eid,
                 "state": own["state"], "words": self.WRITER_WORDS.get(own["state"], own["state"]),
                 "reason": own.get("reason") if own["state"] == "blocked" else None,
                 "unknown_shutdown": (own.get("detail") or {}).get("shutdown") == "unknown"}
            if own["state"] == "released":
                w["resume"] = resume_command(clients[role], conv)
            if own["state"] in ("relay_owned", "returning", "blocked"):
                # This launch episode's writer, by recorded pid AND start time (never by pattern).
                proc = (own.get("detail") or {}).get("relay") or (own.get("spawn") or {}).get("relay")
                if proc:
                    w["writer_process"] = {"pid": proc.get("pid"), "started_ms": proc.get("started_ms")}
            if own["state"] == "awaiting_exit":
                w["step"] = f"Close it in its terminal ({_exit_hint(clients[role])})."
            writers.append(w)
        states = {w["state"] for w in writers if w["state"]}
        # Giving a conversation back is always reachable while the relay may write it,
        # including after the team finished or you stopped managing.
        if states & {"relay_owned", "blocked"}:
            actions.append("back_to_terminal")
        if "blocked" in states:
            actions.append("resolve_handoff")
        if not terminal:
            if "released" in states:
                actions.append("hand_over_again")
            if states & {"awaiting_exit", "relay_starting"}:
                actions.append("cancel_handoff")
        line = None
        waiting = [w for w in writers if w["state"] == "awaiting_exit"]
        back = [w for w in writers if w["state"] == "released"]
        if waiting and not terminal:
            line = ("Waiting for you to close " + " and ".join(w["name"] for w in waiting)
                    + (" in their terminals." if len(waiting) > 1 else " in its terminal."))
        elif back and not terminal:
            line = (" and ".join(w["name"] for w in back) + (" are" if len(back) > 1 else " is")
                    + " back in your terminal. The AI is waiting.")
        out = {"writers": writers, "handoff_actions": actions, **({"handoff_line": line} if line else {})}
        st = self.bus.pair_state(pid)
        unknown = unknown_turns(st)
        if unknown:
            # Nothing is given back until a human records what happens to these turns.
            out["unknown_outcome"] = {"turns": [i + 1 for i, t in enumerate(st["turns"]) if t in unknown],
                                      "statement": ABANDON_STATEMENT, "can_abandon": True}
        return out

    # =============================================================== actions
    def _check_writable(self):
        if self.read_only:
            raise Conflict("this console was started read-only")

    def _participant_for(self, name: str) -> str:
        base = _slug(name)
        used = {s.get("participant") for s in self.bus.sessions()}
        label, n = base, 2
        while label in used or label == HUMAN:
            label, n = f"{base}-{n}", n + 1
        return label

    def _resolve(self, key: str, *, item: dict) -> str:
        """Endpoint id for a catalog item, registering a discovered session (attach)."""
        if item["endpoint"]:
            self._release_stale(item["endpoint"])
            return item["endpoint"]
        _, adapter, client_id = key.split(":", 2)
        found = next((d for d in self._discovered()
                      if d["adapter"] == adapter and d["client_session_id"] == client_id), None)
        if not found:
            raise NotFound("that session is no longer running")
        extra = found.get("extra") or {}
        rec, _ = self.bus.register_session(
            adapter=adapter, client_session_id=client_id, mode="attach",
            participant=self._participant_for(item["name"]), cwd=found.get("cwd"),
            options=dict(self.options), info={"client": item["client"], "name": item["name"],
                                              "activity": found.get("activity"), **extra},
            capabilities=get_adapter(adapter, self.options).capabilities())
        return rec["id"]

    def _release_stale(self, eid: str) -> None:
        """A session held by a finished/stopped team is free again."""
        s = self.bus.get_session(eid)
        pid = s["state"].get("pair")
        if not pid:
            return
        try:
            st = self.bus.pair_state(pid)
        except RelayError:
            return
        if st.get("terminal") and pid not in self.hosted:
            with self.bus.root.lock(CLAIMS_LOCK):
                if team_holds(self.bus, pid):
                    # The ended team may still hold this conversation (not back with its
                    # terminal, or an unacknowledged unknown outcome): its claim stays.
                    raise Conflict("this conversation is still with an earlier team; give it back "
                                   "(or resolve it) there first")
                self.bus.session_event(eid, "released", {"pair": pid}, WHO)

    HANDOVER_MAX = 24 * 1024

    def _review_spec(self, review: dict, manager: dict, workspace: str | None) -> dict:
        """Validate a review-loop request before anything is registered."""
        if not isinstance(review, dict):
            raise InvalidInput("the review loop settings must be an object")
        new = manager.get("new") if isinstance(manager, dict) else None
        if not new or new.get("client") != "codex":
            # D1: the reviewer is always a relay-launched Codex with the read-only policy;
            # an existing chat (or the Codex app chat) is never the target.
            raise InvalidInput("the review loop's reviewer is a new Codex session started by the relay")
        if not workspace:
            raise InvalidInput("the review loop needs the session's folder as its workspace")
        handover = review.get("handover")
        if not isinstance(handover, str) or not handover.strip():
            raise InvalidInput("write the handover: architecture, scope, decisions and the task")
        raw = handover.encode()
        if len(raw) > self.HANDOVER_MAX:
            raise InvalidInput("keep the handover under 24 KiB")
        sources = review.get("sources") or []
        if not isinstance(sources, list) or len(sources) > 16 or \
                not all(isinstance(x, str) and 0 < len(x) <= 300 and "\n" not in x for x in sources):
            raise InvalidInput("sources are up to 16 one-line references")
        done_when = (review.get("done_when") or "").strip()
        if not done_when or len(done_when) > 1000:
            raise InvalidInput("say when the work is done (up to 1000 characters)")
        minutes = review.get("deadline_min", 30)
        if not isinstance(minutes, int) or not 5 <= minutes <= 240:
            raise InvalidInput("the time limit is 5-240 minutes")
        turns = review.get("max_turns", 10)
        if not isinstance(turns, int) or not 2 <= turns <= 40:
            raise InvalidInput("the turn budget is 2-40 turns")
        chk = dict(review.get("check") or {})
        if isinstance(chk.get("command"), str):  # split like a shell would, but nothing is expanded
            try:
                chk["argv"] = shlex.split(chk.pop("command"))
            except ValueError as e:
                raise InvalidInput(f"the check command doesn't parse: {e}")
        spec = V.validate_spec(chk, workspace)
        runner = self.check_runner or V.default_runner()
        why = runner.available()
        return {
            "kind": "review_loop", "done_when": done_when, "deadline_min": minutes,
            "deadline_ts": time.time() + minutes * 60, "max_turns": turns,
            "handover": {"text": handover, "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw),
                         "sources": sources},
            "check": spec, "check_config": "" if why else runner.config_hash(spec),
            "runner": None if why else runner.name, "runner_unavailable": why,
        }

    def _implementer_policy(self, item: dict) -> dict:
        """A handed-over implementer codes only under the reviewed coding policy,
        fixed and hashed here, at team creation. If that route isn't available (no proven
        sandbox or client state paths), the team isn't created; nothing is begun."""
        from .toolpolicy import policy_record
        if item.get("adapter") != "claude-agents":
            raise InvalidInput("the review loop's implementer must be a Claude session")
        writer = self.handoffs.writer_adapter(item["adapter"])
        if not getattr(writer, "supports_writer_policy", False):
            raise Unsupported("this client has no coding route")
        sb = writer.coding_sandbox()
        why = sb.available()
        if why:
            raise Unsupported(why)
        return policy_record(sb.state)

    def manage(self, key: str, manager: dict, goal: str, review: dict | None = None) -> dict:
        self._check_writable()
        goal = (goal or "").strip()
        if not goal:
            raise InvalidInput("say what the manager should work towards")
        if len(goal) > 4000:
            raise InvalidInput("keep the goal under 4000 characters")
        with self.lock:
            cat = {s["key"]: s for s in self.catalog()["sessions"]}
            item = cat.get(key)
            if not item:
                raise NotFound("that session isn't in the list any more")
            if not item.get("can_manage"):
                raise Conflict(item.get("why_not") or "this session can't be managed right now")
            # Validate the whole request before registering anything.
            personality, mitem, spec = PERS.DEFAULT, None, None
            wf = self._review_spec(review, manager, item.get("cwd")) if review is not None else None
            if "existing" in manager:
                mitem = cat.get(manager["existing"])
                if not mitem or not mitem.get("can_manage") or mitem["key"] == key:
                    raise Conflict("pick a different session to be the manager")
            else:
                spec = manager.get("new") or {}
                client = spec.get("client", "claude")
                personality = spec.get("personality") or PERS.DEFAULT
                if client not in self.new_adapters:
                    raise InvalidInput(f"can't start a new {client} session here")
                if personality not in PERS.PERSONALITIES:
                    raise InvalidInput("unknown personality")
            handing = [x for x in (item, mitem) if x is not None and x.get("route") == "handoff"]
            if wf:
                wf["implementer_policy"] = self._implementer_policy(item) if item in handing else None
            begun = []
            try:
                for x in handing:  # all-or-nothing: if one can't begin, none stays begun
                    own = self.handoffs.begin(x["adapter"], x["client_session_id"], x["cwd"] or os.getcwd(),
                                              writer_policy=wf["implementer_policy"] if wf and x is item else None)
                    begun.append((x, own["detail"]["episode"]))
            except RelayError:
                self._rollback_manage(begun, None)
                raise
            created = None
            try:
                follower = self._handoff_endpoint(item) if item in handing else self._resolve(key, item=item)

                if mitem is not None:
                    leader = (self._handoff_endpoint(mitem) if mitem in handing
                              else self._resolve(manager["existing"], item=mitem))
                    mname = mitem["name"]
                else:
                    adapter = self.new_adapters[client]
                    mname = PERS.PERSONALITIES[personality]["name"]
                    launch = {"client": client.capitalize(), "name": mname}
                    if wf:  # D1: read-only, approvals never, no network; verified before any prompt
                        launch["policy"] = "reviewer"
                    if adapter == "fake":  # demo managers talk plausibly and finish after a few turns
                        launch["default"] = {"natural": True, "finish_after": 3, "sleep": 6}
                    opts = dict(self.options, launch=dict(self.options.get("launch", {}), **launch),
                                personality=personality)
                    sid = str(uuid.uuid4()) if adapter in ("fake", "claude-stream") else f"pending-{uuid.uuid4()}"
                    rec, _ = self.bus.register_session(
                        adapter=adapter, client_session_id=sid, mode="launch",
                        participant=self._participant_for(mname), cwd=item["cwd"] or os.getcwd(),
                        options=opts, info={"client": client.capitalize(), "name": mname},
                        capabilities=get_adapter(adapter, self.options).capabilities())
                    leader = rec["id"]
                # Endpoints registered just now aren't in the last catalog's labels yet: keep the
                # names the human picked from, so the console says which session leads.
                self.labels[follower] = item["name"]
                if mitem is not None:
                    self.labels[leader] = mname

                first_line = goal.splitlines()[0]
                scope = (f"Goal: {_trim(goal, 600)}\nWork only in {item['cwd'] or 'the session’s own workspace'}. "
                         "The human can step in at any time.")
                if wf:
                    scope += f"\nReview loop. Done when: {_trim(wf['done_when'], 600)}"
                d = self.bus.create_pair(
                    leader=leader, follower=follower, subject=_trim(first_line, 80), scope=scope,
                    max_turns=wf["max_turns"] if wf else None, workflow=wf)
                created = d["id"]
                brief = PERS.manager_brief(personality=personality if mitem is None else None, goal=goal,
                                           session_name=item["name"], session_client=item["client"],
                                           session_cwd=item["cwd"])
                if wf:  # the inspectable handover, verbatim, as the reviewer's first prompt (D2)
                    brief += (f"\n\nDone when:\n{wf['done_when']}\n\n"
                              f"----- begin handover (sha256 {wf['handover']['sha256']}) -----\n"
                              f"{wf['handover']['text']}\n----- end handover -----")
                self.bus.start_pair(d["id"], body=brief, subject=_trim(first_line, 80), to_leader=True)
            except Exception:
                # Anything failing after the hand-offs began (registration, team creation) must
                # not strand them: undo exactly what this request did, nothing else.
                self._rollback_manage(begun, created)
                raise
            if handing:  # routing starts once every handed-over conversation is relay-owned
                self._kick()
                return {"team": d["id"], "manager": mname, "session": item["name"],
                        "handoff": [{"name": x["name"], "client": x["client"],
                                     "step": f"Close it in its terminal ({_exit_hint(x['client'])})."}
                                    for x in handing]}
            self.start(d["id"])
            return {"team": d["id"], "manager": mname, "session": item["name"]}

    def intervene(self, pid: str) -> dict:
        self._check_writable()
        st = self.bus.pair_state(pid)
        if st.get("terminal"):
            raise Conflict("this team has finished")
        self.bus.pair_event(pid, "paused", {"reason": STEPPED_IN}, WHO)
        if not st.get("attention"):
            self.bus.pair_event(pid, "human_needed", {"reason": STEPPED_IN, "status": "waiting_human"}, WHO)
        return {"ok": True, "finishing_turn": bool(st.get("inflight"))}

    def say(self, pid: str, text: str, to: str | None = None) -> dict:
        """Send your message and hand back (an ordinary intervention or an answer)."""
        self._check_writable()
        text = (text or "").strip()
        if not text:
            raise InvalidInput("write something first")
        st = self.bus.pair_state(pid)
        kind = self.attention_kind(st)
        if kind not in ("stepped_in", "question"):
            raise Conflict(self._why_not_send(kind))
        if st.get("inflight"):
            raise Conflict("the current turn is still finishing; try again in a moment")
        if kind == "question":
            to = None  # an answer goes to whoever asked
        elif to is None:
            to = "follower"
        r = answer(self.bus, pid, text, to_role=to, subject="From you",
                   reply_to=None if kind == "question" else st.get("last_message"))
        self._unpause(pid)
        self.start(pid)
        return r

    @staticmethod
    def _why_not_send(kind: str | None) -> str:
        return {None: "press Intervene first", "approval": "answer the approval in the session's own window",
                "uncertain": "choose Send again or Stop managing first",
                "timeout": "choose Send again or Stop managing first",
                "budget": "allow more turns first", "failed": "this can't continue; stop managing",
                }.get(kind, "this needs a different resolution first")

    def resume(self, pid: str) -> dict:
        """Hand back: only resolves an ordinary intervention."""
        self._check_writable()
        st = self.bus.pair_state(pid)
        kind = self.attention_kind(st)
        if kind not in ("stepped_in", None):
            raise Conflict({"question": "answer the question to hand back"}.get(kind) or self._why_not_send(kind))
        if kind == "stepped_in":
            if st.get("inflight"):
                raise Conflict("the current turn is still finishing; try again in a moment")
            reconcile(self.bus, pid, "continue", reason="handed back by you")
        self._unpause(pid)
        self.start(pid)
        return {"ok": True}

    def resolve(self, pid: str, outcome: str) -> dict:
        """Specific resolutions: send the last message again, or allow more turns."""
        self._check_writable()
        st = self.bus.pair_state(pid)
        kind = self.attention_kind(st)
        if st.get("inflight"):
            raise Conflict("a turn is still in progress")
        wf = (st.get("definition") or {}).get("workflow") or {}
        if wf.get("kind") == "review_loop" and time.time() >= float(wf["deadline_ts"]):
            raise Conflict("this team reached its time limit: nothing more is sent or retried")
        if outcome == "send_again" and kind in ("uncertain", "timeout"):
            reconcile(self.bus, pid, "requeue", reason="sent again by you (the earlier attempt may have run)")
        elif outcome == "more_turns" and kind == "budget":
            self.bus.pair_event(pid, "budget_extended", {"add": 5}, WHO)
            reconcile(self.bus, pid, "continue", reason="you allowed 5 more turns")
        else:
            raise Conflict(f"'{outcome}' doesn't apply here")
        self._unpause(pid)
        self.start(pid)
        return {"ok": True}

    def _unpause(self, pid: str) -> None:
        if self.bus.pair_state(pid).get("paused"):
            self.bus.pair_event(pid, "resumed", {}, WHO)

    def release(self, pid: str) -> dict:
        """Stop managing. Session claims are kept until the controller has stopped."""
        self._check_writable()
        st = self.bus.pair_state(pid)
        if not st.get("terminal"):
            self.bus.pair_event(pid, "stopped", {"reason": "released by you"}, WHO)
        h = self.hosted.get(pid)
        if h:
            h["stop"].set()
            h["thread"].join(timeout=15)
            if h["thread"].is_alive():
                raise Conflict("still stopping — the sessions stay claimed until it has; try again shortly")
        deadline = time.time() + 8
        while self.bus.pair_state(pid).get("owner") and time.time() < deadline:
            time.sleep(0.1)  # a controller in another process exits on stop
        owner = self.bus.pair_state(pid).get("owner")
        if owner and owner.get("host") == socket.gethostname() and owner.get("pid") \
                and owner["pid"] != os.getpid() and not pid_alive(owner["pid"]):
            # A console that crashed never released its controller's lease, and its process is
            # verifiably gone (this host, pid not running). Record that, so stopping can finish.
            # A pid that's alive (even if reused) keeps the lease: never cleared on a guess.
            self.bus.pair_event(pid, "controller_released",
                                {"ctl": owner["ctl"], "reason": "its process is gone"}, WHO)
        if self.bus.pair_state(pid).get("owner"):
            raise Conflict("still stopping — the sessions stay claimed until it has; try again shortly")
        self._detach(pid)
        # Stopping management doesn't hand conversations back by itself: that's an explicit
        # Back to terminal, which stays available on the finished team.
        return {"ok": True, "still_with_relay": self._still_with_relay(pid)}

    def _detach(self, pid: str) -> None:
        """Release the team's claims, except on conversations the relay may still write:
        those stay claimed until Back to terminal / Resolve proves no relay writer runs.
        A hand-off team with an unacknowledged unknown turn outcome keeps every claim."""
        with self.bus.root.lock(CLAIMS_LOCK):
            st = self.bus.pair_state(pid)
            d = st["definition"]
            members = self._handoff_members(pid)
            if members and unknown_hold(st):
                return
            for role in ("leader", "follower"):
                eid = d[role]["endpoint"]
                if eid in members and self.handoffs.own.get(members[eid][2])["state"] not in ("native", "released"):
                    continue
                s = self.bus.get_session(eid)
                if s["state"].get("pair") == pid and s["state"]["state"] != "closed":
                    self.bus.session_event(eid, "released", {"pair": pid}, WHO)

    def _still_with_relay(self, pid: str) -> list[str]:
        out = []
        for eid, (_a, _c, key) in self._handoff_members(pid).items():
            if self.handoffs.own.get(key)["state"] not in ("native", "released"):
                out.append(self.labels.get(eid) or (self.bus.get_session(eid).get("info") or {}).get("name") or eid)
        return out

    # ======================================================= hosted controllers
    def start(self, pid: str) -> None:
        """Run the pair's controller here, unless one is already running anywhere."""
        with self.lock:
            h = self.hosted.get(pid)
            if h and h["thread"].is_alive():
                return
            st = self.bus.pair_state(pid)
            if st.get("owner") or st.get("terminal"):
                return
            stop = threading.Event()
            rec = {"stop": stop, "error": None}
            t = threading.Thread(target=self._run, args=(pid, rec), name=f"relay-{pid}", daemon=True)
            rec["thread"] = t
            self.hosted[pid] = rec
            t.start()

    def _run(self, pid: str, rec: dict) -> None:
        ctl = Controller(self.bus, pid, poll_interval=0.2, check_runner=self.check_runner,
                         **self._handoff_wiring(pid))
        try:
            ctl.acquire()
            ctl.connect_all()
            ctl.run(exit_on=frozenset({"finished", "stopped", "untrusted"}),
                    on_tick=lambda st: not rec["stop"].is_set())
        except Exception as e:  # surfaced to the page and recorded durably
            rec["error"] = str(e)
            try:
                if not self.bus.pair_state(pid).get("terminal"):
                    self.bus.pair_event(pid, "human_needed", {"status": "failed",
                                                              "reason": f"couldn't run: {e}"}, WHO)
            except RelayError:
                pass
        finally:
            ctl.shutdown()
            try:
                if self.bus.pair_state(pid).get("terminal"):
                    self._detach(pid)
            except RelayError:
                pass

    def _stop_hosted(self, pid: str) -> None:
        h = self.hosted.get(pid)
        if h:
            h["stop"].set()
            h["thread"].join(timeout=15)

    def shutdown(self, *, settle_s: float = 10.0) -> None:
        """Stop the console. Teams pause; every relay writer this console holds is given
        back (exit verified) so nothing is left half-owned. A writer whose turn or exit
        can't be settled is recorded as blocked, for Resolve after a restart."""
        self._ticker_stop.set()
        self._notify_stop.set()
        held = set(self.handoffs.writers)
        if held:
            for pid in self.bus.pair_ids():
                try:
                    members = self._handoff_members(pid)
                    st = self.bus.pair_state(pid)
                except RelayError:
                    continue
                mine = {eid: m for eid, m in members.items() if m[2] in held}
                if not mine:
                    continue
                try:
                    if not st.get("terminal") and not st.get("paused"):
                        self.bus.pair_event(pid, "paused", {"reason": CONSOLE_STOPPED}, WHO)
                    for _eid, (_a, _c, key) in mine.items():
                        if self.handoffs.own.get(key)["state"] in ("relay_owned", "blocked"):
                            self.handoffs.begin_return(key)
                    deadline = time.time() + settle_s
                    while self.bus.pair_state(pid).get("inflight") and time.time() < deadline:
                        time.sleep(0.1)
                    unsettled = bool(self.bus.pair_state(pid).get("inflight"))
                    self._stop_hosted(pid)
                    for _eid, (_a, _c, key) in mine.items():
                        if unsettled:
                            self.handoffs.block(key, "The console stopped while a turn was running; its "
                                                     "outcome isn't known.", shutdown="unknown")
                        if self.handoffs.own.get(key)["state"] == "returning":
                            self.handoffs.stop_writer(key)
                except RelayError:
                    pass
        for pid in list(self.hosted):
            self._stop_hosted(pid)

    # ============================================================ hand-off
    def _discover_one(self, adapter: str, conversation: str) -> dict | None:
        """Fresh, read-only discovery of one conversation (None if it can't be determined)."""
        try:
            a = get_adapter(adapter, self.options)
            if hasattr(a, "writers_for"):  # real clients: native-writer evidence only
                w = a.writers_for(conversation)
                # These listings show native writers only: they can't see the relay's own
                # writers, so they never count as evidence that no relay writer exists.
                return None if w is None else {"client_session_id": conversation,
                                               "extra": {"writers": w, "relay_visible": False}}
            for x in a.discover():
                if x.client_session_id == conversation:
                    return {"client_session_id": x.client_session_id, "cwd": x.cwd,
                            "activity": x.activity, "extra": x.extra}
        except RelayError:
            return None
        return None

    def _ownership(self, item: dict) -> None:
        """Route and ownership for a row: hand-off conversations only."""
        if not (item.get("interactive") or item.get("handoff")):
            return
        why = self.handoffs.supported(item["adapter"], item.get("cwd"))
        if why:
            item["handoff_unavailable"] = why
            return
        conv = item["client_session_id"]
        st = self.handoffs.state(item["adapter"], conv)
        item["route"] = "handoff"
        own = {"state": st["state"], "gen": st["gen"], "reason": st.get("reason"),
               "orphaned": bool(st.get("orphaned")), "interrupted": bool(st.get("interrupted"))}
        if st["state"] == "released":
            own["resume"] = resume_command(item["client"], conv)
        if st["state"] == "blocked":
            own["shutdown"] = (st["detail"] or {}).get("shutdown")
        item["ownership"] = own

    def _rollback_manage(self, begun: list[tuple[dict, str]], created: str | None) -> None:
        """Undo a failed Manage: stop the team it created, cancel the hand-offs IT began that
        are still pending, and release claims only through the guarded detach. Never touches
        another team. A conversation whose relay writer may already exist is blocked, not
        cancelled (fail closed). Each hand-off is acted on only if it is still the episode
        this request began: the console lock is per process, so another console may have
        cancelled and begun it again meanwhile, and that later episode is left alone."""
        if created:
            try:
                if not self.bus.pair_state(created).get("terminal"):
                    self.bus.pair_event(created, "stopped", {"reason": "setting up the team failed"}, WHO)
            except RelayError:
                pass
        for x, episode in begun:
            st = self.handoffs.state(x["adapter"], x["client_session_id"])
            try:
                if (st["detail"] or {}).get("episode") != episode:
                    continue  # not ours any more
                if st["state"] == "awaiting_exit":
                    self.handoffs.cancel(x["adapter"], x["client_session_id"], episode=episode)
                elif st["state"] in ("relay_starting", "relay_owned"):
                    self.handoffs.block(st["key"], "Setting up the team failed after the relay started its "
                                                   "writer. Resolve it once no relay writer is running.",
                                        episode=episode)
            except RelayError:
                pass  # it moved meanwhile: leave it as it is rather than guess
        if created:
            try:
                self._detach(created)  # guarded: keeps any claim the relay may still need
            except RelayError:
                pass

    def _handoff_endpoint(self, item: dict) -> str:
        """The endpoint for a handed-over conversation: launch-mode, never (re)launched by a
        controller; the hand-off owns its writer and lends the connection."""
        # A conversation handed over before (an earlier, ended team) keeps its endpoint: one
        # conversation is one endpoint, so reuse its participant instead of minting a new one.
        prior = next((x for x in self.bus.sessions() if x.get("adapter") == item["adapter"]
                      and x.get("client_session_id") == item["client_session_id"]
                      and (x.get("options") or {}).get("handoff")), None)
        rec, _ = self.bus.register_session(
            adapter=item["adapter"], client_session_id=item["client_session_id"], mode="launch",
            participant=prior["participant"] if prior else self._participant_for(item["name"]), cwd=item["cwd"],
            options=dict(self.options, handoff=True),
            info={"client": item["client"], "name": item["name"]},
            capabilities=get_adapter(item["adapter"], self.options).capabilities())
        self._release_stale(rec["id"])
        return rec["id"]

    def _handoff_members(self, pid: str) -> dict[str, tuple[str, str, str]]:
        """eid -> (adapter, conversation, ownership key) for a team's handed-over sessions."""
        d = self.bus.get_pair_def(pid)
        out = {}
        for role in ("leader", "follower"):
            s = self.bus.get_session(d[role]["endpoint"])
            if (s.get("options") or {}).get("handoff"):
                conv = s["client_session_id"]
                out[s["id"]] = (s["adapter"], conv, self.handoffs.key(s["adapter"], conv))
        return out

    def _handoff_wiring(self, pid: str) -> dict:
        members = self._handoff_members(pid)
        if not members:
            return {}

        def fences():
            out = {}
            for _eid, (_a, _c, key) in members.items():
                f = self.handoffs.fence(key)
                if f is None:
                    raise Blocked("a handed-over conversation isn't owned by the relay right now")
                out[key] = f
            return out

        def before_submit(eid):
            # Every handed-over conversation in the team must still have one writer: a
            # manager never submits while its follower has gone back to a terminal.
            for _eid, (_a, _c, key) in members.items():
                why = self.handoffs.check(key)
                if why:
                    return why
            return None
        borrowed = {eid: self.handoffs.writers[key] for eid, (_a, _c, key) in members.items()
                    if key in self.handoffs.writers}
        return {"borrowed": borrowed, "fences": fences, "before_submit": before_submit}

    def _kick(self) -> None:
        with self.lock:
            if self._ticker and self._ticker.is_alive():
                return
            self._ticker_stop.clear()
            self._ticker = threading.Thread(target=self._tick_loop, name="relay-handoff", daemon=True)
            self._ticker.start()

    def _tick_loop(self) -> None:
        while not self._ticker_stop.wait(0.3):
            try:
                if not self.handoff_tick():
                    return
            except Exception:  # pragma: no cover - keep ticking; state is durable
                pass

    def handoff_tick(self) -> bool:
        """Advance hand-offs. Returns True while anything is still pending or watched."""
        busy = False
        with self.lock:
            for pid in self.bus.pair_ids():
                try:
                    st = self.bus.pair_state(pid)
                    members = self._handoff_members(pid)
                except RelayError:
                    continue
                if not members:
                    continue
                if st.get("terminal"):  # no routing, but its conversations still need watching
                    for eid, (adapter, conv, key) in members.items():
                        own = self.handoffs.state(adapter, conv)
                        if own["state"] in ("relay_starting", "relay_owned", "returning") and own.get("orphaned"):
                            self.handoffs.block_if_orphaned(key, "The relay's writer for this conversation "
                                                                 "isn't running in this console. Resolve it "
                                                                 "once none is running.")
                    continue
                states = {}
                for eid, (adapter, conv, key) in members.items():
                    own = self.handoffs.state(adapter, conv)
                    if own["state"] == "awaiting_exit":
                        own = self.handoffs.advance(adapter, conv)
                    if own["state"] == "relay_starting" and not own.get("orphaned"):
                        own = self.handoffs.start_writer(adapter, conv)
                    elif own["state"] == "relay_starting":  # interrupted mid-start (restart)
                        own = self.handoffs.block_if_orphaned(key, "Starting the relay's writer was "
                                                              "interrupted. Resolve it once no relay "
                                                              "writer is running.")
                    if own["state"] in ("relay_owned", "returning") and own.get("orphaned"):
                        own = self.handoffs.block_if_orphaned(key, own["reason"])
                    elif own["state"] == "relay_owned" and self._hosted_alive(pid):
                        self.handoffs.check(key)  # detects a second writer between dispatches
                        own = self.handoffs.state(adapter, conv)
                    states[eid] = own["state"]
                if any(v in ("awaiting_exit", "relay_starting") for v in states.values()):
                    busy = True
                if "blocked" in states.values():
                    if not st.get("paused"):  # routing stops for the whole team; the reason is on the row
                        self.bus.pair_event(pid, "paused", {"reason": HANDOFF_BLOCKED}, WHO)
                    continue
                if all(v == "relay_owned" for v in states.values()) and not self._hosted_alive(pid):
                    if st.get("paused") and st.get("pause_reason") in (RETURNING, HANDOFF_BLOCKED, CONSOLE_STOPPED):
                        self.bus.pair_event(pid, "resumed", {}, WHO)  # handed over again: carry on
                    self.start(pid)
                if self._hosted_alive(pid):
                    busy = True
        return busy

    def _hosted_alive(self, pid: str) -> bool:
        h = self.hosted.get(pid)
        return bool(h and h["thread"].is_alive())

    def back_to_terminal(self, pid: str, roles: list[str], *, settle_s: float = 30.0) -> dict:
        """Give handed-over conversations back to their terminals. Pair-wide: routing
        stops for BOTH sessions first; the current turn settles; the relay's writer for
        each returned conversation is stopped and its exit verified; claims are kept."""
        self._check_writable()
        members = self._handoff_members(pid)
        d = self.bus.get_pair_def(pid)
        want = {d[r]["endpoint"] for r in roles if r in ("leader", "follower")}
        targets = {eid: m for eid, m in members.items() if eid in want}
        if not targets:
            raise Conflict("nothing here was handed over from a terminal")
        st = self.bus.pair_state(pid)
        if not st.get("paused") and not st.get("terminal"):
            self.bus.pair_event(pid, "paused", {"reason": RETURNING}, WHO)
        for _eid, (_a, _c, key) in targets.items():
            if self.handoffs.own.get(key)["state"] in ("released", "native"):
                continue
            self.handoffs.begin_return(key)  # fenced: no submission can commit after this
        deadline = time.time() + settle_s
        if st.get("terminal"):
            # A finished or stopped team records no more turns, so settle on the writers
            # themselves: never stop one while it's still working on something.
            def busy():
                return [k for _e, (_a, _c, k) in targets.items()
                        if k in self.handoffs.writers and self.handoffs.writers[k].status() not in ("idle", "disconnected")]
            while busy() and time.time() < deadline:
                time.sleep(0.1)
            settled = not busy()
        else:
            while self.bus.pair_state(pid).get("inflight") and time.time() < deadline:
                time.sleep(0.1)
            settled = not self.bus.pair_state(pid).get("inflight")
        out = []
        if not settled:
            for eid, (_a, conv, key) in targets.items():
                self.handoffs.block(key, "A turn was still running and its outcome isn't known. The relay "
                                         "kept the conversation; don't reopen it yet.", shutdown="unknown")
                out.append({"endpoint": eid, "state": "blocked", "resume": None})
            return {"ok": False, "settled": False, "sessions": out}
        self._stop_hosted(pid)  # routing stops; claims stay (the team is paused, not released)
        hold = unknown_hold(self.bus.pair_state(pid))
        for eid, (adapter, conv, key) in targets.items():
            own = self.handoffs.own.get(key)
            if own["state"] == "returning":
                own = self.handoffs.stop_writer(key, hold=hold)  # exit verified either way
            s = self.bus.get_session(eid)
            client = self._client_of(adapter, s.get("info"))
            out.append({"endpoint": eid, "state": own["state"], "reason": own.get("reason"),
                        "resume": resume_command(client, conv) if own["state"] == "released" else None})
        if self.bus.pair_state(pid).get("terminal"):
            self._detach(pid)  # a finished team lets go of what's no longer with the relay
        return {"ok": all(x["state"] == "released" for x in out), "settled": True, "sessions": out}

    def hand_over_again(self, pid: str) -> dict:
        """Re-management after a return: hand the released conversations over again.
        The team keeps its history and budget; nothing already sent is sent again."""
        self._check_writable()
        begun = []
        # Serialized with releasing this team's claims (here or `relay pair detach`), so a
        # hand-over can't start on a team whose reservations are being removed.
        with self.bus.root.lock(CLAIMS_LOCK):
            st = self.bus.pair_state(pid)
            if st.get("terminal"):
                raise Conflict("this team has finished")
            d = self.bus.get_pair_def(pid)
            policy = (d.get("workflow") or {}).get("implementer_policy")
            for eid, (adapter, conv, key) in self._handoff_members(pid).items():
                if self.handoffs.own.get(key)["state"] == "released":
                    s = self.bus.get_session(eid)
                    # The implementer goes back under the policy its team was created with.
                    self.handoffs.begin(adapter, conv, s.get("cwd") or os.getcwd(),
                                        writer_policy=policy if eid == d["follower"]["endpoint"] else None)
                    begun.append(eid)
        if not begun:
            raise Conflict("nothing is back in a terminal")
        self._kick()
        return {"ok": True, "waiting_for": begun}

    def cancel_handoff(self, pid: str) -> dict:
        """Stop a team mid hand-off: pending hand-offs are cancelled; any relay writer
        already started is returned (exit verified). Nothing is sent."""
        self._check_writable()
        out = []
        for eid, (adapter, conv, key) in self._handoff_members(pid).items():
            own = self.handoffs.own.get(key)
            if own["state"] == "awaiting_exit":
                own = self.handoffs.cancel(adapter, conv)
            elif own["state"] in ("relay_owned", "relay_starting", "blocked") and key in self.handoffs.writers:
                if own["state"] == "relay_starting":
                    own = self.handoffs.block(key, "hand-off cancelled while starting")
                self.handoffs.begin_return(key)
                own = self.handoffs.stop_writer(key)
            out.append({"endpoint": eid, "state": own["state"]})
        st = self.bus.pair_state(pid)
        if not st.get("terminal"):
            self.bus.pair_event(pid, "stopped", {"reason": "hand-off cancelled"}, WHO)
        self._stop_hosted(pid)
        if all(x["state"] in ("native", "released") for x in out):
            self._detach(pid)  # claims are released only when no relay writer remains
        return {"ok": True, "sessions": out}

    def abandon_unknown(self, pid: str) -> dict:
        """The human's explicit disposition of unknown turn outcomes: end management and
        give back; those turns may have run and will not be resent. Releases nothing by
        itself: Back to terminal / Resolve still need each writer's verified exit."""
        self._check_writable()
        out = abandon_unknown(self.bus, pid, WHO)
        self._stop_hosted(pid)
        return out

    def resolve_handoff(self, pid: str, eid: str) -> dict:
        self._check_writable()
        m = self._handoff_members(pid).get(eid)
        if not m:
            raise NotFound("that session wasn't handed over")
        hold = unknown_hold(self.bus.pair_state(pid))
        if hold:
            # Exit may be proven, but what the turn did isn't: acknowledge first. A writer this
            # console still holds is stopped (exit verified) and the conversation stays blocked.
            if m[2] in self.handoffs.writers:
                if self.handoffs.resolve(m[2])["state"] == "returning":
                    self.handoffs.stop_writer(m[2], hold=hold)
            raise Conflict(hold)
        own = self.handoffs.resolve(m[2])
        if own["state"] == "returning":
            own = self.handoffs.stop_writer(m[2])
        if self.bus.pair_state(pid).get("terminal"):
            self._detach(pid)
        return {"state": own["state"], "reason": own.get("reason")}
