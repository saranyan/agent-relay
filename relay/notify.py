"""Decision notifications.

* Off by default. Each external channel (Custom webhook, Twilio SMS) is configured locally,
  verified with an expiring, rate-limited code, and then enabled by the user. Changing the
  destination invalidates the verification and cancels anything not yet sent.
* Only a NEW unresolved actionable decision notifies (identities come from the console:
  team + the attention event's stream position, approval request ids, ownership episode).
* A durable outbox stream (`notify`) records every step. Reserving a send is an atomic,
  cross-process append; whoever appends `dispatching` first sends, nobody else.
* Only a definite non-delivery retries automatically, once. A timeout, disconnect or crash
  after dispatch is "may have been sent" and is never resent automatically; a manual resend
  explicitly acknowledges a possible duplicate. Exactly-once delivery is NOT promised.
* Kind-only text by default; question excerpts are opt-in. Every field is redacted (secrets,
  absolute paths, relay ids). No code, transcripts, tokens or session ids.
* Delivery never answers a decision or resumes anything. Replies are not read.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

from .protocol import SECRET_PATTERNS
from .storage import Conflict, InvalidInput, NotFound, Root, actor

WHO = actor("relay", "notify")
STREAM = "notify"
CHANNELS = ("webhook", "sms")
HOURLY_LIMIT = 6
RETRY_AFTER_S = 60
STUCK_AFTER_S = 120          # a `dispatching` with no outcome after this is "may have been sent"
SEND_TIMEOUT_S = 10
CODE_TTL_S = 600
CODE_MIN_GAP_S = 60
CODE_PER_HOUR = 5
CODE_ATTEMPTS = 5
MAX_REMINDERS = 3
EXCERPT = 140

KIND_TEXT = {
    "question": "has a question for you",
    "approval": "is waiting for your approval in its own window",
    "budget": "has used its turn budget",
    "deadline": "has reached its time limit",
    "timeout": "had a turn take too long",
    "uncertain": "isn't sure its last message arrived",
    "failed": "stopped and needs you",
    "other": "needs you",
    "unknown_outcome": "needs you to confirm a turn with an unknown outcome",
    "handoff_blocked": "has a conversation that needs you",
    "give_back": "has ended; a conversation is still with the relay",
    "acceptance_refused": "couldn't accept the work and needs you",
}


# ------------------------------------------------------------------ redaction
_PATH = re.compile(r"(?<![\w.])(?:~|/)[\w.\-]+(?:/[\w.\-]+)+")
_IDS = re.compile(r"\b[pstum]_[0-9a-f]{12,}\b|\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")
_CODEY = re.compile(r"`|\$\(|\b(?:def|class|import|function|return|curl|rm -rf|sudo)\b|[{};<>]")


def redact(text: Any, limit: int = EXCERPT) -> str:
    """Applied to EVERY outgoing field. Heuristics can't prove text safe, which is why
    question excerpts are opt-in."""
    s = " ".join(str(text or "").split())
    for _label, pat in SECRET_PATTERNS:
        s = pat.sub("[redacted]", s)
    s = _IDS.sub("[id]", _PATH.sub("[path]", s))
    return s if len(s) <= limit else s[:limit - 1].rstrip() + "…"


def valid_link_base(base: str) -> str:
    """An https address you control, with nothing that could carry a credential: no userinfo,
    query or fragment. Entering it doesn't make the console reachable or secure."""
    if not isinstance(base, str) or len(base) > 300:
        raise InvalidInput("the link address must be a short https:// address")
    u = urllib.parse.urlsplit(base.strip())
    if u.scheme != "https" or not u.hostname:
        raise InvalidInput("the link address must start with https:// and name a host")
    if "@" in u.netloc or u.username or u.password:
        raise InvalidInput("the link address can't contain a user name or password")
    if u.query or u.fragment or "?" in base or "#" in base:
        raise InvalidInput("the link address can't contain a query or fragment (no tokens)")
    return urllib.parse.urlunsplit(("https", u.netloc.lower(), u.path.rstrip("/"), "", ""))


