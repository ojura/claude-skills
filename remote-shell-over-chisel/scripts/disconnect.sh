#!/bin/bash
# Tear down the chisel tunnel and the shared SSH connections.
#   disconnect.sh           -> end them; keep $STATE_DIR cache (binary, key, env)
#   disconnect.sh --purge   -> also wipe $STATE_DIR
#
# Reads the same config.sh as connect.sh to learn which processes to end and
# where the state dir is. Ending a shared connection also ends every ssh
# session running on it, including ones started by ssh-through-remote.sh.

# shellcheck source=common.sh
. "$(dirname "$(readlink -f "${BASH_SOURCE[0]:-$0}")")/common.sh"
load_config || exit 1

for sock in "$SOCKET_DIR"/master-*.sock; do
  [ -S "$sock" ] || continue
  master_stop "$sock" && echo "[$REMOTE_NAME] closed shared connection $(basename "$sock")"
done

pids=$(tunnel_pids)
if [ -n "$pids" ]; then
  # shellcheck disable=SC2086
  kill $pids && echo "[$REMOTE_NAME] tunnel down"
else
  echo "[$REMOTE_NAME] no tunnel running"
fi

if [ "$1" = "--purge" ]; then
  rm -rf "$STATE_DIR"
  echo "[$REMOTE_NAME] purged $STATE_DIR"
fi
