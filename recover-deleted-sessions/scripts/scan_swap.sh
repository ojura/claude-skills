#!/bin/bash
# Scrubbed for public release. Set the CONFIG constants (RECOVERY_DIR, DEV, etc.) below or via env before running.
set -o pipefail

# Root directory for all recovery artifacts (find via df / mount)
RECOVERY_DIR="${RECOVERY_DIR:-./recovery}"

# Swap file path; the affected swap file (find via `swapon --show`)
SWAP="${SWAP:-/swapfile}"

PATTERNS="${RECOVERY_DIR}/patterns.txt"
OUT="${RECOVERY_DIR}/swap_matches.txt"
DONE="${RECOVERY_DIR}/swap_done.txt"
( cat ~/password; echo ) | sudo -S -p '' bash -c "
  dd if='$SWAP' bs=4M status=progress 2>/dev/stderr |
    rg --null-data -aob -f '$PATTERNS' > '$OUT'
"
sz=$(stat -c%s "$OUT" 2>/dev/null || echo 0)
echo "DONE matches=${sz}B" > "$DONE"
