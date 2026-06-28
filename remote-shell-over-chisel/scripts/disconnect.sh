#!/bin/bash
# Tear down the chisel tunnel.
#   disconnect.sh           -> kill the tunnel; keep $STATE_DIR cache (binary, key, env)
#   disconnect.sh --purge   -> also wipe $STATE_DIR
#
# Reads the same config.sh as connect.sh to learn which process to kill and
# where the state dir is.

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)"
CONFIG="${CHISEL_REMOTE_CONFIG:-$SKILL_DIR/config.sh}"
[ -f "$CONFIG" ] && . "$CONFIG"

REMOTE_NAME="${REMOTE_NAME:-remote}"
STATE_DIR="${STATE_DIR:-$HOME/.chisel-remote}"

# Same identifying pattern connect.sh uses. Fall back to a broad match if the
# config is missing (so teardown still works without config.sh).
if [ -n "$CHISEL_SERVER" ]; then
  PGREP_PAT="chisel client.*${CHISEL_SERVER#*://}"
else
  PGREP_PAT="chisel client"
fi

if pkill -f "$PGREP_PAT" 2>/dev/null; then
  echo "[$REMOTE_NAME] tunnel down"
else
  echo "[$REMOTE_NAME] no tunnel running"
fi

if [ "$1" = "--purge" ]; then
  rm -rf "$STATE_DIR"
  echo "[$REMOTE_NAME] purged $STATE_DIR"
fi
