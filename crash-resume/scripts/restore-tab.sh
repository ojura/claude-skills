#!/bin/zsh -i
# usage: restore-tab.sh <team-name> <proxy|direct> <claude args...>
# Runs claude in a terminal tab with CLAUDE_INTERNAL_ASSISTANT_TEAM_NAME set, through the
# CLIProxyAPI proxy when asked, records the launch in the log crash-inventory.py reads, and
# leaves a shell in the tab when claude exits.
export CLAUDE_INTERNAL_ASSISTANT_TEAM_NAME=$1
if [[ $2 == proxy ]] && ! source ~/.local/bin/proxy-codex.sh; then
  print -u2 "restore-tab: the proxy did not start, so claude was not started"
  exec zsh
fi
shift 2
log=${XDG_STATE_HOME:-$HOME/.local/state}/crash-resume/launches.log
mkdir -p ${log:h}
print -r -- "$(date '+%F %T') team=$CLAUDE_INTERNAL_ASSISTANT_TEAM_NAME proxy=${ANTHROPIC_BASE_URL:-none} cwd=$PWD claude ${(j: :)${(q)@}}" >> $log
claude "$@"
exec zsh
