#!/bin/zsh
# usage: env-of-claude.sh <claude-pid> <command...>
# Runs the command with the launch environment of that claude process (/proc/<pid>/environ).
# Refuses a process with CLAUDECODE or CLAUDE_CODE_CHILD_SESSION set (a tool call or a teammate launch).
pid=$1; shift
src=/proc/$pid/environ
[[ -r $src ]] || { print -u2 "env-of-claude: cannot read $src"; exit 2; }
envs=(${(0)"$(<$src)"})
for e in $envs; do
  case $e in
    CLAUDECODE=*|CLAUDE_CODE_CHILD_SESSION=*) print -u2 "env-of-claude: $pid has ${e%%=*} set; a tool call or a teammate launch set it; use the lead's process"; exit 3 ;;
  esac
done
exec env -i "${envs[@]}" "$@"
