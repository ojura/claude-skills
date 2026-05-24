#!/usr/bin/env bash
set -euo pipefail

# Fast, instrumented smoke test for this skill. This is for skill maintenance,
# not for normal user layout iterations. It verifies the two important paths:
# 1) local-only HTML renders through render_job.sh;
# 2) remote Google Fonts fail fast at font_resolution and produce bridge files,
#    with no fallback-font render.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# Probe a writable work root (matches render_job.sh logic) unless overridden.
if [ -z "${POLISHER_WORK_ROOT:-}" ]; then
  for candidate in /mnt/data "${TMPDIR:-/tmp}" "${HOME:-/tmp}"; do
    if [ -d "$candidate" ] && [ -w "$candidate" ]; then
      POLISHER_WORK_ROOT="$candidate"
      break
    fi
  done
fi
export POLISHER_WORK_ROOT
TEST_ROOT="$POLISHER_WORK_ROOT/polisher_skill_quick_test"
RENDER_WORK="$POLISHER_WORK_ROOT/render_work"
TIMINGS="$TEST_ROOT/test-timings.log"
mkdir -p "$TEST_ROOT"
rm -rf "$TEST_ROOT"/*
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
trap 'status=$?; if [ "$status" -ne 0 ]; then stage_fail "$status"; echo "quick smoke failed; timings: $TIMINGS" >&2; fi' EXIT

stage_start "write_local_fixture"
cat > "$TEST_ROOT/local.html" <<'HTML'
<!doctype html>
<html><head><meta charset="utf-8"><title>Local Smoke</title>
<style>
@page { size: A5; margin: 0; }
html, body { margin: 0; padding: 0; }
body { font-family: serif; background: white; }
.page { min-height: 210mm; padding: 18mm; background: #f7f2e9; }
h1 { margin: 0 0 8mm; font-size: 24pt; }
p { font-size: 12pt; line-height: 1.45; }
</style></head><body><div class="page"><h1>Local smoke</h1><p>This fixture uses only local/system fonts and no remote assets.</p></div></body></html>
HTML
stage_ok "write_local_fixture"

stage_start "render_local_fixture"
bash "$SCRIPT_DIR/render_job.sh" "$TEST_ROOT/local.html" polisher-quick-local --dpi=72 > "$TEST_ROOT/local-render.log" 2>&1
stage_ok "render_local_fixture"

stage_start "assert_local_outputs"
test -s "$RENDER_WORK/polisher-quick-local/output.pdf"
test -s "$RENDER_WORK/polisher-quick-local/contact.jpg"
test -s "$RENDER_WORK/polisher-quick-local/render-summary.txt"
grep -q "Pages:" "$RENDER_WORK/polisher-quick-local/render-summary.txt"
grep -q "info: media=print" "$RENDER_WORK/polisher-quick-local/timings.log"
stage_ok "assert_local_outputs"

stage_start "write_known_google_font_fixture"
cat > "$TEST_ROOT/known-google-font.html" <<'HTML'
<!doctype html>
<html><head><meta charset="utf-8"><title>Known Google Font Smoke</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Fraunces:ital,wght@0,300..900;1,300..900&family=Source+Serif+4:ital,opsz,wght@0,8..60,200..900;1,8..60,200..900&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
@page { size: A5; margin: 0; }
body { margin: 0; font-family: 'Source Serif 4', serif; }
.page { min-height: 210mm; padding: 18mm; background: #f7f2e9; }
h1 { font-family: 'Fraunces', serif; font-weight: 800; }
code { font-family: 'JetBrains Mono', monospace; }
</style></head><body><div class="page"><h1>Known Google font smoke</h1><p>This should resolve through deterministic Fontsource embedding. <code>mono</code></p></div></body></html>
HTML
stage_ok "write_known_google_font_fixture"

stage_start "resolve_known_google_fixture_fontsource"
python "$SCRIPT_DIR/resolve_fontsource_fonts.py" "$TEST_ROOT/known-google-font.html" "$TEST_ROOT/known-google-resolved.html" --font-dir "$TEST_ROOT/fonts" --cache-dir "$TEST_ROOT/fontsource-cache" --bridge-dir "$TEST_ROOT/font-bridge" --embed > "$TEST_ROOT/known-google-resolve.log" 2>&1
stage_ok "resolve_known_google_fixture_fontsource"

stage_start "assert_fontsource_outputs"
test -s "$TEST_ROOT/known-google-resolved.html"
test -s "$TEST_ROOT/font-bridge/fontsource-resolution.json"
grep -q "polisher-required-fonts" "$TEST_ROOT/known-google-resolved.html"
grep -q "data:font/woff2" "$TEST_ROOT/known-google-resolved.html"
python - <<PY
from pathlib import Path
text = Path('$TEST_ROOT/known-google-resolved.html').read_text(encoding='utf-8')
assert 'https://fonts.googleapis.com' not in text
assert 'https://fonts.gstatic.com' not in text
for name in ['Fraunces', 'Source Serif 4', 'JetBrains Mono']:
    assert name in text, name
PY
stage_ok "assert_fontsource_outputs"

stage_start "write_autodiscovery_fixture"
# Use a Google Font not in KNOWN_FAMILIES (Inter) to exercise the discovery path.
cat > "$TEST_ROOT/autodiscovery.html" <<'HTML'
<!doctype html>
<html><head><meta charset="utf-8"><title>Auto-Discovery Smoke</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;700&display=swap" rel="stylesheet">
<style>@page { size: A5; margin: 0; } body { font-family: 'Inter', sans-serif; }</style>
</head><body><p>This fixture uses Inter, which is not in the curated family list but is on Fontsource; discovery should resolve it.</p></body></html>
HTML
stage_ok "write_autodiscovery_fixture"

stage_start "resolve_autodiscovery_fixture"
python "$SCRIPT_DIR/resolve_fontsource_fonts.py" "$TEST_ROOT/autodiscovery.html" "$TEST_ROOT/autodiscovery-resolved.html" --font-dir "$TEST_ROOT/autodiscovery-fonts" --cache-dir "$TEST_ROOT/fontsource-cache" --bridge-dir "$TEST_ROOT/autodiscovery-bridge" --embed > "$TEST_ROOT/autodiscovery-resolve.log" 2>&1
test -s "$TEST_ROOT/autodiscovery-resolved.html"
grep -q "Inter" "$TEST_ROOT/autodiscovery-resolved.html"
grep -q "data:font/woff2" "$TEST_ROOT/autodiscovery-resolved.html"
test -s "$TEST_ROOT/autodiscovery-bridge/fontsource-resolution.json"
grep -q '"auto_discovered_families"' "$TEST_ROOT/autodiscovery-bridge/fontsource-resolution.json"
grep -q '"Inter"' "$TEST_ROOT/autodiscovery-bridge/fontsource-resolution.json"
stage_ok "resolve_autodiscovery_fixture"

stage_start "write_nonexistent_google_font_fixture"
cat > "$TEST_ROOT/nonexistent-google-font.html" <<'HTML'
<!doctype html>
<html><head><meta charset="utf-8"><title>Nonexistent Font Smoke</title>
<link href="https://fonts.googleapis.com/css2?family=Polisher+Nonexistent+Test+Font+ZYX99:wght@400;700&display=swap" rel="stylesheet">
<style>@page { size: A5; margin: 0; } body { font-family: 'Polisher Nonexistent Test Font ZYX99', serif; }</style>
</head><body><p>This fixture uses a fictitious family that should not exist as a Fontsource package, so resolution must stop rather than substitute.</p></body></html>
HTML
stage_ok "write_nonexistent_google_font_fixture"

stage_start "resolve_nonexistent_fixture_expect_stop"
set +e
python "$SCRIPT_DIR/resolve_fontsource_fonts.py" "$TEST_ROOT/nonexistent-google-font.html" "$TEST_ROOT/nonexistent-resolved.html" --font-dir "$TEST_ROOT/nonexistent-fonts" --cache-dir "$TEST_ROOT/fontsource-cache" --bridge-dir "$TEST_ROOT/nonexistent-bridge" --embed > "$TEST_ROOT/nonexistent-resolve.log" 2>&1
STATUS="$?"
set -e
if [ "$STATUS" -ne 86 ]; then
  echo "expected nonexistent-font resolver to exit 86, got $STATUS" >&2
  cat "$TEST_ROOT/nonexistent-resolve.log" >&2
  exit 1
fi
if [ -e "$TEST_ROOT/nonexistent-resolved.html" ]; then
  echo "nonexistent-font fixture produced resolved HTML; fallback/substitution path is not allowed" >&2
  exit 1
fi
test -s "$TEST_ROOT/nonexistent-bridge/FONTSOURCE_RESOLUTION_FAILED.md"
stage_ok "resolve_nonexistent_fixture_expect_stop"

log_line "[$(elapsed_from "$START_MS")s] done: test_skill_quick"
trap - EXIT

echo "quick smoke passed"
echo "timings: $TIMINGS"
