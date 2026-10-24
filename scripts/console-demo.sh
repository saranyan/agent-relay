#!/bin/bash
# Fake-session demo of the relay console. Writes only under $DEMO; no real Claude/Codex
# session and no model is used — "new" managers are pretend sessions too.
# Ctrl-C (or `pkill -f console-demo.sh`) stops the console and every fake session.
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
DEMO="${DEMO:-$(mktemp -d -t relay-console)}"
PORT="${PORT:-47811}"
export AGENT_RELAY_ROOT="$DEMO/root" AGENT_RELAY_FAKE_HOME="$DEMO/home" PYTHONDONTWRITEBYTECODE=1
# Notifications in the demo are written to a local file, never sent.
export AGENT_RELAY_NOTIFY_CONFIG="$DEMO/notify.json" AGENT_RELAY_NOTIFY_DEMO="$DEMO/notify-demo.log"
R="$HERE/bin/relay"
mkdir -p "$DEMO/home"
$R init --participants '*' >/dev/null

PIDS=(); SERVE=""
cleanup() { kill $SERVE "${PIDS[@]}" 2>/dev/null || true; }
trap cleanup EXIT
trap 'exit 130' INT TERM

fake() {  # fake <id> <folder> <client> <default-json> [summary] [script-json] [title] [interactive]
  mkdir -p "$DEMO/projects/$2"
  PYTHONPATH="$HERE" python3 -m relay.adapters.fake_client --home "$DEMO/home" --session-id "$1" \
    --cwd "$DEMO/projects/$2" --client "$3" --default "$4" --summary "${5:-}" \
    --script "${6:-[]}" --title "${7:-}" --seed-history 3 ${8:+--interactive} >/dev/null 2>&1 &
  PIDS+=($!)
}
# Pretend sessions take a realistic while per turn, so there is time to watch and step in.
fake s-payments  payments-api     Claude '{"natural":true,"sleep":15}' "Payment client timeouts"
fake s-billing   billing-service  Claude '{"natural":true,"sleep":45}'
fake s-docs      docs-site        Claude '{"natural":true,"sleep":4}' "" \
  '[{"action":"ask_human","subject":"Which docs should change?","text":"I found the retry behaviour in two places: the API reference and the getting-started tutorial. Should I update both, or only the API reference?"}]'
fake s-web       web-app          Codex  '{"natural":true,"sleep":3600}' "Checkout page redesign"
fake s-infra     infra-scripts    Codex  '{"natural":true,"sleep":15}'
# Two pretend sessions in the same folder doing different jobs (simulated, not real sessions):
# the Codex one's summary comes from its (pretend) client metadata, the Claude one's from a manual edit below.
fake s-relay-cx   agent-relay      Codex  '{"natural":true,"sleep":15}' \
  "Architecture and review of session management" '[]' "Session manager review"
fake s-relay-cl   agent-relay      Claude '{"natural":true,"sleep":15}' "" '[]' "Dashboard work"
# Two pretend interactive terminals in one folder: they can't be driven while open, only
# handed over (close them in their terminal; here: stop their process) and taken back.
fake s-mobile-cl  mobile-app       Claude '{"natural":true,"sleep":3}' "" '[]' "Offline sync" interactive
fake s-mobile-cx  mobile-app       Codex  '{"natural":true,"sleep":2,"finish_after":50}' "" '[]' "Sync review" interactive
sleep 1
echo "busy" > "$DEMO/home/sessions/s-web/manual/now.txt"   # web-app is busy with you

$R serve --adapter fake --new-sessions fake --discovery-interval 2 --port "$PORT" 2> "$DEMO/serve.log" & SERVE=$!
for _ in $(seq 50); do grep -q "open:" "$DEMO/serve.log" 2>/dev/null && break; sleep 0.2; done
URL=$(grep -o 'http://[^ ]*' "$DEMO/serve.log" | head -1)
TOKEN=${URL##*t=}
sleep 3  # let discovery find the sessions

# billing-service is already managed by an AI Reviewer, started through the console's own API.
KEY=$(curl -s -H "X-Relay-Token: $TOKEN" "http://127.0.0.1:$PORT/api/sessions" \
      | python3 -c 'import json,sys; print(next(s["key"] for s in json.load(sys.stdin)["sessions"] if s["name"]=="billing-service"))')
curl -s -o /dev/null -X POST "http://127.0.0.1:$PORT/api/manage" -H "X-Relay-Token: $TOKEN" \
  -H "Origin: http://127.0.0.1:$PORT" -H "Content-Type: application/json" \
  -d "{\"key\": \"$KEY\", \"manager\": {\"new\": {\"client\": \"claude\", \"personality\": \"reviewer\"}},
       \"goal\": \"Add retries with backoff to the billing API client, with tests for the failure case.\"}"

# docs-site is managed by a Planner and soon stops to ask you a question — it waits until you answer.
KEY=$(curl -s -H "X-Relay-Token: $TOKEN" "http://127.0.0.1:$PORT/api/sessions" \
      | python3 -c 'import json,sys; print(next(s["key"] for s in json.load(sys.stdin)["sessions"] if s["name"]=="docs-site"))')
curl -s -o /dev/null -X POST "http://127.0.0.1:$PORT/api/manage" -H "X-Relay-Token: $TOKEN" \
  -H "Origin: http://127.0.0.1:$PORT" -H "Content-Type: application/json" \
  -d "{\"key\": \"$KEY\", \"manager\": {\"new\": {\"client\": \"codex\", \"personality\": \"planner\"}},
       \"goal\": \"Document the new retry behaviour.\"}"

# A manual summary for the Claude session in agent-relay (what you'd type under Details → Edit).
CKEY=$(curl -s -H "X-Relay-Token: $TOKEN" "http://127.0.0.1:$PORT/api/sessions" \
      | python3 -c 'import json,sys; print(next(s["context_key"] for s in json.load(sys.stdin)["sessions"] if s["cwd"].endswith("/agent-relay") and s["client"]=="Claude"))')
curl -s -o /dev/null -X POST "http://127.0.0.1:$PORT/api/context" -H "X-Relay-Token: $TOKEN" \
  -H "Origin: http://127.0.0.1:$PORT" -H "Content-Type: application/json" \
  -d "{\"key\": \"$CKEY\", \"summary\": \"Implementing the dashboard and control refinements\"}"

echo "open: $URL"
echo "demo folder: $DEMO"
wait $SERVE
