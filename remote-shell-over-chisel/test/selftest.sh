#!/usr/bin/env bash
# Tests both modes against the remote machine in config.sh, over the network.
#
#   test/selftest.sh [--slow]
#
# The workstation tests run when ssh picks up the lines from ssh-config.sh,
# and the sandbox tests run when the config sets SSH_KEY. CHISEL_REMOTE_CONFIG
# selects another config, as for the scripts. --slow adds a test that freezes
# the chisel client of a shared connection, which takes up to a minute and a
# half.
#
# The tests end shared connections, and ending one ends every ssh session on
# it, so the script refuses to start while one of this config's shared
# connections or its tunnel is up.

set -u

# shellcheck source=../scripts/common.sh
. "$(dirname "$(readlink -f "$0")")/../scripts/common.sh"
load_config || exit 1
forward_ssh_settings

slow=0
[ "${1:-}" = --slow ] && slow=1

failures=0
pass() { echo "PASS $*"; }
fail() { echo "FAIL $*"; failures=$((failures + 1)); }

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

# PIDs of this server's chisel clients with an argument after the server URL
# that starts with $1: "stdio:" for the clients of shared connections, the SSH
# forward for the tunnel connect.sh starts.
chisel_pids() {
  local pid
  for pid in $(pgrep -u "$(id -u)" -x "$(basename "$CHISEL_BIN" | cut -c1-15)"); do
    tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null | grep -qF " $CHISEL_SERVER $1" && echo "$pid"
  done
}
count() { "$@" | grep -c .; }

master_pid() {
  ssh -o ControlPath="$1" -O check placeholder 2>&1 | grep -oP 'pid=\K[0-9]+'
}

# wait_until SECONDS COMMAND...: true once COMMAND succeeds within SECONDS.
wait_until() {
  local limit=$(( $1 * 5 )) i
  shift
  for i in $(seq 1 "$limit"); do
    "$@" && return 0
    sleep 0.2
  done
  return 1
}
gone() { ! kill -0 "$1" 2>/dev/null; }
no_chisel() { [ -z "$(chisel_pids "$1")" ]; }

# timed COMMAND...: runs COMMAND and sets took_ms to how long it took.
timed() {
  local start rc
  start=$(date +%s%N)
  "$@"
  rc=$?
  took_ms=$(( ($(date +%s%N) - start) / 1000000 ))
  return $rc
}

# under LIMIT_MS TIME_MS...: true when every time is below the limit.
under() {
  local limit=$1 t
  shift
  for t in "$@"; do
    [ "$t" -lt "$limit" ] || return 1
  done
}

# --- workstation mode --------------------------------------------------------

sourced_script="$SKILL_DIR/scripts/ssh-through-remote.sh"
ws_sock="$SOCKET_DIR/master-$REMOTE_USER.sock"

if ! SSH_THROUGH_CHISEL=1 bash "$SKILL_DIR/scripts/ssh-config.sh" --check >/dev/null 2>&1; then
  echo "SKIP workstation mode: ssh does not pick up the lines from scripts/ssh-config.sh"
elif master_running "$ws_sock" placeholder; then
  echo "STOP the shared connection on $ws_sock is up; end it first with: bash scripts/disconnect.sh"
  exit 2
