#!/bin/bash
# Scrubbed for public release. Set the CONFIG constants (RECOVERY_DIR, DEV, etc.) below or via env before running.
set -o pipefail

# Root directory for all recovery artifacts (find via df / mount)
RECOVERY_DIR="${RECOVERY_DIR:-./recovery}"

# The affected swap partition (find via `lsblk` / `df -T`); the swap variants point at your swap partition/file
DEV="${DEV:?set to your affected partition, find via lsblk/df}"

PATTERNS="${RECOVERY_DIR}/patterns.txt"
OUT="${RECOVERY_DIR}/swappart_matches.txt"
DONE="${RECOVERY_DIR}/swappart_done.txt"
( cat ~/password; echo ) | sudo -S -p '' bash -c "
  dd if='$DEV' bs=4M status=progress 2>/dev/stderr |
    rg --null-data -aob -f '$PATTERNS' > '$OUT'
"
sz=$(stat -c%s "$OUT" 2>/dev/null || echo 0)
echo "DONE matches=${sz}B" > "$DONE"
