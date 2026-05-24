#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -lt 2 ]; then
  echo "usage: iterate_layout.sh input.html output.pdf [render_dir] [--dpi=N]" >&2
  exit 2
fi

HTML="$1"
PDF="$2"
shift 2
RENDER_DIR="${1:-${PDF%.pdf}_renders}"
if [ "$#" -gt 0 ] && [[ "${1:-}" != --* ]]; then shift; fi
DPI=150
for arg in "$@"; do
  case "$arg" in
    --dpi=*) DPI="${arg#--dpi=}" ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

CONTACT="${PDF%.pdf}_contact.jpg"
SUMMARY="${PDF%.pdf}_summary.txt"
TIMINGS="${PDF%.pdf}_timings.log"
SOURCE_INPUT="${PDF%.pdf}_source-input.html"
RESOLVED_HTML="${PDF%.pdf}_resolved.html"
FONT_DIR="${PDF%.pdf}_fonts"
FONT_BRIDGE="${PDF%.pdf}_font_bridge"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
: > "$TIMINGS"

now_ms() { date +%s%3N; }
JOB_START_MS="$(now_ms)"
CURRENT_STAGE=""
CURRENT_STAGE_START_MS="$JOB_START_MS"
elapsed_from() {
  local start="$1"
  local now="$(now_ms)"
  awk -v s="$start" -v n="$now" 'BEGIN { printf "%.2f", (n-s)/1000 }'
}
log_line() { echo "$1" | tee -a "$TIMINGS"; }
stage_start() { CURRENT_STAGE="$1"; CURRENT_STAGE_START_MS="$(now_ms)"; log_line "[$(elapsed_from "$JOB_START_MS")s] start: $CURRENT_STAGE"; }
stage_ok() { log_line "[$(elapsed_from "$JOB_START_MS")s] ok: $1 ($(elapsed_from "$CURRENT_STAGE_START_MS")s)"; CURRENT_STAGE=""; }
stage_fail() { local status="$1"; if [ -n "$CURRENT_STAGE" ]; then log_line "[$(elapsed_from "$JOB_START_MS")s] fail: $CURRENT_STAGE status=$status after $(elapsed_from "$CURRENT_STAGE_START_MS")s"; fi; }
trap 'status=$?; if [ "$status" -ne 0 ]; then stage_fail "$status"; fi' EXIT

stage_start "deps_check"
if ! python -c "import fitz" >/dev/null 2>&1; then
  set +e
  pip install --quiet pymupdf --break-system-packages >/dev/null 2>&1
  PIP_STATUS=$?
  set -e
  if [ "$PIP_STATUS" -eq 0 ] && python -c "import fitz" >/dev/null 2>&1; then
    log_line "[$(elapsed_from "$JOB_START_MS")s] info: deps_check installed pymupdf"
  else
    log_line "[$(elapsed_from "$JOB_START_MS")s] info: deps_check pymupdf unavailable; raster will use pdftoppm, inspect will use pypdfium2"
  fi
else
  log_line "[$(elapsed_from "$JOB_START_MS")s] info: deps_check pymupdf already available"
fi
stage_ok "deps_check"

python "$SCRIPT_DIR/render_pipeline.py" \
  --input "$HTML" \
  --source-input "$SOURCE_INPUT" \
  --source "$RESOLVED_HTML" \
  --font-dir "$FONT_DIR" \
  --font-bridge "$FONT_BRIDGE" \
  --pdf "$PDF" \
  --renders "$RENDER_DIR" \
  --contact "$CONTACT" \
  --summary "$SUMMARY" \
  --timings "$TIMINGS" \
  --timing-origin-ms "$JOB_START_MS" \
  --dpi "$DPI" \
  --done-label "iterate_layout"
trap - EXIT
