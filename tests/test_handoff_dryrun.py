"""Dry run: the live plan's exact same-team sequence, against fast simulated
interactive terminals (fake clients). Proves the sequencing and accounting the plan
relies on — staged budget checkpoints (6, +2, +2 = one cumulative cap of 10), the
180 s timeout, a non-terminal team across both cycles, intervention, return,
native turns, Hand over again, the mid-turn failure probe and cleanup. It proves
nothing about real clients; the live run stays gated."""

import os
import time

from util import RelayTest

from relay.bus import Bus
from relay.controller import reconcile
from relay.handoff import same_process
from relay.host import Host
from relay.observer import Discovery
from relay.storage import Conflict, actor, init_root

TIMEOUT_S = 180
STAGES = (6, 2, 2)  # cycle 1 (+ intervention), cycle 2, failure probe
CAP = sum(STAGES)


def ctx(home, sid):
    import json
    with open(os.path.join(home, "sessions", sid, "context.json")) as f:
        return json.load(f)


class HandoffDryRun(RelayTest):
    def setUp(self):
        super().setUp()
        # Step 0: a fresh root with the plan's limits (`relay init --max-turns 6 --turn-timeout 180`).
        self.root = init_root(os.path.join(self.tmp, "run", "root"), ["*"], max_turns=STAGES[0],
                              turn_timeout_s=TIMEOUT_S)
        self.bus = Bus(self.root)
        self.discovery = Discovery({"home": self.home}, ("fake",), interval=60)
        self.host = Host(self.bus, self.discovery, new_session_adapters={"claude": "fake", "codex": "fake"},
                         adapter_options={"home": self.home})
        self.scratch = os.path.realpath(os.path.join(self.tmp, "run", "scratch"))
        os.makedirs(self.scratch)

    def tearDown(self):
        self.host.shutdown()
        for c in list(self.host.handoffs.writers.values()):
            try:
                c.close()
            except Exception:
                pass
        super().tearDown()

    # ------------------------------------------------------------ helpers
    def terminal(self, sid, client, seed, **default):
        default.setdefault("natural", True)
        return self.fake(sid, interactive=True, seed_history=seed, client=client, cwd=self.scratch, default=default)

    def tick_until(self, pred, timeout=30):
        end = time.time() + timeout
        while time.time() < end:
            self.host.handoff_tick()
            if pred():
                return
            time.sleep(0.05)
        raise AssertionError("condition not met in time")

    def st(self):
        return self.bus.pair_state(self.pid)

    def own(self, sid):
        h = self.host.handoffs
        return h.own.get(h.key("fake", sid))

    def checkpoint(self):
        """Stop at the staged budget: routing waits for the human, nothing more is sent."""
        self.tick_until(lambda: self.host.attention_kind(self.st()) == "budget")
        used = self.st()["used"]
        time.sleep(0.6)
        self.host.handoff_tick()
        self.assertEqual(self.st()["used"], used)  # stays stopped at the checkpoint
        return used

    def extend(self, n):
        """`relay pair extend <team> --add n` then `relay pair reconcile <team> --outcome continue`."""
        self.bus.pair_event(self.pid, "budget_extended", {"add": n}, actor("human", "cli"))
        reconcile(self.bus, self.pid, "continue", reason=f"checkpoint: {n} more turns")

    # --------------------------------------------------------------- run
    def test_planned_sequence_on_one_team_with_staged_budget(self):
        cl = self.terminal("cl", "Claude", 1, sleep=0.6)        # seeded: one native turn each
        cx = self.terminal("cx", "Codex", 1, sleep=0.1, finish_after=1000)  # never finishes
        self.discovery.refresh_once()
        cat = {x["name"]: x for x in self.host.catalog()["sessions"]}
        r = self.host.manage(cat["scratch · Claude"]["key"], {"existing": cat["scratch · Codex"]["key"]},
                             "Each turn, reply with one short line stating your own code word, then the relay block.")
        self.pid = r["team"]
        d = self.bus.get_pair_def(self.pid)
        self.assertEqual((d["max_turns"], d["turn_timeout_s"]), (STAGES[0], TIMEOUT_S))  # verified before dispatch
        self.assertEqual(self.st()["used"], 0)

        # Step 3: both terminals close; the relay writes both.
        cl.stop(), cx.stop()
        self.tick_until(lambda: self.host._hosted_alive(self.pid))

        # Step 5: Intervene at the observable point ("… Claude is working…").
        self.tick_until(lambda: (self.st().get("inflight") or {}).get("role") == "follower"
                        and self.st()["inflight"]["phase"] == "accepted")
        self.host.intervene(self.pid)
        self.tick_until(lambda: self.host.console(self.pid)["can_send"])
        self.host.say(self.pid, "Keep going; same format.")
        self.assertIsNone(self.st()["attention"])

        # Checkpoint A: cycle 1 stops at 6 by budget, whatever the model wants.
        self.assertEqual(self.checkpoint(), STAGES[0])
        self.assertFalse(self.st()["terminal"])  # same team, still open

        # Steps 6-7: back to the terminals (pair-wide), one native verification turn each.
        for role in ("follower", "leader"):
            res = self.host.back_to_terminal(self.pid, [role])
            self.assertEqual(res["sessions"][0]["state"], "released", role)
            self.assertTrue(res["sessions"][0]["resume"])
        before = {s: len(ctx(self.home, s)["turns"]) for s in ("cl", "cx")}
        for sid, client, extra in (("cl", "Claude", {"sleep": 0.6}), ("cx", "Codex", {"sleep": 0.1, "finish_after": 1000})):
            t = self.terminal(sid, client, 0, **extra)  # the same pretend model, reopened natively
            t.manual_turn("What was your code word, and what did the manager ask you?")
            self.wait_until(lambda: len(ctx(self.home, sid)["turns"]) == before[sid] + 1)
            t.stop()
        self.assertEqual(self.st()["used"], STAGES[0])  # native turns aren't relay turns

        # Step 8: cycle 2 on the SAME team: hand over again, then checkpoint B (+2).
        self.host.hand_over_again(self.pid)
        self.tick_until(lambda: self.host._hosted_alive(self.pid))
        self.extend(STAGES[1])
        self.assertEqual(self.checkpoint(), STAGES[0] + STAGES[1])  # used carried on, not reset

        # Step 9: failure probe (+2): kill the RELAY's Claude writer mid-turn.
        self.extend(STAGES[2])
        self.tick_until(lambda: (self.st().get("inflight") or {}).get("role") == "follower"
                        and self.st()["inflight"]["phase"] == "accepted")
        writer = self.own("cl")["detail"]["relay"]
        shown = next(w for w in self.host.console(self.pid)["writers"] if w["name"] == "scratch · Claude")
        self.assertEqual(shown["writer_process"], writer)  # what the plan tells the operator to read
        os.kill(writer["pid"], 9)
        self.tick_until(lambda: self.own("cl")["state"] == "blocked")
        self.assertLessEqual(self.st()["used"], CAP)
        submits = len([e for e in self.root.read_stream(f"pair-{self.pid}") if e.type == "submit_attempted"])
        time.sleep(0.6)
        self.host.handoff_tick()
        self.assertEqual(len([e for e in self.root.read_stream(f"pair-{self.pid}") if e.type == "submit_attempted"]),
                         submits)  # no resend

        # Step 10: cleanup — stop managing, give back what the relay still holds, resolve the probe.
        self.host.release(self.pid)
        eid_cl = next(e for e, m in self.host._handoff_members(self.pid).items() if m[1] == "cl")
        from relay.protocol import unknown_turns
        if unknown_turns(self.st()):
            # the probe turn's outcome is unknown; exit alone doesn't give it back.
            with self.assertRaises(Conflict):
                self.host.resolve_handoff(self.pid, eid_cl)
            self.host.abandon_unknown(self.pid)  # the recorded, explicit disposition
        self.assertEqual(self.host.resolve_handoff(self.pid, eid_cl)["state"], "released")  # exit proven
        res = self.host.back_to_terminal(self.pid, ["leader"])
        self.assertEqual(res["sessions"][0]["state"], "released")
        for sid in ("cl", "cx"):
            self.assertFalse(same_process([h["relay"] for h in self.own(sid)["history"] if h.get("relay")][-1]))
        d = self.bus.get_pair_def(self.pid)
        for role in ("leader", "follower"):
            self.assertIsNone(self.bus.get_session(d[role]["endpoint"])["state"]["pair"])  # claims released

        # Accounting over the whole run.
        st = self.st()
        self.assertLessEqual(st["used"], CAP)
        self.assertEqual(st["max_turns"], CAP)  # one cumulative budget, extended only explicitly
        ext = [e.data["add"] for e in self.root.read_stream(f"pair-{self.pid}") if e.type == "budget_extended"]
        self.assertEqual(ext, list(STAGES[1:]))
        for sid in ("cl", "cx"):
            reqs = [t["request_id"] for t in ctx(self.home, sid)["turns"] if t["request_id"]]
            self.assertEqual(len(reqs), len(set(reqs)), sid)  # nothing delivered twice
        tid = self.bus.get_pair_def(self.pid)["thread_id"]
        ids = [m["id"] for m in self.bus.thread(tid)["messages"]]
        self.assertEqual(len(ids), len(set(ids)))  # no duplicated relay messages
