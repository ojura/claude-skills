# Source this file to send every ssh connection made from this shell through
# the remote machine, for networks that block outgoing SSH but allow HTTPS.
# Programs started from the shell that run ssh (scp, sftp, rsync, git) go
# through it as well.
#
#   source ssh-through-remote.sh        turn it on in this shell
#   source ssh-through-remote.sh off    turn it off again
#
# It works from a symlink, for example one in ~/.local/bin under a name of your
# choosing. This file only sets SSH_THROUGH_CHISEL; the routing is the lines
# from ssh-config.sh in ~/.ssh/config, which apply while that variable is set.
# The remote resolves each host name and dials the address, so a host that
# only exists on the local network (192.168.x.x, a .local name) is out of
# reach while this is on.

# Run as a program, this file would only set the variable in a child process
# that exits straight away, so refuse and say how to use it.
if [ -n "${ZSH_VERSION:-}" ]; then
  case ${ZSH_EVAL_CONTEXT:-} in *:file*) _str_sourced=1 ;; *) _str_sourced=0 ;; esac
  # zsh syntax, which bash would refuse to parse, so it goes through eval.
  eval '_str_file=${(%):-%x}'
elif [ -n "${BASH_VERSION:-}" ]; then
  [ "${BASH_SOURCE[0]}" != "$0" ] && _str_sourced=1 || _str_sourced=0
  _str_file=${BASH_SOURCE[0]}
else
  _str_sourced=0
fi
if [ "$_str_sourced" = 0 ]; then
  unset _str_sourced _str_file
  echo "$(basename "$0"): source this file instead of running it:" >&2
  echo "  source $(basename "$0") [off]" >&2
  return 1 2>/dev/null || exit 1
fi

if [ "${1:-}" = off ]; then
  unset SSH_THROUGH_CHISEL _str_sourced _str_file
  echo "ssh goes direct again in this shell"
  return 0
fi

# Check what ssh will actually do, so missing or shadowed config lines show up
# here instead of as a mysteriously blocked connection later.
_str_dir=$(dirname "$(readlink -f "$_str_file")")
export SSH_THROUGH_CHISEL=1
if _str_msg=$(bash "$_str_dir/ssh-config.sh" --check); then
  echo "$_str_msg in this shell; 'source $(basename "$_str_file") off' to stop"
  unset _str_sourced _str_file _str_dir _str_msg
else
  unset _str_msg
  unset SSH_THROUGH_CHISEL
  echo "ALERT: ssh still goes direct in this shell; see the messages above." >&2
  unset _str_sourced _str_file _str_dir
  return 1
fi
