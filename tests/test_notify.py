"""Decision notifications with fake delivery adapters only (no network, no real
message)."""

import json
import os
import tempfile
import threading
import time
import unittest
from unittest import mock

import util  # noqa: F401  (puts the repository on sys.path)
from relay import notify as N
from relay.storage import Conflict, InvalidInput, init_root


class FakeAdapter:
    def __init__(self, type_="webhook"):
        self.type, self.label = type_, "Fake " + type_
        self.sent, self.fail, self.delay = [], [], 0.0
        self.lock = threading.Lock()

    def check(self, dest):
        if not dest.get("to"):
            raise InvalidInput("needs a destination")
        return dict(dest)

    def send(self, dest, text, meta):
        if self.delay:
            time.sleep(self.delay)
        with self.lock:
            if self.fail:
                raise self.fail.pop(0)
            self.sent.append({"dest": dest, "text": text, "meta": meta})

    def codes(self):
        return [m["text"].split("code: ")[1][:6] for m in self.sent if "verification code" in m["text"]]

    def notices(self):
        return [m for m in self.sent if "verification code" not in m["text"]]


class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self):
        return self.t


class Base(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.tmp = self._t.name
        self.root = init_root(os.path.join(self.tmp, "root"), ["*"])
        self.hook, self.sms = FakeAdapter("webhook"), FakeAdapter("sms")
        self.cfg = N.Config(os.path.join(self.tmp, "cfg", "notify.json"), {"webhook": self.hook, "sms": self.sms})
        self.clock = Clock()
        self.open = {}
        self.n = self.notifier()

    def tearDown(self):
        self._t.cleanup()

    def notifier(self):
        return N.Notifier(self.root, self.cfg, lambda: list(self.open.values()), self.clock)

    def decide(self, did, kind="question", team="p_1a12aaaaaaaaaaaaaaaaaaa", question=None):
        self.open[did] = {"id": did, "kind": kind, "team": team, "question": question}

    def enable(self, ch="webhook", to="dest-1"):
        self.cfg.set_destination(ch, {"to": to})
        self.cfg.start_verify(ch, now=self.clock())
        adapter = self.hook if ch == "webhook" else self.sms
        self.cfg.confirm_verify(ch, adapter.codes()[-1], now=self.clock())
        self.cfg.set_enabled(ch, True, now=self.clock())
        adapter.sent.clear()


class DefaultsAndVerification(Base):
    def test_off_by_default_nothing_is_sent(self):
        self.decide("d1")
        self.n.tick()
        self.assertEqual((self.hook.sent, self.sms.sent), ([], []))
        self.assertEqual(self.cfg.public()["channels"], {})
        self.assertFalse(self.cfg.public()["question_text"])  # kind-only by default

    def test_verify_before_enable_and_changes_invalidate(self):
        self.cfg.set_destination("webhook", {"to": "dest-1"})
        with self.assertRaises(Conflict):
            self.cfg.set_enabled("webhook", True)
        self.cfg.start_verify("webhook", now=self.clock())
        with self.assertRaises(InvalidInput):
            self.cfg.confirm_verify("webhook", "000000" if self.hook.codes()[-1] != "000000" else "111111", now=self.clock())
        self.cfg.confirm_verify("webhook", self.hook.codes()[-1], now=self.clock())
        self.cfg.set_enabled("webhook", True, now=self.clock())
        self.assertTrue(self.cfg.public()["channels"]["webhook"]["enabled"])
        self.cfg.set_destination("webhook", {"to": "dest-2"})  # a new destination: unverified and off
        ch = self.cfg.public()["channels"]["webhook"]
        self.assertEqual((ch["enabled"], ch["verified"]), (False, False))
        with self.assertRaises(Conflict):
            self.cfg.set_enabled("webhook", True)

    def test_codes_expire_are_rate_limited_and_attempts_bounded(self):
        self.cfg.set_destination("sms", {"to": "+15555550100"})
        self.cfg.start_verify("sms", now=self.clock())
        with self.assertRaises(Conflict):
            self.cfg.start_verify("sms", now=self.clock() + 10)  # a minute between codes
        with self.assertRaises(Conflict):
            self.cfg.confirm_verify("sms", self.sms.codes()[-1], now=self.clock() + N.CODE_TTL_S + 1)
        t = self.clock() + 100
        for i in range(4):
            self.cfg.start_verify("sms", now=t + i * 61)
        with self.assertRaises(Conflict):
            self.cfg.start_verify("sms", now=t + 5 * 61)  # five per hour
        good = self.sms.codes()[-1]
        bad = "000000" if good != "000000" else "111111"
        for _ in range(N.CODE_ATTEMPTS):
            with self.assertRaises(InvalidInput):
                self.cfg.confirm_verify("sms", bad, now=t + 4 * 61)
        with self.assertRaises(Conflict):
            self.cfg.confirm_verify("sms", good, now=t + 4 * 61)

    def test_config_file_is_private_and_public_view_has_no_credentials(self):
        self.cfg.set_destination("sms", {"to": "+15555550100", "auth_token": "tok-SECRET"})
        self.assertEqual(os.stat(self.cfg.path).st_mode & 0o777, 0o600)
        pub = str(self.cfg.public())
        self.assertNotIn("SECRET", pub)
        self.assertNotIn("5555550100", pub)

    def test_real_adapters_validate_destinations(self):
        with self.assertRaises(InvalidInput):
            N.CustomWebhook().check({"url": "http://example.com/hook"})
        with self.assertRaises(InvalidInput):
            N.TwilioSMS().check({"account_sid": "nope", "auth_token": "t", "from": "+15555550100", "to": "+15555550101"})
        self.assertEqual(N.CustomWebhook.label, "Custom webhook")  # not presented as Slack/Discord-compatible
        with self.assertRaises(N.DeliveryError) as cm:
            N._post("http://example.com", b"{}", {}, ())
        self.assertTrue(cm.exception.definite)
        self.assertIsNone(N._NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://elsewhere"))


class Delivery(Base):
    def test_one_notice_per_new_decision_across_polls_and_restarts(self):
        self.enable()
        self.decide("d1")
        for _ in range(3):
            self.n.tick()
        self.notifier().tick()  # a restarted console
        self.assertEqual(len(self.hook.notices()), 1)
        self.assertEqual(self.n.status("d1")[0]["state"], "delivered")

    def test_decisions_open_before_enabling_are_not_notified(self):
        self.decide("old")
        self.n.tick()
        self.clock.t += 5
        self.enable()
        self.decide("new")
        self.n.tick()
        self.assertEqual(len(self.hook.notices()), 1)
        self.assertEqual(self.n.status("old"), [])

    def test_each_approval_request_is_its_own_decision(self):
        self.enable()
        self.decide("p_x:approval:u_1:req-1", kind="approval")
        self.decide("p_x:approval:u_1:req-2", kind="approval")
        self.n.tick()
        self.assertEqual(len(self.hook.notices()), 2)

    def test_two_concurrent_dispatchers_send_once(self):
        self.enable()
        self.decide("d1")
        self.hook.delay = 0.3
        a, b = self.notifier(), self.notifier()
        ts = [threading.Thread(target=x.tick) for x in (a, b, a, b)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(len(self.hook.notices()), 1)

    def test_resolution_before_dispatch_cancels_after_dispatch_cannot_retract(self):
        self.enable()
        self.decide("d1")
        calls = {"n": 0}
        real = list

        def decisions():
            calls["n"] += 1
            return real(self.open.values()) if calls["n"] == 1 else []  # resolved right before sending
        n = N.Notifier(self.root, self.cfg, decisions, self.clock)
        n.tick()
        self.assertEqual(self.hook.notices(), [])
        self.assertEqual(n.status("d1")[0]["state"], "cancelled")
        # Already dispatched: resolving afterwards can't take it back.
        self.decide("d2")
        self.n.tick()
        del self.open["d2"]
        self.n.tick()
        self.assertEqual(self.n.status("d2")[0]["state"], "delivered")

    def test_uncertain_delivery_is_never_resent_automatically(self):
        self.enable()
        self.decide("d1")
        self.hook.fail = [N.DeliveryError("no answer from the provider", definite=False)]
        self.n.tick()
        self.clock.t += 3600
        self.n.tick()
        st = self.n.status("d1")[0]
        self.assertEqual((st["state"], st["may_have_been_sent"]), ("uncertain", True))
        self.assertEqual(self.hook.notices(), [])
        with self.assertRaises(Conflict):
            self.n.resend(st["key"])  # must acknowledge a possible duplicate
        self.n.resend(st["key"], acknowledge_duplicate=True)
        self.n.tick()
        self.assertEqual(len(self.hook.notices()), 1)
        self.assertEqual(self.n.status("d1")[0]["attempt"], 2)

    def test_definite_failure_retries_once_then_waits_for_you(self):
        self.enable()
        self.decide("d1")
        self.hook.fail = [N.DeliveryError("the provider answered 400", True)] * 2
        self.n.tick()
        self.clock.t += N.RETRY_AFTER_S - 1
        self.n.tick()
        self.assertEqual(self.n.status("d1")[0]["attempt"], 1)
        self.clock.t += 2
        self.n.tick()  # the one automatic retry, which fails again
        self.clock.t += 3600
        self.n.tick()
        st = self.n.status("d1")[0]
        self.assertEqual((st["state"], st["attempt"]), ("failed", 2))
        self.assertIn("400", st["reason"])
        self.n.resend(st["key"])
        self.n.tick()
        self.assertEqual(len(self.hook.notices()), 1)

    def test_crash_mid_send_reads_as_may_have_been_sent(self):
        self.enable()
        self.decide("d1")
        self.n._reserve("d1|webhook|0", "d1", "webhook", self.cfg.active()["webhook"][1], self.clock())
        self.root.append(N.STREAM, lambda ev: ("dispatching", {"key": "d1|webhook|0", "attempt": 1,
                                                              "at": self.clock()}), N.WHO)
        self.clock.t += N.STUCK_AFTER_S + 1
        self.notifier().tick()
        self.assertEqual(self.n.status("d1")[0]["state"], "uncertain")
        self.assertEqual(self.hook.notices(), [])

    def test_changing_or_disabling_the_channel_cancels_unsent(self):
        self.enable()
        self.decide("d1")
        self.n._reserve("d1|webhook|0", "d1", "webhook", self.cfg.active()["webhook"][1], self.clock())
        self.cfg.set_destination("webhook", {"to": "dest-2"})
        self.n.tick()
        self.assertEqual(self.n.status("d1")[0]["state"], "cancelled")
        self.assertEqual(self.hook.notices(), [])

    def test_rate_limit_survives_restart_and_aggregate_uses_a_slot(self):
        self.enable()
        for i in range(8):
            self.decide(f"d{i}")
        self.n.tick()
        notices = self.hook.notices()
        self.assertEqual(len(notices), N.HOURLY_LIMIT)  # 5 single + 1 combined, never a 7th
        self.assertIn("3 decisions are waiting", notices[-1]["text"])
        self.decide("late")
        self.clock.t += 600
        self.notifier().tick()  # restarted: the hour's count is in the outbox
        self.assertEqual(len(self.hook.notices()), N.HOURLY_LIMIT)
        self.clock.t += 3600
        self.notifier().tick()
        self.assertEqual(len(self.hook.notices()), N.HOURLY_LIMIT + 1)

    def test_reminders_only_while_open_and_bounded(self):
        self.enable()
        self.cfg.set_options(reminder={"after_min": 30, "max": 2})
        self.decide("d1")
        self.n.tick()
        for _ in range(5):
            self.clock.t += 31 * 60
            self.n.tick()
        self.assertEqual(len(self.hook.notices()), 3)  # first + 2 reminders
        self.decide("d2")
        self.n.tick()
        del self.open["d2"]
        self.clock.t += 31 * 60
        self.n.tick()
        self.assertEqual(len([m for m in self.hook.notices()]), 4)  # no reminder for a resolved one


class Content(Base):
    def test_kind_only_by_default_and_every_field_redacted(self):
        self.enable()
        self.cfg.set_options(display_label="R sk-ant-" + "A" * 25)
        self.decide("d1", question="Should I edit /Users/me/proj/src/app.py for p_1a12bbbbbbbbbbbbbbbbbbb?")
        self.n.tick()
        text = self.hook.notices()[0]["text"]
        self.assertNotIn("sk-ant-", text)
        self.assertNotIn("Should I edit", text)  # question text is opt-in
        self.assertIn("Open Agent Relay on your computer.", text)
        self.assertNotIn("p_1a12", text)

    def test_opt_in_excerpt_is_redacted_and_code_is_never_sent(self):
        cfg = {"display_label": "Relay", "question_text": True, "link_base": None}
        t = N.compose({"id": "d", "kind": "question", "team": "p_1",
                       "question": "Edit /Users/me/proj/app.py in s_1a12cccccccccccccccc? key ghp_" + "x" * 36}, cfg)
        self.assertIn("[path]", t)
        self.assertIn("[id]", t)
        self.assertNotIn("ghp_", t)
        self.assertNotIn("/Users/me", t)
        code = N.compose({"id": "d", "kind": "question", "team": "p_1", "question": "Run `rm -rf build`?"}, cfg)
        self.assertNotIn("rm -rf", code)

    def test_link_base_must_be_credential_free(self):
        for bad in ("http://mac.example", "https://user:pw@mac.example", "https://tok@mac.example",
                    "https://mac.example/?t=abc", "https://mac.example/#t=abc", "https://", "ftp://mac.example"):
            with self.subTest(bad=bad), self.assertRaises(InvalidInput):
                self.cfg.set_options(link_base=bad)
        self.cfg.set_options(link_base="https://Mac.Tailnet.example/relay/")
        self.assertEqual(self.cfg.public()["link_base"], "https://mac.tailnet.example/relay")

    def test_links_are_opaque_and_payloads_carry_no_ids_or_tokens(self):
        self.enable()
        self.cfg.set_options(link_base="https://mac.tailnet.example")
        team = "p_1a12dddddddddddddddddddd"
        self.decide(f"{team}:att:7", team=team)
        self.n.tick()
        m = self.hook.notices()[0]
        blob = json.dumps(m)
        ref = N.decision_ref(f"{team}:att:7", self.cfg.link_key())
        self.assertEqual(m["meta"], {"decision": ref, "link": f"https://mac.tailnet.example/#/d/{ref}"})
        self.assertIn(f"Open: https://mac.tailnet.example/#/d/{ref}", m["text"])
        for leak in (team, "p_1a12", ":att:", "t=", "token"):
            self.assertNotIn(leak, blob)
        # Per-install: another install's key gives another reference.
        self.assertNotEqual(ref, N.decision_ref(f"{team}:att:7", b"another-install"))


class Hardening(Base):
    """Codex R1/R2 (2026-10-11)."""

    def seed_dispatches(self, n):
        for i in range(n):
            key = f"old{i}|webhook|0"
            self.root.append(N.STREAM, lambda ev, key=key: ("reserved", {
                "key": key, "decision": f"old{i}", "channel": "webhook", "dest": "x", "attempt": 1,
                "covers": [f"old{i}"], "at": self.clock()}), N.WHO)
            self.root.append(N.STREAM, lambda ev, key=key: ("dispatching", {"key": key, "attempt": 1, "at": self.clock()}), N.WHO)
            self.root.append(N.STREAM, lambda ev, key=key: ("delivered", {"key": key, "at": self.clock()}), N.WHO)

    def test_two_dispatchers_cannot_both_spend_the_last_slot(self):
        self.enable()
        self.seed_dispatches(N.HOURLY_LIMIT - 1)
        self.decide("new-a")
        self.decide("new-b")
        b = self.cfg.active()["webhook"][1]
        for d in ("new-a", "new-b"):
            self.n._reserve(f"{d}|webhook|0", d, "webhook", b, self.clock())
        keys = [self.n.state()["keys"][f"{d}|webhook|0"] for d in ("new-a", "new-b")]
        workers = [N.Notifier(self.root, self.cfg, lambda: list(self.open.values()), self.clock) for _ in keys]
        cfg = self.cfg.load()
        ts = [threading.Thread(target=w._dispatch, args=(k, self.open, cfg, self.clock())) for w, k in zip(workers, keys)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        st = self.n.state()
        self.assertEqual(len(st["dispatches"]), N.HOURLY_LIMIT)  # never a 7th
        self.assertEqual(sorted(st["keys"][f"{d}|webhook|0"]["state"] for d in ("new-a", "new-b")),
                         ["delivered", "reserved"])

    def test_retries_and_combined_messages_use_the_same_fence_after_restart(self):
        self.enable()
        self.decide("d1")
        self.decide("d2")
        self.hook.fail = [N.DeliveryError("the provider answered 400", True)] * 2
        self.n.tick()  # two definite failures: 2 slots used
        self.seed_dispatches(N.HOURLY_LIMIT - 2)  # the hour is now full
        st = self.n.state()
        key = next(k for k in st["keys"] if k.startswith("d1|"))
        self.n.resend(key)  # your manual retry ...
        self.clock.t += N.RETRY_AFTER_S + 1  # ... and d2's automatic one
        for _ in range(2):
            self.notifier().tick()  # restarted consoles
        self.assertEqual(len(self.n.state()["dispatches"]), N.HOURLY_LIMIT)
        self.assertEqual(self.hook.notices(), [])
        self.clock.t += 3600
        self.notifier().tick()
        self.assertEqual(len(self.hook.notices()), 2)  # both, once slots free

    def test_resaving_a_destination_does_not_reset_code_limits(self):
        self.cfg.set_destination("sms", {"to": "+15555550100"})
        self.cfg.start_verify("sms", now=self.clock())
        refused = 0
        for _ in range(6):
            self.cfg.set_destination("sms", {"to": "+15555550100"})
            try:
                self.cfg.start_verify("sms", now=self.clock())
            except Conflict:
                refused += 1
        self.assertEqual((len(self.sms.codes()), refused), (1, 6))

    def test_concurrent_changes_are_never_lost(self):
        self.enable()
        v0 = self.cfg.load()["version"]
        errs = []

        def label(i):
            try:
                self.cfg.set_options(display_label=f"Relay {i}")
            except Exception as e:  # pragma: no cover - reported below
                errs.append(e)
        ts = [threading.Thread(target=label, args=(i,)) for i in range(12)]
        ts.append(threading.Thread(target=lambda: self.cfg.set_enabled("webhook", False)))
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        c = self.cfg.load()
        self.assertEqual(errs, [])
        self.assertEqual(c["version"], v0 + 13)  # every change applied, none overwritten
        self.assertFalse(c["channels"]["webhook"]["enabled"])  # the disable wasn't undone
        self.assertEqual([f for f in os.listdir(os.path.dirname(self.cfg.path)) if f.endswith(".tmp")], [])

    def test_a_disable_and_re_enable_never_revives_an_old_reservation(self):
        self.enable()
        self.decide("d1")
        self.n._reserve("d1|webhook|0", "d1", "webhook", self.cfg.active()["webhook"][1], self.clock())
        self.cfg.set_enabled("webhook", False)
        self.cfg.set_enabled("webhook", True, now=self.clock())  # same destination, still verified
        self.n.tick()
        self.assertEqual(self.n.status("d1")[0]["state"], "cancelled")
        self.assertEqual(self.hook.notices(), [])

# ------------------------------------------------------- with a real console (fake sessions)
from test_console_refine import Base as ConsoleBase  # noqa: E402


class HostDecisions(ConsoleBase):
    def setUp(self):
        super().setUp()
        self.hook, self.sms = FakeAdapter("webhook"), FakeAdapter("sms")
        cfg = N.Config(os.path.join(self.tmp, "notify-cfg", "notify.json"), {"webhook": self.hook, "sms": self.sms})
        self.host.notify_config = cfg
        self.host.notifier = N.Notifier(self.bus.root, cfg, self.host.open_decisions)
        cfg.set_destination("webhook", {"to": "dest"})
        cfg.start_verify("webhook")
        cfg.confirm_verify("webhook", self.hook.codes()[-1])
        cfg.set_enabled("webhook", True, now=time.time() - 1)
        self.hook.sent.clear()

    def asking_team(self):
        self.session("s1", "payments-api", script=[{"action": "ask_human", "subject": "Which folder?",
                                                    "text": "May I edit vendor/ too?"}])
        pid = self.manage("payments-api")
        self.wait(lambda: self.host.console(pid)["attention"] == "question")
        return pid

    def test_a_question_is_one_stable_decision_and_resolves_when_answered(self):
        pid = self.asking_team()
        ids = [d["id"] for d in self.host.open_decisions()]
        self.assertEqual(len(ids), 1)
        self.assertTrue(ids[0].startswith(f"{pid}:att:"))
        for _ in range(3):
            self.host.notify_tick()
        self.assertEqual([d["id"] for d in self.host.open_decisions()], ids)  # polling never mints new ones
        self.assertEqual(len(self.hook.notices()), 1)
        text = self.hook.notices()[0]["text"]
        self.assertIn("has a question for you", text)
        self.assertNotIn("vendor", text)  # kind-only by default
        self.assertNotIn(pid, text)
        self.assertEqual(self.host.console(pid)["notices"][0]["state"], "delivered")
        ref = N.decision_ref(ids[0], self.host.notify_config.link_key())
        self.assertEqual(self.host.resolve_decision_ref(ref)["team"], pid)  # the opaque link resolves
        with self.assertRaises(InvalidInput):
            self.host.resolve_decision_ref(pid)
        self.host.say(pid, "Yes, vendor/ too.")
        self.wait(lambda: not [d for d in self.host.open_decisions() if d["id"] == ids[0]])
        self.host.notify_tick()
        self.assertIn(ids[0], self.host.notifier.state()["resolved"])
        from relay.storage import NotFound
        with self.assertRaises(NotFound):
            self.host.resolve_decision_ref(ref)  # a closed decision no longer opens

    def test_stepping_in_yourself_never_notifies(self):
        self.session("s1", "payments-api", sleep=3)
        pid = self.manage("payments-api")
        self.host.intervene(pid)
        self.wait(lambda: self.host.console(pid)["attention"] == "stepped_in")
        self.assertEqual(self.host.open_decisions(), [])
        self.host.notify_tick()
        self.assertEqual(self.hook.notices(), [])

    def test_stopping_cancels_an_open_question(self):
        pid = self.asking_team()
        self.host.release(pid)
        self.wait(lambda: self.bus.pair_state(pid)["terminal"])
        self.assertEqual([d for d in self.host.open_decisions() if d["team"] == pid], [])

    def test_each_approval_is_its_own_decision(self):
        def team(seq, rid=None):
            return {"pair_id": "p_x", "definition": {}, "status": "running", "terminal": None, "attention": None,
                    "inflight": {"turn": "u_1", "waiting": "approval_pending in the follower's own interface",
                                 "waiting_request": rid, "waiting_seq": seq, "waiting_at": "2026-10-10T00:00:00.000Z"},
                    "turns": [], "disposed": []}
        with mock.patch.object(self.host, "_handoff_members", return_value={}):
            ids = []
            for st in (team(12), team(15), team(20, rid="req-7")):
                with mock.patch.object(self.host, "_teams", return_value={"p_x": st}):
                    ids += [d["id"] for d in self.host.open_decisions()]
        self.assertEqual(ids, ["p_x:approval:u_1:seq12", "p_x:approval:u_1:seq15", "p_x:approval:u_1:req-7"])
