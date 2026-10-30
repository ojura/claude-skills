#!/bin/zsh
# usage: with-proxy.sh <command...>
# Sources the CLIProxyAPI proxy script, which starts the shared proxy when it is down and
# exports its address and token, then runs the command. Start it under env-of-claude.sh, for
# example to resume a teammate on a GPT model that does not have a live lead to inherit from.
source ~/.local/bin/proxy-codex.sh || exit 1
exec "$@"
