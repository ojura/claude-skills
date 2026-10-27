#!/bin/zsh
# usage: env-of-claude.sh <claude-pid> <command...>
# Runs the command with the launch environment of that claude process (/proc/<pid>/environ):
# the environment of the shell it was started from. Refuses a process that is not claude, and
# a claude process that a tool call or a teammate launch started (they add the variables below).
# Drops TMUX and TMUX_PANE, so programs it starts do not take themselves to be inside the tmux
# session the agent may run in.
pid=$1; shift
src=/proc/$pid/environ
[[ -r $src && $(readlink /proc/$pid/exe) == */claude/versions/* ]] || {
  print -u2 "env-of-claude: $pid is not a readable claude process"; exit 2; }
envs=(${(0)"$(<$src)"})
for e in $envs; do
  case ${e%%=*} in
    CLAUDECODE|CLAUDE_CODE_CHILD_SESSION|CLAUDE_PID|CLAUDE_CODE_SESSION_ID|CLAUDE_CODE_MESSAGING_SOCKET|\
    CLAUDE_CODE_MESSAGING_TOKEN|AI_AGENT|CLAUDE_CODE_ENTRYPOINT|CLAUDE_EFFORT)
      print -u2 "env-of-claude: $pid has ${e%%=*} set, so a tool call or a teammate launch started it; use the lead's process"
      exit 3 ;;
  esac
done
envs=(${envs:#TMUX=*})
envs=(${envs:#TMUX_PANE=*})
exec env -i "${envs[@]}" "$@"
