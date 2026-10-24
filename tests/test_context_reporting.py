"""Context-reporting rules (docs/CONTEXT-REPORTING.md §4): uncertain Codex identities,
fork sources, unknown activity, pinned manual context, reports as unverified claims
unless ancestry corroborates them, and verified identities for `relay context report`."""

import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
from unittest import mock

from util import REPO, RelayTest, env_for

from relay.adapters import codex_local
from relay.adapters.codex_local import CodexLocalAdapter, turn_activity
from relay.bus import Bus
from relay.context import ContextStore, context_key, process_ancestry, process_start_ms
from relay.host import Host
from relay.observer import Discovery
from relay.storage import Conflict, NotFound, init_root

NOW = int(time.time() * 1000)
T1, T2, T3 = ("aaaaaaaa-0000-0000-0000-00000000000%d" % i for i in (1, 2, 3))


def codex_home(tmp, threads, turns=()):
    home = os.path.join(tmp, "codex-home")
    os.makedirs(home, exist_ok=True)
    st = sqlite3.connect(os.path.join(home, "state_5.sqlite"))
    st.execute("create table if not exists threads (id text, cwd text, name text, title text, preview text, "
               "first_user_message text, updated_at_ms integer, archived integer)")
    st.execute("delete from threads")
    for i, cwd, name, upd in threads:
        st.execute("insert into threads values (?,?,?,'SECRET','SECRET','SECRET',?,0)", (i, cwd, name, upd))
    st.commit()
    th = sqlite3.connect(os.path.join(home, "thread_history_1.sqlite"))
    th.execute("create table if not exists thread_turns (thread_id text, rollout_ordinal integer, status text)")
    th.execute("delete from thread_turns")
    for n, (tid, status) in enumerate(turns):
        th.execute("insert into thread_turns values (?,?,?)", (tid, n, status))
    th.commit()
    return home


def proc(pid, cwd, thread=None, forked_from=None, started=NOW - 60000):
    return {"pid": pid, "started_ms": started, "cwd": cwd, "thread": thread, "forked_from": forked_from, "argv": []}


class CodexIdentityTests(RelayTest):
    def test_two_plain_processes_in_one_folder_stay_separate_and_unclaimed(self):
        home = codex_home(self.tmp, [(T1, "/p/agent-relay", "Newest thread", NOW - 1000)])
        found = CodexLocalAdapter({"codex_home": home, "_procs": [proc(11, "/p/agent-relay"), proc(12, "/p/agent-relay")]}).discover()
        ids = [s.client_session_id for s in found]
        self.assertEqual(len(set(ids)), 2)  # never collapsed into one
        self.assertTrue(all(i.startswith("pid-") for i in ids))
        self.assertEqual([s.extra["match"] for s in found], ["ambiguous", "ambiguous"])
        self.assertTrue(all(s.extra["title"] is None and s.activity == "unknown" for s in found))

    def test_candidate_can_change_but_process_identity_does_not(self):
        home = codex_home(self.tmp, [(T1, "/p/x", "First", NOW - 1000)])
        a = CodexLocalAdapter({"codex_home": home, "_procs": [proc(21, "/p/x")]})
        first = a.discover()[0]
        codex_home(self.tmp, [(T1, "/p/x", "First", NOW - 1000), (T2, "/p/x", "Second", NOW)])
        second = a.discover()[0]
        self.assertEqual(first.client_session_id, second.client_session_id)
        self.assertEqual((first.extra["candidate_thread"], second.extra["candidate_thread"]), (T1, T2))
        self.assertEqual(first.extra["identity"], "uncertain")

    def test_fork_names_its_source_not_itself(self):
        out = ("  31 Fri Oct  2 10:00:00 2026     /usr/local/bin/codex fork " + T1 + "\n"
               "  32 Fri Oct  2 10:00:00 2026     codex resume " + T2 + "\n")
        with mock.patch.object(codex_local.subprocess, "run", return_value=mock.Mock(stdout=out)):
            procs = codex_local.running_codex()
        self.assertEqual((procs[0]["thread"], procs[0]["forked_from"]), (None, T1))
        self.assertEqual((procs[1]["thread"], procs[1]["forked_from"]), (T2, None))
        # the source is the newest thread in the folder, yet the fork is never matched to it
        home = codex_home(self.tmp, [(T1, "/p/f", "Source", NOW)])
        s = CodexLocalAdapter({"codex_home": home, "_procs": [proc(31, "/p/f", forked_from=T1)]}).discover()[0]
        self.assertNotEqual(s.extra["match"], "exact")
        self.assertIsNone(s.extra["candidate_thread"])
        self.assertTrue(s.client_session_id.startswith("pid-31-"))
        self.assertIn("forked", " ".join(s.limitations))

    def test_unknown_turn_status_stays_unknown(self):
        self.assertEqual([turn_activity(x) for x in ("inProgress", "completed", "failed", "interrupted", "paused", None)],
                         ["busy", "idle", "idle", "idle", "unknown", "unknown"])
        home = codex_home(self.tmp, [(T3, "/p/u", "U", NOW)], turns=[(T3, "somethingNew")])
        s = CodexLocalAdapter({"codex_home": home, "_procs": [proc(41, "/p/u", thread=T3)]}).discover()[0]
        self.assertEqual((s.extra["match"], s.activity), ("exact", "unknown"))


