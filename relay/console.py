"""Foreground terminal console (`relay console`).

A plain line-oriented menu: no browser, no server, no background process. While
a pair runs, single-letter commands are read between controller steps.
"""

from __future__ import annotations

import os
import select
import sys
import uuid

from .adapters import ADAPTERS, get_adapter
from .bus import Bus, summarize_pair
from .cli import fmt_pair, fmt_status, fmt_thread, pair_preview
from .controller import Controller, answer, reconcile
from .storage import RelayError, actor

MENU = """
[l] list sessions   [a] attach existing   [n] launch new   [p] create pair
[s] start pair      [r] run/resume pair   [t] thread       [v] status
[x] reconcile pair  [h] answer as human    [d] detach pair  [q] quit
"""
RUN_KEYS = ("while running: [p] pause  [r] resume  [s] stop routing  [t] thread  [v] status  "
            "[q] quit console (client work is not cancelled)")


class Console:
    def __init__(self, bus: Bus, adapter_opts: dict):
        self.bus, self.opts = bus, adapter_opts

    def ask(self, prompt: str, default: str | None = None) -> str:
        v = input(f"{prompt}{f' [{default}]' if default else ''}: ").strip()
        return v or (default or "")

    def pick(self, prompt: str, options: list[str]) -> str:
        for i, o in enumerate(options, 1):
            print(f"  {i}. {o}")
        while True:
            v = self.ask(prompt)
            if v.isdigit() and 1 <= int(v) <= len(options):
                return options[int(v) - 1]
            if v in options:
                return v
            print("  choose a number from the list")

    def loop(self) -> None:
        print("agent relay console — root", self.bus.root.path)
        print("Selecting sessions and pressing Start authorizes routing only within the shown "
              "scope and limits.")
        while True:
            print(MENU)
            try:
                c = self.ask("choice").lower()
            except EOFError:
                return
            try:
                if c == "q":
                    return
                {"l": self.list_sessions, "a": self.attach, "n": self.launch, "p": self.create_pair,
                 "s": self.start_pair, "r": self.run_pair, "t": self.thread, "v": self.status,
                 "x": self.reconcile, "h": self.answer, "d": self.detach}.get(c, lambda: print("unknown choice"))()
            except RelayError as e:
                print(f"error: {e}")
            except KeyboardInterrupt:
                print("\n(cancelled)")

    # ------------------------------------------------------------ sessions
    def list_sessions(self) -> None:
        for name in ADAPTERS:
            try:
                ad = get_adapter(name, self.opts)
                sessions = ad.discover()
            except RelayError as e:
                print(f"[{name}] {e}")
                continue
            caps = ad.capabilities()
            print(f"[{name}] attach_live={caps['attach_live']} submit={caps['submit']} "
                  f"turn_complete={caps['turn_complete']} verified={ad.verified}")
            for s in sessions:
                print(f"   {s.client_session_id}  cwd={s.cwd}  activity={s.activity}  "
                      f"attachable={s.attachable}")
                for lim in s.limitations:
                    print(f"      limitation: {lim}")
        print("registered endpoints:")
        for s in self.bus.sessions():
            print(f"   {s['id']} {s['participant']} {s['adapter']}/{s.get('mode')} "
                  f"{s['client_session_id']} [{s['state']['state']}]")

    def attach(self) -> None:
        name = self.pick("adapter", list(ADAPTERS))
        ad = get_adapter(name, self.opts)
        if not ad.capabilities()["attach_live"]:
            print(f"{name} cannot attach to a live session (see docs/INTEGRATION-FEASIBILITY.md). "
                  "The console will not substitute resume, fork or launch.")
            return
        found = [s for s in ad.discover() if s.attachable]
        if found:
            sid = self.pick("session", [s.client_session_id for s in found])
            info = next(s for s in found if s.client_session_id == sid)
        else:
            sid = self.ask("no sessions discovered; client session id to register explicitly")
            info = None
        part = self.pick("participant", self.bus.participants)
        rec, _ = self.bus.register_session(adapter=name, client_session_id=sid, mode="attach",
                                           participant=part, cwd=info.cwd if info else None,
                                           options=self.opts, capabilities=ad.capabilities())
        print(f"endpoint {rec['id']} = {part} -> {name} session {sid} (attach; never closed by relay)")

    def launch(self) -> None:
        names = [n for n in ADAPTERS if get_adapter(n, self.opts).capabilities()["launch"]]
        name = self.pick("adapter", names)
        cwd = os.path.realpath(self.ask("working directory", os.getcwd()))
        part = self.pick("participant", self.bus.participants)
        sid = str(uuid.uuid4()) if name in ("fake", "claude-stream") else f"pending-{uuid.uuid4()}"
        rec, _ = self.bus.register_session(adapter=name, client_session_id=sid, mode="launch",
                                           participant=part, cwd=cwd, options=self.opts,
                                           capabilities=get_adapter(name, self.opts).capabilities())
        print(f"endpoint {rec['id']} = {part} -> new {name} session in {cwd} (starts with the pair; "
              "closed when the console's controller exits)")

    # --------------------------------------------------------------- pairs
    def free_endpoints(self) -> list[str]:
        return [s["id"] for s in self.bus.sessions()
                if s["state"]["state"] in ("registered",) and "error" not in s]

    def create_pair(self) -> None:
        free = self.free_endpoints()
        if len(free) < 2:
            print("need two free endpoints; attach or launch sessions first")
            return
        print("leader (plans and reviews):")
        leader = self.pick("leader endpoint", free)
        print("follower (implements and reports):")
        follower = self.pick("follower endpoint", [e for e in free if e != leader])
        scope = self.ask("scope (authorized files/workflow and exclusions)")
        limits = self.bus.root.config["limits"]
        mt = int(self.ask("max automatic turns", str(limits["max_turns"])))
        to = int(self.ask("turn timeout seconds", str(limits["turn_timeout_s"])))
        d = self.bus.create_pair(leader=leader, follower=follower, scope=scope, max_turns=mt,
                                 turn_timeout_s=to)
        print(pair_preview(self.bus, d["id"]))

    def choose_pair(self, statuses=None) -> str | None:
        pairs = []
        for p in self.bus.pair_ids():
            try:
                pairs.append(summarize_pair(self.bus.pair_state(p)))
            except RelayError as e:
                print(f"  pair {p}: untrusted ({e}); skipped")
        pairs = [p for p in pairs if statuses is None or p["status"] in statuses]
        if not pairs:
            print("no matching pairs")
            return None
        return self.pick("pair", [p["pair"] for p in pairs])

    def read_body(self, prompt: str) -> str:
        path = self.ask(f"{prompt}: file path, or empty to type (end with a line containing only .)")
        if path:
            with open(path) as f:
                return f.read()
        lines = []
        while True:
            line = input()
            if line == ".":
                return "\n".join(lines)
            lines.append(line)

    def start_pair(self) -> None:
        pid = self.choose_pair({"idle"})
        if not pid:
            return
        st = self.bus.pair_state(pid)
        if st["started"]:
            print("pair already started; use run")
            return
        d = st["definition"]
        mode = self.pick("initial message", ["write a new leader handoff",
                                             "select an existing leader message",
                                             "send an initial request to the leader"])
        print(pair_preview(self.bus, pid))
        if mode.startswith("write"):
            body = self.read_body("handoff for the follower")
            subject = self.ask("subject", "Handoff")
            kwargs = {"body": body, "subject": subject}
        elif mode.startswith("select"):
            msgs = [m for m in self.bus.all_messages()[0]
                    if m["from"] == d["leader"]["participant"] and m["to"] == d["follower"]["participant"]]
            if not msgs:
                print("no leader->follower messages exist")
                return
            kwargs = {"message_id": self.pick("message", [m["id"] for m in msgs])}
        else:
            kwargs = {"body": self.read_body("request for the leader"),
                      "subject": self.ask("subject", "Initial request"), "to_leader": True}
        if self.ask("Start routing? (y/N)").lower() != "y":
            return
        r = self.bus.start_pair(pid, **kwargs)
        print(f"started with message {r['initial_message']} ({r['mode']})")
        self.drive(pid)

    def run_pair(self) -> None:
        pid = self.choose_pair()
        if pid:
            self.drive(pid)

    def drive(self, pid: str) -> None:
        ctl = Controller(self.bus, pid, log=lambda s: print(s, flush=True))
        st = self.bus.pair_state(pid)
        takeover = False
        if st.get("owner"):
            if self.ask(f"pair owned by {st['owner']['ctl']} (pid {st['owner'].get('pid')}); take "
                        "over? only if that controller is gone (y/N)").lower() != "y":
                return
            takeover = True
        ctl.acquire(takeover=takeover)
        print(RUN_KEYS)
        try:
            ctl.connect_all()

            stdin_open = [True]

            def tick(state) -> bool:
                if not stdin_open[0]:
                    return True
                r, _, _ = select.select([sys.stdin], [], [], 0)
                if not r:
                    return True
                line = sys.stdin.readline()
                if not line:  # EOF: keep routing, stop reading keys
                    stdin_open[0] = False
                    return True
                key = line.strip().lower()
                if not key:
                    return True
                who = actor("human")
                try:
                    if key == "p":
                        self.bus.pair_event(pid, "paused", {"reason": "console"}, who)
                        print("paused: no new dispatch; in-flight work continues and is recorded")
                    elif key == "r":
                        self.bus.pair_event(pid, "resumed", {}, who)
                        print("resumed")
                    elif key == "s":
                        self.bus.pair_event(pid, "stopped", {"reason": "console"}, who)
                        print("routing stopped")
                    elif key == "t":
                        print(fmt_thread(self.bus.thread(state["definition"]["thread_id"])))
                    elif key == "v":
                        print(fmt_pair(summarize_pair(self.bus.pair_state(pid))))
                    elif key == "q":
                        return False
                    else:
                        print(RUN_KEYS)
                except RelayError as e:
                    print(f"error: {e}")
                return True

            last = [None]

            def on_tick(state) -> bool:
                s = summarize_pair(state)
                sig = (s["status"], s["turns_used"], s["paused"])
                if sig != last[0]:
                    last[0] = sig
                    print(f"-- {s['status']}{' (paused)' if s['paused'] else ''} · turns "
                          f"{s['turns_used']}/{s['max_turns']} · whose turn: {s['whose_turn'] or '-'}"
                          + (f" · needs human: {s['attention']['reason']}" if s["attention"] else ""))
                return tick(state)

            ctl.run(exit_on=frozenset({"finished", "stopped"}), on_tick=on_tick)
        finally:
            launched = [e for e, c in ctl.connections.items() if c.mode == "launch"]
            ctl.shutdown()
            if launched:
                print(f"closed console-launched session(s) {', '.join(launched)}; they cannot be "
                      "reconnected. Attached sessions were only detached.")
        print(fmt_pair(summarize_pair(self.bus.pair_state(pid))))

    def thread(self) -> None:
        pid = self.choose_pair()
        if pid:
            print(fmt_thread(self.bus.thread(self.bus.get_pair_def(pid)["thread_id"])))

    def status(self) -> None:
        print(fmt_status(self.bus.status()))

    def reconcile(self) -> None:
        pid = self.choose_pair({"waiting_human", "delivery_uncertain", "failed", "disconnected"})
        if not pid:
            return
        print(fmt_pair(summarize_pair(self.bus.pair_state(pid))))
        outcome = self.pick("outcome", ["continue", "requeue", "drop", "route-late"])
        msg = self.ask("late message id") if outcome == "route-late" else None
        reason = self.ask("reason (recorded)")
        reconcile(self.bus, pid, outcome, message=msg, reason=reason)
        print("recorded; use [r] to run the pair again")

    def answer(self) -> None:
        pid = self.choose_pair({"waiting_human", "failed"})
        if not pid:
            return
        print(fmt_pair(summarize_pair(self.bus.pair_state(pid))))
        body = self.read_body("your answer")
        to = self.ask("send to (leader/follower, empty = whoever asked)") or None
        r = answer(self.bus, pid, body, to_role=to)
        print(f"answer {r['message']} queued for the {r['to']}; use [r] to continue the pair")

    def detach(self) -> None:
        pid = self.choose_pair()
        if not pid:
            return
        from .cli import cmd_pair_detach, Out
        import argparse
        cmd_pair_detach(argparse.Namespace(root=self.bus.root.path, pair_id=pid, json=False),
                        Out(False))
