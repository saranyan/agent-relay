#!/usr/bin/env python3
"""Supplemental failure probes (scratch runs only).

Watches a relay root's own records and acts at the exact observable moment, so a
probe never depends on human reaction time. It signals a process only with POSITIVE
proof of identity (see `strict_identity`): a valid recorded start time and a valid
observed start time that match at ps's 1-second resolution. Missing, malformed,
unobservable or different means refuse, never signal. Identity and the current launch
episode are rechecked immediately before every signal. Verdicts are honest: if the
moment had already passed when the signal landed, it's `not_observed`, never a pass.

  console        launch the console: record pid + start time + exact argv durably,
                 then exec `relay serve` (same pid). The only console identity accepted.
  midturn        SIGKILL the relay's writer for ROLE while the team's Nth turn (1-based)
                 is accepted and not yet ended (a mid-turn writer failure).
  crash-console  SIGKILL the recorded console while the team's Nth turn is in flight AND
                 a handed-over conversation is `returning` (a crash during a transition).
  stop-orphan    after an observed console crash only: SIGTERM the very writer launch that
                 crash recorded (same root, team, conversation, pid + start), once the
                 crashed console is proven gone and the writer provably has no live parent.
  status         read-only snapshot: ownership, relay writers, in-flight turn, claims,
                 unknown turn outcomes.

Reads the root; never appends to it. Exit 0 = observed/done, 3 = not observed,
4 = refused (identity not proven; nothing signalled).
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time

REPO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, REPO)

from relay.bus import Bus  # noqa: E402
from relay.context import process_start_ms, valid_start  # noqa: E402
from relay.handoff import Ownership, ownership_keys  # noqa: E402
from relay.protocol import TURN_END, unknown_turns  # noqa: E402
from relay.storage import Root, now_iso, pid_alive  # noqa: E402

POLL_S = 0.02


class Refused(Exception):
    """Identity not proven: nothing was signalled."""


# ------------------------------------------------------------- identity
def _second(ms: float) -> int:
    return int(ms // 1000)  # ps reports start times to the second


def strict_identity(rec) -> str | None:
    """None if `rec` provably names a running process (pid + start time); else why not.

    Deliberately stricter than relay.handoff.same_process, which treats an unverifiable
    start time as "maybe the same" so ownership is retained. Here an unverifiable start
    time is a refusal: permission to signal needs positive proof."""
    if not isinstance(rec, dict):
        return "no process record"
    pid, started = rec.get("pid"), rec.get("started_ms")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1:
        return f"invalid pid {pid!r}"
    if not valid_start(started):
        return f"recorded start time missing or malformed ({started!r})"
    if not pid_alive(pid):
        return f"pid {pid} is not running"
    now = process_start_ms(pid)
    if not valid_start(now):
        return f"can't observe pid {pid}'s start time"
    if _second(now) != _second(started):
        return f"pid {pid} started at a different time than recorded (pid reused)"
    return None


def proven_gone(rec) -> bool:
    """The process a record names is provably not running: no such pid, or that pid now
    belongs to a process with a different (valid) start time."""
    if not isinstance(rec, dict) or not isinstance(rec.get("pid"), int) or not valid_start(rec.get("started_ms")):
        return False
    if not pid_alive(rec["pid"]):
        return True
    stat = subprocess.run(["ps", "-o", "stat=", "-p", str(rec["pid"])], capture_output=True, text=True).stdout.strip()
    if stat.startswith("Z"):
        return True  # exited; only its parent hasn't collected it yet
    now = process_start_ms(rec["pid"])
    return valid_start(now) and _second(now) != _second(rec["started_ms"])


def _args(pid: int) -> str:
    return subprocess.run(["ps", "-ww", "-o", "args=", "-p", str(pid)], capture_output=True,
                          text=True).stdout.rstrip("\n")


def console_identity(record_path: str, root: str) -> dict:
    """The console from its durable launch record: same root, provably the same process,
    and running exactly the recorded command. Raises Refused otherwise."""
    try:
        with open(record_path) as f:
            rec = json.load(f)
    except (OSError, ValueError) as e:
        raise Refused(f"no readable console launch record at {record_path} ({e})")
    if rec.get("root") != os.path.realpath(root):
        raise Refused(f"the recorded console serves {rec.get('root')!r}, not {os.path.realpath(root)!r}")
    why = strict_identity(rec)
    if why:
        raise Refused(f"console: {why}")
    argv = rec.get("argv") or []
    tail = " ".join(argv[1:])
    seen = _args(rec["pid"])
    # macOS framework Python reports its own executable as argv[0]; everything after must match exactly.
    head = seen[:-len(tail) - 1] if tail and seen.endswith(" " + tail) else None
    if head is None or not os.path.basename(head).lower().startswith("python"):
        raise Refused(f"pid {rec['pid']} isn't running the recorded console command")
    return rec


# --------------------------------------------------------------- records
def members(bus: Bus, team: str) -> dict[str, dict]:
    """role -> {endpoint, adapter, conversation, key, name} for a team's sessions."""
    d = bus.get_pair_def(team)
    keys, _ = ownership_keys(bus)  # from durable records alone, as the relay itself attributes them
    out = {}
    for role in ("leader", "follower"):
        s = bus.get_session(d[role]["endpoint"])
        key = keys.get((s["adapter"], s["client_session_id"]))
        out[role] = {"endpoint": s["id"], "adapter": s.get("adapter"), "conversation": s.get("client_session_id"),
                     "key": key, "name": (s.get("info") or {}).get("name")}
    return out


