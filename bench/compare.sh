#!/usr/bin/env bash
# Head-to-head on the same bench: bench/compare.sh TAG SET1 [SET2 ...]
# Runs our harness and the alternative one (ALT_PY/ALT_ENTRY) on every set, 3 workers each.
set -uo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
TAG=$1; shift
ALT_PY=${ALT_PY:-$HOME/Prog/hack2-alt/.venv/bin/python}
ALT_ENTRY=${ALT_ENTRY:-$HOME/Prog/hack2-alt/run_agent.py}
for SET in "$@"; do
  S=$(basename "$SET")
  echo "=== ours $S"
  WORKERS=3 "$ROOT/bench/run_local.sh" "$SET" "cmp_${TAG}_ours_$S" forward 0 2>&1 | grep -E "^(math|data|search|coding|TOTAL)|tokens spent|run_seconds"
  echo "=== alt $S"
  RUNH="$ROOT/bench/runs/cmp_${TAG}_alt_$S/home"
  WORKERS=3 PY="$ALT_PY" ENTRY="$ALT_ENTRY" "$ROOT/bench/run_local.sh" "$SET" "cmp_${TAG}_alt_$S" forward 0 \
    TMPDIR="$RUNH" PYTHONWARNINGS=ignore 2>&1 | grep -E "^(math|data|search|coding|TOTAL)|tokens spent|run_seconds"
done
