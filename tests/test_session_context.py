"""Session context: folder, client, title and work summary — sources, precedence,
staleness, persistence, no control granted — and read-only real-client discovery
(codex-local, Claude titles) without reading prompts or transcripts."""

import json
import os
import sqlite3
import time
from unittest import mock

from util import RelayTest

from relay.adapters.claude import latest_ai_title
from relay.adapters.codex_local import CodexLocalAdapter
from relay.bus import Bus
from relay.host import Host
from relay.observer import Discovery
from relay.storage import Conflict, InvalidInput, NotFound, init_root


class Base(RelayTest):
    def setUp(self):
        super().setUp()
        self.root = init_root(os.path.join(self.tmp, "console-root"), ["*"])
        self.bus = Bus(self.root)
        self.discovery = Discovery({"home": self.home}, ("fake",), interval=60)
        self.host = self.new_host()
        self.proj = os.path.join(self.tmp, "Projects", "agent-relay")
        os.makedirs(self.proj)

    def new_host(self, **kw):
        return Host(self.bus, self.discovery, new_session_adapters={"claude": "fake", "codex": "fake"},
                    adapter_options={"home": self.home}, **kw)

    def tearDown(self):
        self.host.shutdown()
        super().tearDown()

    def cat(self, host=None):
        self.discovery.refresh_once()
        return (host or self.host).catalog()["sessions"]

    def by_client(self, client, host=None):
        return next(x for x in self.cat(host) if x["client"] == client and x["cwd"] == self.proj)


class SameFolderTests(Base):
    def test_two_sessions_in_one_folder_show_different_work(self):
        self.fake("cx", client="Codex", cwd=self.proj, title="Session manager review",
                  summary="Architecture and review of session management")
        self.fake("cl", client="Claude", cwd=self.proj, title="Dashboard work")
        claude = self.by_client("Claude")
        self.host.set_context(claude["context_key"], summary="Implementing the dashboard and control refinements")
        codex, claude = self.by_client("Codex"), self.by_client("Claude")
        # distinguishable by client, then by their own work
        self.assertEqual((codex["name"], claude["name"]), ("agent-relay · Codex", "agent-relay · Claude"))
        self.assertEqual(codex["folder"], self.proj)
        self.assertEqual(codex["summary"]["text"], "Architecture and review of session management")
        self.assertEqual(codex["summary"]["source"], "client metadata (demo)")
        self.assertEqual(claude["summary"]["text"], "Implementing the dashboard and control refinements")
        self.assertEqual(claude["summary"]["source"], "you")
        self.assertTrue(claude["summary"]["at"])
        self.assertEqual(claude["title"]["text"], "Dashboard work")  # client title kept alongside

    def test_missing_context_is_reported_as_unavailable(self):
        self.fake("bare", client="Claude", cwd=self.proj)
        x = self.by_client("Claude")
        self.assertIsNone(x["title"])
        self.assertIsNone(x["summary"])  # the page says "Work summary unavailable"
        self.assertEqual(x["context_sources"], [])

    def test_same_folder_same_client_still_distinct(self):
        self.fake("one", client="Claude", cwd=self.proj)
        self.fake("two", client="Claude", cwd=self.proj)
        names = sorted(x["name"] for x in self.cat() if x["cwd"] == self.proj)
        self.assertEqual(len(set(names)), 2)
        self.assertTrue(all(n.startswith("agent-relay · Claude · ") for n in names))


class StoreTests(Base):
    def test_context_persists_across_restarts_and_keeps_history(self):
        self.fake("cl", client="Claude", cwd=self.proj)
        key = self.by_client("Claude")["context_key"]
        self.host.set_context(key, title="First title", summary="First summary")
        self.host.set_context(key, summary="Second summary")
        again = self.new_host()  # a fresh console process on the same relay root
        x = self.by_client("Claude", again)
        self.assertEqual((x["title"]["text"], x["summary"]["text"]), ("First title", "Second summary"))
        stream = [n for n in self.root.listdir("streams") if n.startswith("ctx-")]
        self.assertEqual(len(stream), 1)
        self.assertEqual(len(self.root.read_stream(stream[0])), 2)  # append-only history
        self.host.set_context(key, summary="")  # an empty value clears your summary
        self.assertIsNone(self.by_client("Claude")["summary"])

    def test_context_grants_no_control(self):
        self.fake("cl", client="Claude", cwd=self.proj)
        x = self.by_client("Claude")
        self.host.set_context(x["context_key"], title="Mine now?", summary="No.")
        self.assertEqual(self.bus.sessions(), [])  # nothing registered
        self.assertEqual(self.bus.pair_ids(), [])  # nothing paired
        self.assertEqual(self.by_client("Claude")["managed_by"], {"kind": "none"})
        with self.assertRaises(NotFound):
            self.host.set_context("claude:not-listed", summary="x")
        with self.assertRaises(InvalidInput):
            self.host.set_context(x["context_key"], title="t" * 121)
        with self.assertRaises(InvalidInput):
            self.host.set_context(x["context_key"], summary="s" * 401)
        ro = self.new_host(read_only=True)
        with self.assertRaises(Conflict):
            ro.set_context(x["context_key"], summary="x")

    def test_old_context_is_labelled_stale(self):
        self.fake("cl", client="Claude", cwd=self.proj)
        key = self.by_client("Claude")["context_key"]
        self.host.set_context(key, summary="Yesterday's job")
        later = time.time() + 7 * 3600
        with mock.patch("relay.host.time.time", return_value=later):
            x = self.by_client("Claude")
        self.assertTrue(x["summary"]["stale"])
        self.assertFalse(self.by_client("Claude")["summary"]["stale"])

    def test_relay_recorded_work_comes_first_but_nothing_is_hidden(self):
        self.fake("cl", client="Claude", cwd=self.proj, default={"natural": True, "sleep": 30})
        x = self.by_client("Claude")
        self.host.set_context(x["context_key"], summary="My own note")
        self.host.manage(x["key"], {"new": {"client": "codex"}}, "Ship the context view")
        x = next(s for s in self.cat() if s["client"] == "Claude" and s["cwd"] == self.proj)
        self.assertEqual(x["summary"]["text"], "My own note")  # yours stays pinned
        self.assertEqual(x["managed_goal"]["text"], "Ship the context view")  # shown separately, not hidden
        self.assertEqual(x["managed_goal"]["source"], "the relay (the goal it is managing)")


