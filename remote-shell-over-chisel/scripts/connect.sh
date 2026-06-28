#!/bin/bash
# Establish (or verify) a chisel-over-HTTPS tunnel to a remote machine, then
# expose it to the local shell as $REMOTE_SSH (an ssh command) and $REMOTE_CDP
# (a cdp-daemon URL). All host/user/secret details come from config.sh. This
# script contains nothing machine-specific.
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

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)"   # repo dir (scripts/..)

# --- load config -------------------------------------------------------------
CONFIG="${CHISEL_REMOTE_CONFIG:-$SKILL_DIR/config.sh}"
if [ ! -f "$CONFIG" ]; then
  echo "[chisel-remote] ERROR: no config at $CONFIG" >&2
  echo "[chisel-remote] copy config.sh.example to config.sh and fill it in (see README.md)." >&2
  if [ "$_SOURCED" = 1 ]; then return 1; else exit 1; fi
fi
# shellcheck disable=SC1090
. "$CONFIG"

# Defaults for anything the config didn't set.
REMOTE_NAME="${REMOTE_NAME:-remote}"
SSH_LOCAL_PORT="${SSH_LOCAL_PORT:-2222}"
SSH_REMOTE_PORT="${SSH_REMOTE_PORT:-22}"
CDP_LOCAL_PORT="${CDP_LOCAL_PORT:-7799}"
CDP_REMOTE_PORT="${CDP_REMOTE_PORT:-7799}"
STATE_DIR="${STATE_DIR:-$HOME/.chisel-remote}"
CHISEL_VERSION="${CHISEL_VERSION:-1.10.1}"

_log()  { [ "$QUIET" = 1 ] || echo "[$REMOTE_NAME] $*" >&2; }
_fail() { echo "[$REMOTE_NAME] ERROR: $*" >&2; if [ "$_SOURCED" = 1 ]; then return 1; else exit 1; fi; }

# Validate required config.
[ -n "$CHISEL_SERVER" ] || { _fail "CHISEL_SERVER unset in config"; return 1 2>/dev/null; }
[ -n "$REMOTE_USER" ]   || { _fail "REMOTE_USER unset in config";   return 1 2>/dev/null; }
[ -n "$SSH_KEY" ]       || { _fail "SSH_KEY unset in config";       return 1 2>/dev/null; }

# Chisel auth token: inline CHISEL_TOKEN, or read from CHISEL_TOKEN_FILE.
if [ -z "$CHISEL_TOKEN" ] && [ -n "$CHISEL_TOKEN_FILE" ] && [ -f "$CHISEL_TOKEN_FILE" ]; then
  CHISEL_TOKEN="$(cat "$CHISEL_TOKEN_FILE")"
fi
[ -n "$CHISEL_TOKEN" ] || { _fail "no chisel token (set CHISEL_TOKEN or CHISEL_TOKEN_FILE in config)"; return 1 2>/dev/null; }

# --- persistent state dir ----------------------------------------------------
# On the box's own disk so the chisel binary, staged key, and env file survive
# across sessions. 700 because it holds a copy of the private key.
mkdir -p "$STATE_DIR" 2>/dev/null
chmod 700 "$STATE_DIR" 2>/dev/null

CHISEL_BIN="${CHISEL_BIN:-$STATE_DIR/chisel}"
LOG="${CHISEL_REMOTE_LOG:-$STATE_DIR/chisel.log}"
ENV_FILE="$STATE_DIR/env.sh"
KNOWN_HOSTS="$STATE_DIR/known_hosts"

# Stage a tight 600 copy of the key so ssh never refuses it and we don't depend
# on the source key's perms or location being writable.
KEY="$STATE_DIR/id"
if ! cp -f "$SSH_KEY" "$KEY" 2>/dev/null; then _fail "cannot read SSH key at $SSH_KEY"; return 1 2>/dev/null; fi
chmod 600 "$KEY"

