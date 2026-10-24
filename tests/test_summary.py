"""The plain-language session summary and the optional manager progress report."""

import json
import unittest
from unittest import mock

from test_console_refine import Base

from relay import protocol as P
from relay.host import Host


def block(progress, action="continue", turn="u_1"):
    b = {"relay": 1, "turn_id": turn, "action": action, "subject": "s"}
    if progress is not None:
        b["progress"] = progress
    return f"Body.\n\n```relay\n{json.dumps(b)}\n```\n"


class ProgressContract(unittest.TestCase):
    def test_valid_progress_is_normalised_plain_text(self):
        ok = P.validate_progress({"done": ["Tests  pass\nnow"], "now": " Reviewing ", "next": "Finish"})
        self.assertEqual(ok, {"done": ["Tests pass now"], "now": "Reviewing", "next": "Finish"})
        self.assertEqual(P.validate_progress({"now": "x"}), {"done": [], "now": "x"})

    def test_caps_and_shape(self):
        bad = [
            "text", [], {"done": ["a", "b", "c", "d"]}, {"done": ["x" * 241]}, {"now": "x" * 241},
            {"done": "a"}, {"done": [1]}, {"now": 3}, {"who": "Codex"}, {}, {"done": []}, {"now": "   "},
            {"done": ["a\x00b"]}, {"now": "key sk-ant-" + "A" * 40},
            {"done": ["é" * 240] * 3, "now": "é" * 240, "next": "é" * 240},  # > 2 KB UTF-8
        ]
        for raw in bad:
            with self.subTest(raw=str(raw)[:60]), self.assertRaises(P.ProgressError):
                P.validate_progress(raw)

    def test_progress_is_separate_from_the_routing_envelope(self):
        body, b = P.parse_response(block({"done": ["x" * 999]}), "u_1")
        self.assertEqual((body, b["action"]), ("Body.", "continue"))  # still a valid route
        self.assertEqual(b["progress"], {"done": ["x" * 999]})        # validated later, separately
        self.assertIsNone(P.parse_response(block(None), "u_1")[1]["progress"])


class SummaryBase(Base):
    def team(self, leader_script, follower_sleep=0.2, follower_script=None, goal="Fix the parser."):
        self.session("s1", "payments-api", script=follower_script, sleep=follower_sleep)
        self.session("m1", "infra-scripts", client="Codex", script=leader_script, natural=False)
        cat = self.cat()
        return self.host.manage(cat["payments-api"]["key"], {"existing": cat["infra-scripts"]["key"]}, goal)["team"]

    def summary(self, pid, host=None):
        return (host or self.host).console(pid)["summary"]


