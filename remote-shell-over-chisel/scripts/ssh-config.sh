#!/usr/bin/env bash
# The ~/.ssh/config lines that route ssh through the remote machine while
# SSH_THROUGH_CHISEL is set, built from config.sh.
#
#   ssh-config.sh            print the lines; append them to ~/.ssh/config once:
#                              bash scripts/ssh-config.sh >> ~/.ssh/config
#   ssh-config.sh --check    if ssh picks them up, print "ssh goes through
#                            ALIAS" and exit 0; otherwise say why and exit 1

set -u

# shellcheck source=common.sh
. "$(dirname "$(readlink -f "$0")")/common.sh"
load_config || exit 1

proxy="$SKILL_DIR/scripts/ssh-through-remote-proxy"
# A config other than the default has to reach the proxy, which ssh starts
# with the environment of the shell that ran ssh.
proxy_cmd="\"$proxy\""
if [ "$CONFIG" != "$SKILL_DIR/config.sh" ]; then
  proxy_cmd="env CHISEL_REMOTE_CONFIG=\"$CONFIG\" $proxy_cmd"
fi

print_lines() {
  cat <<EOF
# remote-shell-over-chisel: while SSH_THROUGH_CHISEL is set, every ssh
# connection goes through $SSH_ALIAS. Turn it on in a shell with
#   source $SKILL_DIR/scripts/ssh-through-remote.sh
# These lines come from $SKILL_DIR/scripts/ssh-config.sh.
#
# Keep them after your other Host blocks. ssh uses the first ProxyCommand or
# ProxyJump it reads, so a host with a ProxyJump of its own keeps it. The
# separate ssh that connects to its jump host matches the last Match block, so
# only that first hop goes through $SSH_ALIAS.
#
# ssh to $SSH_ALIAS itself uses the shared master connection directly when it
# is running, which skips the handshake with $SSH_ALIAS. ControlMaster no
# keeps it from starting a master of its own; the proxy starts the only one.
Host $SSH_ALIAS
EOF
  [ -n "${SSH_HOSTNAME:-}" ] && echo "    HostName $SSH_HOSTNAME"
  cat <<EOF
    User $REMOTE_USER
    HostKeyAlias $SSH_ALIAS
Match originalhost $SSH_ALIAS exec "printenv SSH_THROUGH_CHISEL"
    ControlMaster no
    ControlPath "$SOCKET_DIR/master-%r.sock"
    ProxyCommand $proxy_cmd 127.0.0.1 $SSH_REMOTE_PORT
Match exec "printenv SSH_THROUGH_CHISEL"
    ProxyCommand $proxy_cmd %h %p
EOF
}

check() {
  local ok=1
  if ! SSH_THROUGH_CHISEL=1 ssh -G example.invalid 2>/dev/null |
       grep -qF "proxycommand $proxy_cmd %h %p"; then
    echo "[$REMOTE_NAME] ssh does not route other hosts through $proxy." >&2
    ok=0
  fi
  if ! SSH_THROUGH_CHISEL=1 ssh -G "$SSH_ALIAS" 2>/dev/null |
       grep -qxF "controlpath $SOCKET_DIR/master-$REMOTE_USER.sock"; then
    echo "[$REMOTE_NAME] ssh to $SSH_ALIAS does not use the shared connection's socket." >&2
    ok=0
  fi
  if [ "$ok" = 0 ]; then
    echo "[$REMOTE_NAME] The lines from $SKILL_DIR/scripts/ssh-config.sh are missing from" >&2
    echo "[$REMOTE_NAME] ~/.ssh/config, are out of date, or come after a block that sets" >&2
    echo "[$REMOTE_NAME] ProxyCommand, ProxyJump or ControlPath for every host." >&2
    return 1
  fi
  echo "ssh goes through $SSH_ALIAS"
}

case "${1:-}" in
  "") print_lines ;;
  --check) check ;;
  *) echo "usage: $(basename "$0") [--check]" >&2; exit 2 ;;
esac
