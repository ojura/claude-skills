#!/usr/bin/env bash
set -euo pipefail

# Optional full regression for maintenance. This is intentionally not run by
# package_with_timings.sh unless a human asks for full regression.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# Probe writable work root unless overridden (matches render_job.sh logic).
if [ -z "${POLISHER_WORK_ROOT:-}" ]; then
  for candidate in /mnt/data "${TMPDIR:-/tmp}" "${HOME:-/tmp}"; do
    if [ -d "$candidate" ] && [ -w "$candidate" ]; then
      POLISHER_WORK_ROOT="$candidate"
      break
    fi
  done
fi
export POLISHER_WORK_ROOT
TEST_ROOT="$POLISHER_WORK_ROOT/polisher_skill_full_test"
RENDER_WORK="$POLISHER_WORK_ROOT/render_work"
rm -rf "$TEST_ROOT"
mkdir -p "$TEST_ROOT"

cat > "$TEST_ROOT/known-google-font.html" <<'HTML'
<!doctype html>
<html><head><meta charset="utf-8"><title>Known Google Font Full Smoke</title>
<link href="https://fonts.googleapis.com/css2?family=Fraunces:ital,wght@0,300..900;1,300..900&family=Source+Serif+4:ital,opsz,wght@0,8..60,200..900;1,8..60,200..900&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
@page { size: A5; margin: 0; }
body { margin: 0; font-family: 'Source Serif 4', serif; }
.page { min-height: 210mm; padding: 18mm; background: #f7f2e9; }
h1 { font-family: 'Fraunces', serif; font-weight: 800; }
code { font-family: 'JetBrains Mono', monospace; }
</style></head><body><div class="page"><h1>Known Google font full smoke</h1><p>This should render with requested embedded families. <code>mono</code></p></div></body></html>
HTML

bash "$SCRIPT_DIR/render_job.sh" "$TEST_ROOT/known-google-font.html" polisher-full-known-fonts --dpi=72
pdffonts "$RENDER_WORK/polisher-full-known-fonts/output.pdf" > "$TEST_ROOT/pdffonts.txt"
grep -q "Fraunces" "$TEST_ROOT/pdffonts.txt"
grep -q "SourceSerif4" "$TEST_ROOT/pdffonts.txt"
grep -q "JetBrainsMono" "$TEST_ROOT/pdffonts.txt"
grep -q "info: media=print" "$RENDER_WORK/polisher-full-known-fonts/timings.log"

echo "full smoke passed"
echo "pdf: $RENDER_WORK/polisher-full-known-fonts/output.pdf"
echo "timings: $RENDER_WORK/polisher-full-known-fonts/timings.log"
