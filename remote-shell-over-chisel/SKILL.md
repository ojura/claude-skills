---
name: remote-shell-over-chisel
description: Remote shell (and optional Chrome/CDP control) over a chisel-over-HTTPS tunnel to a machine you own, from a sandbox whose egress only allows HTTPS. Use when the user asks you to run something on their own machine, box, home PC, or Linux box ("run X on my machine", "check on <host>", "pull a file from my computer", "drive my browser at home"), especially from an environment where raw SSH is blocked but HTTPS is allowed. CUSTOMIZE the trigger words below for your machine (see "Customizing"). Sets up the tunnel, gives an SSH command and a CDP endpoint, and tears down cleanly.
---

# remote-shell-over-chisel

Gives you a shell on a machine you own, from a sandbox whose egress is an
HTTPS-only L7 proxy that rejects raw SSH on any port. It does this by tunnelling
SSH (and optionally the cdp-daemon port for browser control) inside an HTTPS
WebSocket via [chisel](https://github.com/jpillora/chisel).

> **Trust.** This skill grants shell access to the configured machine as the
> configured user, equivalent to the operator sitting at the keyboard. Only
> point it at a machine you own and intend to expose to your assistant. Nothing
> machine-specific or secret lives in this repo; everything is read from a
> gitignored `config.sh` (see **Setup**).

## Architecture

The sandbox egress is a TLS-MITM L7 proxy that only passes valid HTTPS; raw SSH
on any port is rejected. The chain that works:

```
sandbox  --HTTPS WebSocket-->  https://<your-host>/chisel  (nginx / reverse proxy)
         --> chisel server (--reverse) on the remote machine
         --> forwards loopback ports back into the sandbox
```

Two forwards are established (the second is optional):
- `sandbox:$SSH_LOCAL_PORT` → `remote:22` (SSH, key-authed as `$REMOTE_USER`)
- `sandbox:$CDP_LOCAL_PORT` → `remote:7799` (cdp-daemon HTTP API when running - pairs with the `cdp-daemon` skill)

## Setup

One-time, before first use. Full walkthrough in `README.md`; in brief:

1. **Server side (remote machine):** run `chisel server --reverse --auth user:secret`, fronted by a reverse proxy that maps an HTTPS path to it (nginx snippet in README).
2. **Key:** generate a dedicated SSH key, add its public half to `~/.ssh/authorized_keys` on the remote.
3. **Config:** `cp config.sh.example config.sh` and fill in `CHISEL_SERVER`, `REMOTE_USER`, `SSH_KEY` (path, outside the repo), and the chisel token (`CHISEL_TOKEN_FILE` or `CHISEL_TOKEN`). `config.sh` is gitignored.

## Usage

**1. Connect.** Each `bash_tool` call is a fresh shell, so the env vars
(`$REMOTE_SSH`, `$REMOTE_CDP`) have to be put back every call, but the tunnel
process and `$STATE_DIR` persist within a session, so that's usually just a file
source, not a reconnect. Precedence per call:

- **Default - tunnel already up this session (every call after the first):** source the env file. Instant, no probe:
  ```bash
  . ~/.chisel-remote/env.sh      # = $STATE_DIR/env.sh
  ```
- **First call of a session, or when unsure the tunnel is up:** the self-healing line - brings the tunnel up if needed and sets the vars in the current shell:
  ```bash
  eval "$(bash scripts/connect.sh --print-env)"
  ```
- **A command came back `Connection refused`:** the forward went stale (normal after an idle gap or a remote reboot, since the process can be alive with a dead forward). Re-run the self-healing line; its SSH probe detects the dead forward and rebuilds.

Rule of thumb: `connect.sh` to *establish or repair*, `. $STATE_DIR/env.sh` to
*use* a tunnel that's already up. Don't default to the `eval` line every call. It works, but pays an SSH probe round-trip you don't need once the tunnel's live.

After either form, in the same shell:
- `$REMOTE_SSH "command"` runs a command on the remote as `$REMOTE_USER`.
- `$REMOTE_CDP` is the cdp-daemon URL (only useful if cdp-daemon is running on the remote; start it via SSH if needed).

What `connect.sh` does: reads `config.sh`; fetches the chisel binary if missing
(cached in `$STATE_DIR`); installs `openssh-client` if missing; stages the
private key 600 at `$STATE_DIR/id`; brings the tunnel up; writes
`$STATE_DIR/env.sh`. State lives in `$STATE_DIR` (mode 700) on the persistent
disk and survives across sessions, so a fresh session's connect short-circuits: binary cached, key staged, just the handshake (the live TCP connection itself
never survives a session gap, so one `connect.sh` per session is still needed).
It's idempotent and self-healing: a real SSH probe (not a port check - see Notes), restarting chisel only if the probe fails, so it's safe to run on any
call; the cost when healthy is one fast probe. Under interactive bash you can
`source scripts/connect.sh` directly. There's no `set -e`, so a failure won't take your
shell down.

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

**3. Disconnect when done** (optional - the sandbox is ephemeral):

```bash
bash scripts/disconnect.sh            # keep cache
bash scripts/disconnect.sh --purge    # wipe $STATE_DIR too
```

## Customizing

- **Trigger words:** edit the `description:` frontmatter above to name *your*
  machine and the phrases you'll actually use ("check on bigbox", "run X on
  homebox"). That's what makes the assistant reach for this skill.
- **CDP off:** set `CDP_LOCAL_PORT=""` in `config.sh` if you don't need browser
  control; the tunnel then forwards SSH only.
- **Multiple machines:** copy the skill dir per machine, or point
  `CHISEL_REMOTE_CONFIG` at a different config file.

## Failure modes

- **`HTTP/1.1 400` or empty response from the chisel server:** an HTTP-only path or a misrouted proxy; check that the HTTPS path in `CHISEL_SERVER` routes to the chisel server, not the default site.
- **`Connection refused` on the local port:** tunnel isn't up. Re-run `connect.sh`; it will (re)start chisel. Check `$STATE_DIR/chisel.log` if it won't come up.
- **`Permission denied (publickey)`:** the key in `SSH_KEY` isn't in `authorized_keys` on the remote, or `REMOTE_USER` is wrong.
- **Tunnel process alive but SSH refused/hangs (stale forward):** handled automatically: `connect.sh`'s SSH probe detects it and restarts chisel. If hit mid-script, re-run `connect.sh`. Can be provoked by killing and restarting chisel within the same second (socket hasn't drained); a clean `disconnect.sh` + brief wait + `connect.sh` clears it.

## Notes

- State lives in `$STATE_DIR` (mode 700) on the persistent disk and survives across sessions. Each fresh session still needs one `connect.sh` run to rebuild the tunnel (the live connection doesn't survive a session gap), but it short-circuits the fetch and key-stage. Within a session the tunnel persists across calls and `. $STATE_DIR/env.sh` is enough.
- The `chisel` binary is not bundled; `connect.sh` fetches the pinned `CHISEL_VERSION` from GitHub releases on first use and reuses it thereafter.
- **Don't trust `ss`/port checks for tunnel health in some sandboxes** - socket-table enumeration can return false zeros (a working forward shows up as "no listener"). Liveness is judged by an actual SSH probe; that's what `connect.sh` uses.
- The private key is staged to `$STATE_DIR/id` (600) on connect, so health doesn't depend on the skill dir being writable or the source key's perms.
- Secrets never appear in `ps`: the chisel token is passed via the `AUTH` env var, not argv.