else
  for sh in bash zsh; do
    command -v "$sh" >/dev/null || continue
    # shellcheck disable=SC2016
    if "$sh" -c 'source "$1" >/dev/null && [ "$SSH_THROUGH_CHISEL" = 1 ] &&
                 source "$1" off >/dev/null && [ -z "${SSH_THROUGH_CHISEL:-}" ]' _ "$sourced_script"; then
      pass "$sh: sourcing sets SSH_THROUGH_CHISEL, and 'off' unsets it"
    else
      fail "$sh: sourcing ssh-through-remote.sh"
    fi
    if "$sh" "$sourced_script" 2>/dev/null; then
      fail "$sh: running ssh-through-remote.sh instead of sourcing it succeeded"
    else
      pass "$sh: running ssh-through-remote.sh instead of sourcing it is refused"
    fi
  done

  export SSH_THROUGH_CHISEL=1

  # Connections that start together while the master is not running: six to
  # the alias, which look for the master's socket themselves, and two to the
  # remote's sshd by address, which go through the proxy and -W. Without the
  # lock in master_ensure each would start a master and a chisel client of
  # its own.
  for i in 1 2 3 4 5 6; do
    ( ssh -o BatchMode=yes "$SSH_ALIAS" true 2>"$work/burst$i.err"; echo $? >"$work/burst$i.rc" ) &
  done
  for i in 7 8; do
    ( ssh -o BatchMode=yes -o HostKeyAlias="$SSH_ALIAS" "$REMOTE_USER@127.0.0.1" true \
        2>"$work/burst$i.err"; echo $? >"$work/burst$i.rc" ) &
  done
  wait
  bad=""
  for i in 1 2 3 4 5 6 7 8; do
    [ "$(cat "$work/burst$i.rc")" = 0 ] && [ ! -s "$work/burst$i.err" ] || bad="$bad $i"
  done
  n=$(count chisel_pids stdio:)
  if [ -z "$bad" ] && [ "$n" = 1 ]; then
    pass "8 connections started together share 1 chisel client, without warnings"
  else
    fail "connections started together: failed or warned:${bad:- none}; chisel clients: $n"
    cat "$work"/burst*.err
  fi

  times=()
  for i in 1 2 3; do
    timed ssh -o BatchMode=yes "$SSH_ALIAS" true
    times+=("$took_ms")
  done
  if under 500 "${times[@]}"; then
    pass "ssh $SSH_ALIAS on the running master: ${times[*]} ms"
  else
    fail "ssh $SSH_ALIAS on the running master took 500 ms or more: ${times[*]} ms"
  fi

  # A master killed without cleanup leaves its socket file behind.
  m=$(master_pid "$ws_sock")
  kill -9 "$m"
  wait_until 5 gone "$m"
  if ssh -o BatchMode=yes "$SSH_ALIAS" true 2>"$work/killed.err" &&
     ! grep -v 'Control socket connect(.*): Connection refused' "$work/killed.err" | grep -q . &&
     wait_until 5 eval '[ "$(count chisel_pids stdio:)" = 1 ]'; then
    pass "after the master is killed, the next connection starts a new one"
  else
    fail "after the master is killed: $(cat "$work/killed.err"); chisel clients: $(count chisel_pids stdio:)"
  fi

  master_stop "$ws_sock"
  wait_until 5 no_chisel stdio:
  CHISEL_REMOTE_IDLE=5s ssh -o BatchMode=yes "$SSH_ALIAS" true
  m=$(master_pid "$ws_sock")
  if [ -n "$m" ] && wait_until 20 gone "$m" && wait_until 5 no_chisel stdio: && [ ! -e "$ws_sock" ]; then
    pass "a master with MASTER_IDLE=5s exits when unused, with its chisel client and socket"
  else
    fail "idle exit: master ${m:-none}, chisel clients $(count chisel_pids stdio:), socket $(ls "$ws_sock" 2>/dev/null)"
  fi

  if [ "$slow" = 1 ]; then
    ssh -o BatchMode=yes "$SSH_ALIAS" true
    c=$(chisel_pids stdio:)
    if [ "$(echo "$c" | grep -c .)" != 1 ]; then
      fail "freezing the chisel client: expected 1 chisel client, found: ${c:-none}"
    else
      kill -STOP "$c"
      if timed timeout 120 ssh -o BatchMode=yes "$SSH_ALIAS" true 2>"$work/frozen.err" &&
         [ "$took_ms" -gt 10000 ] && [ "$took_ms" -lt 90000 ]; then
        pass "with the chisel client frozen, a connection gives up on the master and succeeds after $took_ms ms"
      else
        fail "with the chisel client frozen: $took_ms ms, $(cat "$work/frozen.err")"
      fi
      kill -CONT "$c"
      wait_until 10 gone "$c" || fail "the frozen chisel client $c did not exit after SIGCONT"
    fi
  fi

  master_stop "$ws_sock"
  unset SSH_THROUGH_CHISEL
