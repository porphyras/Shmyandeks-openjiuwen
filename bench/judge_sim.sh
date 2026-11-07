#!/usr/bin/env bash
# Local replica of the judge run.
#   bench/judge_sim.sh SET_DIR RUN_NAME [mock|forward] [BUDGET] [extra stub args...]
# SET_DIR is a bench set (tasks/, keys.json, oracle_mock.json). The harness container is
# started with exactly the judge's flags; the stub proxy runs in the same network.
set -euo pipefail
SET=$(cd "$1" && pwd); NAME=$2; MODE=${3:-mock}; BUDGET=${4:-0}; shift $(( $# < 4 ? $# : 4 ))
ROOT=$(cd "$(dirname "$0")/.." && pwd)
RUN="$ROOT/bench/runs/$NAME"
rm -rf "$RUN"; mkdir -p "$RUN"
cp -R "$SET/tasks" "$RUN/tasks"
[ "${OPEN_PERMS:-0}" = "1" ] && chmod -R a+rwX "$RUN/tasks"
docker network inspect hack-net >/dev/null 2>&1 || docker network create --internal hack-net >/dev/null
STUB="stub-$NAME"
docker rm -f "$STUB" >/dev/null 2>&1 || true
STUB_ARGS=(--mode "$MODE" --budget "$BUDGET" --stats /run/stats.json "$@")
[ "$MODE" = "mock" ] && [ -f "$SET/oracle_mock.json" ] && STUB_ARGS+=(--oracle /set/oracle_mock.json)
ENVS=()
if [ "$MODE" = "forward" ]; then
  set -a; . "$HOME/.config/hack2/llm.env"; set +a
  export UPSTREAM_BASE_URL="$LLM_BASE_URL" UPSTREAM_API_KEY="$LLM_API_KEY"
  ENVS=(-e UPSTREAM_BASE_URL -e UPSTREAM_API_KEY)
fi
docker run -d --rm --name "$STUB" --network hack-net ${ENVS[@]+"${ENVS[@]}"} \
  -v "$ROOT/bench:/bench:ro" -v "$SET:/set:ro" -v "$RUN:/run" \
  "${STUB_IMAGE:-python:3.12.13-slim-bookworm}" python /bench/stub_proxy.py "${STUB_ARGS[@]}" >/dev/null
[ "$MODE" = "forward" ] && docker network connect bridge "$STUB"
unset LLM_BASE_URL LLM_API_KEY UPSTREAM_BASE_URL UPSTREAM_API_KEY
sleep 1

# ---- judge command (verbatim apart from the build context) ----
export TASKS="$RUN/tasks" OUT="$RUN/out" TEAM="$NAME"
export LLM_BASE_URL="http://$STUB:8000/v1" LLM_API_KEY="dummy-key"
mkdir -p "$OUT" && chmod 777 "$OUT"
cd "$ROOT"
B0=$(date +%s)
docker build -q -t "hack:latest" . >/dev/null
B1=$(date +%s)
echo "build_seconds=$((B1-B0))" | tee "$RUN/build.txt"
docker images --format "{{.Repository}}:{{.Tag}} -> {{.Size}}" | grep '^hack:latest' | tee -a "$RUN/build.txt"
R0=$(date +%s)
set +e
timeout -k 1000s 1h docker run --rm --name "run-${TEAM}" \
  --network hack-net \
  -e LLM_BASE_URL -e LLM_API_KEY \
  -v "$TASKS:/data/tasks:rw" \
  --read-only --tmpfs /tmp:rw,size=2g -e HOME=/tmp \
  --cap-drop ALL --security-opt no-new-privileges \
  --cpus 8 --memory 16g --pids-limit 512 \
  --workdir /app --entrypoint python \
  "hack:latest" \
  /app/run_agent.py --tasks-dir /data/tasks > "$RUN/harness.log" 2>&1
RC=$?
set -e
R1=$(date +%s)
echo "run_rc=$RC run_seconds=$((R1-R0))" | tee -a "$RUN/build.txt"
docker stop "$STUB" >/dev/null 2>&1 || true
python3 "$ROOT/bench/grade.py" --set "$SET" --run "$TASKS" --stats "$RUN/stats.json" --json "$RUN/grade.json" | tee "$RUN/grade.txt"
