"""The review loop with fake sessions and the real isolated check runner. Instruction -> implementation -> relay check ->
findings -> fix -> relay check -> acceptance, with no human message copying."""

import hashlib
import os
import sys
import time
import unittest
from unittest import mock

from util import RelayTest

from relay import verify as V
from relay.adapters.codex import reviewer_policy
from relay.adapters.base import WrongSession
from relay.bus import Bus
from relay.controller import Controller
from relay.host import Host
from relay.observer import Discovery
from relay.prompts import handoff_prompt
from relay.storage import InvalidInput, init_root

RUNNER = V.SandboxRunner()
NEEDS_SANDBOX = unittest.skipIf(RUNNER.available() is not None, "no isolated runner on this machine")
WRONG = "def add(a, b):\n    return a - b\n"
STILL_WRONG = "def add(a, b):\n    return a * b\n"
RIGHT = "def add(a, b):\n    return a + b\n"
CHECK = {"argv": [sys.executable, "-c", "from src import add; assert add(2, 3) == 5, add(2, 3); print('check ok')"],
         "timeout_s": 60}
HANDOVER = ("Architecture: one module, src.py.\nScope: src.py only; tests are out of scope.\n"
            "Decisions: keep the signature add(a, b).\nTask: add() must return the sum.\n")


def say(text, **kw):
    return dict({"text": text, "sleep": 0}, **kw)


class Crash(BaseException):
    """Stands in for the relay process dying mid-check (not an ordinary error)."""


class CrashingRunner:
    name = RUNNER.name

    def available(self):
        return None

    def config_hash(self, spec):
        return RUNNER.config_hash(spec)

    def run(self, spec):
        raise Crash()


class LoopBase(RelayTest):
    def setUp(self):
        super().setUp()
        self.root = init_root(os.path.join(self.tmp, "console-root"), ["*"], max_turns=10)
        self.bus = Bus(self.root)
        self.ws = os.path.realpath(os.path.join(self.tmp, "ws"))
        os.makedirs(self.ws)
        self.put("src.py", WRONG)
        self.discovery = Discovery({"home": self.home}, ("fake",), interval=60)
        self.hosts = []

    def tearDown(self):
        for h in self.hosts:
            h.shutdown()
        super().tearDown()

    def put(self, rel, text):
        with open(os.path.join(self.ws, rel), "w") as f:
            f.write(text)

    def host(self, reviewer_script, runner=RUNNER):
        h = Host(self.bus, self.discovery, new_session_adapters={"claude": "fake", "codex": "fake"},
                 adapter_options={"home": self.home, "launch": {"script": reviewer_script}}, check_runner=runner)
        self.hosts.append(h)
        return h

    def team(self, reviewer, implementer, runner=RUNNER, **review):
        self.h = self.host(reviewer, runner)
        self.fake("impl", script=implementer, default={"sleep": 0}, name="impl", client="Claude", cwd=self.ws)
        self.discovery.refresh_once()
        cat = {x["name"]: x for x in self.h.catalog()["sessions"]}
        spec = dict({"handover": HANDOVER, "done_when": "The relay check passes and the reviewer accepts.",
                     "check": CHECK, "deadline_min": 30, "max_turns": 8, "sources": ["review-loop-spec"]}, **review)
        return self.h.manage(cat["impl"]["key"], {"new": {"client": "codex", "personality": "reviewer"}},
                             "Make add() return the sum.", review=spec)["team"]

    def st(self, pid):
        return self.bus.pair_state(pid)

    def wait(self, pred, timeout=40):
        end = time.time() + timeout
        while time.time() < end:
            if pred():
                return
            time.sleep(0.1)
        raise AssertionError("timed out")


