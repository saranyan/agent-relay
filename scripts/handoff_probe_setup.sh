#!/bin/bash
# supplemental run setup (scratch only).
#
# Every failed prerequisite exits non-zero BEFORE `relay init`, and the env file that the
# other windows source is removed first and written only when everything passed, so a
# failed setup can't be followed by later steps using stale or partial values.
#
#   bash scripts/handoff_probe_setup.sh && source ~/relay-runs/supplement.env
#
# Test hooks: RELAY_PROBE_RUNS_DIR (default ~/relay-runs), RELAY_PROBE_RELAY (default bin/relay).
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
RUNS="${RELAY_PROBE_RUNS_DIR:-$HOME/relay-runs}"
RELAY="${RELAY_PROBE_RELAY:-$HERE/bin/relay}"
ENV_FILE="$RUNS/supplement.env"
WANT_CLAUDE="2.1.296"
WANT_CODEX="codex-cli 0.162.1"

fail() { echo "STOP: $*" >&2; exit 1; }

mkdir -p "$RUNS"
rm -f "$ENV_FILE"  # never leave a previous run's values for later windows

claude_v="$(claude --version 2>/dev/null | awk '{print $1}')" || fail "can't run claude --version"
[ "$claude_v" = "$WANT_CLAUDE" ] || fail "Claude Code is '$claude_v', not the reviewed $WANT_CLAUDE"
codex_v="$(codex --version 2>/dev/null)" || fail "can't run codex --version"
[ "$codex_v" = "$WANT_CODEX" ] || fail "codex is '$codex_v', not the reviewed $WANT_CODEX"

RUN="$(mktemp -d "$RUNS/handoff.XXXXXX")" || fail "can't create a run folder"
RUN="$(cd "$RUN" && pwd -P)"
[ -z "$(ls -A "$RUN")" ] || fail "$RUN is not empty"

mkdir "$RUN/scratch"
echo "scratch for the hand-off probe" > "$RUN/scratch/README.md"
"$RELAY" init --root "$RUN/root" --participants '*' --max-turns 2 --turn-timeout 180

printf 'export RUN=%q SCRATCH=%q ROOT=%q\n' "$RUN" "$RUN/scratch" "$RUN/root" > "$ENV_FILE.tmp"
mv "$ENV_FILE.tmp" "$ENV_FILE"
echo "ready: $RUN (other windows: source $ENV_FILE)"