fi

# --- sandbox mode ------------------------------------------------------------

if [ -z "${SSH_KEY:-}" ]; then
  echo "SKIP sandbox mode: the config does not set SSH_KEY"
elif [ -n "$(tunnel_pids)" ] || master_running "$FORWARD_SOCK" placeholder; then
  echo "STOP the tunnel or the shared connection on $FORWARD_SOCK is up; end them first with: bash scripts/disconnect.sh"
  exit 2
elif ss -tln | grep -q "127.0.0.1:$SSH_LOCAL_PORT "; then
  echo "STOP another program listens on 127.0.0.1:$SSH_LOCAL_PORT; set SSH_LOCAL_PORT to a free port"
  exit 2
else
  if eval "$(bash "$SKILL_DIR/scripts/connect.sh" --print-env)" && [ -n "${REMOTE_SSH:-}" ]; then
    pass "connect.sh brings the tunnel up and sets REMOTE_SSH"
  else
    fail "connect.sh"
  fi

  if master_running "$FORWARD_SOCK" placeholder; then
    pass "connect.sh starts the shared connection"
  else
    fail "connect.sh did not start the shared connection; see $MASTER_LOG"
  fi

  times=()
  for i in 1 2 3; do
    timed $REMOTE_SSH true
    times+=("$took_ms")
  done
  if under 500 "${times[@]}"; then
    pass "\$REMOTE_SSH on the running master: ${times[*]} ms"
  else
    fail "\$REMOTE_SSH on the running master took 500 ms or more: ${times[*]} ms"
  fi

  master_stop "$FORWARD_SOCK"
  for i in 1 2 3 4 5; do
    ( $REMOTE_SSH true 2>"$work/sburst$i.err"; echo $? >"$work/sburst$i.rc" ) &
  done
  wait
  bad=""
  for i in 1 2 3 4 5; do
    [ "$(cat "$work/sburst$i.rc")" = 0 ] && [ ! -s "$work/sburst$i.err" ] || bad="$bad $i"
  done
  if [ -z "$bad" ] && master_running "$FORWARD_SOCK" placeholder; then
    pass "5 \$REMOTE_SSH calls started together start 1 shared connection, without warnings"
  else
    fail "\$REMOTE_SSH calls started together: failed or warned:${bad:- none}"
    cat "$work"/sburst*.err
  fi

  # A tunnel that dies takes the master with it, and connect.sh rebuilds both.
  # The call just before the kill shows that the failure after it comes from
  # the dead tunnel.
  t=$(tunnel_pids)
  if $REMOTE_SSH true && kill "$t" && wait_until 5 gone "$t" && ! $REMOTE_SSH true 2>/dev/null; then
    pass "\$REMOTE_SSH works, and fails once the tunnel is down"
  else
    fail "\$REMOTE_SSH before and after the tunnel was killed"
  fi
  if bash "$SKILL_DIR/scripts/connect.sh" --quiet && $REMOTE_SSH true; then
    pass "connect.sh rebuilds the tunnel, and \$REMOTE_SSH works again"
  else
    fail "connect.sh after the tunnel died"
  fi

  bash "$SKILL_DIR/scripts/disconnect.sh" >/dev/null
  if wait_until 5 eval '[ -z "$(tunnel_pids)" ]' && ! master_running "$FORWARD_SOCK" placeholder; then
    pass "disconnect.sh ends the tunnel and the shared connection"
  else
    fail "disconnect.sh left: tunnel $(tunnel_pids | tr '\n' ' '), master $(master_pid "$FORWARD_SOCK")"
  fi
fi

if [ "$failures" -gt 0 ]; then
  echo "$failures test(s) failed"
  exit 1
fi
echo "all tests passed"
