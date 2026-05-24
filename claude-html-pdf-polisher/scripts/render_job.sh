#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -lt 2 ]; then
  echo "usage: render_job.sh input.html job-slug [--large-contact] [--dpi N]" >&2
  exit 2
fi

INPUT="$1"
JOB="$2"
shift 2
LARGE_CONTACT=0
DPI=150
for arg in "$@"; do
  case "$arg" in
    --large-contact) LARGE_CONTACT=1 ;;
    --dpi) echo "--dpi requires a value; use --dpi=N" >&2; exit 2 ;;
    --dpi=*) DPI="${arg#--dpi=}" ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

case "$JOB" in
  *[!A-Za-z0-9._-]*|"" ) echo "job-slug must contain only letters, numbers, dot, underscore, or hyphen" >&2; exit 2 ;;
esac

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ -z "${POLISHER_WORK_ROOT:-}" ]; then
  for candidate in /mnt/data "${TMPDIR:-/tmp}" "${HOME:-/tmp}"; do
    if [ -d "$candidate" ] && [ -w "$candidate" ]; then
      POLISHER_WORK_ROOT="$candidate"
      break
    fi
  done
fi
WORK_ROOT="$POLISHER_WORK_ROOT/render_work"
WORK="$WORK_ROOT/$JOB"
SOURCE_INPUT="$WORK/source-input.html"
SOURCE="$WORK/source.html"
FONT_DIR="$WORK/fonts"
FONT_BRIDGE="$WORK/font-bridge"
PDF="$WORK/output.pdf"
RENDERS="$WORK/renders"
CONTACT="$WORK/contact.jpg"
LARGE="$WORK/contact-large.jpg"
SUMMARY="$WORK/render-summary.txt"
TIMINGS="$WORK/timings.log"

if [ ! -f "$INPUT" ]; then
  echo "input HTML not found: $INPUT" >&2
  exit 2
fi

mkdir -p "$WORK" "$FONT_DIR" "$FONT_BRIDGE"
rm -f "$PDF" "$CONTACT" "$LARGE" "$SUMMARY" "$TIMINGS"
rm -rf "$RENDERS"
mkdir -p "$RENDERS"
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

PIPELINE_ARGS=(
  --input "$INPUT"
  --source-input "$SOURCE_INPUT"
  --source "$SOURCE"
  --font-dir "$FONT_DIR"
  --font-bridge "$FONT_BRIDGE"
  --pdf "$PDF"
  --renders "$RENDERS"
  --contact "$CONTACT"
  --summary "$SUMMARY"
  --timings "$TIMINGS"
  --timing-origin-ms "$JOB_START_MS"
  --dpi "$DPI"
  --done-label "render_job"
)
if [ "$LARGE_CONTACT" -eq 1 ]; then
  PIPELINE_ARGS+=(--large-contact "$LARGE")
fi
python "$SCRIPT_DIR/render_pipeline.py" "${PIPELINE_ARGS[@]}"
trap - EXIT

echo ""
echo "artifacts:"
echo "  work dir: $WORK"
echo "  source: $SOURCE"
echo "  pdf: $PDF"
echo "  renders: $RENDERS"
echo "  contact: $CONTACT"
if [ "$LARGE_CONTACT" -eq 1 ]; then echo "  large contact: $LARGE"; fi
echo "  summary: $SUMMARY"
echo "  timings: $TIMINGS"
