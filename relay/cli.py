"""`relay` command line: scriptable commands and the foreground console.

Exit codes: 0 ok, 1 internal error, 2 usage, 3 not found, 4 conflict, 5 blocked
(paused/stopped/budget), 6 invalid input, 7 timeout, 8 untrusted history,
9 unsupported by adapter.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid

from . import __version__
from . import protocol as P
from .adapters import ADAPTERS, get_adapter
from .bus import Bus, summarize_pair
from .controller import STOP_STATUSES, Controller, abandon_unknown, answer, reconcile

# `pair run` stays in the foreground through recoverable waits (so launched
# children keep their context) and exits only at these, unless asked otherwise.
FINAL_STATUSES = frozenset({"finished", "stopped", "untrusted"})
from .storage import Conflict, InvalidInput, RelayError, Root, Unsupported, UsageError, actor, init_root


# ------------------------------------------------------------------ helpers
def read_text(path: str | None, what: str = "body") -> str:
    if path is None:
        raise UsageError(f"--{what}-file is required (use - for stdin)")
    limit = P.MAX_BODY + 1
    if path == "-":
        data = sys.stdin.buffer.read(limit)
    else:
        with open(path, "rb") as f:
            data = f.read(limit)
    if len(data) >= limit:
        raise InvalidInput(f"{what} exceeds {P.MAX_BODY} bytes; not truncated")
    try:
        return data.decode()
    except UnicodeDecodeError:
        raise InvalidInput(f"{what} must be UTF-8 text")


def get_root(a) -> Root:
    return Root(a.root or os.environ.get("AGENT_RELAY_ROOT", ""))


def adapter_options(a) -> dict:
    opts = {}
    if getattr(a, "fake_home", None):
        opts["home"] = os.path.realpath(a.fake_home)
    return opts


class Out:
    def __init__(self, as_json: bool):
        self.json = as_json

    def __call__(self, obj, human=None):
        if self.json:
            print(json.dumps(obj, indent=2, sort_keys=True, default=str))
        elif human is not None:
            print(human if isinstance(human, str) else human(obj))
        else:
            print(json.dumps(obj, indent=2, sort_keys=True, default=str))


def fmt_pair(s: dict) -> str:
    if s.get("status") == "incomplete":
        return f"pair {s['pair']}: incomplete creation"
    flags = ("  [PAUSED]" if s["paused"] else "") + ("  [GLOBAL PAUSE]" if s.get("global_paused") else "")
    lines = [f"pair {s['pair']}  status={s['status'].upper()}" + flags,
             f"  leader   {s['leader']['participant']:<8} endpoint {s['leader']['endpoint']}",
             f"  follower {s['follower']['participant']:<8} endpoint {s['follower']['endpoint']}",
             f"  turns {s['turns_used']}/{s['max_turns']}  timeout {s['turn_timeout_s']}s  "
             f"thread {s['thread_id']}"]
    if s["inflight"]:
        i = s["inflight"]
        delivery = {"authorized": "authorized, not yet submitted", "submitting":
                    "submitted, delivery not yet confirmed", "accepted": "delivery confirmed by client"}
        lines.append(f"  in flight: turn {i['turn']} -> {i['role']} ({delivery.get(i['phase'], i['phase'])})")
        if i.get("waiting"):
            lines.append(f"  waiting: {i['waiting']}")
    elif s["queued"]:
        lines.append(f"  queued: {s['queued']['message']} for the {s['queued']['role']}")
    if s["attention"]:
        lines.append(f"  NEEDS HUMAN ({s['attention']['status']}): {s['attention']['reason']}")
    if s["owner"]:
        lines.append(f"  controller {s['owner']['ctl']} pid {s['owner'].get('pid')}")
    if s["late_responses"]:
        lines.append(f"  late responses: {', '.join(x['message'] for x in s['late_responses'])}")
    return "\n".join(lines)


def fmt_status(st: dict) -> str:
    c = st["control"]
    out = [f"relay root {st['root']}", f"participants: {', '.join(st['participants'])}",
           "global control: " + ("PAUSED — " + str(c.get("reason")) if c.get("paused") else "running"),
           "manual messages: " + (", ".join(f"{k}={v}" for k, v in sorted(st["messages"].items()))
                                  or "none")]
    for claim in st["open_claims"]:
        out.append(f"  open claim {claim['message']} by {claim['by']} ({claim['session']}) since "
                   f"{claim['since']} — `relay recover` if the claimant is gone")
    out.append("sessions:" if st["sessions"] else "sessions: none")
    for s in st["sessions"]:
        state = s["state"]
        ident = s.get("verified_client_session_id") or \
            f"{s['client_session_id']} (not confirmed by the client)"
        out.append(f"  {s['id']} {s['participant']} {s['adapter']}/{s.get('mode')} client "
                   f"{ident} [{state['state']}{' pair ' + state['pair'] if state.get('pair') else ''}]")
    for p in st["pairs"]:
        out.append(fmt_pair(p) if "leader" in p or p.get("status") == "incomplete"
                   else f"pair {p['pair']}: {p['status']} {p.get('error', '')}")
    for e in st["errors"]:
        out.append(f"ERROR {e}")
    return "\n".join(out)


def fmt_thread(t: dict) -> str:
    out = [f"thread {t['thread_id']} ({len(t['messages'])} messages)"]
    for m in t["messages"]:
        ind = "  " * m["depth"]
        out.append(f"{ind}- {m['id']} {m['from']} -> {m['to']} [{m['kind']}] {m['subject']}"
                   + (f" (turn {m['turn_id']})" if m.get("turn_id") else "")
                   + ("" if m["reply_to_resolved"] else f" (reply_to {m['reply_to']} UNRESOLVED)"))
        for line in m["body"].splitlines()[:12]:
            out.append(f"{ind}    | {line}")
        if len(m["body"].splitlines()) > 12:
            out.append(f"{ind}    | … (`relay read {m['id']}` for the full body)")
        for r in m["refs"]:
            flag = {True: "resolved", False: "UNRESOLVED", None: "inert, not fetched"}[r["resolved"]]
            out.append(f"{ind}    ref {r['ref']} ({flag})")
    for e in t["errors"]:
        out.append(f"ERROR {e}")
    return "\n".join(out)


def pair_preview(bus: Bus, pair_id: str) -> str:
    d = bus.get_pair_def(pair_id)
    lines = [f"Pair {pair_id}"]
    for role in ("leader", "follower"):
        s = bus.get_session(d[role]["endpoint"])
        ident = s["verified_client_session_id"] or \
            f"{s['client_session_id']} (requested; not yet confirmed by the client)"
        cwd = s["verified_cwd"] or f"{s['cwd']} (registered; not yet confirmed)"
        lines.append(f"  {role:<8} {s['participant']:<7} {s['adapter']}/{s['mode']} "
                     f"session {ident}  cwd {cwd}")
    lines += [f"  scope: {d['scope'] or '(none)'}",
              f"  limits: {d['max_turns']} automatic turns, {d['turn_timeout_s']}s per turn",
              "  Start authorizes routing within this scope and these limits only. It approves",
              "  nothing an agent proposes; native approval prompts stay in each client."]
    return "\n".join(lines)


# ----------------------------------------------------------------- commands
def cmd_init(a, out):
    root = init_root(a.root or os.environ.get("AGENT_RELAY_ROOT", ""), a.participants,
                     max_turns=a.max_turns, turn_timeout_s=a.turn_timeout)
    out({"root": root.path, "participants": a.participants}, f"initialized relay root {root.path}")


def cmd_send(a, out):
    bus = Bus(get_root(a))
    env, created, warns = bus.send(
        sender=a.sender, to=a.to, kind=a.kind, subject=a.subject, body=read_text(a.body_file),
        thread_id=a.thread, reply_to=a.reply_to, scope=a.scope or "",
        approval_refs=a.approval_ref or [], evidence_refs=a.evidence_ref or [],
        idempotency_key=a.idempotency_key)
    for w in warns:
        print(f"warning: message looks like it may contain a {w}; this check is not a guarantee",
              file=sys.stderr)
    out({"id": env["id"], "thread_id": env["thread_id"], "created": created, "warnings": warns},
        f"{'sent' if created else 'already sent (idempotent)'} {env['id']} thread {env['thread_id']}"
        " — written to the relay; not proof the recipient read or acted on it")


def cmd_inbox(a, out):
    res = Bus(get_root(a)).inbox(a.participant, a.unread)
    out(res, lambda r: "\n".join(
        [f"{m['id']} [{m['state']}] {m['from']} {m['kind']}: {m['subject']}" for m in r["messages"]]
        + [f"ERROR {e}" for e in r["errors"]]) or "no messages")


def cmd_read(a, out):
    m = Bus(get_root(a)).get_message(a.message_id)
    out(m, lambda m: "\n".join([f"{k}: {m[k]}" for k in ("id", "thread_id", "reply_to", "pair_id",
                                                          "turn_id", "from", "to", "kind",
                                                          "subject", "created_at", "scope")]
                               + [f"approval_refs: {m['approval_refs']}",
                                  f"evidence_refs: {m['evidence_refs']}", "", m["body"]]))


def cmd_claim(a, out):
    r = Bus(get_root(a)).claim(a.message_id, a.as_, a.session)
    out(r, f"claimed {r['message']} token {r['token']}")


def cmd_ack(a, out):
    out(Bus(get_root(a)).ack(a.message_id, a.claim), f"acknowledged {a.message_id} (received)")


def cmd_reply(a, out):
    env, created, warns = Bus(get_root(a)).reply(
        a.message_id, a.sender, a.kind, read_text(a.body_file), a.subject,
        evidence_refs=a.evidence_ref or [], approval_refs=a.approval_ref or [],
        idempotency_key=a.idempotency_key)
    for w in warns:
        print(f"warning: possible {w}; not a guarantee", file=sys.stderr)
    out({"id": env["id"], "created": created, "warnings": warns}, f"replied {env['id']}")


def cmd_complete(a, out):
    out(Bus(get_root(a)).complete(a.message_id, a.claim, a.result), f"completed {a.message_id}")


def cmd_fail(a, out):
    out(Bus(get_root(a)).fail(a.message_id, a.claim, a.reason), f"failed {a.message_id}")


def cmd_release(a, out):
    out(Bus(get_root(a)).release(a.message_id, a.claim, a.reason), f"released {a.message_id}")


def cmd_recover(a, out):
    out(Bus(get_root(a)).recover(a.message_id, a.reason),
        f"recovered {a.message_id}: claim abandoned; the previous claimant may already have acted")


def cmd_pause(a, out):
    r = Bus(get_root(a)).pause(a.reason)
    out(r, "globally paused: no new dispatch or claim is authorized; work already running "
           "continues and is recorded")


def cmd_resume(a, out):
    r = Bus(get_root(a)).resume()
    out(r, "global pause lifted; " + r["note"])


def cmd_status(a, out):
    out(Bus(get_root(a)).status(), fmt_status)


def cmd_thread(a, out):
    out(Bus(get_root(a)).thread(a.thread_id), fmt_thread)


def cmd_wait(a, out):
    r = Bus(get_root(a)).wait(a.participant, a.timeout)
    out(r, lambda r: r["reason"] + "".join(f"\n{m['id']} {m['subject']}" for m in r["messages"]))


def cmd_rebuild(a, out):
    out(Bus(get_root(a)).rebuild_views())


def cmd_sessions_list(a, out):
    names = [a.adapter] if a.adapter else ADAPTERS
    res = {"discovered": [], "registered": Bus(get_root(a)).sessions()}
    for name in names:
        try:
            ad = get_adapter(name, adapter_options(a))
            for s in ad.discover():
                res["discovered"].append(dict(vars(s), adapter=name, capabilities=ad.capabilities(),
                                              verified_adapter=ad.verified))
        except RelayError as e:
            res["discovered"].append({"adapter": name, "error": str(e)})

    def human(r):
        lines = ["discovered sessions (a visible session is not proof it can be attached):"]
        for s in r["discovered"]:
            if "error" in s:
                lines.append(f"  [{s['adapter']}] {s['error']}")
                continue
            caps = s["capabilities"]
            lines.append(f"  [{s['adapter']}] {s['client_session_id']} cwd={s['cwd']} "
                         f"activity={s['activity']} attachable={s['attachable']} "
                         f"submit={caps['submit']} turn_complete={caps['turn_complete']}")
            for lim in s["limitations"]:
                lines.append(f"      limitation: {lim}")
        lines.append("registered endpoints:")
        for s in r["registered"]:
            lines.append(f"  {s['id']} {s['participant']} {s['adapter']}/{s.get('mode')} "
                         f"{s['client_session_id']} [{s['state']['state']}]")
        return "\n".join(lines)

    out(res, human)


def cmd_sessions_attach(a, out):
    bus = Bus(get_root(a))
    ad = get_adapter(a.adapter, adapter_options(a))
    caps = ad.capabilities()
    if not caps["attach_live"]:
        raise Unsupported(f"adapter {a.adapter} cannot attach to a live session "
                         "(see docs/INTEGRATION-FEASIBILITY.md); refusing to substitute resume or launch")
    found = {s.client_session_id: s for s in ad.discover()} if caps["discover"] else {}
    info = found.get(a.session)
    if caps["discover"] and info is None and not a.unlisted:
        raise RelayError(f"session {a.session} was not discovered by {a.adapter}; pass --unlisted "
                         "to register it explicitly (it will be verified when the pair starts)")
    rec, created = bus.register_session(
        adapter=a.adapter, client_session_id=a.session, mode="attach", participant=a.as_,
        cwd=info.cwd if info else a.cwd, options=adapter_options(a),
        info=dict(info.extra, activity=info.activity) if info else {}, capabilities=caps)
    out(rec, f"{'registered' if created else 'already registered'} endpoint {rec['id']} for "
             f"{a.as_} -> {a.adapter} session {a.session} (attach; the relay never closes it)")


def cmd_sessions_launch(a, out):
    bus = Bus(get_root(a))
    ad = get_adapter(a.adapter, adapter_options(a))
    caps = ad.capabilities()
    if not caps["launch"]:
        raise Unsupported(f"adapter {a.adapter} cannot launch sessions")
    cwd = os.path.realpath(a.cwd)
    if not os.path.isdir(cwd):
        raise InvalidInput(f"cwd does not exist: {cwd}")
    sid = a.session_id or (str(uuid.uuid4()) if a.adapter in ("fake", "claude-stream")
                           else f"pending-{uuid.uuid4()}")
    opts = adapter_options(a)
    if a.launch_options:
        opts["launch"] = json.loads(read_text(a.launch_options, "launch-options"))
    rec, _ = bus.register_session(adapter=a.adapter, client_session_id=sid, mode="launch",
                                  participant=a.as_, cwd=cwd, options=opts, capabilities=caps)
    out(rec, f"registered launch endpoint {rec['id']} for {a.as_} ({a.adapter}, cwd {cwd}); the "
             "session starts when a controller runs the pair and closes when it exits")


def cmd_sessions_release(a, out):
    bus = Bus(get_root(a))
    s = bus.get_session(a.endpoint)
    pair = s["state"]["pair"]
    if pair:
        st = bus.pair_state(pair)
        if not st.get("terminal") and st.get("status") != "incomplete":
            raise RelayError(f"endpoint reserved by active pair {pair}; stop or detach it first")
    bus.session_event(a.endpoint, "released", {"pair": pair}, actor("human"))
    out({"endpoint": a.endpoint, "released_from": pair}, f"released {a.endpoint}")


def cmd_pair_create(a, out):
    bus = Bus(get_root(a))
    scope = read_text(a.scope_file, "scope") if a.scope_file else (a.scope or "")
    d = bus.create_pair(leader=a.leader, follower=a.follower, scope=scope, subject=a.subject or "",
                        max_turns=a.max_turns, turn_timeout_s=a.turn_timeout)
    out(d, pair_preview(bus, d["id"]))


def confirm(bus, pair_id, yes: bool) -> None:
    print(pair_preview(bus, pair_id), file=sys.stderr)
    if yes:
        return
    if not sys.stdin.isatty():
        raise UsageError("confirmation required: re-run with --yes after reviewing the pair above")
    if input("Start routing? [y/N] ").strip().lower() != "y":
        raise UsageError("not started")


def exit_statuses(a) -> frozenset:
    s = STOP_STATUSES if getattr(a, "exit_on_attention", False) else FINAL_STATUSES
    return s | ({"paused"} if getattr(a, "exit_on_pause", False) else set())


def run_controller(bus: Bus, pair_id: str, a, *, exit_on=None) -> str:
    log = lambda s: print(s, file=sys.stderr, flush=True)  # noqa: E731
    ctl = Controller(bus, pair_id, log=log)
    ctl.acquire(takeover=getattr(a, "takeover", False), force=getattr(a, "force", False))
    last = [None]

    def on_tick(st) -> bool:
        att = st.get("attention")
        key = (st["status"], att and att.get("reason"))
        if key != last[0]:
            last[0] = key
            if att or st["status"] == "paused":
                log(f"waiting for the human ({st['status']}): "
                    f"{att['reason'] if att else 'paused'} — resolve from another terminal with "
                    "`relay pair answer|reconcile|resume`; the controller keeps observing. "
                    "Ctrl-C quits (closes console-launched sessions).")
        return True

    try:
        ctl.connect_all()
        return ctl.run(exit_on=exit_on or exit_statuses(a), max_seconds=getattr(a, "max_seconds", None),
                       on_tick=on_tick)
    except KeyboardInterrupt:
        log("interrupted: controller exiting; client work already running is not cancelled")
        return bus.pair_state(pair_id)["status"]
    finally:
        launched = [e for e, c in ctl.connections.items() if c.mode == "launch"]
        ctl.shutdown()
        if launched:
            log(f"closed console-launched session(s) {', '.join(launched)}; they cannot be "
                "reconnected. Attached sessions were only detached.")


def cmd_pair_start(a, out):
    bus = Bus(get_root(a))
    confirm(bus, a.pair_id, a.yes)
    if a.message:
        r = bus.start_pair(a.pair_id, message_id=a.message)
    elif a.leader_request_file:
        r = bus.start_pair(a.pair_id, body=read_text(a.leader_request_file, "leader-request"),
                           subject=a.subject, to_leader=True)
    else:
        r = bus.start_pair(a.pair_id, body=read_text(a.body_file), subject=a.subject)
    if not a.no_run:
        r["final_status"] = run_controller(bus, a.pair_id, a)
    out(dict(r, pair=summarize_pair(bus.pair_state(a.pair_id))),
        lambda r: f"started {r['pair']['pair']} with {r['initial_message']} ({r['mode']})\n"
                  + fmt_pair(r["pair"]))


def cmd_pair_run(a, out):
    bus = Bus(get_root(a))
    status = run_controller(bus, a.pair_id, a)
    out({"status": status, "pair": summarize_pair(bus.pair_state(a.pair_id))},
        lambda r: fmt_pair(r["pair"]))


def _pair_human(a, out, etype, data, msg):
    bus = Bus(get_root(a))
    bus.pair_event(a.pair_id, etype, data, actor("human"))
    out(summarize_pair(bus.pair_state(a.pair_id)), lambda s: msg + "\n" + fmt_pair(s))


def cmd_pair_pause(a, out):
    _pair_human(a, out, "paused", {"reason": a.reason}, "paused: no new turn will be dispatched; "
                "a turn already in flight keeps running and its result is recorded")


def cmd_pair_resume(a, out):
    _pair_human(a, out, "resumed", {}, "resumed (budget is not reset)")


def cmd_pair_stop(a, out):
    _pair_human(a, out, "stopped", {"reason": a.reason}, "stopped routing; running client work is "
                "not cancelled")


def cmd_pair_extend(a, out):
    _pair_human(a, out, "budget_extended", {"add": a.add}, f"budget extended by {a.add}")


def cmd_pair_reconcile(a, out):
    bus = Bus(get_root(a))
    r = reconcile(bus, a.pair_id, a.outcome, message=a.message, reason=a.reason)
    out(dict(r, pair=summarize_pair(bus.pair_state(a.pair_id))),
        lambda r: f"reconciled: {a.outcome}\n" + fmt_pair(r["pair"]))


def cmd_pair_answer(a, out):
    bus = Bus(get_root(a))
    r = answer(bus, a.pair_id, read_text(a.body_file), to_role=a.to, subject=a.subject)
    out(dict(r, pair=summarize_pair(bus.pair_state(a.pair_id))),
        lambda r: f"answer {r['message']} queued for the {r['to']} (budget unchanged)\n"
                  + fmt_pair(r["pair"]))


def cmd_pair_abandon_unknown(a, out):
    if not a.confirm:
        raise UsageError(f"this records: \"{P.ABANDON_STATEMENT}\" Re-run with --confirm to record it")
    bus = Bus(get_root(a))
    r = abandon_unknown(bus, a.pair_id)
    out(r, lambda r: f"recorded for turn(s) {', '.join(r['abandoned'])}: {r['statement']}\n"
                     "Give the conversations back from the console (Back to terminal / Resolve).")


def cmd_pair_detach(a, out):
    from .handoff import CLAIMS_LOCK, team_holds
    bus = Bus(get_root(a))
    # Preflight everything before changing anything; held against a concurrent hand-over.
    with bus.root.lock(CLAIMS_LOCK):
        st = bus.pair_state(a.pair_id)
        if st.get("owner"):
            raise RelayError(f"controller {st['owner']['ctl']} is running this pair; quit it first")
        holds = team_holds(bus, a.pair_id, ending=True)  # detach ends the team
        if holds:
            raise Conflict("not detached; claims kept while the relay may still hold these "
                           "conversations:\n  - " + "\n  - ".join(holds) + "\nIn the console: cancel the "
                           "hand-off, Back to terminal, or Resolve; then detach again.")
        _detach_team(bus, a, st, out)


def _detach_team(bus, a, st, out):
    """Stop the team and release its claims. The caller has preflighted, under the lock."""
    who = actor("human")
    if not st.get("terminal"):
        bus.pair_event(a.pair_id, "stopped", {"reason": "detached"}, who)
    d = st["definition"]
    for role in ("leader", "follower"):
        eid = d[role]["endpoint"]
        bus.pair_event(a.pair_id, "detached", {"endpoint": eid}, who)
        if bus.get_session(eid)["state"]["state"] != "closed":
            bus.session_event(eid, "released", {"pair": a.pair_id}, who)
    out(summarize_pair(bus.pair_state(a.pair_id)),
        lambda s: "detached: reservations released; attached sessions keep running untouched\n"
                  + fmt_pair(s))


def cmd_pair_show(a, out):
    out(summarize_pair(Bus(get_root(a)).pair_state(a.pair_id)), fmt_pair)


# ------------------------------------------------------------------ context
def cmd_context_report(a, out):
    """A session describes its own work. Descriptive only: grants no control."""
    from .context import ContextStore, context_key, process_ancestry, verify_identity
    bus = Bus(get_root(a))
    env = {"claude": "CLAUDE_CODE_SESSION_ID", "codex": "CODEX_THREAD_ID"}[a.client]
    session = a.session or os.environ.get(env)
    if not session:
        raise UsageError(f"no session id: pass --session or run this inside the session ({env})")
    cwd = os.path.realpath(a.cwd or os.getcwd())
    verify_identity(a.client, session, cwd, codex_home=a.codex_home)
    key = context_key(a.client, session)
    ContextStore(bus).save(key, title=a.title, summary=a.summary, source="report", cwd=cwd,
                           reporter={"ancestry": process_ancestry(), "host": os.uname().nodename},
                           by=actor("reporter", a.client))
    out({"key": key, "cwd": cwd, "reported": True},
        f"reported context for {a.client} {session[:8]}… in {cwd}. It only describes the session; "
        "it gives the relay no control.")


# ------------------------------------------------------------------ serve
def cmd_serve(a, out):
    from .host import Host
    from .observer import Discovery
    from .server import serve
    bus = Bus(get_root(a))
    discovery = None
    if not a.no_discovery:
        names = tuple(a.adapter or [n for n in ADAPTERS if n != "fake" or adapter_options(a).get("home")
                                    or os.environ.get("AGENT_RELAY_FAKE_HOME")])
        discovery = Discovery(adapter_options(a), names, interval=a.discovery_interval)
    new = {"claude": "fake", "codex": "fake"} if a.new_sessions == "fake" else None
    host = Host(bus, discovery, new_session_adapters=new, adapter_options=adapter_options(a),
                read_only=a.read_only)
    if not a.read_only:
        host.start_notifier()  # does nothing until you configure and verify a channel
    serve(bus, port=a.port, discovery=discovery, host=host)


# ------------------------------------------------------------------ console
def cmd_console(a, out):
    from .console import Console
    Console(Bus(get_root(a)), adapter_options(a)).loop()


# ------------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--root", help="relay root (or AGENT_RELAY_ROOT)")
    common.add_argument("--json", action="store_true", help="machine-readable output")
    common.add_argument("--fake-home", help="home directory for the fake adapter")

    ap = argparse.ArgumentParser(prog="relay", description="Local agent relay", parents=[common])
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add(name, fn, **kw):
        p = sub.add_parser(name, parents=[common], **kw)
        p.set_defaults(fn=fn)
        return p

    p = add("init", cmd_init, help="create a relay root")
    p.add_argument("--participants", nargs="+", default=["codex", "claude"])
    p.add_argument("--max-turns", type=int, default=10)
    p.add_argument("--turn-timeout", type=int, default=900)

    def msg_opts(p):
        p.add_argument("--body-file", help="file with the body, or - for stdin")
        p.add_argument("--approval-ref", action="append", help="inert reference (repeatable)")
        p.add_argument("--evidence-ref", action="append", help="inert reference (repeatable)")
        p.add_argument("--idempotency-key")

    p = add("send", cmd_send, help="publish a message")
    p.add_argument("--from", dest="sender", required=True)
    p.add_argument("--to", required=True)
    p.add_argument("--kind", required=True, choices=P.KINDS)
    p.add_argument("--subject", required=True)
    p.add_argument("--thread")
    p.add_argument("--reply-to")
    p.add_argument("--scope")
    msg_opts(p)

    p = add("inbox", cmd_inbox, help="list messages for a participant (no side effects)")
    p.add_argument("--for", dest="participant", required=True)
    p.add_argument("--unread", action="store_true", help="only unclaimed manual messages")

    p = add("read", cmd_read, help="show a message (no side effects)")
    p.add_argument("message_id")

    p = add("claim", cmd_claim, help="claim a manual-mode message")
    p.add_argument("message_id")
    p.add_argument("--as", dest="as_", required=True)
    p.add_argument("--session")

    for name, fn, extra in (("ack", cmd_ack, None), ("complete", cmd_complete, "result"),
                            ("fail", cmd_fail, "reason"), ("release", cmd_release, "reason")):
        p = add(name, fn)
        p.add_argument("message_id")
        p.add_argument("--claim", required=True, help="claim token")
        if extra == "result":
            p.add_argument("--result", help="reply message id")
        elif extra:
            p.add_argument("--reason", required=True)

    p = add("reply", cmd_reply, help="reply to a message")
    p.add_argument("message_id")
    p.add_argument("--from", dest="sender", required=True)
    p.add_argument("--kind", required=True, choices=P.KINDS)
    p.add_argument("--subject")
    msg_opts(p)

    p = add("recover", cmd_recover, help="human: abandon a stale claim (history kept)")
    p.add_argument("message_id")
    p.add_argument("--reason", required=True)

    p = add("pause", cmd_pause, help="pause all claims and dispatch")
    p.add_argument("--reason", required=True)
    add("resume", cmd_resume, help="resume after a global pause")
    add("status", cmd_status, help="show relay status")
    p = add("thread", cmd_thread, help="show a thread")
    p.add_argument("thread_id")
    p = add("wait", cmd_wait, help="bounded wait for eligible messages")
    p.add_argument("--for", dest="participant", required=True)
    p.add_argument("--timeout", type=float, default=30)
    add("rebuild", cmd_rebuild, help="rebuild views/ caches from streams")
    add("console", cmd_console, help="interactive foreground console")
    p = add("serve", cmd_serve, help="foreground loopback dashboard (read-only observer)")
    p.add_argument("--port", type=int, default=0, help="default: a random free port")
    p.add_argument("--no-discovery", action="store_true",
                   help="do not look for other sessions on this machine")
    p.add_argument("--adapter", action="append", choices=ADAPTERS,
                   help="limit discovery to these adapters (repeatable)")
    p.add_argument("--discovery-interval", type=float, default=10.0)
    p.add_argument("--read-only", action="store_true", help="show state only; no actions")
    p.add_argument("--new-sessions", choices=("real", "fake"), default="real",
                   help="what 'new manager' starts: real Claude/Codex (default) or fake demo sessions")

    cp = sub.add_parser("context", help="describe a session's work (no control)", parents=[common])
    cs = cp.add_subparsers(dest="sub", required=True)
    p = cs.add_parser("report", parents=[common], help="a running session reports its own title/summary")
    p.set_defaults(fn=cmd_context_report)
    p.add_argument("--client", required=True, choices=("claude", "codex"))
    p.add_argument("--session", help="default: CLAUDE_CODE_SESSION_ID / CODEX_THREAD_ID")
    p.add_argument("--cwd", help="the session's folder (default: current folder)")
    p.add_argument("--title")
    p.add_argument("--summary")
    p.add_argument("--codex-home", help=argparse.SUPPRESS)

    sp = sub.add_parser("sessions", help="session endpoints", parents=[common])
    ss = sp.add_subparsers(dest="sub", required=True)

    def sadd(name, fn, **kw):
        p = ss.add_parser(name, parents=[common], **kw)
        p.set_defaults(fn=fn)
        return p

    p = sadd("list", cmd_sessions_list)
    p.add_argument("--adapter", choices=ADAPTERS)
    p = sadd("attach", cmd_sessions_attach, help="register an existing session")
    p.add_argument("--adapter", required=True, choices=ADAPTERS)
    p.add_argument("--session", required=True, help="client session id")
    p.add_argument("--as", dest="as_", required=True, help="participant label")
    p.add_argument("--cwd")
    p.add_argument("--unlisted", action="store_true", help="register without discovery")
    p = sadd("launch", cmd_sessions_launch, help="register a session the console will launch")
    p.add_argument("--adapter", required=True, choices=ADAPTERS)
    p.add_argument("--cwd", required=True)
    p.add_argument("--as", dest="as_", required=True)
    p.add_argument("--session-id")
    p.add_argument("--launch-options", help="JSON file with adapter launch options")
    p = sadd("release", cmd_sessions_release, help="human: release an endpoint reservation")
    p.add_argument("endpoint")

    pp = sub.add_parser("pair", help="leader/follower pairs", parents=[common])
    ps = pp.add_subparsers(dest="sub", required=True)

    def padd(name, fn, **kw):
        p = ps.add_parser(name, parents=[common], **kw)
        p.set_defaults(fn=fn)
        return p

    p = padd("create", cmd_pair_create)
    p.add_argument("--leader", required=True, help="endpoint id")
    p.add_argument("--follower", required=True, help="endpoint id")
    p.add_argument("--scope")
    p.add_argument("--scope-file")
    p.add_argument("--subject")
    p.add_argument("--max-turns", type=int)
    p.add_argument("--turn-timeout", type=int)

    def run_opts(p):
        p.add_argument("--takeover", action="store_true", help="take over from a dead controller")
        p.add_argument("--force", action="store_true", help="with --takeover: owner may be alive")
        p.add_argument("--max-seconds", type=float)
        p.add_argument("--exit-on-pause", action="store_true")
        p.add_argument("--exit-on-attention", action="store_true",
                       help="exit when the pair needs the human (closes launched sessions)")

    p = padd("start", cmd_pair_start, help="record Start with an initial handoff, then run")
    p.add_argument("pair_id")
    p.add_argument("--body-file", help="new leader handoff for the follower (- for stdin)")
    p.add_argument("--message", help="existing complete leader->follower message id")
    p.add_argument("--leader-request-file", help="initial request to the leader instead")
    p.add_argument("--subject")
    p.add_argument("--yes", action="store_true", help="confirm the previewed pair")
    p.add_argument("--no-run", action="store_true", help="record Start only")
    run_opts(p)
    p = padd("run", cmd_pair_run, help="run the foreground controller for a pair")
    p.add_argument("pair_id")
    run_opts(p)
    for name, fn in (("pause", cmd_pair_pause), ("stop", cmd_pair_stop)):
        p = padd(name, fn)
        p.add_argument("pair_id")
        p.add_argument("--reason", default="human request")
    p = padd("resume", cmd_pair_resume)
    p.add_argument("pair_id")
    p = padd("extend", cmd_pair_extend)
    p.add_argument("pair_id")
    p.add_argument("--add", type=int, required=True)
    p = padd("abandon-unknown", cmd_pair_abandon_unknown,
             help="end management; record that unknown turn outcomes may have run and won't be resent")
    p.add_argument("pair_id")
    p.add_argument("--confirm", action="store_true")
    p = padd("reconcile", cmd_pair_reconcile)
    p.add_argument("pair_id")
    p.add_argument("--outcome", required=True, choices=("continue", "requeue", "drop", "route-late"))
    p.add_argument("--message")
    p.add_argument("--reason", required=True)
    p = padd("answer", cmd_pair_answer, help="human answer that continues the same pair")
    p.add_argument("pair_id")
    p.add_argument("--body-file", required=True)
    p.add_argument("--to", choices=("leader", "follower"),
                   help="default: whoever asked")
    p.add_argument("--subject")
    p = padd("detach", cmd_pair_detach)
    p.add_argument("pair_id")
    p = padd("show", cmd_pair_show)
    p.add_argument("pair_id")
    return ap


def main(argv=None) -> int:
    ap = build_parser()
    try:
        a = ap.parse_args(argv)
    except SystemExit as e:
        return 2 if e.code else 0
    out = Out(a.json)
    try:
        a.fn(a, out)
        return 0
    except RelayError as e:
        if a.json:
            print(json.dumps({"error": str(e), "code": e.code, "type": type(e).__name__,
                              "details": e.details}, default=str))
        else:
            print(f"error: {e}", file=sys.stderr)
        return e.code
    except (OSError, ValueError) as e:
        print(json.dumps({"error": str(e), "code": 1}) if a.json else f"error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