@NEEDS_SANDBOX
class ReviewLoopTests(LoopBase):
    def test_findings_fix_and_acceptance_flow_through_the_relay(self):
        pid = self.team(
            [say("Make add() return the sum. Change only src.py."),
             say("The relay's check failed: add(2, 3) must be 5. Fix it."),
             say("The relay's check passed for this revision. Accepted.", action="finish", accepted=True)],
            [say("Done: add() fixed, all tests pass.", write={"src.py": STILL_WRONG}),
             say("Fixed: add() now returns a + b.", write={"src.py": RIGHT})])
        self.wait(lambda: self.st(pid)["terminal"])
        st = self.st(pid)
        self.assertEqual(st["terminal"], "finished")
        self.assertEqual(st["used"], 5)  # no hidden model turns: the checks are commands
        impl = [t["turn"] for t in st["turns"] if t["role"] == "follower"]
        self.assertEqual([(c["revision"], c["status"]) for c in st["checks"]],
                         [(impl[0], "failed"), (impl[1], "passed")])
        self.assertIn("check ok", st["checks"][1]["output_tail"])
        self.assertEqual(st["accepted_work"]["revision"], impl[1])
        self.assertEqual(st["accepted_work"]["fingerprint"], V.fingerprint(self.ws)["sha256"])
        # The implementer's claim ("all tests pass") is in the conversation as its message; the
        # relay's own record says the first revision failed.
        c = self.h.console(pid)
        self.assertIn("all tests pass", next(m["body"] for m in c["messages"] if m["role"] == "follower"))
        rv = c["review"]
        self.assertTrue(rv["accepted"])
        self.assertEqual([x["status"] for x in rv["checks"]], ["failed", "passed"])
        self.assertIn("Accepted by the reviewer, with the relay's passing check of the latest revision.",
                      c["summary"]["facts"])
        # D2: the handover, verbatim and hashed, is the reviewer's first prompt.
        brief = next(m for m in c["messages"] if m.get("brief"))
        self.assertIn(HANDOVER, brief["body"])
        self.assertEqual(rv["handover"]["sha256"], hashlib.sha256(HANDOVER.encode()).hexdigest())
        self.assertIn(rv["handover"]["sha256"], brief["body"])
        # D1: the reviewer was launched with the reviewer policy.
        d = self.bus.get_pair_def(pid)
        self.assertEqual(self.bus.get_session(d["leader"]["endpoint"])["options"]["launch"]["policy"], "reviewer")

    def test_the_implementer_sees_the_relays_check_of_its_previous_revision(self):
        # in the next implementer prompt, labelled as relay data; no extra turn.
        import relay.controller as C
        seen, real = [], C.handoff_prompt
        def spy(**kw):
            seen.append((kw["role"], kw.get("check"), real(**kw)))
            return seen[-1][2]
        with mock.patch.object(C, "handoff_prompt", spy):
            pid = self.team(
                [say("Make add() return the sum."), say("The check failed. Fix it."),
                 say("Accepted.", action="finish", accepted=True)],
                [say("Done.", write={"src.py": STILL_WRONG}), say("Fixed.", write={"src.py": RIGHT})])
            self.wait(lambda: self.st(pid)["terminal"])
        impl = [x for x in seen if x[0] == "follower"]
        self.assertIsNone(impl[0][1])  # nothing before the first revision
        first = [t["turn"] for t in self.st(pid)["turns"] if t["role"] == "follower"][0]
        self.assertEqual((impl[1][1]["revision"], impl[1][1]["status"]), (first, "failed"))
        self.assertIn(f"Relay check of your previous revision ({first})", impl[1][2])
        self.assertIn("This is not a pass.", impl[1][2])
        self.assertEqual(self.st(pid)["used"], 5)  # no added model turns

    def test_acceptance_refused_after_a_failing_check(self):
        pid = self.team([say("Fix add()."), say("Looks fine to me.", action="finish", accepted=True)],
                        [say("Done, tests pass.", write={"src.py": STILL_WRONG})])
        self.wait(lambda: self.h.console(pid)["attention"] == "acceptance_refused")
        st = self.st(pid)
        self.assertIsNone(st["terminal"])
        self.assertIsNone(st["accepted_work"])
        c = self.h.console(pid)
        self.assertIn("check of the latest revision is failed", c["summary"]["decision"]["text"])
        self.assertTrue(c["can_send"])  # you say what happens next; nothing is sent meanwhile
        self.assertEqual(st["used"], 3)

    def test_finish_without_accepted_is_not_acceptance(self):
        pid = self.team([say("Fix add()."), say("Done here.", action="finish")],
                        [say("Fixed.", write={"src.py": RIGHT})])
        self.wait(lambda: self.h.console(pid)["attention"] == "acceptance_refused")
        self.assertIn("without accepting", self.st(pid)["attention"]["reason"])
        self.assertEqual(self.st(pid)["checks"][-1]["status"], "passed")
        self.assertIsNone(self.st(pid)["accepted_work"])

    def test_a_workspace_change_after_the_passing_check_invalidates_it(self):
        pid = self.team([say("Fix add()."), say("Accepted.", action="finish", accepted=True, sleep=3)],
                        [say("Fixed.", write={"src.py": RIGHT})])
        self.wait(lambda: self.st(pid)["checks"] and (self.st(pid)["inflight"] or {}).get("role") == "leader")
        self.put("notes.txt", "an untracked file appeared after the check\n")
        self.wait(lambda: self.h.console(pid)["attention"] == "acceptance_refused")
        self.assertIn("workspace changed after the passing check", self.st(pid)["attention"]["reason"])

    def test_swapping_a_directory_link_after_the_passing_check_blocks_acceptance(self):
        # Codex R1: src.py imports lib.source; lib is a directory link inside the workspace.
        for name, val in (("one", 5), ("two", 6)):
            os.makedirs(os.path.join(self.ws, name))
            self.put(f"{name}/__init__.py", "")
            self.put(f"{name}/source.py", f"FIVE = {val}\n")
        os.symlink("one", os.path.join(self.ws, "lib"))
        check = {"argv": [sys.executable, "-c", "from lib.source import FIVE; assert FIVE == 5"], "timeout_s": 60}
        pid = self.team([say("Check it."), say("Accepted.", action="finish", accepted=True, sleep=3)],
                        [say("Done.")], check=check)
        self.wait(lambda: self.st(pid)["checks"] and (self.st(pid)["inflight"] or {}).get("role") == "leader")
        self.assertEqual(self.st(pid)["checks"][0]["status"], "passed")
        os.unlink(os.path.join(self.ws, "lib"))
        os.symlink("two", os.path.join(self.ws, "lib"))
        self.wait(lambda: self.h.console(pid)["attention"] == "acceptance_refused")
        self.assertIn("workspace changed after the passing check", self.st(pid)["attention"]["reason"])

    def test_a_link_to_code_outside_the_workspace_can_never_pass(self):
        ext = os.path.realpath(os.path.join(self.tmp, "outside"))
        os.makedirs(ext)
        with open(os.path.join(ext, "__init__.py"), "w") as f:
            f.write("FIVE = 5\n")
        os.symlink(ext, os.path.join(self.ws, "lib"))
        check = {"argv": [sys.executable, "-c", "from lib import FIVE; assert FIVE == 5"], "timeout_s": 60}
        pid = self.team([say("Check it."), say("Accepted.", action="finish", accepted=True)],
                        [say("Done.")], check=check)
        self.wait(lambda: self.h.console(pid)["attention"] == "acceptance_refused")
        chk = self.st(pid)["checks"][0]
        self.assertEqual(chk["status"], "unknown")
        self.assertIn("links outside the workspace", chk["reason"])
        self.assertIsNone(self.st(pid)["accepted_work"])

    def test_a_link_into_an_excluded_folder_can_never_pass(self):
        # Codex R1 recheck: lib -> .relay-verify/generated (and a nested cache) isn't fingerprinted.
        for folder in (V.OUTPUT_DIR + "/generated", "pkg/__pycache__"):
            with self.subTest(folder=folder):
                os.makedirs(os.path.join(self.ws, folder))
                self.put(f"{folder}/__init__.py", "FIVE = 5\n")
                os.symlink(folder, os.path.join(self.ws, "lib"))
                check = {"argv": [sys.executable, "-c", "from lib import FIVE; assert FIVE == 5"], "timeout_s": 60}
                pid = self.team([say("Check it."), say("Accepted.", action="finish", accepted=True)],
                                [say("Done.")], check=check)
                self.wait(lambda: self.h.console(pid)["attention"] == "acceptance_refused")
                chk = self.st(pid)["checks"][0]
                self.assertEqual(chk["status"], "unknown")
                self.assertIn("excluded folder", chk["reason"])
                self.assertIsNone(self.st(pid)["accepted_work"])
                self.h.release(pid)
                os.unlink(os.path.join(self.ws, "lib"))

    def test_a_hidden_read_outside_the_workspace_can_never_pass(self):
        # Codex R1 recheck: the check's code reads a file outside the workspace that argv doesn't name.
        ext = os.path.realpath(os.path.join(self.tmp, "outside.py"))
        with open(ext, "w") as f:
            f.write("FIVE = 5\n")
        check = {"argv": [sys.executable, "-c", f"exec(open({ext!r}).read()); assert FIVE == 5"], "timeout_s": 60}
        pid = self.team([say("Check it."), say("Accepted.", action="finish", accepted=True)],
                        [say("Done.")], check=check)
        self.wait(lambda: self.h.console(pid)["attention"] == "acceptance_refused")
        chk = self.st(pid)["checks"][0]
        self.assertEqual(chk["status"], "failed", chk.get("output_tail"))
        self.assertIn("Operation not permitted", chk["output_tail"])
        self.assertIsNone(self.st(pid)["accepted_work"])

    def test_crash_mid_check_is_unknown_and_never_rerun_or_accepted(self):
        pid = self.team([say("Fix add()."), say("Accepted.", action="finish", accepted=True)],
                        [say("Fixed.", write={"src.py": RIGHT})], runner=CrashingRunner())
        self.wait(lambda: self.st(pid)["check_running"] is not None)
        time.sleep(0.5)  # the hosted controller thread died with the "crash"
        self.assertIsNotNone(self.st(pid)["check_running"])
        # A restarted controller (the crashed one's launched reviewer died with it).
        ctl = Controller(self.bus, pid, check_runner=RUNNER)
        ctl.acquire(takeover=True, force=True)
        end = time.time() + 20
        while time.time() < end and not self.st(pid)["attention"]:
            ctl.step()
            time.sleep(0.1)
        st = self.st(pid)
        self.assertEqual([c["status"] for c in st["checks"]], ["unknown"])  # settled, never re-run
        self.assertIn("relay stopped during the check", st["checks"][0]["reason"])
        self.assertIsNone(st["check_running"])
        self.assertEqual(len([t for t in st["turns"] if t["role"] == "follower"]), 1)  # nothing resent
        why, _ = ctl.acceptance_problem({"accepted": True})
        self.assertIn("is unknown", why)  # an unknown check never counts as passed
        ctl.release()

    def test_deadline_stops_prompts_checks_and_retries(self):
        pid = self.team([say("Fix add().", sleep=2), say("Accepted.", action="finish", accepted=True)],
                        [say("Fixed.", write={"src.py": RIGHT})], deadline_min=5)
        d = self.bus.get_pair_def(pid)["workflow"]
        self.assertAlmostEqual(d["deadline_ts"], time.time() + 300, delta=10)  # persisted at creation
        self.wait(lambda: self.st(pid)["used"] == 1)  # the reviewer's first turn is under way
        with mock.patch.object(Controller, "past_deadline", return_value=True):
            self.wait(lambda: self.h.console(pid)["attention"] == "deadline")
            st = self.st(pid)
            self.assertEqual(st["checks"], [])          # no check after the limit
            self.assertEqual(st["used"], 1)             # no new prompt
            c = self.h.console(pid)
            self.assertEqual((c["line"], c["actions"]), ("Time limit reached.", ["stop"]))
        # A restarted console reads the same persisted deadline (never reset).
        again = self.host([])
        self.assertEqual(again.console(pid)["review"]["deadline_at"], c["review"]["deadline_at"])

    def test_unavailable_runner_means_no_acceptance(self):
        pid = self.team([say("Fix add()."), say("Accepted.", action="finish", accepted=True)],
                        [say("Fixed.", write={"src.py": RIGHT})], runner=V.UnavailableRunner("no sandbox here"))
        self.wait(lambda: self.h.console(pid)["attention"] == "acceptance_refused")
        st = self.st(pid)
        self.assertEqual([c["status"] for c in st["checks"]], ["unavailable"])
        self.assertIn("unavailable", st["attention"]["reason"])
        self.assertIn("Relay checks are unavailable here", self.h.console(pid)["summary"]["facts"][0])


