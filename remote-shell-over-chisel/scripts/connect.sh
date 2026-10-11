#!/bin/bash
# Establish (or verify) a chisel-over-HTTPS tunnel to a remote machine, then
# expose it to the local shell as $REMOTE_SSH (an ssh command) and $REMOTE_CDP
# (a cdp-daemon URL). All host/user/secret details come from config.sh. This
# script does not contain anything machine-specific.
#
# Usage:
#   source scripts/connect.sh                         # sets $REMOTE_SSH / $REMOTE_CDP in your shell
#   bash   scripts/connect.sh                         # brings the tunnel up; writes $STATE_DIR/env.sh
#   eval "$(bash scripts/connect.sh --print-env)"     # self-heal + set vars from any shell (incl. dash)
#
# Steady state, once the tunnel exists:  . ~/.chisel-remote/env.sh   (path = $STATE_DIR/env.sh)
#
# No `set -e`: this script is meant to be sourced, and a leaked errexit (or an
# exit on failure) would take the caller's shell down with it. Errors are
# checked explicitly and reported via _fail, which returns when sourced and
# exits when executed.

# --- arg parsing -------------------------------------------------------------
PRINT_ENV=0
QUIET=0
for arg in "$@"; do
  case "$arg" in
    --print-env) PRINT_ENV=1; QUIET=1 ;;   # emit only export lines on stdout
    --quiet|-q)  QUIET=1 ;;
  esac
done

# Sourced or executed? Decides return-vs-exit on failure.
_SOURCED=0
[ "${BASH_SOURCE[0]:-$0}" != "$0" ] && _SOURCED=1

# --- load config -------------------------------------------------------------
# shellcheck source=common.sh
. "$(dirname "$(readlink -f "${BASH_SOURCE[0]:-$0}")")/common.sh"
if ! load_config; then
  if [ "$_SOURCED" = 1 ]; then return 1; else exit 1; fi
fi
forward_ssh_settings

_log()  { [ "$QUIET" = 1 ] || echo "[$REMOTE_NAME] $*" >&2; }
_fail() { echo "[$REMOTE_NAME] ERROR: $*" >&2; if [ "$_SOURCED" = 1 ]; then return 1; else exit 1; fi; }

[ -n "${SSH_KEY:-}" ] || { _fail "SSH_KEY unset in config"; return 1 2>/dev/null; }

LOG="${CHISEL_REMOTE_LOG:-$STATE_DIR/chisel.log}"
ENV_FILE="$STATE_DIR/env.sh"

# Stage a tight 600 copy of the key so ssh never refuses it and we don't depend
# on the source key's perms or location being writable.
KEY="$STATE_DIR/id"
if ! cp -f "$SSH_KEY" "$KEY" 2>/dev/null; then _fail "cannot read SSH key at $SSH_KEY"; return 1 2>/dev/null; fi
chmod 600 "$KEY"

# What $REMOTE_SSH runs; see scripts/remote-ssh. A config other than the
# default has to reach it as well.
REMOTE_SSH_CMD="bash $SKILL_DIR/scripts/remote-ssh"
if [ "$CONFIG" != "$SKILL_DIR/config.sh" ]; then
  REMOTE_SSH_CMD="env CHISEL_REMOTE_CONFIG=$CONFIG $REMOTE_SSH_CMD"
fi

# Forward specs.
FORWARDS=( "${SSH_LOCAL_PORT}:127.0.0.1:${SSH_REMOTE_PORT}" )
[ -n "$CDP_LOCAL_PORT" ] && FORWARDS+=( "${CDP_LOCAL_PORT}:127.0.0.1:${CDP_REMOTE_PORT}" )

# --- helpers -----------------------------------------------------------------

# True liveness: actually open an SSH connection through the forward. This
# catches the stale-forward case where the chisel process is alive but the
# tunnel no longer carries traffic. ControlPath=none keeps the shared master
# out of it, so a master that is still running cannot hide a dead forward.
_tunnel_healthy() {
  env -u SSH_THROUGH_CHISEL ssh "${FORWARD_SSH_OPTS[@]}" -o ControlPath=none \
    -o ConnectTimeout=5 "$FORWARD_DEST" true >/dev/null 2>&1
}

