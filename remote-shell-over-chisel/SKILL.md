---
name: remote-shell-over-chisel
description: Shell access to a machine you own over a chisel-over-HTTPS tunnel, for networks that block SSH but pass HTTPS. Two modes. Sandbox mode gives a remote shell (and optional Chrome/CDP control) from a sandbox where egress only allows HTTPS; use it when the user asks you to run something on their own machine, box, home PC, or Linux box ("run X on my machine", "check on <host>", "pull a file from my computer", "drive my browser at home"). Workstation mode sends every ssh, scp, rsync and git-over-SSH connection from a shell through that machine; use it when the user says SSH is blocked on their network ("ssh is blocked here", "route ssh through my box"). CUSTOMIZE the trigger words below for your machine (see "Customizing").
---

# remote-shell-over-chisel

Reaches a machine you own from networks that block SSH but pass HTTPS, by
carrying SSH inside an HTTPS WebSocket via
[chisel](https://github.com/jpillora/chisel). Two modes use the same server:

- **Sandbox mode** (`scripts/connect.sh`): from a sandbox behind an HTTPS-only
  L7 proxy, you get `$REMOTE_SSH`, which runs commands on the remote, and
  optionally `$REMOTE_CDP` for driving its browser.
- **Workstation mode** (`scripts/ssh-through-remote.sh`): on a computer on a
  network that blocks outgoing SSH, every ssh connection from a shell goes
  through the remote, which then connects to the target host. That includes ssh to
  the remote itself, and scp, sftp, rsync and git over SSH.

> **Trust.** This skill grants shell access to the configured machine as the
> configured user, equivalent to the operator sitting at the keyboard. Only
> point it at a machine you own and intend to expose to your assistant. This
> repo does not contain anything machine-specific or secret; the scripts read
> all of it from a gitignored `config.sh` (see **Setup**).

## Architecture

```
client  --HTTPS WebSocket-->  https://<your-host>/chisel  (nginx / reverse proxy)
        --> chisel server on the remote machine
        --> the remote's sshd at 127.0.0.1:22
```

Both modes keep one SSH connection to the remote open and run every later
session over it (OpenSSH connection sharing, `ControlMaster`), so only the
first session pays for the chisel and SSH handshakes. It closes after
`MASTER_IDLE` (default 10 minutes) without a session.

- **Sandbox mode:** a chisel client runs in the background with two forwards,
  `sandbox:$SSH_LOCAL_PORT` → `remote:22` and, optionally,
  `sandbox:$CDP_LOCAL_PORT` → `remote:7799` (the cdp-daemon HTTP API; pairs
  with the `cdp-daemon` skill). `$REMOTE_SSH` logs in through the first
  forward with a dedicated key.
- **Workstation mode:** `scripts/ssh-through-remote-proxy` is ssh's
  ProxyCommand. Its shared connection runs over a chisel client of its own in
  stdio mode, and it reaches other hosts with `ssh -W` through the remote's
  sshd, so the chisel server needs neither `--socks5` nor `--reverse`.

## Setup

One-time, before first use. Full walkthrough in `README.md`; in brief:

1. **Server side (remote machine):** run `chisel server --auth user:secret`, fronted by a reverse proxy that maps an HTTPS path to it (nginx snippet in README).
2. **Config:** `cp config.sh.example config.sh` and fill in `CHISEL_SERVER`, `REMOTE_USER` and the chisel credentials (`CHISEL_TOKEN_FILE` or `CHISEL_TOKEN`). `config.sh` is gitignored.
3. **Sandbox mode:** generate a dedicated SSH key, add its public half to `~/.ssh/authorized_keys` on the remote, and set `SSH_KEY` to the private half.
4. **Workstation mode:** set `SSH_ALIAS` (and `SSH_HOSTNAME` for direct access), then append the ssh lines once: `bash scripts/ssh-config.sh >> ~/.ssh/config`. The ssh login uses the user's own key or agent.

## Sandbox mode

**1. Connect.** Each `bash_tool` call is a fresh shell, so the env vars
(`$REMOTE_SSH`, `$REMOTE_CDP`) have to be put back every call, but the tunnel
process and `$STATE_DIR` persist within a session, so that's usually just a file
source, not a reconnect. Precedence per call:

- **Default, when the tunnel is already up this session (every call after the first):** source the env file. Instant, without a probe:
  ```bash
  . ~/.chisel-remote/env.sh      # = $STATE_DIR/env.sh
  ```
- **First call of a session, or when unsure the tunnel is up:** the self-healing line, which brings the tunnel up if needed and sets the vars in the current shell:
  ```bash
  eval "$(bash scripts/connect.sh --print-env)"
  ```
- **A command came back `Connection refused`:** the forward went stale (normal after an idle gap or a remote reboot, since the process can be alive with a dead forward). Re-run the self-healing line; its SSH probe detects the dead forward and rebuilds.

Rule of thumb: `connect.sh` to *establish or repair*, `. $STATE_DIR/env.sh` to
*use* a tunnel that's already up. Don't default to the `eval` line every call. It works, but pays an SSH probe round-trip you don't need once the tunnel's live.

After either form, in the same shell:
- `$REMOTE_SSH "command"` runs a command on the remote as `$REMOTE_USER`. It runs `scripts/remote-ssh`, which opens the session on the shared connection and starts that connection first if it has closed.
- `$REMOTE_CDP` is the cdp-daemon URL (only useful if cdp-daemon is running on the remote; start it via SSH if needed).

What `connect.sh` does: reads `config.sh`; fetches the chisel binary if missing
(cached in `$STATE_DIR`, checked against the release checksums); installs
`openssh-client` if missing; stages the private key 600 at `$STATE_DIR/id`;
brings the tunnel up; starts the shared SSH connection; writes
`$STATE_DIR/env.sh`. State lives in `$STATE_DIR` (mode 700) on the persistent
disk and survives across sessions, so a fresh session's connect skips the
download and only redoes the handshake (the live TCP connection never survives
a session gap, so one `connect.sh` per session is still needed). It is safe to
run on any call: it probes with a real SSH login (not a port check, see Notes)
and restarts chisel only if the probe fails, so the cost when healthy is one
probe. Under interactive bash you can `source scripts/connect.sh` directly.
There's no `set -e`, so a failure won't take your shell down.

**2. Run things.** Examples:

```bash
$REMOTE_SSH "uname -a"
$REMOTE_SSH "cd ~/src/some-repo && git status"

# pull a file off the remote:
$REMOTE_SSH "cat ~/notes.md" > /tmp/notes.md
# push a file to the remote:
cat local.txt | $REMOTE_SSH "cat > ~/from-assistant.txt"
```

For Chrome / browser tasks, ensure cdp-daemon is running on the remote first
(see the `cdp-daemon` skill), then drive it through `$REMOTE_CDP`:

```bash
curl -s "$REMOTE_CDP/status"
curl -s "$REMOTE_CDP/targets"
```

**3. Disconnect when done** (optional, since the sandbox is ephemeral):

```bash
bash scripts/disconnect.sh            # keep cache
bash scripts/disconnect.sh --purge    # wipe $STATE_DIR too
```

## Workstation mode

A person turns it on for one shell, and off again, with:

```bash
source scripts/ssh-through-remote.sh       # or a symlink to it on PATH
source scripts/ssh-through-remote.sh off
```

Sourcing only exports `SSH_THROUGH_CHISEL=1`; the lines from
`scripts/ssh-config.sh` in `~/.ssh/config` route ssh while that variable is
set. The script checks with `ssh -G` that ssh picks the lines up, and leaves
the variable unset with an ALERT if it does not.

When you run commands for the user on such a machine (for example in Claude
Code, where each Bash call is a fresh shell), set the variable on each command
instead of sourcing:

```bash
bash scripts/ssh-config.sh --check                   # once: are the lines installed?
SSH_THROUGH_CHISEL=1 ssh bigbox "uname -a"
SSH_THROUGH_CHISEL=1 git -C ~/src/repo fetch
SSH_THROUGH_CHISEL=1 rsync -a ./out/ bigbox:out/
```

If `--check` fails, show the user what `bash scripts/ssh-config.sh` prints and
ask before appending it to `~/.ssh/config`. To close the shared connection
right away instead of waiting for `MASTER_IDLE`, run `bash
scripts/disconnect.sh`; that also ends every session running on it.

While it is on, the remote resolves each host name and dials the address, so a
host that exists only on the local network (192.168.x.x, a `.local` name) is
unreachable from that shell.

## Customizing

- **Trigger words:** edit the `description:` frontmatter above to name *your*
  machine and the phrases you'll actually use ("check on bigbox", "run X on
  homebox"). That's what makes the assistant reach for this skill.
- **CDP off:** set `CDP_LOCAL_PORT=""` in `config.sh` if you don't need browser
  control; the tunnel then forwards SSH only.
- **Multiple machines:** copy the skill dir per machine, or point
  `CHISEL_REMOTE_CONFIG` at a different config file. Workstation mode routes
  through one machine per `~/.ssh/config`.

## Failure modes

- **`HTTP/1.1 400` or empty response from the chisel server:** an HTTP-only path or a misrouted proxy; check that the HTTPS path in `CHISEL_SERVER` routes to the chisel server, not the default site.
- **`Connection refused` on the local port (sandbox):** tunnel isn't up. Re-run `connect.sh`; it will (re)start chisel. Check `$STATE_DIR/chisel.log` if it won't come up.
- **`Permission denied (publickey)`:** the key isn't in `authorized_keys` on the remote (`SSH_KEY` in sandbox mode, the user's own key in workstation mode), or `REMOTE_USER` is wrong.
- **Tunnel process alive but SSH refused/hangs (stale forward):** handled automatically: `connect.sh`'s SSH probe detects it and restarts chisel. If hit mid-script, re-run `connect.sh`. Can be provoked by killing and restarting chisel within the same second (socket hasn't drained); a clean `disconnect.sh` + brief wait + `connect.sh` clears it.
- **`could not connect to ... through ...` (workstation):** the shared connection did not start; `$STATE_DIR/master.log` holds the messages from ssh and chisel.
- **`Control socket connect(...): Connection refused`, once:** the shared connection was killed without cleaning up its socket. The same command continues and starts a new one.
- **A command waits up to about 45 seconds, then works (or prints `mux_client_request_session: ... Broken pipe` first):** the network went away under a running shared connection, after a suspend or a network change. Its keepalive gives up after two unanswered 15-second probes, and the command then starts a new connection. Sessions that were open on the old one end.