def writer_of(own: dict) -> dict | None:
    """The relay writer recorded for the CURRENT launch episode, or None."""
    return (own.get("spawn") or {}).get("relay") or None


def turn_end(bus: Bus, team: str, turn: str):
    for e in bus.root.read_stream(f"pair-{team}"):
        if e.type in TURN_END and e.data.get("turn") == turn:
            return e
    return None


def record(path: str | None, out: dict) -> None:
    text = json.dumps(out, indent=2, sort_keys=True)
    print(text)
    if path:
        with open(path, "w") as f:
            f.write(text + "\n")


# ---------------------------------------------------------------- probes
def midturn(bus: Bus, team: str, role: str, nth: int, wait_s: float, kill=os.kill) -> dict:
    """Catch the team's `nth` turn (1-based) while it's accepted and not ended."""
    m = members(bus, team)[role]
    own = Ownership(bus)
    deadline = time.time() + wait_s
    while time.time() < deadline:
        st = bus.pair_state(team)
        turns = st["turns"]
        target = turns[nth - 1] if len(turns) >= nth else None
        infl = st.get("inflight") or {}
        if target and target["role"] != role:
            return {"probe": "midturn", "verdict": "not_observed",
                    "why": f"turn {nth} is the {target['role']}'s, not the {role}'s: check --turn"}
        if target and infl.get("turn") == target["turn"] and infl.get("phase") == "accepted":
            o = own.get(m["key"])
            w = writer_of(o)
            why = strict_identity(w) if o["state"] == "relay_owned" else f"ownership is {o['state']}"
            if why:
                raise Refused(f"relay writer: {why}")
            # Immediately before the signal: same episode, same writer, still provably it.
            again = own.get(m["key"])
            if again["gen"] != o["gen"] or again["state"] != "relay_owned" or writer_of(again) != w:
                raise Refused("the launch episode changed just before the signal")
            why = strict_identity(w)
            if why:
                raise Refused(f"relay writer: {why}")
            kill(w["pid"], signal.SIGKILL)
            killed_at = now_iso()
            end = turn_end(bus, team, target["turn"])
            if end is not None and end.at <= killed_at:
                return {"probe": "midturn", "verdict": "not_observed", "killed": w, "killed_at": killed_at,
                        "why": f"turn {nth} had already ended ({end.type} at {end.at}): idle writer, not counted"}
            return {"probe": "midturn", "verdict": "observed", "nth": nth, "turn": target["turn"],
                    "role": role, "killed": w, "killed_at": killed_at}
        if (target and target["phase"] not in ("authorized", "submitting", "accepted")) or st.get("terminal"):
            return {"probe": "midturn", "verdict": "not_observed",
                    "why": f"turn {nth} ended before it could be caught"}
        time.sleep(POLL_S)
    return {"probe": "midturn", "verdict": "not_observed", "why": f"turn {nth} wasn't running within {wait_s:.0f}s"}


