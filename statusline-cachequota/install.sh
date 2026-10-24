#!/usr/bin/env bash
# Build the client and point your Claude Code config dir at this checkout.
#
# Idempotent: safe to re-run after editing anything here. Touches nothing but
# the renderer links, the plugin link and the compiled client; settings are read.
set -euo pipefail

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLAUDE_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"

for tool in python3 cc; do
    command -v "$tool" >/dev/null || { echo "need $tool on PATH" >&2; exit 1; }
done
mkdir -p "$CLAUDE_DIR"

# Claude Code 2.1.309 auto-loads plugins from the skills directory. Refuse to
# replace a separate checkout or a real directory owned by the user.
mkdir -p "$CLAUDE_DIR/skills"
PLUGIN_LINK="$CLAUDE_DIR/skills/statusline-cachequota"
if [[ -e "$PLUGIN_LINK" || -L "$PLUGIN_LINK" ]]; then
    if [[ "$(readlink -f "$PLUGIN_LINK")" != "$SKILL_DIR" ]]; then
        echo "already exists and points elsewhere: $PLUGIN_LINK" >&2
        exit 1
    fi
fi

# The harness runs the client once per refresh, so it is C. The whole path
# costs ~3ms per render.
cc -O2 -o "$CLAUDE_DIR/.statusline-client.new" "$SKILL_DIR/statusline-client.c"
mv -f "$CLAUDE_DIR/.statusline-client.new" "$CLAUDE_DIR/statusline-client"
echo "built $CLAUDE_DIR/statusline-client"

# The client and the daemon find the engine by path, so these two live in
# ~/.claude as links. Swap them in with an atomic rename, so a render landing
# mid-install never sees a missing file.
for f in statusline-render.py statusline-command.sh; do
    ln -sfn "$SKILL_DIR/$f" "$CLAUDE_DIR/.$f.newlink"
    mv -T "$CLAUDE_DIR/.$f.newlink" "$CLAUDE_DIR/$f"
    echo "linked $CLAUDE_DIR/$f"
done

# Make the observer discoverable only after its client and renderer are ready.
if [[ ! -e "$PLUGIN_LINK" && ! -L "$PLUGIN_LINK" ]]; then
    ln -s "$SKILL_DIR" "$PLUGIN_LINK"
fi
echo "linked plugin $PLUGIN_LINK"

# Also starts the renderer daemon, which is what serves every later render.
printf '\nsmoke test (this starts the renderer): '
printf '{"model":{"display_name":"Claude"},"context_window":{"used_percentage":42}}' \
    | "$CLAUDE_DIR/statusline-client"
printf '\n'
echo "In an existing Claude session, run /reload-plugins to load the selected-agent observer."

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