_ensure_ssh() {
  command -v ssh >/dev/null 2>&1 && return 0
  _log "installing openssh-client..."
  apt-get update -qq 2>/dev/null || true
  apt-get install -y -qq openssh-client >/dev/null 2>&1 || { _fail "openssh-client install failed"; return 1; }
}

# Ends the shared master, which connects through the tunnel, and then the
# tunnel. SIGTERM makes chisel close its connections and exit.
_stop_tunnel() {
  local pids i
  master_stop "$FORWARD_SOCK"
  pids=$(tunnel_pids)
  [ -n "$pids" ] || return 0
  # shellcheck disable=SC2086
  kill $pids 2>/dev/null
  for i in $(seq 1 20); do
    [ -n "$(tunnel_pids)" ] || return 0
    sleep 0.25
  done
  _log "chisel tunnel did not exit after SIGTERM (pids: $(tunnel_pids | tr '\n' ' '))"
  return 1
}

_start_tunnel() {
  _stop_tunnel || return 1
  : > "$LOG"
  # AUTH env keeps the token out of argv / ps output.
  AUTH="$CHISEL_TOKEN" \
    setsid nohup "$CHISEL_BIN" client "$CHISEL_SERVER" "${FORWARDS[@]}" \
    </dev/null >"$LOG" 2>&1 &
  local i
  for i in $(seq 1 15); do
    grep -q "Connected" "$LOG" 2>/dev/null && return 0
    sleep 1
  done
  return 1
}

# --- main --------------------------------------------------------------------
_ensure_ssh    || return 1 2>/dev/null
ensure_chisel  || { _fail "no chisel binary at $CHISEL_BIN"; return 1 2>/dev/null; }

_cdp_note=""
[ -n "$CDP_LOCAL_PORT" ] && _cdp_note=", cdp:$CDP_LOCAL_PORT"

if _tunnel_healthy; then
  _log "tunnel healthy (ssh:$SSH_LOCAL_PORT$_cdp_note)"
else
  if [ -n "$(tunnel_pids)" ]; then _log "tunnel process alive but not passing traffic; restarting"; fi
  if ! _start_tunnel; then
    _log "tunnel failed to come up. Log:"; cat "$LOG" >&2 2>/dev/null
    _fail "could not establish tunnel"; return 1 2>/dev/null
  fi
  if ! _tunnel_healthy; then
    _fail "tunnel up but SSH probe failed (key in authorized_keys? sshd up on remote?)"; return 1 2>/dev/null
  fi
  _log "tunnel up (ssh:$SSH_LOCAL_PORT$_cdp_note)"
fi

# Start the shared connection now, so the first $REMOTE_SSH call does not wait
# for it. remote-ssh starts it again whenever it has exited.
if ! master_ensure "$FORWARD_SOCK" "$FORWARD_DEST" "${FORWARD_SSH_OPTS[@]}"; then
  _log "the shared SSH connection did not start; each \$REMOTE_SSH call will connect on its own (see $MASTER_LOG)"
fi

# POSIX-sourceable env file: any shell (incl. dash) can `. $STATE_DIR/env.sh`.
{
  echo "export REMOTE_SSH='$REMOTE_SSH_CMD'"
  [ -n "$CDP_LOCAL_PORT" ] && echo "export REMOTE_CDP='http://127.0.0.1:$CDP_LOCAL_PORT'"
} > "$ENV_FILE"

export REMOTE_SSH="$REMOTE_SSH_CMD"
[ -n "$CDP_LOCAL_PORT" ] && export REMOTE_CDP="http://127.0.0.1:$CDP_LOCAL_PORT"

if [ "$PRINT_ENV" = 1 ]; then
  echo "export REMOTE_SSH='$REMOTE_SSH_CMD'"
  [ -n "$CDP_LOCAL_PORT" ] && echo "export REMOTE_CDP='http://127.0.0.1:$CDP_LOCAL_PORT'"
fi