class ReviewSetupTests(LoopBase):
    def setUp(self):
        super().setUp()
        self.h = self.host([])
        self.fake("impl", default={"sleep": 0}, name="impl", client="Claude", cwd=self.ws)
        self.fake("other", default={"sleep": 0}, name="other", client="Codex", cwd=self.ws)
        self.discovery.refresh_once()
        self.cat = {x["name"]: x for x in self.h.catalog()["sessions"]}

    def manage(self, manager=None, **review):
        spec = dict({"handover": HANDOVER, "done_when": "x", "check": CHECK}, **review)
        return self.h.manage(self.cat["impl"]["key"], manager or {"new": {"client": "codex"}}, "Goal.", review=spec)

    def test_the_reviewer_is_always_a_new_relay_launched_codex(self):
        for m in ({"existing": self.cat["other"]["key"]}, {"new": {"client": "claude"}}):
            with self.subTest(m=m), self.assertRaises(InvalidInput):
                self.manage(manager=m)

    def test_bounds_are_required_and_checked(self):
        bad = [{"handover": ""}, {"handover": "x" * (24 * 1024 + 1)}, {"done_when": ""},
               {"deadline_min": 4}, {"deadline_min": 241}, {"max_turns": 1},
               {"check": {"argv": "pytest"}}, {"check": {"argv": ["true"], "cwd": "/"}},
               {"check": {"command": "python3 -m pytest 'unterminated"}}, {"sources": ["a\nb"]}]
        for b in bad:
            with self.subTest(b=str(b)[:40]), self.assertRaises(InvalidInput):
                self.manage(**b)
        self.assertEqual(self.bus.pair_ids(), [])  # nothing registered by a refused request

    def test_command_is_split_without_any_expansion(self):
        pid = self.manage(check={"command": "python3 -c 'print($HOME)' *.py"})["team"]
        argv = self.bus.get_pair_def(pid)["workflow"]["check"]["argv"]
        self.assertEqual(argv, ["python3", "-c", "print($HOME)", "*.py"])


