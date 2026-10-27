#!/bin/zsh
# usage: restore-tab.sh <team-name> <proxy|direct> <claude args...>
# Runs claude in a terminal tab with CLAUDE_INTERNAL_ASSISTANT_TEAM_NAME set, through the
# CLIProxyAPI proxy for "proxy", records the launch in the log crash-inventory.py reads, and
# leaves an interactive shell in the tab when claude exits. Start it under env-of-claude.sh,
# which supplies the user's shell environment.
if (( $# < 3 )) || [[ $2 != proxy && $2 != direct ]]; then
  print -u2 "usage: restore-tab.sh <team-name> <proxy|direct> <claude args...>"
  exec zsh -i
fi
export CLAUDE_INTERNAL_ASSISTANT_TEAM_NAME=$1
if [[ $2 == proxy ]] && ! source ~/.local/bin/proxy-codex.sh; then
  print -u2 "restore-tab: the proxy did not start, so claude was not started"
  unset CLAUDE_INTERNAL_ASSISTANT_TEAM_NAME
  exec zsh -i
fi
if [[ $2 == direct && -n $ANTHROPIC_BASE_URL ]]; then
  print -u2 "restore-tab: direct was asked for, but ANTHROPIC_BASE_URL=$ANTHROPIC_BASE_URL came with the environment; claude was not started"
  unset CLAUDE_INTERNAL_ASSISTANT_TEAM_NAME
  exec zsh -i
fi
shift 2
log=${XDG_STATE_HOME:-$HOME/.local/state}/crash-resume/launches.log
mkdir -p ${log:h}
print -r -- "$(date '+%F %T') team=$CLAUDE_INTERNAL_ASSISTANT_TEAM_NAME proxy=${ANTHROPIC_BASE_URL:-none} cwd=$PWD claude ${(j: :)${(q)@}}" >> $log
claude "$@"
team=$CLAUDE_INTERNAL_ASSISTANT_TEAM_NAME
unset CLAUDE_INTERNAL_ASSISTANT_TEAM_NAME
print -r -- "To run it again: CLAUDE_INTERNAL_ASSISTANT_TEAM_NAME=$team claude ${(j: :)${(q)@}}"
exec zsh -i
