"""Regressions for the console refinement pass:
trustworthy controls, plain-language states, causal order, event records, demo mode."""

import json
import os
import threading
import time

from util import RelayTest

from relay.bus import Bus
from relay.host import STEPPED_IN, Host
from relay.observer import Discovery, Timeline
from relay.storage import Conflict, actor, init_root


class Base(RelayTest):
    max_turns = 10

    def setUp(self):
        super().setUp()
        self.root = init_root(os.path.join(self.tmp, "console-root"), ["*"], max_turns=self.max_turns)
        self.bus = Bus(self.root)
        self.discovery = Discovery({"home": self.home}, ("fake",), interval=60)
        self.host = Host(self.bus, self.discovery, new_session_adapters={"claude": "fake", "codex": "fake"},
                         adapter_options={"home": self.home})

    def tearDown(self):
        self.host.shutdown()
        super().tearDown()

    def session(self, sid, name, client="Claude", script=None, summary="", **default):
        default.setdefault("natural", True)
        f = self.fake(sid, script=script, default=default, seed_history=1, name=name, client=client)
        if summary:  # what the session itself reports doing (written the way --summary does)
            f.stop()
            self.fakes.remove(f)
            from util import FakeSession
            import subprocess, sys
            from util import REPO
            f2 = FakeSession.__new__(FakeSession)
            f2.home, f2.sid = os.path.join(self.tmp, "home"), sid
            f2.dir = os.path.join(f2.home, "sessions", sid)
            f2.proc = subprocess.Popen([sys.executable, "-m", "relay.adapters.fake_client", "--home", f2.home,
                                        "--session-id", sid, "--cwd", self.tmp, "--name", name, "--client", client,
                                        "--summary", summary, "--default", json.dumps(default)],
                                       cwd=REPO, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.fakes.append(f2)
            self.wait(lambda: json.load(open(os.path.join(f2.dir, "info.json"))).get("summary") == summary)
            return f2
        return f

    def cat(self):
        self.discovery.refresh_once()
        return {s["name"]: s for s in self.host.catalog()["sessions"]}

    def wait(self, pred, timeout=25):
        end = time.time() + timeout
        while time.time() < end:
            try:
                if pred():
                    return True
            except (OSError, ValueError, KeyError):
                pass
            time.sleep(0.1)
        raise AssertionError("timed out")

    def manage(self, name, goal="Do the thing.", manager=None):
        r = self.host.manage(self.cat()[name]["key"], manager or {"new": {"client": "claude"}}, goal)
        return r["team"]


class ControlTests(Base):
    def test_hand_back_only_resolves_an_ordinary_intervention(self):
        self.session("s1", "payments-api", script=[{"action": "ask_human", "subject": "Which folder?",
                                                    "text": "May I edit vendor/ too?"}])
        pid = self.manage("payments-api")
        self.wait(lambda: self.host.console(pid)["attention"] == "question")
        c = self.host.console(pid)
        self.assertEqual(c["asker"]["name"], "payments-api")  # the actual asker, by name
        self.assertEqual(c["line"], "Waiting for your answer.")  # the asker is named once, in the question box
        self.assertEqual(c["actions"], ["answer", "stop"])
        with self.assertRaises(Conflict):
            self.host.resume(pid)  # a question is not resolved by "hand back"
        self.assertEqual(self.host.console(pid)["attention"], "question")
        self.host.say(pid, "src/ only.")
        msgs = self.host.console(pid)["messages"]
        mine = [m for m in msgs if m["role"] == "you" and not m["brief"]][-1]
        self.assertEqual(mine["body"], "src/ only.")

    def test_uncertain_delivery_needs_its_own_resolution(self):
        self.session("s1", "payments-api", script=[{"crash_after_accept": True}])
        pid = self.manage("payments-api")
        self.wait(lambda: self.host.console(pid)["attention"] == "uncertain")
        c = self.host.console(pid)
        self.assertEqual(c["actions"], ["send_again", "stop"])
        for bad in (lambda: self.host.resume(pid), lambda: self.host.say(pid, "hello")):
            with self.assertRaises(Conflict):
                bad()
        with self.assertRaises(Conflict):
            self.host.resolve(pid, "more_turns")  # wrong resolution for this state
        used = self.bus.pair_state(pid)["used"]
        self.host._stop_hosted(pid)  # the crashed session can't take it; check the queue only
        self.host.resolve(pid, "send_again")
        st = self.bus.pair_state(pid)
        self.assertIsNone(st["attention"])
        self.assertEqual(st["queued"]["role"], "follower")  # the same message, deliberately resent
        self.assertEqual(st["used"], used)

    def test_native_approval_cannot_be_handed_back(self):
        self.session("s1", "payments-api", script=[{"approval": True, "approval_wait": 30}])
        pid = self.manage("payments-api")
        self.wait(lambda: self.host.console(pid)["attention"] == "approval")
        c = self.host.console(pid)
        self.assertIn("waiting for an approval in its own window", c["line"])
        self.assertEqual(c["actions"], ["intervene"])
        with self.assertRaises(Conflict):
            self.host.say(pid, "approved!")


class BudgetTests(Base):
    max_turns = 2

    def test_budget_needs_more_turns_not_hand_back(self):
        self.session("s1", "payments-api")
        pid = self.manage("payments-api")
        self.wait(lambda: self.host.console(pid)["attention"] == "budget")
        self.assertEqual(self.host.console(pid)["actions"], ["more_turns", "stop"])
        with self.assertRaises(Conflict):
            self.host.resume(pid)
        self.host.resolve(pid, "more_turns")
        st = self.bus.pair_state(pid)
        self.assertEqual(st["max_turns"], 7)
        self.assertIsNone(st["attention"])


class InterveneTests(Base):
    def test_intervene_finishes_the_turn_then_hands_you_control_keeping_the_response(self):
        self.session("s1", "payments-api", sleep=2.0)
        pid = self.manage("payments-api")
        self.wait(lambda: self.host.console(pid)["working"] == "follower")
        self.host.intervene(pid)
        c = self.host.console(pid)
        self.assertTrue(c["finishing_turn"])
        self.assertTrue(c["line"].startswith("Finishing the current turn"))
        self.assertFalse(c["can_send"])
        self.wait(lambda: not self.host.console(pid)["finishing_turn"])
        c = self.host.console(pid)
        self.assertEqual(c["line"], "You're in control.")
        self.assertEqual(c["actions"], ["send", "hand_back", "stop"])
        self.assertEqual(c["messages"][-1]["role"], "follower")  # the in-flight reply was kept
        cat = self.cat()
        self.assertEqual(cat["payments-api"]["managed_by"]["kind"], "you")  # an explicit assignment


class ReleaseTests(Base):
    def test_claims_are_kept_until_the_controller_has_stopped(self):
        self.session("s1", "payments-api", sleep=30)
        pid = self.manage("payments-api")
        self.wait(lambda: self.host.console(pid)["working"] == "follower")
        stuck = threading.Event()
        real = self.host.hosted[pid]["thread"]

        class Stuck:  # a controller thread that won't stop in time
            def join(self, timeout=None):
                pass

            def is_alive(self):
                return not stuck.is_set()

        self.host.hosted[pid]["thread"] = Stuck()
        with self.assertRaises(Conflict):
            self.host.release(pid)
        d = self.bus.get_pair_def(pid)
        for role in ("leader", "follower"):
            self.assertEqual(self.bus.get_session(d[role]["endpoint"])["state"]["pair"], pid)  # still claimed
        stuck.set()
        self.host.hosted[pid]["thread"] = real
        self.host.release(pid)
        self.assertIsNone(self.bus.get_session(d["follower"]["endpoint"])["state"]["pair"])

    def test_release_waits_for_a_controller_in_another_process(self):
        self.session("s1", "payments-api")
        key = self.cat()["payments-api"]["key"]
        host2 = Host(self.bus, self.discovery, new_session_adapters={"claude": "fake"},
                     adapter_options={"home": self.home})
        host2.start = lambda pid: None  # no controller here
        pid = host2.manage(key, {"new": {"client": "claude"}}, "x")["team"]
        self.bus.pair_event(pid, "controller_acquired", {"ctl": "ctl_other", "pid": 1, "host": "elsewhere"},
                            actor("controller", "ctl_other"))
        with self.assertRaises(Conflict):
            self.host.release(pid)
        self.assertEqual(self.bus.get_session(self.bus.get_pair_def(pid)["follower"]["endpoint"])["state"]["pair"], pid)


class ManagerTests(Base):
    def test_existing_manager_keeps_its_own_style(self):
        self.session("s1", "payments-api")
        self.session("s2", "infra-scripts", client="Codex")
        cat = self.cat()
        r = self.host.manage(cat["payments-api"]["key"], {"existing": cat["infra-scripts"]["key"]}, "Add a check.")
        brief = self.host.console(r["team"])["messages"][0]
        self.assertTrue(brief["brief"])
        self.assertNotIn("Your style", brief["body"])
        self.assertIsNone(self.host.console(r["team"])["manager"]["personality"])

    def test_new_manager_gets_the_chosen_style(self):
        self.session("s1", "payments-api")
        pid = self.manage("payments-api", manager={"new": {"client": "claude", "personality": "pair"}})
        brief = self.host.console(pid)["messages"][0]
        self.assertIn("Your style: Be a friendly pair programmer", brief["body"])


class PlainLanguageTests(Base):
    def test_no_manager_assigned_unknown_activity_and_summaries_from_evidence(self):
        self.session("s1", "payments-api", summary="Fixing the flaky retry test")
        self.session("s2", "docs-site")
        cat = self.cat()
        self.assertEqual(cat["docs-site"]["managed_by"], {"kind": "none"})
        self.assertEqual(cat["docs-site"]["doing"], "Idle")  # no evidence: no invented task
        self.assertEqual(cat["payments-api"]["doing"], "Idle · last: Fixing the flaky retry test")
        # a registered session with no current evidence of activity stays unknown
        rec, _ = self.bus.register_session(adapter="fake", client_session_id="gone-quiet", mode="attach",
                                           participant="gone-quiet", cwd=self.tmp, options={"home": self.home},
                                           info={"name": "gone-quiet"})
        cat = self.cat()
        self.assertEqual((cat["gone-quiet"]["state"], cat["gone-quiet"]["doing"]), ("unknown", "Activity unknown"))

    def test_conversation_is_in_causal_order(self):
        self.session("s1", "payments-api", sleep=0.5)
        pid = self.manage("payments-api")
        self.wait(lambda: len(self.host.console(pid)["messages"]) >= 3)
        self.host.intervene(pid)
        self.wait(lambda: self.host.console(pid)["can_send"])
        self.host.say(pid, "Also log each retry.")
        msgs = self.host.console(pid)["messages"]
        # Rewrite one reply's timestamp into the past: order must still follow reply links.
        victim = msgs[2]["id"]
        path = self.root.p("messages", victim + ".json")
        env = json.load(open(path))
        env["created_at"] = "2000-01-01T00:00:00.000Z"
        os.chmod(path, 0o600)
        with open(path, "w") as f:
            json.dump(env, f)
        after = [m["id"] for m in self.host.console(pid)["messages"]]
        self.assertEqual(after, [m["id"] for m in msgs])
        mine = next(m for m in self.host.console(pid)["messages"] if m["body"] == "Also log each retry.")
        self.assertIsNotNone(self.bus.get_message(mine["id"])["reply_to"])  # threaded, not a new root

    def test_demo_mode_is_labelled(self):
        self.assertTrue(self.host.catalog()["fake_mode"])
        real = Host(self.bus, self.discovery)
        self.assertFalse(real.catalog()["fake_mode"])


class EventRecordTests(Base):
    def test_full_records_with_labelled_redactions_and_no_tokens(self):
        m, _, _ = self.bus.send(sender="ops", to="dev", kind="task", subject="s", body="b")
        tok = self.bus.claim(m["id"], "dev")["token"]
        self.session("s1", "payments-api")
        pid = self.manage("payments-api")
        self.wait(lambda: self.bus.pair_state(pid)["used"] >= 1)
        events, _ = Timeline(self.bus).events(limit=1000)
        blob = json.dumps(events)
        self.assertNotIn(tok, blob)
        self.assertNotIn("ctl_", blob)  # controller ownership tokens never leave the server
        claim = next(e for e in events if e["type"] == "claimed")
        self.assertEqual(set(claim) >= {"stream", "seq", "event_id", "type", "at", "actor", "data", "redacted"}, True)
        self.assertIn("token (claim token)", claim["redacted"])
        disp = next(e for e in events if e["type"] == "dispatch_authorized")
        self.assertIn("ctl (controller ownership token)", disp["redacted"])
        self.assertIn("actor.id (controller ownership token)", disp["redacted"])
        self.assertIn("turn", disp["data"])
