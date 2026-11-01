#!/bin/sh
# Run a command with the plain Claude process's launch environment. Resolver in
# agent-resume owns source validation, procfs reads and terminal-variable removal.
if [ "$#" -lt 2 ]; then
    printf '%s\n' 'usage: env-of-claude.sh <claude-pid> <command...>' >&2
    exit 2
fi
pid=$1
shift
exec /usr/bin/python3 -I -B "$(/usr/bin/dirname -- "$0")/../../team-necromancy/agent-resume" \
    --env-of "$pid" -- "$@"