class ReviewerPolicyTests(unittest.TestCase):
    GOOD = {"approvalPolicy": "never", "approvalsReviewer": "user", "sandbox": {"type": "readOnly", "networkAccess": False},
            "activePermissionProfile": {"id": ":read-only", "extends": None}}

    def test_accepts_only_read_only_never_user_no_network(self):
        self.assertEqual(reviewer_policy(dict(self.GOOD))["sandbox"]["type"], "readOnly")
        # No defaults: a missing reviewer or profile is refused.
        no_reviewer = {k: v for k, v in self.GOOD.items() if k != "approvalsReviewer"}
        no_profile = {k: v for k, v in self.GOOD.items() if k != "activePermissionProfile"}
        bad = [no_reviewer, no_profile, dict(self.GOOD, activePermissionProfile=None),
               dict(self.GOOD, activePermissionProfile={"id": ":workspace"}),dict(self.GOOD, approvalPolicy="on-request"), dict(self.GOOD, approvalsReviewer="auto_review"),
               dict(self.GOOD, sandbox={"type": "workspaceWrite"}), dict(self.GOOD, sandbox={"type": "readOnly", "networkAccess": True}),
               dict(self.GOOD, sandbox={"type": "readOnly", "writableRoots": ["/"]}), dict(self.GOOD, sandbox="read-only"),
               dict(self.GOOD, activePermissionProfile={"id": "mine", "extends": ":workspace"})]
        for b in bad:
            with self.subTest(b=str(b)[:80]), self.assertRaises(WrongSession):
                reviewer_policy(b)


class PromptTests(unittest.TestCase):
    PAIR = {"id": "p_1", "leader": {"participant": "rev"}, "follower": {"participant": "impl"}, "scope": "s",
            "workflow": {"kind": "review_loop"}}

    def test_reviewer_sees_claims_and_the_relays_check_separately(self):
        msg = {"id": "m_1", "from": "impl", "subject": "done", "body": "All tests pass (claim).", "turn_id": "u_2"}
        p = handoff_prompt(pair=self.PAIR, turn_id="u_3", role="leader", message=msg,
                           check={"status": "failed", "exit": 1, "duration_s": 0.2, "output_tail": "AssertionError: 6"})
        self.assertIn("All tests pass (claim).", p)
        self.assertIn("Relay check of this revision (run by the relay itself, not by the implementer)", p)
        self.assertIn("status: failed (exit 1, 0.2 s)", p)
        self.assertIn("| AssertionError: 6", p)
        self.assertIn('"accepted": true', p)
        f = handoff_prompt(pair=self.PAIR, turn_id="u_4", role="follower", message=dict(msg, **{"from": "rev"}))
        self.assertIn("your report is shown as your claims", f)
        self.assertNotIn("Relay check of this revision", f)


if __name__ == "__main__":
    unittest.main()
