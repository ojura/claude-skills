#!/usr/bin/env bash
# Build the client and point your Claude Code config dir at this checkout.
#
# Idempotent: safe to re-run after editing anything here. Touches nothing but
# the two symlinks and the compiled client; your settings.json is only read.
set -euo pipefail

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLAUDE_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"

for tool in python3 cc; do
    command -v "$tool" >/dev/null || { echo "need $tool on PATH" >&2; exit 1; }
done
mkdir -p "$CLAUDE_DIR"

# The harness runs the client once per refresh, so it is C. The whole path
# costs ~3ms per render.
cc -O2 -o "$CLAUDE_DIR/statusline-client" "$SKILL_DIR/statusline-client.c"
echo "built $CLAUDE_DIR/statusline-client"

# The client and the daemon find the engine by path, so these two live in
# ~/.claude as links. Swap them in with an atomic rename, so a render landing
# mid-install never sees a missing file.
for f in statusline-render.py statusline-command.sh; do
    ln -sfn "$SKILL_DIR/$f" "$CLAUDE_DIR/.$f.newlink"
    mv -T "$CLAUDE_DIR/.$f.newlink" "$CLAUDE_DIR/$f"
    echo "linked $CLAUDE_DIR/$f"
done

# Also starts the renderer daemon, which is what serves every later render.
printf '\nsmoke test (this starts the renderer): '
printf '{"model":{"display_name":"Claude"},"context_window":{"used_percentage":42}}' \
    | "$CLAUDE_DIR/statusline-client"
printf '\n'

if grep -q 'statusline-client' "$CLAUDE_DIR/settings.json" 2>/dev/null; then
    echo "settings.json already points at the client."
else
    cat <<EOF

One step left. Add this to $CLAUDE_DIR/settings.json:

  "statusLine": {
    "type": "command",
    "command": "$CLAUDE_DIR/statusline-client",
    "refreshInterval": 1
  }

refreshInterval is what makes the countdowns tick.
EOF
fi
