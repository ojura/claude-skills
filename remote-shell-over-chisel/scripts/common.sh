# Settings and functions shared by the scripts in this directory. Sourced, not
# run. The functions return non-zero after printing why instead of exiting,
# because connect.sh is sourced into the caller's shell.

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Reads config.sh, or the file named by $CHISEL_REMOTE_CONFIG, fills in the
# defaults, loads the chisel token and creates $STATE_DIR.
load_config() {
  CONFIG="${CHISEL_REMOTE_CONFIG:-$SKILL_DIR/config.sh}"
  if [ ! -f "$CONFIG" ]; then
    echo "[chisel-remote] ERROR: no config at $CONFIG" >&2
    echo "[chisel-remote] copy config.sh.example to config.sh and fill it in (see README.md)." >&2
    return 1
  fi
  # shellcheck disable=SC1090
  . "$CONFIG"

  REMOTE_NAME="${REMOTE_NAME:-remote}"
  SSH_ALIAS="${SSH_ALIAS:-$REMOTE_NAME}"
  SSH_LOCAL_PORT="${SSH_LOCAL_PORT:-2222}"
  SSH_REMOTE_PORT="${SSH_REMOTE_PORT:-22}"
  CDP_LOCAL_PORT="${CDP_LOCAL_PORT-7799}"
  CDP_REMOTE_PORT="${CDP_REMOTE_PORT:-7799}"
  STATE_DIR="${STATE_DIR:-$HOME/.chisel-remote}"
  CHISEL_VERSION="${CHISEL_VERSION:-1.12.0}"
  CHISEL_BIN="${CHISEL_BIN:-$STATE_DIR/chisel}"
  # CHISEL_REMOTE_IDLE in the environment overrides the config, for tests.
  MASTER_IDLE="${CHISEL_REMOTE_IDLE:-${MASTER_IDLE:-10m}}"
  MASTER_LOG="$STATE_DIR/master.log"

  if [ -z "${CHISEL_SERVER:-}" ]; then
    echo "[$REMOTE_NAME] ERROR: CHISEL_SERVER unset in $CONFIG" >&2; return 1
  fi
  if [ -z "${REMOTE_USER:-}" ]; then
    echo "[$REMOTE_NAME] ERROR: REMOTE_USER unset in $CONFIG" >&2; return 1
  fi
  if [ -z "${CHISEL_TOKEN:-}" ] && [ -n "${CHISEL_TOKEN_FILE:-}" ] && [ -f "$CHISEL_TOKEN_FILE" ]; then
    CHISEL_TOKEN="$(cat "$CHISEL_TOKEN_FILE")"
  fi
  if [ -z "${CHISEL_TOKEN:-}" ]; then
    echo "[$REMOTE_NAME] ERROR: no chisel token (set CHISEL_TOKEN or CHISEL_TOKEN_FILE in $CONFIG)" >&2
    return 1
  fi

  # 700 because it holds a copy of the private key and the control sockets,
  # and a control socket gives a shell on the remote to whoever can open it.
  mkdir -p "$STATE_DIR" && chmod 700 "$STATE_DIR" || return 1
  set_socket_dir
}