def crash_console(bus: Bus, team: str, record_path: str, root: str, nth: int, wait_s: float,
                  kill=os.kill) -> dict:
    """SIGKILL the recorded console while turn `nth` is in flight and ownership is `returning`."""
    console_identity(record_path, root)  # refuse up front, before waiting
    ms = members(bus, team)
    own = Ownership(bus)
    deadline = time.time() + wait_s
    while time.time() < deadline:
        st = bus.pair_state(team)
        turns = st["turns"]
        target = turns[nth - 1] if len(turns) >= nth else None
        infl = st.get("inflight") or {}
        states = {r: own.get(m["key"])["state"] for r, m in ms.items() if m["key"]}
        returning = [r for r, v in states.items() if v == "returning"]
        if target and infl.get("turn") == target["turn"] and returning:
            rec = console_identity(record_path, root)  # immediately before the signal
            kill(rec["pid"], signal.SIGKILL)
            killed_at = now_iso()
            end = turn_end(bus, team, target["turn"])
            moved = {r: own.get(ms[r]["key"])["state"] for r in returning}
            if (end is not None and end.at <= killed_at) or any(v != "returning" for v in moved.values()):
                return {"probe": "crash-console", "verdict": "not_observed", "killed": rec, "killed_at": killed_at,
                        "why": f"transition had already completed: turn end {end.type if end else None}, "
                               f"ownership {moved}"}
            return {"probe": "crash-console", "verdict": "observed", "nth": nth, "turn": target["turn"],
                    "inflight_phase": infl["phase"], "killed": {"pid": rec["pid"], "started_ms": rec["started_ms"]},
                    "killed_at": killed_at, "returning": returning, **crash_binding(bus, root, team)}
        if target and target["phase"] not in ("authorized", "submitting", "accepted"):
            return {"probe": "crash-console", "verdict": "not_observed",
                    "why": f"turn {nth} ended before Back to terminal reached `returning`"}
        if any(v in ("released", "blocked") for v in states.values()) or st.get("terminal"):
            return {"probe": "crash-console", "verdict": "not_observed",
                    "why": f"the return finished (or the team ended) without turn {nth} in flight: {states}"}
        time.sleep(POLL_S)
    return {"probe": "crash-console", "verdict": "not_observed",
            "why": f"no return during turn {nth} within {wait_s:.0f}s"}


def crash_binding(bus: Bus, root: str, team: str) -> dict:
    """What a crash verdict binds to: the canonical root, the team, and per role the
    endpoint, conversation, ownership key and the CURRENT launch's writer (pid + start)."""
    own = Ownership(bus)
    roles = {}
    for r, m in members(bus, team).items():
        if m["key"]:
            o = own.get(m["key"])
            roles[r] = {"endpoint": m["endpoint"], "conversation": m["conversation"], "key": m["key"],
                        "gen": o["gen"], "state": o["state"], "writer": writer_of(o)}
    return {"root": os.path.realpath(root), "team": team, "roles": roles}


def parent_pid(pid: int) -> int | None:
    out = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True).stdout.split()
    return int(out[0]) if out and out[0].isdigit() else None


