#!/usr/bin/env python3
"""The single reviewer-launch diagnostic.

One fresh, empty scratch workspace; ONE `codex app-server` started through the relay's own
`CodexAppServerAdapter.launch(policy="reviewer")`, so the request is EXACTLY production's:
`thread/start {cwd, approvalPolicy: never, approvalsReviewer: user,
config: {default_permissions: ":read-only"}}`. This script never sends `turn/start`: zero
model turns. It records only the outcome, the redacted diagnostic and the child's confirmed
exit, under an overall deadline, and never retries.

Refused before anything is spawned (exit 4): a codex version other than the reviewed one, a run
folder that isn't brand new and empty, or an evidence file that already exists.

Exit codes: 0 accepted, 3 refused by the relay's checks, 4 preconditions not met (nothing
started), 5 timed out or the child's exit couldn't be confirmed.

Usage (live use needs the user's specific authorization):
  RUN=$(mktemp -d ~/relay-runs/codex-reviewer.XXXXXX)
  python3 scripts/codex_reviewer_probe.py --run-dir "$RUN"
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from relay import probes as PR  # noqa: E402
from relay.adapters import codex as CX  # noqa: E402
from relay.context import process_start_ms  # noqa: E402
from relay.storage import RelayError, now_iso  # noqa: E402

WANT_CODEX = "codex-cli 0.162.1"
EVIDENCE = "probe-reviewer.json"
DEADLINE_RANGE = (5.0, 600.0)
CLEANUP_S = 15.0  # cleanup's own bound, reported separately from the run's deadline
REQUEST = {"approvalPolicy": "never", "approvalsReviewer": "user",
           "config": {"default_permissions": CX.REVIEWER_PROFILE}}  # plus cwd; see CX.launch


def codex_version() -> str:
    out = subprocess.run([CX._codex(), "--version"], capture_output=True, text=True, timeout=20)
    return out.stdout.strip()


def gone(child: dict) -> bool:
    """The recorded child has exited. With its handle: the handle says so (reaped or not).
    Without: only a missing pid, or a pid now proven to be another process, counts."""
    proc = child.get("proc")
    if proc is not None:
        return proc.poll() is not None
    try:
        os.kill(child["pid"], 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    observed = process_start_ms(child["pid"])
    return PR.valid_start(observed) and PR.valid_start(child.get("started_ms")) and observed != child["started_ms"]


def kill_child(child: dict, grace: float = 5.0) -> bool:
    """Stop the recorded child; True once its exit is confirmed. Through its Popen
    handle when retained; otherwise by pid ONLY with a proven identity (both start times valid
    and equal). Unknown identity sends nothing and returns False (cleanup unconfirmed)."""
    if child.get("proc") is not None:
        return PR.stop_child(proc=child["proc"], grace=grace)
    if gone(child):
        return True
    return PR.stop_child(pid=child.get("pid"), started_ms=child.get("started_ms"), grace=grace,
                         kill=os.kill, start_of=process_start_ms)


def preconditions(run_dir: str, version_of) -> str | None:
    if not os.path.isdir(run_dir):
        return "the run folder doesn't exist (create it with mktemp -d)"
    if os.listdir(run_dir):
        return "the run folder isn't empty: use a brand-new one"
    got = version_of()
    if got != WANT_CODEX:
        return f"codex is {got!r}, not the reviewed {WANT_CODEX!r}"
    return None


def main(argv=None, *, version_of=codex_version, adapter_options=None, clock=time.monotonic) -> int:
    t_start = clock()  # the run's defined start: the deadline includes the precondition checks
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--deadline", type=PR.deadline_arg(*DEADLINE_RANGE), default=90.0,
                    help=f"overall seconds from the run's start, {DEADLINE_RANGE[0]:g}-{DEADLINE_RANGE[1]:g} (default 90)")
    try:
        a = ap.parse_args(argv)
    except SystemExit:
        return 4
    budget = PR.Budget(a.deadline, clock, start=t_start)
    run_dir = os.path.realpath(os.path.expanduser(a.run_dir))
    ev = {"probe": "codex-reviewer-launch", "started_at": now_iso(), "request": dict(REQUEST, cwd="<run>/ws"),
          "turn_start_sent": False, "model_turns": 0, "retries": 0, "deadline_s": a.deadline}
    why = preconditions(run_dir, version_of)
    if why:
        print(json.dumps({"outcome": "not_started", "why": why}))
        return 4
    ev["codex_version"] = WANT_CODEX
    ws = os.path.join(run_dir, "ws")
    os.mkdir(ws, 0o700)

    child: dict = {}
    box: dict = {}

    def run():
        try:
            box["conn"] = CX.CodexAppServerAdapter(adapter_options or {}).launch(
                ws, {"policy": "reviewer"}, on_spawn=child.update)
        except RelayError as e:
            box["error"] = e
        except Exception as e:  # noqa: BLE001 - recorded by class only
            box["unexpected"] = type(e).__name__

    th = threading.Thread(target=run, daemon=True, name="reviewer-launch")
    code = 5
    try:
        th.start()
        th.join(budget.remaining())  # the rest of the ONE budget from the run's start
        if th.is_alive():  # the overall deadline: stop the exact child, never retry
            ev["outcome"] = "timed_out"
        elif "conn" in box:
            conn = box["conn"]
            ev["outcome"] = "accepted"
            ev["policy"] = conn.policy
            ev["diagnostic"] = conn.diagnostic
            code = 0
        else:
            e = box.get("error")
            ev["outcome"] = "refused"
            ev["error_class"] = type(e).__name__ if e else box.get("unexpected")
            ev["diagnostic"] = ((e.details if e else {}) or {}).get("diagnostic")
            code = 3
    except BaseException as e:  # noqa: BLE001 - Ctrl-C or anything unexpected: still clean up
        ev["outcome"] = "interrupted" if isinstance(e, KeyboardInterrupt) else "error"
        ev["error_class"] = type(e).__name__
        code = 5
    finally:
        # Cleanup has its own bound and is reported; it never borrows from or extends the run.
        c0 = time.monotonic()
        # An interruption can land before the launch thread has spawned its child: wait (within
        # cleanup's bound) until the child is known or the thread is done, so none is missed.
        while not child and th.is_alive() and time.monotonic() - c0 < CLEANUP_S:
            time.sleep(0.02)
        conn = box.get("conn")
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001 - the handle below still decides
                pass
        confirmed = kill_child(child, grace=CLEANUP_S) if child else True
        while th.is_alive() and time.monotonic() - c0 < CLEANUP_S:  # bounded; never join() again
            time.sleep(0.05)
        if th.is_alive() or (child and not gone(child)):
            confirmed = False  # a child may still exist that this run couldn't prove stopped
        ev["cleanup"] = {"exit_confirmed": bool(confirmed), "seconds": round(time.monotonic() - c0, 2)}
        ev["child_exit_confirmed"] = bool(confirmed)
        if not confirmed:
            code = 5
        elif ev.get("outcome") == "timed_out":
            code = 5
    ev["child_pid"] = child.get("pid")
    ev["duration_s"] = round(clock() - t_start, 2)
    with open(os.path.join(run_dir, EVIDENCE), "x") as f:
        json.dump(ev, f, indent=2, sort_keys=True)
    print(json.dumps({"outcome": ev["outcome"], "evidence": os.path.join(run_dir, EVIDENCE)}))
    return code


if __name__ == "__main__":
    sys.exit(main())