class SummaryTests(SummaryBase):
    def test_working_summary_attributes_reports_and_keeps_facts_separate(self):
        pid = self.team([{"progress": {"done": ["payments-api reports the parser fix is in"],
                                       "now": "Waiting for the test run", "next": "Review the results"}},
                         {"action": "ask_human", "text": "Should I drop Python 3.9 support?",
                          "progress": {"now": "Asking you about Python 3.9"}}], follower_sleep=3)
        st = lambda: self.bus.pair_state(pid)
        self.wait(lambda: st()["progress"] and (st()["inflight"] or {}).get("role") == "follower")
        s = self.summary(pid)
        self.assertEqual(s["task"], "Fix the parser.")
        self.assertEqual(s["folder"], self.tmp)
        self.assertEqual([x["name"] for x in s["sessions"]], ["payments-api", "infra-scripts"])
        self.assertIsNone(s["decision"])
        self.assertEqual(s["now"]["relay"], "payments-api is working…")      # authoritative
        self.assertEqual(s["now"]["reported"]["text"], "Waiting for the test run")  # attributed claim
        self.assertEqual(s["now"]["reported"]["by"]["name"], "infra-scripts")
        self.assertFalse(s["now"]["reported"]["stale"])
        self.assertEqual(s["next"]["text"], "Review the results")
        self.assertEqual([h["text"] for h in s["happened"]], ["payments-api reports the parser fix is in"])
        self.assertEqual(s["happened"][0]["by"]["name"], "infra-scripts")
        self.assertEqual(s["facts"], ["2 of 10 turns used."])  # the manager's turn and this one
        # The reporter, role and turn come from the relay's record, not from the model.
        rep = self.bus.pair_state(pid)["progress"][0]
        self.assertEqual(rep["role"], "leader")
        self.assertEqual(rep["turn"], self.bus.pair_state(pid)["turns"][0]["turn"])

        # Then the manager asks: the decision is shown with the actual question, and the
        # reported "now" gives way to the lifecycle state.
        self.wait(lambda: self.host.console(pid)["attention"] == "question", timeout=30)
        s = self.summary(pid)
        self.assertEqual(s["decision"]["kind"], "question")
        self.assertEqual(s["decision"]["who"], "infra-scripts")
        self.assertIn("Should I drop Python 3.9 support?", s["decision"]["text"])
        self.assertIsNone(s["now"]["reported"])
        self.assertIsNone(s["next"])
        self.assertEqual(s["now"]["relay"], "Waiting for your answer.")
        self.assertEqual(len(s["happened"]), 1)  # the earlier outcome stays

    def test_invalid_or_follower_progress_never_breaks_routing(self):
        pid = self.team([{"progress": {"done": ["x" * 300]}}, {"action": "finish"}],
                        follower_script=[{"progress": {"done": ["I'm the follower"]}}])
        self.wait(lambda: self.bus.pair_state(pid)["terminal"] == "finished", timeout=30)
        st = self.bus.pair_state(pid)
        self.assertEqual(st["used"], 3)  # leader -> follower -> leader: routing unaffected
        self.assertEqual(st["progress"], [])
        reasons = [r["reason"] for r in st["progress_rejected"]]
        self.assertEqual(len(reasons), 2)
        self.assertIn("longer than 240", reasons[0])
        self.assertIn("only the manager", reasons[1])
        s = self.summary(pid)
        self.assertEqual((s["happened"], s["reported"], s["rejected"]), ([], False, 2))  # "Not reported yet."
        self.assertEqual(s["facts"][0], "infra-scripts ended the exchange.")

    def test_finished_and_stopped_clear_present_tense_but_keep_history(self):
        pid = self.team([{"progress": {"now": "Asking for the fix", "next": "Review"}},
                         {"action": "finish", "progress": {"done": ["Reviewed the reported test run"]}}])
        self.wait(lambda: self.bus.pair_state(pid)["terminal"] == "finished", timeout=30)
        s = self.summary(pid)
        self.assertIsNone(s["now"]["reported"])
        self.assertIsNone(s["next"])
        self.assertEqual(s["now"]["relay"], "Done.")
        self.assertEqual([h["text"] for h in s["happened"]], ["Reviewed the reported test run"])
        self.assertIsNone(s["decision"])
        # A "finish" is the manager ending the exchange, not proof the task is correct.
        self.assertNotIn("goal met", " ".join(s["facts"]).lower())

    def test_stale_reports_are_marked_after_ten_minutes(self):
        pid = self.team([{"progress": {"now": "Waiting for the test run", "next": "Review"}}], follower_sleep=3)
        self.wait(lambda: self.bus.pair_state(pid)["progress"])
        with mock.patch("relay.host._age_s", return_value=P.PROGRESS_FRESH_S - 1):
            self.assertFalse(self.summary(pid)["now"]["reported"]["stale"])
        with mock.patch("relay.host._age_s", return_value=P.PROGRESS_FRESH_S + 1):
            s = self.summary(pid)
        self.assertTrue(s["now"]["reported"]["stale"])
        self.assertTrue(s["next"]["stale"])
        self.assertEqual(s["fresh_s"], 600)  # independent of the per-turn timeout

    def test_intervene_overrides_the_reported_now(self):
        pid = self.team([{"progress": {"now": "Waiting for the test run"}}], follower_sleep=3)
        self.wait(lambda: self.bus.pair_state(pid)["progress"])
        self.host.intervene(pid)
        s = self.summary(pid)
        self.assertIsNone(s["now"]["reported"])
        self.assertIn(s["now"]["relay"], ("You're in control.",
                                          "Finishing the current turn — payments-api is wrapping up…"))
        self.assertIsNone(s["decision"])  # you stepped in yourself: nothing to decide

    def test_restart_preserves_the_summary(self):
        pid = self.team([{"progress": {"done": ["First change reviewed"]}}, {"action": "finish"}])
        # Settled: finished and the controller has let go (its release is the last event).
        self.wait(lambda: self.bus.pair_state(pid)["terminal"] == "finished"
                  and not self.bus.pair_state(pid)["owner"], timeout=30)
        before = self.summary(pid)
        again = Host(self.bus, self.discovery, new_session_adapters={"claude": "fake", "codex": "fake"},
                     adapter_options={"home": self.home})
        self.addCleanup(again.shutdown)
        after = self.summary(pid, again)
        for k in ("task", "folder", "happened", "facts", "decision", "now", "next", "runs", "updated_at"):
            self.assertEqual(after[k], before[k], k)

    def test_two_sessions_in_one_folder_and_earlier_runs(self):
        self.session("s1", "payments-api", sleep=0.2)
        self.session("s2", "billing", sleep=0.2)
        self.session("m1", "infra-scripts", client="Codex",
                     script=[{"progress": {"done": ["A: payments"]}}, {"action": "finish"}], natural=False)
        self.session("m2", "reviewer-two", client="Codex",
                     script=[{"progress": {"done": ["B: billing"]}}, {"action": "finish"}], natural=False)
        cat = self.cat()
        a = self.host.manage(cat["payments-api"]["key"], {"existing": cat["infra-scripts"]["key"]}, "Task A")["team"]
        b = self.host.manage(cat["billing"]["key"], {"existing": cat["reviewer-two"]["key"]}, "Task B")["team"]
        self.wait(lambda: all(self.bus.pair_state(p)["terminal"] for p in (a, b)), timeout=30)
        sa, sb = self.summary(a), self.summary(b)
        self.assertEqual(sa["folder"], sb["folder"])  # same folder ...
        self.assertEqual((sa["task"], sb["task"]), ("Task A", "Task B"))  # ... never mixed
        self.assertEqual([h["text"] for h in sa["happened"]], ["A: payments"])
        self.assertEqual([h["text"] for h in sb["happened"]], ["B: billing"])
        self.assertEqual([r["id"] for r in sa["runs"]], [a])
        self.assertEqual([r["id"] for r in sb["runs"]], [b])

        # A second run on the same session lists both; each keeps its own history.
        for p in (a, b):
            self.host.release(p)
        self.session("m3", "third", client="Codex", script=[{"action": "finish"}], natural=False)
        cat = self.cat()
        c = self.host.manage(cat["payments-api"]["key"], {"existing": cat["third"]["key"]}, "Task C")["team"]
        self.wait(lambda: self.bus.pair_state(c)["terminal"], timeout=30)
        sc = self.summary(c)
        self.assertEqual([(r["id"], r["current"]) for r in sc["runs"]], [(a, False), (c, True)])
        self.assertEqual(sc["happened"], [])
        self.assertEqual([h["text"] for h in self.summary(a)["happened"]], ["A: payments"])


if __name__ == "__main__":
    unittest.main()
