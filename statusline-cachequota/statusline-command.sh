#!/usr/bin/env bash
# Compatibility entry point. settings.json should invoke
# ~/.claude/statusline-client directly; this keeps working for anyone whose
# settings still name the old shell script.
#
# All the rendering lives in statusline-render.py, fronted by the compiled
# client: one resident renderer serves every session over
# ~/.claude/statusline.sock, the client spawns it on demand, and it falls back
# to a one-shot render if the socket cannot be reached.
exec "${CLAUDE_CONFIG_DIR:-$HOME/.claude}/statusline-client"
