#!/bin/bash
# Reproducible fake-client walkthrough. Writes only under $DEMO (default: a new
# temporary directory). Uses no real Claude/Codex session and no model.
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
DEMO="${DEMO:-$(mktemp -d -t relay-demo)}"
export AGENT_RELAY_ROOT="$DEMO/root" AGENT_RELAY_FAKE_HOME="$DEMO/home" PYTHONDONTWRITEBYTECODE=1
export R="$HERE/bin/relay"
j() { python3 -c "import json,sys; print(json.load(sys.stdin)$1)"; }
say() { printf '\n$ %s\n' "$*"; }

mkdir -p "$DEMO/home" "$DEMO/workspace"
touch "$DEMO/.start-marker"; sleep 1

say relay init --participants codex claude
$R init --participants codex claude

# Two independent fake "terminal" sessions that already have context of their own.
PYTHONPATH="$HERE" python3 -m relay.adapters.fake_client --home "$DEMO/home" --session-id codex-term-1 \
  --cwd "$DEMO/workspace" --seed-history 4 --default '{"finish_after":3}' >/dev/null 2>&1 &
CODEX_PID=$!
PYTHONPATH="$HERE" python3 -m relay.adapters.fake_client --home "$DEMO/home" --session-id claude-term-1 \
  --cwd "$DEMO/workspace" --seed-history 2 --script '[{}, {"sleep": 3}]' >/dev/null 2>&1 &
CLAUDE_PID=$!
trap 'kill $CODEX_PID $CLAUDE_PID 2>/dev/null || true' EXIT
sleep 1

say relay sessions list --adapter fake
$R sessions list --adapter fake

say relay sessions attach --adapter fake --session codex-term-1 --as codex
L=$($R sessions attach --adapter fake --session codex-term-1 --as codex --json | j '["id"]')
echo "leader endpoint $L"
say relay sessions attach --adapter fake --session claude-term-1 --as claude
F=$($R sessions attach --adapter fake --session claude-term-1 --as claude --json | j '["id"]')
echo "follower endpoint $F"

say relay pair create --leader "$L" --follower "$F" --scope "demo workspace only" --max-turns 10
P=$($R pair create --leader "$L" --follower "$F" --scope "demo workspace only; no network" \
  --max-turns 10 --turn-timeout 120 --json | j '["id"]')
$R pair show "$P"

printf 'Implement step 1 of the demo task and report files/tests.\n' > "$DEMO/handoff.md"
say relay pair start "$P" --body-file handoff.md --yes --no-run
$R pair start "$P" --body-file "$DEMO/handoff.md" --yes --no-run 2>/dev/null

say "relay pair run $P   (foreground controller, run here in the background for the demo)"
$R pair run "$P" --max-seconds 60 2> "$DEMO/controller.log" > "$DEMO/controller.out" &
CTL=$!

# Pause while the follower's second turn (3 s) is in flight.
python3 - "$P" <<'EOF'
import json, subprocess, sys, time, os
p = sys.argv[1]
while True:
    s = json.loads(subprocess.run([os.environ["R"], "pair", "show", p, "--json"], capture_output=True,
                                  text=True).stdout)
    if s["turns_used"] == 3 and s["inflight"] and s["inflight"]["phase"] == "accepted":
        break
    time.sleep(0.05)
EOF
say relay pair pause "$P" --reason "Human review"
$R pair pause "$P" --reason "Human review"
sleep 4
say "relay pair show $P   (in-flight result recorded, next handoff queued, nothing dispatched)"
$R pair show "$P"
say relay pair resume "$P"
$R pair resume "$P"
wait $CTL || true
echo "--- controller log ---"; cat "$DEMO/controller.log"

say "(controller has exited: restart the CLI and inspect durable history)"
say relay status
$R status
T=$($R pair show "$P" --json | j '["thread_id"]')
say relay thread "$T"
$R thread "$T"

say relay pair detach "$P"
$R pair detach "$P"
kill -0 $CODEX_PID && kill -0 $CLAUDE_PID && echo "both attached sessions are still running (never closed by the relay)"

say "fake session contexts (pre-existing turns preserved, no replay)"
for s in codex-term-1 claude-term-1; do
  python3 -c "import json;c=json.load(open('$DEMO/home/sessions/$s/context.json'));print('$s', 'turns', len(c['turns']), 'routed', c['routed'], 'pre-existing', sum(1 for t in c['turns'] if t['request_id'] is None))"
done
say "files changed outside the demo root since start (expect none)"
find "$HERE" -newer "$DEMO/.start-marker" -type f -not -path '*/__pycache__/*' \
  -not -name 'WALKTHROUGH.md' | sed 's/^/  changed: /' || true
echo "  (end of list)"
echo "demo directory: $DEMO"