class Base(RelayTest):
    def setUp(self):
        super().setUp()
        self.root = init_root(os.path.join(self.tmp, "console-root"), ["*"])
        self.bus = Bus(self.root)
        self.discovery = Discovery({"home": self.home}, ("fake",), interval=60)
        self.host = Host(self.bus, self.discovery, new_session_adapters={"claude": "fake", "codex": "fake"},
                         adapter_options={"home": self.home})
        self.proj = os.path.realpath(os.path.join(self.tmp, "Projects", "agent-relay"))
        os.makedirs(self.proj)
        self.store = ContextStore(self.bus)

    def tearDown(self):
        self.host.shutdown()
        super().tearDown()

    def row(self):
        self.discovery.refresh_once()
        return next(x for x in self.host.catalog()["sessions"] if x["cwd"] == self.proj)

    def report(self, key, ancestry=(), cwd=None, **kw):
        return self.store.save(key, source="report", cwd=cwd or self.proj,
                               reporter={"ancestry": list(ancestry)}, **kw)


class PrecedenceTests(Base):
    def test_manual_values_stay_pinned_until_cleared(self):
        f = self.fake("cl", client="Claude", cwd=self.proj, title="Client title")
        key = self.row()["context_key"]
        self.host.set_context(key, title="My title", summary="My summary")
        time.sleep(0.01)
        self.report(key, title="Reported title", summary="Reported summary")  # newer, still not shown first
        x = self.row()
        self.assertEqual((x["title"]["text"], x["summary"]["text"]), ("My title", "My summary"))
        alt = {(c["field"], c["text"]) for c in x["context_sources"]}
        self.assertIn(("title", "Reported title"), alt)
        self.assertIn(("summary", "Reported summary"), alt)
        self.assertIn(("title", "Client title"), alt)
        self.host.set_context(key, title="", summary="")  # clearing un-pins
        x = self.row()
        self.assertEqual((x["title"]["text"], x["summary"]["text"]), ("Reported title", "Reported summary"))

    def test_managed_goal_is_separate_from_your_summary(self):
        self.fake("cl", client="Claude", cwd=self.proj, default={"natural": True, "sleep": 30})
        x = self.row()
        self.host.set_context(x["context_key"], summary="My own note")
        self.host.manage(x["key"], {"new": {"client": "codex"}}, "Ship the context view")
        x = next(s for s in self.host.catalog()["sessions"] if s["client"] == "Claude" and s["cwd"] == self.proj)
        self.assertEqual(x["summary"]["text"], "My own note")
        self.assertEqual(x["managed_goal"]["text"], "Ship the context view")