def unowned(pid: int, parent_of=parent_pid) -> str | None:
    """None if the process has provably no live owner: its parent console died and it was
    re-parented to launchd/init (pid 1). Anything else (a live parent, or unknown) is a refusal."""
    ppid = parent_of(pid)
    if ppid is None:
        return f"can't observe pid {pid}'s parent"
    if ppid != 1:
        return f"pid {pid} still has a live parent (pid {ppid}): it may belong to a running console"
    return None


def stop_orphan(bus: Bus, root: str, team: str, role: str, crash_record_path: str | None, kill=os.kill,
                parent_of=parent_pid) -> dict:
    """SIGTERM a relay writer left behind by an OBSERVED console crash: only the very writer
    launch that crash recorded, for this root, team and conversation, once nothing owns it."""
    if not crash_record_path:
        raise Refused("a crash record (crash-console --out) is required")
    try:
        with open(crash_record_path) as f:
            crash = json.load(f)
    except (OSError, ValueError) as e:
        raise Refused(f"no readable crash record at {crash_record_path} ({e})")
    if crash.get("probe") != "crash-console" or crash.get("verdict") != "observed" or crash.get("killed") is None:
        raise Refused("the crash record isn't an observed console crash")
    if not proven_gone(crash["killed"]):
        raise Refused("the crashed console isn't proven gone (it may still own its writers)")
    # The crash must be about THIS root, team and conversation.
    if crash.get("root") != os.path.realpath(root):
        raise Refused(f"the crash record is for root {crash.get('root')!r}, not {os.path.realpath(root)!r}")
    if crash.get("team") != team:
        raise Refused(f"the crash record is for team {crash.get('team')!r}, not {team!r}")
    at_crash = (crash.get("roles") or {}).get(role)
    m = members(bus, team)[role]
    if not at_crash or (at_crash.get("endpoint"), at_crash.get("conversation"), at_crash.get("key")) != \
            (m["endpoint"], m["conversation"], m["key"]):
        raise Refused(f"the crash record doesn't name this {role}'s conversation")
    crashed_writer = at_crash.get("writer")
    if not (isinstance(crashed_writer, dict) and isinstance(crashed_writer.get("pid"), int)
            and valid_start(crashed_writer.get("started_ms"))):
        raise Refused(f"the crash record has no verifiable writer launch for the {role}")
    o = Ownership(bus).get(m["key"])
    w = writer_of(o)
    # The same launch the crash captured: same recorded writer (pid AND start). The fence may
    # have moved since (returning -> blocked during recovery): that alone isn't a mismatch.
    if w != crashed_writer:
        raise Refused(f"the {role}'s current writer launch isn't the one the crash recorded "
                      f"(now {w}, at crash {crashed_writer})")
    if o["state"] not in ("returning", "blocked"):
        raise Refused(f"the {role}'s ownership is {o['state']}, not an interrupted return")
    if isinstance(w.get("pid"), int) and not pid_alive(w["pid"]):
        return {"probe": "stop-orphan", "role": role, "verdict": "nothing_running", "writer": w}
    why = strict_identity(w) or unowned(w["pid"], parent_of)
    if why:
        raise Refused(f"relay writer: {why}")
    again = Ownership(bus).get(m["key"])  # immediately before the signal
    if writer_of(again) != w or again["state"] not in ("returning", "blocked") or strict_identity(w) \
            or unowned(w["pid"], parent_of):
        raise Refused("the writer's launch or ownership changed just before the signal")
    kill(w["pid"], signal.SIGTERM)
    for _ in range(100):
        if proven_gone(w):
            return {"probe": "stop-orphan", "role": role, "verdict": "stopped", "writer": w, "at": now_iso()}
        time.sleep(0.1)
    return {"probe": "stop-orphan", "role": role, "verdict": "still_running", "writer": w}