def decision_ref(decision_id: str, key: bytes) -> str:
    """An opaque, per-install reference to a decision; the console resolves it after its
    normal sign-in. It reveals no team, session or turn id."""
    return hmac.new(key, decision_id.encode(), hashlib.sha256).hexdigest()[:24]


def decision_link(base: str | None, ref: str) -> str | None:
    return f"{base}/#/d/{ref}" if base else None


def compose(decision: dict, cfg: dict, link: str | None = None) -> str:
    """The message. Kind-only by default, with the user's own display label."""
    label = redact(cfg.get("display_label") or "Agent Relay", 40)
    what = KIND_TEXT.get(decision.get("kind"), KIND_TEXT["other"])
    parts = [f"{label}: an agent team {what}."]
    if cfg.get("question_text") and decision.get("question"):
        q = decision["question"]
        if not _CODEY.search(q):  # code-like questions never leave as text
            parts.append(f"“{redact(q)}”")
    parts.append(f"Open: {link}" if link else "Open Agent Relay on your computer.")
    return " ".join(parts)


def compose_aggregate(n: int, cfg: dict) -> str:
    label = redact(cfg.get("display_label") or "Agent Relay", 40)
    return f"{label}: {n} decisions are waiting for you. Open Agent Relay on your computer."


# --------------------------------------------------------------- delivery
class DeliveryError(Exception):
    """`definite`: the provider certainly did not deliver (safe to retry). Otherwise the
    message may have been sent."""

    def __init__(self, reason: str, definite: bool):
        super().__init__(reason)
        self.reason, self.definite = reason, definite


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):  # never follow: credentials must not travel on
        return None


def _post(url: str, body: bytes, headers: dict, secrets_: tuple[str, ...]) -> None:
    if not url.startswith("https://"):
        raise DeliveryError("only HTTPS destinations are allowed", True)
    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")

    def clean(msg: str) -> str:
        for s in secrets_:
            if s:
                msg = msg.replace(s, "[redacted]")
        return redact(msg, 200)
    try:
        with opener.open(req, timeout=SEND_TIMEOUT_S) as r:
            r.read(4096)
            if not 200 <= r.status < 300:
                raise DeliveryError(clean(f"the provider answered {r.status}"), r.status < 500)
    except urllib.error.HTTPError as e:  # an answer: 4xx means it refused (definite)
        raise DeliveryError(clean(f"the provider answered {e.code}"), 300 <= e.code < 500)
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", e)
        refused = isinstance(reason, (ConnectionRefusedError,)) or "Name or service" in str(reason) \
            or "nodename nor servname" in str(reason)
        raise DeliveryError(clean(f"couldn't reach the provider: {reason}"), refused)
    except (TimeoutError, OSError) as e:
        raise DeliveryError(clean(f"no answer from the provider: {e}"), False)


class CustomWebhook:
    """POSTs {text, link, decision} JSON to an HTTPS URL you provide. This is a custom
    format: it is NOT automatically compatible with Slack, Discord, ntfy or Pushover."""
    type = "webhook"
    label = "Custom webhook"

    def check(self, dest: dict) -> dict:
        url = dest.get("url")
        if not isinstance(url, str) or not url.startswith("https://") or len(url) > 2000:
            raise InvalidInput("the webhook needs an https:// URL")
        return {"url": url}

    def mask(self, dest: dict) -> str:
        return f"https://{urllib.parse.urlsplit(dest['url']).hostname}/…"

    def send(self, dest: dict, text: str, meta: dict) -> None:
        body = json.dumps({"text": text, "link": meta.get("link"), "decision": meta.get("decision")}).encode()
        _post(dest["url"], body, {"Content-Type": "application/json"}, (dest["url"],))