# Sets SOCKET_DIR, where the control sockets of the shared connections go:
# STATE_DIR, unless that makes their paths too long. A Unix socket path holds
# at most 107 bytes, and ssh first creates the socket under a name 17
# characters longer, so a ControlPath over 90 characters makes ssh stop with
# "ControlPath too long". In that case the sockets go in a directory named
# after STATE_DIR in $XDG_RUNTIME_DIR, or in /tmp when that is unset, and the
# directory must belong to you and be closed to everyone else.
set_socket_dir() {
  local longest=$(( ${#REMOTE_USER} + 12 ))
  # "master-forward.sock" is 19 characters, "master-USER.sock" 12 plus the user.
  [ "$longest" -lt 19 ] && longest=19
  SOCKET_DIR="$STATE_DIR"
  [ $(( ${#SOCKET_DIR} + 1 + longest )) -le 90 ] && return 0

  SOCKET_DIR="${XDG_RUNTIME_DIR:-/tmp}/chisel-remote-$(id -u)-$(printf '%s' "$STATE_DIR" | sha256sum | cut -c1-8)"
  if ! mkdir -m 700 "$SOCKET_DIR" 2>/dev/null &&
     { [ ! -d "$SOCKET_DIR" ] || [ ! -O "$SOCKET_DIR" ] || [ "$(stat -c %a "$SOCKET_DIR")" != 700 ]; }; then
    echo "[$REMOTE_NAME] ERROR: $SOCKET_DIR exists but is not a directory that only you can use" >&2
    return 1
  fi
  if [ $(( ${#SOCKET_DIR} + 1 + longest )) -gt 90 ]; then
    echo "[$REMOTE_NAME] ERROR: control socket paths in $SOCKET_DIR are too long; set a shorter STATE_DIR" >&2
    return 1
  fi
}

# Downloads the chisel release named by $CHISEL_VERSION to $CHISEL_BIN unless
# an executable is already there, and checks it against the release's
# checksums file before installing it.
ensure_chisel() {
  [ -x "$CHISEL_BIN" ] && return 0
  local arch gz base dl
  case "$(uname -m)" in
    x86_64) arch=amd64 ;;
    aarch64|arm64) arch=arm64 ;;
    *) echo "[$REMOTE_NAME] ERROR: no chisel release for $(uname -m); set CHISEL_BIN in the config" >&2; return 1 ;;
  esac
  gz="chisel_${CHISEL_VERSION}_linux_${arch}.gz"
  base="https://github.com/jpillora/chisel/releases/download/v${CHISEL_VERSION}"
  echo "[$REMOTE_NAME] fetching chisel $CHISEL_VERSION..." >&2
  # A fresh directory per download, so two scripts fetching at the same time
  # do not write into each other's files.
  dl="$(mktemp -d "$STATE_DIR/download.XXXXXX")" || return 1
  if ! curl -fsSL -o "$dl/$gz" "$base/$gz" ||
     ! curl -fsSL -o "$dl/checksums.txt" "$base/chisel_${CHISEL_VERSION}_checksums.txt"; then
    echo "[$REMOTE_NAME] ERROR: chisel download failed ($base/$gz)" >&2
    rm -rf "$dl"; return 1
  fi
  if ! (cd "$dl" && grep " $gz\$" checksums.txt | sha256sum --check --quiet --strict -); then
    echo "[$REMOTE_NAME] ERROR: $gz does not match the release checksums" >&2
    rm -rf "$dl"; return 1
  fi
  mkdir -p "$(dirname "$CHISEL_BIN")"
  if ! gunzip -c "$dl/$gz" > "$CHISEL_BIN.tmp" || ! chmod 755 "$CHISEL_BIN.tmp" ||
     ! mv -f "$CHISEL_BIN.tmp" "$CHISEL_BIN"; then
    echo "[$REMOTE_NAME] ERROR: cannot unpack chisel to $CHISEL_BIN" >&2
    rm -rf "$dl" "$CHISEL_BIN.tmp"; return 1
  fi
  rm -rf "$dl"
}

# Sets FORWARD_SSH_OPTS, FORWARD_DEST and FORWARD_SOCK: how connect.sh and
# remote-ssh reach the remote's sshd through the tunnel's local port, logging
# in with the copy of SSH_KEY that connect.sh keeps in $STATE_DIR/id.
# shellcheck disable=SC2034
forward_ssh_settings() {
  FORWARD_SSH_OPTS=(-p "$SSH_LOCAL_PORT" -i "$STATE_DIR/id"
    -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile="$STATE_DIR/known_hosts"
    -o BatchMode=yes -o ConnectTimeout=10)
  FORWARD_DEST="$REMOTE_USER@127.0.0.1"
  FORWARD_SOCK="$SOCKET_DIR/master-forward.sock"
}

# Prints the PIDs of the tunnel connect.sh starts: processes named like the
# chisel binary, with this server and the SSH forward among their arguments.
# Matching on the process name, not with pgrep -f, keeps out shells that
# mention chisel in their command line, and the forward tells the tunnel apart
# from the chisel client of a shared connection that ssh-through-remote-proxy
# starts.
tunnel_pids() {
  local pid
  for pid in $(pgrep -u "$(id -u)" -x "$(basename "$CHISEL_BIN" | cut -c1-15)"); do
    tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null |
      grep -qF " $CHISEL_SERVER ${SSH_LOCAL_PORT}:127.0.0.1:${SSH_REMOTE_PORT}" && echo "$pid"
  done
}

# master_running SOCK DEST [SSH_OPTION...]
# True when an SSH master connection answers on the control socket SOCK.
master_running() {
  local sock=$1 dest=$2
  shift 2
  ssh "$@" -o ControlPath="$sock" -O check "$dest" >/dev/null 2>&1
}

# master_ensure SOCK DEST [SSH_OPTION...]
# Makes sure one SSH master connection to DEST listens on the control socket
# SOCK, so that later ssh runs with ControlPath=SOCK and ControlMaster=no open
# their sessions on it without a handshake of their own. The master starts
# with the given ssh options and exits after $MASTER_IDLE without a session.
#
# When the master is not running, callers that start together wait on a lock
# next to the socket, and only the first one starts the master; the others
# find it running when they get the lock. ssh's own ControlMaster=auto has
# each of them start a master, and all but one print "ControlSocket ...
# already exists, disabling multiplexing" and keep a connection of their own.
master_ensure() {
  local sock=$1 dest=$2
  shift 2
  master_running "$sock" "$dest" "$@" && return 0
  (
    if ! flock -w 30 9; then
      echo "[$REMOTE_NAME] ERROR: timed out waiting for $sock.lock" >&2
      exit 1
    fi
    master_running "$sock" "$dest" "$@" && exit 0
    # A master killed without cleanup leaves its socket file behind, and a new
    # master cannot bind over it. It does not answer (checked above, under the
    # lock), so it is safe to remove.
    rm -f "$sock"
    # The master runs on in the background, so it gets /dev/null and the log
    # instead of the caller's stdin and stdout, which may carry an ssh
    # connection, and it must not keep the lock open. A master that stops
    # getting replies, after a suspend or a network change, exits within
    # about 45 seconds, and the next caller starts a new one.
    ssh "$@" -fN -o ControlMaster=yes -o ControlPersist="$MASTER_IDLE" \
      -o ControlPath="$sock" -o ServerAliveInterval=15 -o ServerAliveCountMax=2 \
      "$dest" 9>&- </dev/null >/dev/null 2>>"$MASTER_LOG"
  ) 9>"$sock.lock"
}

# master_stop SOCK
# Asks the master on SOCK to exit, which also ends every session on it.
master_stop() {
  [ -S "$1" ] || return 0
  ssh -o ControlPath="$1" -O exit placeholder >/dev/null 2>&1
}