class ReportTests(Base):
    def test_unverified_unless_ancestry_reaches_the_session(self):
        f = self.fake("cl", client="Claude", cwd=self.proj)
        key = self.row()["context_key"]
        self.report(key, summary="Implementing reporting")
        self.assertEqual(self.row()["summary"]["source"], "reported context; reporter not verified")
        time.sleep(0.01)
        with mock.patch("relay.context.REPORT_EVERY_S", 0):
            self.report(key, summary="Implementing reporting", ancestry=[{"pid": 999999, "started_ms": 1},
                                                                         {"pid": f.proc.pid, "started_ms": process_start_ms(f.proc.pid)}])
        self.assertEqual(self.row()["summary"]["source"], "the session itself")

    def test_missing_start_time_on_either_side_does_not_corroborate(self):
        item, rep = {"cwd": self.proj}, lambda started: {"cwd": self.proj, "reporter": {"ancestry": [{"pid": 77, "started_ms": started}]}}
        session = {"pid": 77, "started_ms": 1_700_000_000_000}
        self.assertEqual(Host._judge_report(item, session, rep(1_700_000_000_000)), "the session itself")
        for reporter_start in (None, 0, -5, "1700000000000", True):  # missing/invalid on the reporter's side
            self.assertEqual(Host._judge_report(item, session, rep(reporter_start)),
                             "reported context; reporter not verified", reporter_start)
        for session_start in (None, 0, "x"):  # missing/invalid on the discovered session's side
            self.assertEqual(Host._judge_report(item, dict(session, started_ms=session_start), rep(1_700_000_000_000)),
                             "reported context; reporter not verified", session_start)

    def test_concurrent_reports_only_one_is_accepted(self):
        """Two writers both read the stream before either appends: exactly one report lands."""
        self.fake("cl", client="Claude", cwd=self.proj)
        key = self.row()["context_key"]
        real_read, barrier, seen = self.bus.root.read_stream, threading.Barrier(2, timeout=10), threading.local()

        def read_then_wait(stream):
            events = real_read(stream)
            if stream.startswith("ctx-") and not getattr(seen, "done", False):
                seen.done = True
                barrier.wait()  # both writers now hold the same (empty) view
            return events
        outcomes = []

        def write(n):
            try:
                self.report(key, summary=f"Writer {n}")
                outcomes.append("ok")
            except Conflict:
                outcomes.append("conflict")
        with mock.patch.object(self.bus.root, "read_stream", side_effect=read_then_wait):
            ts = [threading.Thread(target=write, args=(n,)) for n in (1, 2)]
            [t.start() for t in ts]
            [t.join(20) for t in ts]
        self.assertEqual(sorted(outcomes), ["conflict", "ok"])
        stream = next(n for n in self.root.listdir("streams") if n.startswith("ctx-"))
        self.assertEqual(len(self.root.read_stream(stream)), 1)

    def test_pid_reuse_does_not_corroborate(self):
        chain = process_ancestry()
        self.assertEqual(chain[0]["pid"], os.getpid())
        me = {"pid": os.getpid(), "started_ms": chain[0]["started_ms"]}
        rep = {"cwd": self.proj, "reporter": {"ancestry": chain}}
        item = {"cwd": self.proj}
        self.assertEqual(Host._judge_report(item, me, rep), "the session itself")
        reused = dict(me, started_ms=me["started_ms"] + 3600_000)  # same pid, different process
        self.assertEqual(Host._judge_report(item, reused, rep), "reported context; reporter not verified")
        self.assertIsNone(Host._judge_report({"cwd": "/elsewhere"}, me, rep))  # wrong folder: not attached

    def test_unmatched_reports_stay_out_of_the_list(self):
        self.fake("cl", client="Claude", cwd=self.proj)
        key = self.row()["context_key"]
        self.report(key, summary="About another folder", cwd=self.tmp)  # folder mismatch
        self.report("claude:not-running", summary="Nobody's here")
        x = self.row()
        self.assertIsNone(x["summary"])
        self.assertNotIn("Nobody's here", json.dumps(self.host.catalog()))

    def test_rate_limit_and_no_control(self):
        self.fake("cl", client="Claude", cwd=self.proj)
        key = self.row()["context_key"]
        self.report(key, summary="One")
        with self.assertRaises(Conflict):
            self.report(key, summary="Two")
        self.assertEqual(self.bus.sessions(), [])
        self.assertEqual(self.bus.pair_ids(), [])

    def test_no_manual_context_against_a_guessed_conversation(self):
        home = codex_home(self.tmp, [(T1, self.proj, "Guess", NOW)])
        disc = Discovery({"codex_home": home, "_procs": [proc(51, self.proj)]}, ("codex-local",), interval=60)
        host = Host(self.bus, disc, read_only=False)
        disc.refresh_once()
        x = next(s for s in host.catalog()["sessions"] if s["client"] == "Codex")
        self.assertEqual((x["identity"], x["identity_note"]), ("uncertain", "likely"))
        self.assertIsNone(x["context_key"])
        for key in (None, context_key("Codex", T1), context_key("Codex", x["client_session_id"])):
            with self.assertRaises(NotFound):
                host.set_context(key, summary="Would be saved against a guess")
        self.assertEqual([n for n in self.root.listdir("streams") if n.startswith("ctx-")], [])