class TwilioSMS:
    type = "sms"
    label = "SMS (Twilio)"

    def check(self, dest: dict) -> dict:
        out = {k: dest.get(k) for k in ("account_sid", "auth_token", "from", "to")}
        if not all(isinstance(v, str) and v for v in out.values()):
            raise InvalidInput("SMS needs your Twilio account SID, auth token, from-number and your number")
        if not re.fullmatch(r"AC[0-9a-fA-F]{32}", out["account_sid"]):
            raise InvalidInput("that doesn't look like a Twilio account SID")
        for k in ("from", "to"):
            if not re.fullmatch(r"\+[1-9]\d{6,14}", out[k]):
                raise InvalidInput(f"the {k}-number must be in +<country><number> form")
        return out

    def mask(self, dest: dict) -> str:
        return f"…{dest['to'][-4:]}"

    def send(self, dest: dict, text: str, meta: dict) -> None:
        import base64
        url = f"https://api.twilio.com/2010-04-01/Accounts/{dest['account_sid']}/Messages.json"
        auth = base64.b64encode(f"{dest['account_sid']}:{dest['auth_token']}".encode()).decode()
        body = urllib.parse.urlencode({"From": dest["from"], "To": dest["to"], "Body": text}).encode()
        _post(url, body, {"Authorization": f"Basic {auth}", "Content-Type": "application/x-www-form-urlencoded"},
              (dest["account_sid"], dest["auth_token"], auth, dest["to"]))


ADAPTERS = {"webhook": CustomWebhook, "sms": TwilioSMS}


class DemoAdapter:
    """Demo mode only (AGENT_RELAY_NOTIFY_DEMO=<file>): validates like the real channel but
    appends the message to a local file instead of sending anything."""

    def __init__(self, real, path: str):
        self.real, self.path = real, path
        self.type, self.label = real.type, f"{real.label} (demo: written to a local file)"

    def check(self, dest):
        return self.real.check(dest)

    def mask(self, dest):
        return self.real.mask(dest)

    def send(self, dest, text, meta):
        with open(self.path, "a") as f:
            f.write(json.dumps({"channel": self.type, "text": text, "meta": meta}) + "\n")


def default_adapters() -> dict:
    demo = os.environ.get("AGENT_RELAY_NOTIFY_DEMO")
    return {k: (DemoAdapter(v(), demo) if demo else v()) for k, v in ADAPTERS.items()}


# ---------------------------------------------------------------- config
def _dest_hash(dest: dict) -> str:
    return hashlib.sha256(json.dumps(dest, sort_keys=True).encode()).hexdigest()