## Notes

- **One connection, started under a lock.** When the shared connection is not running, commands that start together wait on a lock next to its socket, and only the first one starts it. With ssh's own `ControlMaster auto`, each would start one and all but one would print `ControlSocket ... already exists, disabling multiplexing`.
- **Why not chisel's `--socks5`.** A chisel client's forwards are fixed when it starts, so reaching any host through one client would need a SOCKS remote and `--socks5` on the server. The shared SSH connection gives ssh to the remote itself without a new handshake per session, does not need a change on the server, and keeps the access with the user's SSH key: a control socket in a directory only the user can open, instead of an unauthenticated local SOCKS port and a SOCKS exit for chisel's users. For other hosts, both send every step of the target's handshake via the remote, which adds one round trip to the remote per step. ssh cannot carry UDP; that needs chisel's `udpsocks` remote (1.13 and later on both ends).
- **Control socket paths** must stay under 91 characters (a Unix socket path holds 107 bytes, and ssh first creates the socket under a name 17 characters longer). When `$STATE_DIR` is too long for that, the sockets go in a private directory in `$XDG_RUNTIME_DIR` or `/tmp`.
- **Cost when off:** the `Match exec "printenv SSH_THROUGH_CHISEL"` lines run a shell for every ssh command, about 13 ms, even when workstation mode is off.
- State lives in `$STATE_DIR` (mode 700) on the persistent disk and survives across sessions. Each fresh session still needs one `connect.sh` run to rebuild the tunnel (the live connection doesn't survive a session gap), but it skips the fetch and key-stage. Within a session the tunnel persists across calls and `. $STATE_DIR/env.sh` is enough.
- The `chisel` binary is not bundled; the scripts fetch `CHISEL_VERSION` from GitHub releases on first use, check it against the release's checksums, and reuse it thereafter. `CHISEL_BIN` can point at a chisel that is already installed.
- **Don't trust `ss`/port checks for tunnel health in some sandboxes**: socket-table enumeration can return false zeros (a working forward shows up as "no listener"). Liveness is judged by an actual SSH probe; that's what `connect.sh` uses.
- The private key is staged to `$STATE_DIR/id` (600) on connect, so health doesn't depend on the skill dir being writable or the source key's perms.
- Secrets never appear in `ps`: the chisel token is passed via the `AUTH` env var, not argv.
- `test/selftest.sh` runs both modes against the configured remote (see README).