# Canonical ssh invocation. ConnectTimeout bounds a hung handshake; keepalives
# catch a mid-session drop within ~90s.
REMOTE_SSH_CMD="ssh -p $SSH_LOCAL_PORT -i $KEY -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=$KNOWN_HOSTS -o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=30 -o ServerAliveCountMax=3 $REMOTE_USER@127.0.0.1"

# Pattern that identifies *our* chisel client process (host/path part of the URL).
PGREP_PAT="chisel client.*${CHISEL_SERVER#*://}"

# Forward specs.
FORWARDS=( "${SSH_LOCAL_PORT}:127.0.0.1:${SSH_REMOTE_PORT}" )
[ -n "$CDP_LOCAL_PORT" ] && FORWARDS+=( "${CDP_LOCAL_PORT}:127.0.0.1:${CDP_REMOTE_PORT}" )

# --- helpers -----------------------------------------------------------------
_tunnel_proc()    { pgrep -f "$PGREP_PAT" >/dev/null 2>&1; }

# True liveness: actually open an SSH channel. Catches the stale-forward case
# where the chisel process is alive but the tunnel no longer carries traffic.
_tunnel_healthy() { $REMOTE_SSH_CMD -o ConnectTimeout=5 true >/dev/null 2>&1; }

_ensure_chisel() {
  [ -x "$CHISEL_BIN" ] && return 0
  _log "fetching chisel v$CHISEL_VERSION..."
  local url="https://github.com/jpillora/chisel/releases/download/v${CHISEL_VERSION}/chisel_${CHISEL_VERSION}_linux_amd64.gz"
  if ! curl -fsSL -o "$STATE_DIR/chisel.gz" "$url"; then _fail "chisel download failed ($url)"; return 1; fi
  gunzip -f "$STATE_DIR/chisel.gz" && chmod +x "$STATE_DIR/chisel" || { _fail "chisel unpack failed"; return 1; }
  CHISEL_BIN="$STATE_DIR/chisel"
}

_ensure_ssh() {
  command -v ssh >/dev/null 2>&1 && return 0
  _log "installing openssh-client..."
  apt-get update -qq 2>/dev/null || true
  apt-get install -y -qq openssh-client >/dev/null 2>&1 || { _fail "openssh-client install failed"; return 1; }
}

_start_tunnel() {
  pkill -f "$PGREP_PAT" 2>/dev/null      # drop any stale process first
  : > "$LOG"
  # AUTH env keeps the token out of argv / ps output.
  AUTH="$CHISEL_TOKEN" \
    setsid nohup "$CHISEL_BIN" client "$CHISEL_SERVER" "${FORWARDS[@]}" \
    </dev/null >"$LOG" 2>&1 &
  unset AUTH
  local i
  for i in $(seq 1 15); do
    grep -q "Connected" "$LOG" 2>/dev/null && return 0
    sleep 1
  done
  return 1
}

# --- main --------------------------------------------------------------------
_ensure_ssh    || return 1 2>/dev/null
_ensure_chisel || return 1 2>/dev/null

_cdp_note=""
[ -n "$CDP_LOCAL_PORT" ] && _cdp_note=", cdp:$CDP_LOCAL_PORT"

if _tunnel_healthy; then
  _log "tunnel healthy (ssh:$SSH_LOCAL_PORT$_cdp_note)"
else
  if _tunnel_proc; then _log "tunnel process alive but not passing traffic; restarting"; fi
  if ! _start_tunnel; then
    _log "tunnel failed to come up. Log:"; cat "$LOG" >&2 2>/dev/null
    _fail "could not establish tunnel"; return 1 2>/dev/null
  fi
  if ! _tunnel_healthy; then
    _fail "tunnel up but SSH probe failed (key in authorized_keys? sshd up on remote?)"; return 1 2>/dev/null
  fi
  _log "tunnel up (ssh:$SSH_LOCAL_PORT$_cdp_note)"
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