class CodexLocalTests(RelayTest):
    def make_codex_home(self):
        home = os.path.join(self.tmp, "codex-home")
        os.makedirs(home)
        st = sqlite3.connect(os.path.join(home, "state_5.sqlite"))
        st.execute("create table threads (id text, cwd text, name text, title text, preview text, "
                   "first_user_message text, updated_at_ms integer, archived integer)")
        now = int(time.time() * 1000)
        rows = [("aaaaaaaa-0000-0000-0000-000000000001", "/p/agent-relay", "Session manager review", now - 1000),
                ("aaaaaaaa-0000-0000-0000-000000000002", "/p/agent-relay", None, now - 10 ** 9),
                ("aaaaaaaa-0000-0000-0000-000000000003", "/p/other", "Exact one", now - 5000)]
        for i, cwd, name, upd in rows:
            st.execute("insert into threads values (?,?,?,?,?,?,?,0)",
                       (i, cwd, name, "SECRET PROMPT title", "SECRET PROMPT preview", "SECRET PROMPT", upd))
        st.commit()
        th = sqlite3.connect(os.path.join(home, "thread_history_1.sqlite"))
        th.execute("create table thread_turns (thread_id text, rollout_ordinal integer, status text)")
        th.execute("insert into thread_turns values (?,?,?)", (rows[0][0], 1, "completed"))
        th.execute("insert into thread_turns values (?,?,?)", (rows[0][0], 2, "inProgress"))
        th.execute("insert into thread_turns values (?,?,?)", (rows[2][0], 1, "completed"))
        th.commit()
        return home, now

    def test_matches_threads_read_only_without_reading_prompts(self):
        home, now = self.make_codex_home()
        procs = [{"pid": 11, "started_ms": now - 60000, "thread": None, "cwd": "/p/agent-relay", "argv": ["codex", "resume"]},
                 {"pid": 12, "started_ms": now - 60000, "thread": "aaaaaaaa-0000-0000-0000-000000000003", "cwd": "/p", "argv": []},
                 {"pid": 13, "started_ms": now, "thread": None, "cwd": "/p/new", "argv": ["codex"]}]
        before = os.path.getmtime(os.path.join(home, "state_5.sqlite"))
        found = CodexLocalAdapter({"codex_home": home, "_procs": procs}).discover()
        by = {s.extra["pid"]: s for s in found}
        self.assertEqual((by[11].extra["match"], by[11].extra["title"], by[11].activity), ("likely", "Session manager review", "busy"))
        self.assertIn("likely match", by[11].extra["title_source"])
        self.assertEqual((by[12].extra["match"], by[12].cwd, by[12].activity), ("exact", "/p/other", "idle"))
        self.assertEqual((by[13].extra["match"], by[13].client_session_id, by[13].activity), ("none", f"pid-13-{now}", "unknown"))  # pid + start time (§4)
        self.assertFalse(any(s.attachable for s in found))  # observe only
        self.assertNotIn("SECRET", json.dumps([vars(s) for s in found]))  # prompts never surface
        self.assertEqual(os.path.getmtime(os.path.join(home, "state_5.sqlite")), before)  # read-only

    def test_nothing_running_or_no_codex_home(self):
        self.assertEqual(CodexLocalAdapter({"codex_home": "/nonexistent", "_procs": []}).discover(), [])
        s = CodexLocalAdapter({"codex_home": "/nonexistent", "_procs": [{"pid": 9, "started_ms": 0, "thread": None,
                                                                          "cwd": "/x", "argv": ["codex"]}]}).discover()
        self.assertEqual((s[0].client_session_id, s[0].extra["match"], s[0].activity), ("pid-9-0", "none", "unknown"))  # pid + start time (§4)


class ClaudeTitleTests(RelayTest):
    def test_reads_only_the_latest_ai_title(self):
        path = os.path.join(self.tmp, "t.jsonl")
        with open(path, "w") as f:
            f.write(json.dumps({"type": "ai-title", "aiTitle": "Old title", "sessionId": "s"}) + "\n")
            f.write(json.dumps({"type": "user", "message": {"content": "SECRET ai-title in a prompt"}}) + "\n")
            f.write(json.dumps({"type": "ai-title", "aiTitle": "Current title", "sessionId": "s"}) + "\n")
            f.write(json.dumps({"type": "assistant", "message": {"content": "SECRET reply"}}) + "\n")
        self.assertEqual(latest_ai_title(path), "Current title")
        self.assertIsNone(latest_ai_title(os.path.join(self.tmp, "missing.jsonl")))