class ReportCommandTests(Base):
    def run_cli(self, *args, env_extra=None):
        env = env_for(self.tmp)
        env["AGENT_RELAY_ROOT"] = self.root.path
        env.update(env_extra or {})
        return subprocess.run([sys.executable, "-m", "relay", "context", "report", *args, "--json"], cwd=REPO,
                              env=env, capture_output=True, text=True, timeout=60)

    def test_codex_identity_is_verified_before_use(self):
        home = codex_home(self.tmp, [(T1, self.proj, "Mine", NOW), (T2, "/elsewhere", "Other", NOW)])
        base = ["--client", "codex", "--cwd", self.proj, "--codex-home", home, "--summary", "Reviewing"]
        r = self.run_cli(*base, env_extra={"CODEX_THREAD_ID": T1})
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertEqual(json.loads(r.stdout)["key"], f"codex:{T1}")
        r = self.run_cli(*base, env_extra={"CODEX_THREAD_ID": T2})  # another folder's thread
        self.assertEqual(r.returncode, 6)
        r = self.run_cli(*base, env_extra={"CODEX_THREAD_ID": T3})  # unknown thread
        self.assertEqual(r.returncode, 6)
        r = self.run_cli("--client", "codex", "--cwd", self.proj, "--codex-home", home, "--summary", "x",
                         env_extra={"CODEX_THREAD_ID": ""})
        self.assertEqual(r.returncode, 2)  # no identity at all

    def test_claude_identity_is_verified_and_reports_are_rate_limited(self):
        fake_home = os.path.join(self.tmp, "userhome")
        tdir = os.path.join(fake_home, ".claude", "projects", re.sub(r"[^A-Za-z0-9-]", "-", self.proj))  # Claude's observed folder encoding
        os.makedirs(tdir)
        open(os.path.join(tdir, "sess-1.jsonl"), "w").close()
        env = {"HOME": fake_home, "CLAUDE_CODE_SESSION_ID": "sess-1"}
        r = self.run_cli("--client", "claude", "--cwd", self.proj, "--title", "Dashboard work", env_extra=env)
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        r = self.run_cli("--client", "claude", "--cwd", self.proj, "--title", "Again", env_extra=env)
        self.assertEqual(r.returncode, 4)  # too soon
        r = self.run_cli("--client", "claude", "--cwd", self.proj, "--title", "x",
                         env_extra=dict(env, CLAUDE_CODE_SESSION_ID="sess-unknown"))
        self.assertEqual(r.returncode, 6)
        stored = self.store.get("claude:sess-1")["report"]
        self.assertEqual(stored["title"]["text"], "Dashboard work")
        self.assertEqual(stored["title"]["cwd"], self.proj)
        self.assertTrue(stored["title"]["reporter"]["ancestry"])  # recorded for corroboration
        self.assertEqual(self.bus.sessions(), [])  # reporting registered nothing


class ConfirmationTests(Base):
    """A corroborated report confirms a likely Codex conversation."""

    def catalog_for(self, home, procs):
        disc = Discovery({"codex_home": home, "_procs": procs}, ("codex-local",), interval=60)
        disc.refresh_once()
        return next(s for s in Host(self.bus, disc, read_only=True).catalog()["sessions"] if s["client"] == "Codex")

    def test_corroborated_report_confirms_a_likely_conversation(self):
        home = codex_home(self.tmp, [(T1, self.proj, "Build context", NOW)])
        p = proc(61, self.proj, started=NOW - 60000)
        self.report(context_key("Codex", T1), summary="Architecture and review of session management",
                    ancestry=[{"pid": 5, "started_ms": 1}, {"pid": 61, "started_ms": NOW - 60000}])
        x = self.catalog_for(home, [p])
        self.assertEqual((x["identity"], x["identity_note"], x["client_session_id"]),
                         ("exact", "confirmed by its own report", T1))
        self.assertEqual(x["summary"]["source"], "the session itself")
        self.assertEqual(x["context_key"], context_key("Codex", T1))

    def test_uncorroborated_report_does_not_confirm(self):
        home = codex_home(self.tmp, [(T1, self.proj, "Build context", NOW)])
        self.report(context_key("Codex", T1), summary="Claimed", ancestry=[{"pid": 62, "started_ms": NOW - 999999}])
        x = self.catalog_for(home, [proc(62, self.proj, started=NOW - 60000)])  # same pid, other process
        self.assertEqual((x["identity"], x["context_key"], x["summary"]), ("uncertain", None, None))