def status(bus: Bus, team: str) -> dict:
    own = Ownership(bus)
    st = bus.pair_state(team)
    out = {"team": team, "used": st["used"], "max_turns": st["max_turns"], "terminal": st.get("terminal"),
           "paused": st.get("paused"), "inflight": st.get("inflight"),
           "unknown_turns": [i + 1 for i, t in enumerate(st["turns"]) if t in unknown_turns(st)],
           "abandoned_turns": [i + 1 for i, t in enumerate(st["turns"]) if t.get("abandoned")],
           "sessions": {}}
    for role, m in members(bus, team).items():
        o = own.get(m["key"]) if m["key"] else {"state": "native", "spawn": {}, "detail": {}}
        recorded = [r for r in (writer_of(o), o["detail"].get("relay")) if r]
        out["sessions"][role] = {
            "name": m["name"], "ownership": o["state"], "reason": o.get("reason"),
            "relay_writer_running": any(strict_identity(r) is None for r in recorded),
            "relay_writer_unverifiable": any(strict_identity(r) not in (None,) and not proven_gone(r) for r in recorded),
            "claimed_by": (bus.get_session(m["endpoint"])["state"] or {}).get("pair")}
    return out


# ---------------------------------------------------------------- console
def launch_console(root: str, record_path: str, serve_args: list[str]) -> None:
    """Record this process's pid + start time + exact argv, then become `relay serve`."""
    root = os.path.realpath(root)
    started = process_start_ms(os.getpid())
    if not valid_start(started):
        raise SystemExit("can't observe this process's start time: refusing to launch without identity evidence")
    argv = [sys.executable, "-m", "relay", "serve", "--root", root, *serve_args]
    rec = {"pid": os.getpid(), "started_ms": started, "argv": argv, "root": root, "recorded_at": now_iso()}
    fd = os.open(record_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)  # never overwrite a record
    with os.fdopen(fd, "w") as f:
        json.dump(rec, f, indent=2)
    env = dict(os.environ, PYTHONPATH=os.path.realpath(REPO) + (os.pathsep + os.environ["PYTHONPATH"]
                                                               if os.environ.get("PYTHONPATH") else ""))
    os.execve(sys.executable, argv, env)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("mode", choices=("console", "midturn", "crash-console", "stop-orphan", "status"))
    p.add_argument("--root", required=True)
    p.add_argument("--team")
    p.add_argument("--role", choices=("leader", "follower"), default="follower")
    p.add_argument("--turn", type=int, help="midturn/crash-console: which turn (1-based count in this team)")
    p.add_argument("--console-record", help="console: where to write the launch record; crash-console: read it")
    p.add_argument("--crash-record", help="stop-orphan: the crash-console verdict file (required)")
    p.add_argument("--wait", type=float, default=300)
    p.add_argument("--out", help="also write the verdict JSON here")
    argv = list(sys.argv[1:] if argv is None else argv)
    serve_args = argv[argv.index("--") + 1:] if "--" in argv else []  # console: `relay serve` options
    a = p.parse_args(argv[:argv.index("--")] if "--" in argv else argv)
    if a.mode == "console":
        if not a.console_record:
            p.error("console needs --console-record")
        launch_console(a.root, a.console_record, serve_args)
        return 1  # not reached
    if not a.team:
        p.error(f"{a.mode} needs --team")
    bus = Bus(Root(a.root))
    try:
        if a.mode == "midturn":
            if not a.turn:
                p.error("midturn needs --turn")
            res = midturn(bus, a.team, a.role, a.turn, a.wait)
        elif a.mode == "crash-console":
            if not (a.turn and a.console_record):
                p.error("crash-console needs --turn and --console-record")
            res = crash_console(bus, a.team, a.console_record, a.root, a.turn, a.wait)
        elif a.mode == "stop-orphan":
            res = stop_orphan(bus, a.root, a.team, a.role, a.crash_record)
        else:
            res = status(bus, a.team)
    except Refused as e:
        record(a.out, {"probe": a.mode, "verdict": "refused", "why": str(e)})
        return 4
    record(a.out, res)
    return 3 if res.get("verdict") == "not_observed" else 0


if __name__ == "__main__":
    sys.exit(main())
