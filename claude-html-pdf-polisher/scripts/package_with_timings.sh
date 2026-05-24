#!/usr/bin/env bash
set -euo pipefail

# Instrumented packaging helper for maintaining this skill. It avoids the
# unaccounted outer-loop timeout problem by logging each build/test/package stage.
# Usage:
#   scripts/package_with_timings.sh /mnt/data/out_dir [--quick-test]

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SKILL_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
# Out dir: default to a writable location matching render_job.sh probe.
if [ -z "${1:-}" ] || [ "${1:0:1}" = "-" ]; then
  if [ -z "${POLISHER_WORK_ROOT:-}" ]; then
    for candidate in /mnt/data "${TMPDIR:-/tmp}" "${HOME:-/tmp}"; do
      if [ -d "$candidate" ] && [ -w "$candidate" ]; then
        POLISHER_WORK_ROOT="$candidate"
        break
      fi
    done
  fi
  OUT_DIR="$POLISHER_WORK_ROOT/skill_dist"
else
  OUT_DIR="$1"
  shift
fi
RUN_QUICK=0
RUN_FULL=0
for arg in "$@"; do
  case "$arg" in
    --quick-test) RUN_QUICK=1 ;;
    --full-test) RUN_FULL=1 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

mkdir -p "$OUT_DIR"
TIMINGS="$OUT_DIR/package-timings.log"
: > "$TIMINGS"

now_ms() { date +%s%3N; }
START_MS="$(now_ms)"
CURRENT_STAGE=""
CURRENT_STAGE_START_MS="$START_MS"
elapsed_from() {
  local start="$1"
  local now="$(now_ms)"
  awk -v s="$start" -v n="$now" 'BEGIN { printf "%.2f", (n-s)/1000 }'
}
log_line() { echo "$1" | tee -a "$TIMINGS"; }
stage_start() {
  CURRENT_STAGE="$1"
  CURRENT_STAGE_START_MS="$(now_ms)"
  log_line "[$(elapsed_from "$START_MS")s] start: $CURRENT_STAGE"
}
stage_ok() {
  local name="$1"
  log_line "[$(elapsed_from "$START_MS")s] ok: $name ($(elapsed_from "$CURRENT_STAGE_START_MS")s)"
  CURRENT_STAGE=""
}
stage_fail() {
  local status="$1"
  if [ -n "$CURRENT_STAGE" ]; then
    log_line "[$(elapsed_from "$START_MS")s] fail: $CURRENT_STAGE status=$status after $(elapsed_from "$CURRENT_STAGE_START_MS")s"
  fi
}
trap 'status=$?; if [ "$status" -ne 0 ]; then stage_fail "$status"; echo "package failed; timings: $TIMINGS" >&2; fi' EXIT

if [ "$RUN_QUICK" -eq 1 ]; then
  stage_start "quick_smoke_test"
  bash "$SCRIPT_DIR/test_skill_quick.sh" > "$OUT_DIR/quick-smoke.log" 2>&1
  stage_ok "quick_smoke_test"
fi

if [ "$RUN_FULL" -eq 1 ]; then
  stage_start "full_smoke_test"
  bash "$SCRIPT_DIR/test_skill_full.sh" > "$OUT_DIR/full-smoke.log" 2>&1
  stage_ok "full_smoke_test"
fi

stage_start "package_skill"
# Discover skill-creator location: explicit env var first, then known
# vendor paths, then fall back to a built-in zip if none found. The built-in
# path skips skill-creator's validation step but produces a usable .skill.
SKILL_CREATOR=""
for candidate in \
  "${SKILL_CREATOR_PATH:-}" \
  "/home/oai/skills/skill-creator" \
  "/mnt/skills/examples/skill-creator" \
  "/mnt/skills/public/skill-creator"; do
  if [ -n "$candidate" ] && [ -f "$candidate/scripts/package_skill.py" ]; then
    SKILL_CREATOR="$candidate"
    break
  fi
done

if [ -n "$SKILL_CREATOR" ]; then
  log_line "[$(elapsed_from "$START_MS")s] info: using skill-creator at $SKILL_CREATOR"
  # Set PYTHONPATH so the script's `from scripts.quick_validate import ...` works
  # regardless of invocation form. This is the portable answer to the two-env
  # split: OAI's setup makes direct invocation work; Anthropic's needs -m or
  # PYTHONPATH. PYTHONPATH covers both.
  PYTHONPATH="$SKILL_CREATOR${PYTHONPATH:+:$PYTHONPATH}" \
    python "$SKILL_CREATOR/scripts/package_skill.py" "$SKILL_DIR" "$OUT_DIR" \
    > "$OUT_DIR/package.log" 2>&1
else
  log_line "[$(elapsed_from "$START_MS")s] info: skill-creator not found; using built-in zip (skips validation)"
  python - "$SKILL_DIR" "$OUT_DIR" > "$OUT_DIR/package.log" 2>&1 <<'PY'
import sys
import zipfile
import fnmatch
from pathlib import Path

skill_dir = Path(sys.argv[1]).resolve()
out_dir = Path(sys.argv[2]).resolve()
out_dir.mkdir(parents=True, exist_ok=True)

EXCLUDE_DIRS = {"__pycache__", "node_modules"}
EXCLUDE_GLOBS = {"*.pyc"}
EXCLUDE_FILES = {".DS_Store"}
ROOT_EXCLUDE_DIRS = {"evals"}

def should_exclude(rel: Path) -> bool:
    parts = rel.parts
    if any(p in EXCLUDE_DIRS for p in parts):
        return True
    if len(parts) > 1 and parts[1] in ROOT_EXCLUDE_DIRS:
        return True
    if rel.name in EXCLUDE_FILES:
        return True
    return any(fnmatch.fnmatch(rel.name, pat) for pat in EXCLUDE_GLOBS)

artifact = out_dir / f"{skill_dir.name}.skill"
with zipfile.ZipFile(artifact, "w", zipfile.ZIP_DEFLATED) as zf:
    for path in sorted(skill_dir.rglob("*")):
        rel = path.relative_to(skill_dir.parent)
        if should_exclude(rel):
            continue
        if path.is_file():
            zf.write(path, str(rel))
print(f"wrote {artifact}")
PY
fi
stage_ok "package_skill"

ARTIFACT=""
for candidate in "$OUT_DIR"/*.skill "$OUT_DIR"/skill.zip; do
  if [ -s "$candidate" ]; then
    ARTIFACT="$candidate"
    break
  fi
done
if [ -z "$ARTIFACT" ]; then
  echo "no skill artifact found in $OUT_DIR" >&2
  exit 1
fi
log_line "[$(elapsed_from "$START_MS")s] done: package_with_timings"
trap - EXIT

echo "package created: $ARTIFACT"
echo "timings: $TIMINGS"