class Config:
    """Local, mode 0600. Holds credentials; nothing here goes into events, logs or the API.

    Every change is a read-modify-write under one cross-process lock, written through a unique
    temporary file. Each channel has a generation that changes with its
    destination, verification or on/off state; reservations are bound to it, so a disable,
    re-enable or new destination can never revive an older reservation. Code-send history is
    kept per channel apart from the destination, so re-saving can't reset the rate limits."""

    def __init__(self, path: str | None = None, adapters: dict | None = None):
        self.path = path or os.path.join(os.path.expanduser("~/.config/agent-relay"), "notify.json")
        self.adapters = adapters or default_adapters()
        self._tlock = threading.RLock()

    def load(self) -> dict:
        try:
            with open(self.path) as f:
                c = json.load(f)
        except FileNotFoundError:
            c = {}
        c.setdefault("version", 0)
        c.setdefault("channels", {})
        c.setdefault("code_sends", {})
        c.setdefault("display_label", "Agent Relay")
        c.setdefault("question_text", False)
        c.setdefault("link_base", None)
        c.setdefault("reminder", {"after_min": None, "max": 0})
        return c

    @contextlib.contextmanager
    def _mutate(self):
        """Yields the current config; writes it back (version + 1) if the block succeeds."""
        os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
        with self._tlock:
            fd = os.open(self.path + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                c = self.load()
                before = c["version"]
                yield c
                if self.load()["version"] != before:  # never under the lock: a writer bypassed it
                    raise Conflict("the notification settings changed meanwhile; try again")
                c["version"] = before + 1
                tmp = f"{self.path}.{os.getpid()}.{threading.get_ident()}.{secrets.token_hex(4)}.tmp"
                wfd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(wfd, "w") as f:
                    json.dump(c, f, indent=1, sort_keys=True)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, self.path)
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)

    @staticmethod
    def _bump(ch: dict) -> None:
        ch["gen"] = int(ch.get("gen", 0)) + 1

    # The settings a user changes.
    def set_options(self, *, display_label=None, question_text=None, link_base=None, reminder=None) -> dict:
        with self._mutate() as c:
            if display_label is not None:
                if not isinstance(display_label, str) or not 0 < len(display_label.strip()) <= 40:
                    raise InvalidInput("the display label is 1-40 characters")
                c["display_label"] = display_label.strip()
            if question_text is not None:
                c["question_text"] = bool(question_text)
            if link_base is not None:
                c["link_base"] = valid_link_base(link_base) if link_base else None
            if reminder is not None:
                after, n = reminder.get("after_min"), reminder.get("max", 0)
                if after is not None and (not isinstance(after, int) or not 5 <= after <= 1440):
                    raise InvalidInput("remind after 5-1440 minutes")
                if not isinstance(n, int) or not 0 <= n <= MAX_REMINDERS:
                    raise InvalidInput(f"at most {MAX_REMINDERS} reminders")
                c["reminder"] = {"after_min": after, "max": n if after else 0}
            c.setdefault("link_secret", secrets.token_hex(16))
        return self.public()

    def set_destination(self, channel: str, dest: dict) -> dict:
        if channel not in CHANNELS:
            raise InvalidInput("unknown channel")
        d = self.adapters[channel].check(dest)
        with self._mutate() as c:
            old = c["channels"].get(channel) or {}
            # A new destination is unverified and off; the code history stays (rate limits).
            ch = {"dest": d, "enabled": False, "verified_for": None, "code": None, "enabled_at": None,
                  "gen": old.get("gen", 0)}
            self._bump(ch)
            c["channels"][channel] = ch
        return self.public()

    def start_verify(self, channel: str, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        with self._mutate() as c:
            ch = c["channels"].get(channel)
            if not ch:
                raise NotFound("set a destination first")
            sends = [t for t in c["code_sends"].get(channel, []) if now - t < 3600]
            if sends and now - sends[-1] < CODE_MIN_GAP_S:
                raise Conflict("wait a minute before asking for another code")
            if len(sends) >= CODE_PER_HOUR:
                raise Conflict("too many codes this hour; try later")
            code = f"{secrets.randbelow(10 ** 6):06d}"
            salt = secrets.token_hex(8)
            ch["code"] = {"salt": salt, "hash": hashlib.sha256((salt + code).encode()).hexdigest(),
                          "expires": now + CODE_TTL_S, "attempts": 0, "for": _dest_hash(ch["dest"])}
            c["code_sends"][channel] = sends + [now]  # counted before sending: a failure still counts
            dest = ch["dest"]
        try:  # sent outside the lock
            self.adapters[channel].send(dest, f"Agent Relay verification code: {code}. It expires in 10 minutes.",
                                        {"decision": None, "link": None})
        except DeliveryError as e:
            return {"sent": False, "reason": redact(e.reason, 200)}
        return {"sent": True}

    def confirm_verify(self, channel: str, code: str, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        ok = False
        with self._mutate() as c:
            ch = c["channels"].get(channel) or {}
            v = ch.get("code")
            if not v or now > v["expires"] or v["for"] != _dest_hash(ch["dest"]):
                raise Conflict("that code has expired; ask for a new one")
            if v["attempts"] >= CODE_ATTEMPTS:
                raise Conflict("too many wrong codes; ask for a new one")
            v["attempts"] += 1
            ok = hmac.compare_digest(hashlib.sha256((v["salt"] + str(code)).encode()).hexdigest(), v["hash"])
            if ok:
                ch["verified_for"], ch["code"] = _dest_hash(ch["dest"]), None
                self._bump(ch)
        if not ok:
            raise InvalidInput("that code isn't right")
        return self.public()

    def set_enabled(self, channel: str, on: bool, now: float | None = None) -> dict:
        with self._mutate() as c:
            ch = c["channels"].get(channel)
            if not ch:
                raise NotFound("set a destination first")
            if on and ch.get("verified_for") != _dest_hash(ch["dest"]):
                raise Conflict("verify this destination first")
            ch["enabled"] = bool(on)
            ch["enabled_at"] = (time.time() if now is None else now) if on else None
            self._bump(ch)
        return self.public()

    def active(self, c: dict | None = None) -> dict:
        """channel -> (dest, binding, enabled_at) for channels that are on AND verified. The
        binding names this exact configuration generation; reservations carry it."""
        c = c or self.load()
        out = {}
        for name, ch in c["channels"].items():
            if ch.get("enabled") and ch.get("verified_for") == _dest_hash(ch["dest"]):
                out[name] = (ch["dest"], f"{ch['verified_for']}:{ch.get('gen', 0)}", ch.get("enabled_at") or 0)
        return out

    def link_key(self) -> bytes:
        c = self.load()
        if not c.get("link_secret"):
            with self._mutate() as m:
                m.setdefault("link_secret", secrets.token_hex(16))
            c = self.load()
        return c["link_secret"].encode()

    def public(self, c: dict | None = None) -> dict:
        """What the console may show: no credentials, destinations masked."""
        c = c or self.load()

        def mask(name, ch):
            m = getattr(self.adapters[name], "mask", None)
            return m(ch["dest"]) if m else "configured"
        return {"display_label": c["display_label"], "question_text": c["question_text"],
                "link_base": c["link_base"], "reminder": c["reminder"],
                "channels": {name: {"label": self.adapters[name].label, "destination": mask(name, ch),
                                    "enabled": bool(ch.get("enabled")),
                                    "verified": ch.get("verified_for") == _dest_hash(ch["dest"])}
                             for name, ch in c["channels"].items()}}


# ---------------------------------------------------------------- outbox
def fold(events) -> dict:
    st = {"seen": {}, "resolved": set(), "keys": {}, "dispatches": []}
    for e in events:
        d, t = e.data, e.type
        if t == "seen":
            st["seen"].setdefault(d["decision"], {"at": d["at"], "team": d.get("team"), "kind": d.get("kind")})
        elif t == "resolved":
            st["resolved"].add(d["decision"])
        elif t == "reserved":
            k = st["keys"].setdefault(d["key"], {"key": d["key"], "decision": d["decision"], "channel": d["channel"],
                                                 "covers": d.get("covers") or [d["decision"]], "attempt": 0,
                                                 "history": []})
            k.update(state="reserved", attempt=d["attempt"], dest=d["dest"], at=d["at"], ack=d.get("ack", False))
        elif t == "dispatching":
            k = st["keys"][d["key"]]
            k.update(state="dispatching", at=d["at"])
            st["dispatches"].append({"channel": k["channel"], "at": d["at"]})
        elif t == "delivered":
            st["keys"][d["key"]].update(state="delivered", at=d["at"])
        elif t == "failed":
            st["keys"][d["key"]].update(state="failed" if d["definite"] else "uncertain", at=d["at"],
                                        reason=d.get("reason"))
        elif t == "cancelled":
            st["keys"][d["key"]].update(state="cancelled", at=d["at"], reason=d.get("reason"))
        if t in ("reserved", "dispatching", "delivered", "failed", "cancelled"):
            st["keys"][d["key"]]["history"].append(t)
    return st


def _still_open(k: dict, open_) -> bool:
    """A single notice needs its decision open; a combined one, any of the decisions it covers."""
    return any(c in open_ for c in k["covers"]) if len(k["covers"]) > 1 else k["decision"] in open_


class Notifier:
    """One tick: notice new decisions, cancel resolved ones, reserve and send. Safe to run
    from several processes at once (the outbox append is the fence)."""

    def __init__(self, root: Root, config: Config, decisions: Callable[[], list[dict]],
                 clock: Callable[[], float] = time.time):
        self.root, self.config, self.decisions, self.clock = root, config, decisions, clock

    def state(self) -> dict:
        return fold(self.root.read_stream(STREAM))

    def _append(self, fn) -> Any:
        return self.root.append(STREAM, lambda events: fn(fold(events)), WHO)

    def tick(self) -> None:
        now = self.clock()
        cfg = self.config.load()
        active = self.config.active(cfg)
        open_ = {d["id"]: d for d in self.decisions()}
        st = self.state()
        for did, d in open_.items():  # first observation of each decision
            if did not in st["seen"]:
                self._append(lambda s, did=did, d=d: None if did in s["seen"] else
                             ("seen", {"decision": did, "team": d.get("team"), "kind": d.get("kind"), "at": now}))
        for did in list(st["seen"]):
            if did not in open_ and did not in st["resolved"]:
                self._append(lambda s, did=did: None if did in s["resolved"] else ("resolved", {"decision": did}))
        st = self.state()
        # Initial notices: only for decisions that began after the channel was enabled (by the
        # decision's own start time when known, else when this outbox first saw it).
        for did, d in open_.items():
            began = d.get("since") or st["seen"][did]["at"]
            for ch, (_dest, dh, enabled_at) in active.items():
                if began >= enabled_at:
                    self._reserve(f"{did}|{ch}|0", did, ch, dh, now)
        # Reminders, only while open and only after the previous notice was delivered.
        rem = cfg.get("reminder") or {}
        if rem.get("after_min") and rem.get("max"):
            for did in open_:
                for ch, (_dest, dh, _e) in active.items():
                    for n in range(rem["max"]):
                        prev = st["keys"].get(f"{did}|{ch}|{n}")
                        if prev and prev["state"] == "delivered" and now - prev["at"] >= rem["after_min"] * 60:
                            self._reserve(f"{did}|{ch}|{n + 1}", did, ch, dh, now)
        st = self.state()
        for k in list(st["keys"].values()):
            if k["state"] == "dispatching" and now - k["at"] > STUCK_AFTER_S:
                self._outcome(k["key"], "failed", definite=False, reason="the relay stopped while sending; "
                              "it may have been sent", now=now)
            elif k["state"] == "failed" and k["attempt"] == 1 and now - k["at"] >= RETRY_AFTER_S \
                    and _still_open(k, open_):
                self._reserve(k["key"], k["decision"], k["channel"], k["dest"], now, attempt=2)
        st = self.state()
        held: dict[str, list] = {}
        for k in st["keys"].values():
            if k["state"] != "reserved":
                continue
            ch = active.get(k["channel"])
            if not _still_open(k, open_):
                self._cancel(k["key"], "the decision was resolved", now)
            elif not ch or ch[1] != k["dest"]:
                self._cancel(k["key"], "the channel was turned off or changed", now)
            else:
                held.setdefault(k["channel"], []).append(k)
        for ch, keys in held.items():
            used = sum(1 for x in self.state()["dispatches"] if x["channel"] == ch and now - x["at"] < 3600)
            slots = HOURLY_LIMIT - used
            if slots <= 0:
                continue  # stays reserved until a slot frees
            if len(keys) > slots:
                # Combine the overflow into ONE message that takes one of the same slots.
                single, rest = keys[:slots - 1], keys[slots - 1:]
                covers = sorted({c for k in rest for c in k["covers"]})
                agg = f"agg|{ch}|{hashlib.sha256('|'.join(covers).encode()).hexdigest()[:16]}"
                for k in rest:
                    self._cancel(k["key"], "combined into one message", now)
                if self._reserve(agg, covers[0], ch, active[ch][1], now, covers=covers):
                    single.append(self.state()["keys"][agg])
                keys = single
            for k in keys:
                self._dispatch(k, open_, cfg, now)

    # -- steps (each one conditional on the outbox as it is now) --
    def _reserve(self, key, did, ch, dh, now, attempt=1, covers=None, ack=False) -> bool:
        def v(s):
            k = s["keys"].get(key)
            if k and not (attempt > k["attempt"] and k["state"] in ("failed", "uncertain")):
                return None  # already reserved/sent/cancelled, or not retryable
            if k and k["state"] == "uncertain" and not ack:
                return None
            return "reserved", {"key": key, "decision": did, "channel": ch, "dest": dh, "attempt": attempt,
                                "covers": covers or [did], "at": now, "ack": ack}
        return self._append(v) is not None

    def _cancel(self, key, reason, now):
        self._append(lambda s: ("cancelled", {"key": key, "reason": reason, "at": now})
                     if s["keys"][key]["state"] == "reserved" else None)

    def _outcome(self, key, etype, *, now, definite=None, reason=None):
        data = {"key": key, "at": now}
        if etype == "failed":
            data.update(definite=definite, reason=redact(reason, 200))
        self._append(lambda s: (etype, data) if s["keys"][key]["state"] == "dispatching" else None)

    def _dispatch(self, k: dict, open_: dict, cfg: dict, now: float) -> None:
        # Revalidate immediately before sending: still open, channel still on and unchanged.
        live = {d["id"] for d in self.decisions()}
        cfg_now = self.config.load()
        ch = self.config.active(cfg_now).get(k["channel"])
        if not _still_open(k, live):
            return self._cancel(k["key"], "the decision was resolved", now)
        if not ch or ch[1] != k["dest"]:
            return self._cancel(k["key"], "the channel was turned off or changed", now)
        def grant(st):
            # One conditional append grants BOTH this key and an hourly slot, judged on the
            # outbox as it is now and the current time: two dispatchers can never
            # both spend the last slot. Retries and combined messages come through here too.
            if st["keys"][k["key"]]["state"] != "reserved":
                return None  # another dispatcher took it
            t = self.clock()
            used = sum(1 for x in st["dispatches"] if x["channel"] == k["channel"] and t - x["at"] < 3600)
            if used >= HOURLY_LIMIT:
                return None  # stays reserved until a slot frees
            return "dispatching", {"key": k["key"], "attempt": k["attempt"], "at": t}
        if self._append(grant) is None:
            return
        if len(k["covers"]) > 1:
            n = sum(1 for c in k["covers"] if c in live)
            text, meta = compose_aggregate(n, cfg_now), {"decision": None, "link": None}
        else:
            d = open_.get(k["decision"]) or {"id": k["decision"], "kind": "other"}
            ref = decision_ref(k["decision"], self.config.link_key())
            link = decision_link(cfg_now.get("link_base"), ref)
            text = compose(d, cfg_now, link)
            meta = {"decision": ref, "link": link}
        try:
            self.config.adapters[k["channel"]].send(ch[0], text, meta)
        except DeliveryError as e:
            return self._outcome(k["key"], "failed", now=self.clock(), definite=e.definite, reason=e.reason)
        except Exception as e:  # anything unexpected after dispatch: it may have gone out
            return self._outcome(k["key"], "failed", now=self.clock(), definite=False,
                                 reason=f"unexpected error while sending ({type(e).__name__})")
        self._outcome(k["key"], "delivered", now=self.clock())

    # -- what the console shows and the user's own actions --
    def status(self, decision: str) -> list[dict]:
        out = []
        for k in self.state()["keys"].values():
            if decision in k["covers"]:
                out.append({"key": k["key"], "channel": k["channel"], "state": k["state"], "at": k["at"],
                            "attempt": k["attempt"], "reason": k.get("reason"),
                            "may_have_been_sent": k["state"] in ("uncertain", "dispatching")})
        return out

    def resend(self, key: str, *, acknowledge_duplicate: bool = False) -> None:
        """Your explicit retry. After an uncertain delivery you must acknowledge that the
        first message may have arrived (a duplicate is possible)."""
        k = self.state()["keys"].get(key)
        if not k or k["state"] not in ("failed", "uncertain"):
            raise Conflict("nothing to resend")
        if k["state"] == "uncertain" and not acknowledge_duplicate:
            raise Conflict("the first message may have been sent; confirm that a duplicate is OK")
        cfg_ch = self.config.active().get(k["channel"])
        if not cfg_ch:
            raise Conflict("that channel is off")
        if not self._reserve(key, k["decision"], k["channel"], cfg_ch[1], self.clock(), attempt=k["attempt"] + 1,
                             covers=k["covers"], ack=acknowledge_duplicate):
            raise Conflict("it changed meanwhile; refresh")
