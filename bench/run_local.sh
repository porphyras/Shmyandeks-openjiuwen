#!/usr/bin/env bash
# Fast local run without Docker (same harness code, venv python, cwd=/ to mimic read-only root).
#   bench/run_local.sh SET_DIR RUN_NAME [mock|forward] [BUDGET] [-- extra harness env as K=V ...]
# Env: WORKERS (default 2 = our share of the dev account's 3 concurrent requests), PY, ENTRY (another run_agent.py).
set -euo pipefail
SET=$(cd "$1" && pwd); NAME=$2; MODE=${3:-mock}; BUDGET=${4:-0}; shift $(( $# < 4 ? $# : 4 ))
ROOT=$(cd "$(dirname "$0")/.." && pwd)
RUN="$ROOT/bench/runs/$NAME"
PY=${PY:-$ROOT/.venv/bin/python}
rm -rf "$RUN"; mkdir -p "$RUN/home" "$RUN/code"
cp -R "$SET/tasks" "$RUN/tasks"
# Run from a snapshot so edits to the working copy during a run cannot mix versions.
if [ -z "${ENTRY:-}" ]; then
  cp "$ROOT/run_agent.py" "$RUN/code/" && cp -R "$ROOT/harness" "$RUN/code/harness"
  rm -rf "$RUN/code/harness/__pycache__"
  (cd "$ROOT" && git rev-parse --short HEAD 2>/dev/null; git -C "$ROOT" diff --stat 2>/dev/null | tail -1) > "$RUN/code/VERSION" || true
  ENTRY="$RUN/code/run_agent.py"
fi
PORT=$(python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1])')
STUB_ARGS=(--port "$PORT" --host 127.0.0.1 --mode "$MODE" --budget "$BUDGET" --stats "$RUN/stats.json")
[ "$MODE" = "mock" ] && [ -f "$SET/oracle_mock.json" ] && STUB_ARGS+=(--oracle "$SET/oracle_mock.json")
(
  if [ "$MODE" = "forward" ]; then
    set -a; . "$HOME/.config/hack2/llm.env"; set +a
    export UPSTREAM_BASE_URL="$LLM_BASE_URL" UPSTREAM_API_KEY="$LLM_API_KEY"
    unset LLM_BASE_URL LLM_API_KEY
  fi
  exec python3 "$ROOT/bench/stub_proxy.py" "${STUB_ARGS[@]}"
) > "$RUN/stub.log" 2>&1 &
STUB_PID=$!
trap 'kill $STUB_PID 2>/dev/null || true' EXIT
sleep 0.7
T0=$(date +%s)
(
  cd /
  mkdir -p "$RUN/out"; env -i PATH="$PATH" HOME="$RUN/home" OUT="$RUN/out" LLM_BASE_URL="http://127.0.0.1:$PORT/v1" LLM_API_KEY=dummy \
    HARNESS_TRACE="$RUN/trace.jsonl" HARNESS_WORKERS="${WORKERS:-2}" "$@" \
    "$PY" "$ENTRY" --tasks-dir "$RUN/tasks" > "$RUN/harness.log" 2>&1
) || true
echo "run_seconds=$(( $(date +%s) - T0 ))" | tee "$RUN/run.txt"
python3 "$ROOT/bench/grade.py" --set "$SET" --run "$RUN/tasks" --stats "$RUN/stats.json" --json "$RUN/grade.json" | tee "$RUN/grade.txt"
