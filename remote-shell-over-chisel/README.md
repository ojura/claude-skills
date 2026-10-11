# remote-shell-over-chisel

Reach a machine you own from networks that block SSH but pass HTTPS. Many
hosted code/agent sandboxes front their network with a TLS-MITM L7 proxy that
passes valid HTTPS and rejects everything else, including raw SSH on any port,
and some office, hotel and university networks block outgoing SSH the same
way. This skill carries SSH inside an HTTPS WebSocket using
[chisel](https://github.com/jpillora/chisel), in two modes:

- **Sandbox mode** gives the assistant a real shell on your box from such a
  sandbox (and optionally control of your browser there), without poking a
  hole for raw SSH.
- **Workstation mode** sends every ssh, scp, rsync and git-over-SSH connection
  from a shell on your own computer through your box, which then connects to
  the target host. You turn it on with `source`, like a virtualenv.

This repo does not contain anything machine-specific or secret. The scripts
read all host, user, and credential details from a gitignored `config.sh`.

## How it works

```
sandbox or workstation  --HTTPS WebSocket-->  https://<your-host>/chisel  (nginx reverse proxy)
                        --> chisel server running on your machine
                        --> your machine's sshd at 127.0.0.1:22
```

In **sandbox mode**, `scripts/connect.sh` keeps a chisel client running with
forwards on loopback ports in the sandbox:

```
sandbox:2222 -> your-machine:22    (ssh)
sandbox:7799 -> your-machine:7799  (cdp-daemon, optional)
```

The assistant runs commands with `$REMOTE_SSH "..."`, which lands on your
machine's sshd through the first forward.

In **workstation mode**, `scripts/ssh-through-remote-proxy` is ssh's
ProxyCommand for every host while the mode is on. It connects to your
machine's sshd over a chisel client in stdio mode, and asks that sshd to open
the connection to the target host (`ssh -W`), so `ssh github.com` from your
laptop leaves the network from your machine.

Both modes keep **one SSH connection** to your machine open and run every
later session over it (OpenSSH's `ControlMaster`), so only the first session
pays for the chisel and SSH handshakes. With a 55 ms round trip to the
machine, `ssh <your-machine>` took about 0.12 s on the shared connection
against about 1.3 s when each connection started its own chisel client, and a
`$REMOTE_SSH` call about 0.15 s against about 0.5 s when each logged in
separately. The first session starts the shared connection while holding a
lock, so sessions that start together share it rather than each opening one.
It closes after `MASTER_IDLE` (default 10 minutes) without a session, and the
next session opens a new one.

## Setup

### 1. Server side: chisel + reverse proxy on your machine

Install chisel (same binary is server and client):

```bash
curl -fsSL https://github.com/jpillora/chisel/releases/download/v1.12.0/chisel_1.12.0_linux_amd64.gz \
  | gunzip > /usr/local/bin/chisel && chmod +x /usr/local/bin/chisel
```

Run a server bound to loopback, with credentials in `user:secret` form:

```bash
chisel server --auth "user:$(head -c24 /dev/urandom | base64)" \
  --host 127.0.0.1 --port 8080
```

Note the `user:secret` you chose. That's your `CHISEL_TOKEN`. Neither mode
needs `--reverse` or `--socks5`: both only open connections from the server to
your sshd (and to the cdp-daemon port). An `--authfile` can hold the
credentials instead of `--auth`, and can limit them to those addresses, given
as regular expressions:

```json
{
  "user:secret": ["^127\\.0\\.0\\.1:22$", "^127\\.0\\.0\\.1:7799$"]
}
```

Put the server behind your existing HTTPS vhost so it's reachable as
`https://<host>/chisel`. nginx:

```nginx
location /chisel {
    proxy_pass http://127.0.0.1:8080;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;     # WebSocket upgrade
    proxy_set_header Connection "upgrade";
    proxy_set_header Host $host;
    proxy_read_timeout 1h;                       # keep idle tunnels alive
}
```

Run the chisel server under a process manager (systemd, etc.) so it stays up.
A `--keepalive 25s` on the server is wise if your proxy is aggressive about
idle connections. Your sshd has to allow TCP forwarding for workstation mode
(`AllowTcpForwarding yes`, the default).

### 2. Client config

```bash
cp config.sh.example config.sh
$EDITOR config.sh
```

Set at least `CHISEL_SERVER`, `REMOTE_USER`, and the token
(`CHISEL_TOKEN_FILE` pointing at a file with your `user:secret`, or
`CHISEL_TOKEN` inline). Stash the token outside the repo, e.g.:

```bash
mkdir -p ~/.config/chisel-remote
printf 'user:secret\n' > ~/.config/chisel-remote/token
chmod 600 ~/.config/chisel-remote/token
```

The scripts download the chisel client (`CHISEL_VERSION`) on first use and
check it against the release's checksums, unless `CHISEL_BIN` points at one
you already have.

### 3. Sandbox mode: a dedicated SSH key

Generate a key used *only* for this, and authorize it on the machine:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/id_remote_shell -N "" -C "remote-shell-over-chisel"
cat ~/.ssh/id_remote_shell.pub >> ~/.ssh/authorized_keys
```

Keep the private key wherever you'll point `SSH_KEY` at, outside this repo.

### 4. Workstation mode: the ssh lines

Set `SSH_ALIAS` in `config.sh` to the name you want to type (`ssh bigbox`),
and `SSH_HOSTNAME` to the machine's address if you also reach it directly
when SSH is not blocked. Then append the lines `ssh-config.sh` prints to your
ssh config, once:

```bash
bash scripts/ssh-config.sh                     # look at them first
bash scripts/ssh-config.sh >> ~/.ssh/config
```

They must come after your other `Host` blocks: ssh uses the first
ProxyCommand or ProxyJump it reads, so a host with a ProxyJump of its own
keeps it, and only the connection to its jump host goes through your machine.
Re-run the two commands after changing `SSH_ALIAS`, `REMOTE_USER`,
`STATE_DIR` or the skill's location, replacing the old lines.

Workstation mode logs in with your usual SSH key or agent, so that key must
be in `authorized_keys` on the machine. For a short command, link the
sourced script onto your PATH under any name:

```bash
ln -s "$PWD/scripts/ssh-through-remote.sh" ~/.local/bin/ssh-through-bigbox.sh
```

### 5. Customize the skill's triggers

Edit the `description:` in `SKILL.md` to name your machine and the phrases you'll
actually use, so the assistant knows when to reach for it.

## Using sandbox mode (from the sandbox)

```bash
# bring the tunnel up and load $REMOTE_SSH / $REMOTE_CDP (any shell):
eval "$(bash scripts/connect.sh --print-env)"

# after that, within the same session, just:
. ~/.chisel-remote/env.sh

$REMOTE_SSH "uname -a"
$REMOTE_SSH "cd ~/project && git status"
```

See `SKILL.md` for the per-call precedence, failure modes, and the self-healing
behavior. The tunnel state (cached binary, staged key, env file) lives in
`~/.chisel-remote/` and survives across sessions on a persistent disk; the live
connection does not, so one `connect.sh` per session re-establishes it cheaply.

## Using workstation mode

```bash
source scripts/ssh-through-remote.sh    # or: source ssh-through-bigbox.sh
ssh bigbox
git pull                                # remotes over SSH work too
source scripts/ssh-through-remote.sh off
```

Sourcing sets `SSH_THROUGH_CHISEL=1` in that shell and checks with `ssh -G`
that ssh picks up the lines; if it does not, it says why, leaves the variable
unset and prints an ALERT. Programs started from the shell inherit the
variable, so scp, sftp, rsync and git go through your machine too. For a
single command, `SSH_THROUGH_CHISEL=1 ssh ...` does the same.

Things to know while it is on:

- Your machine resolves each host name and dials the address, so hosts that
  exist only on your local network (192.168.x.x, `.local` names) are out of
  reach from that shell.
- Each step of a target's SSH handshake travels via your machine, which adds
  one round trip to it per step.
- If the network goes away under the shared connection (a suspend, a switch
  to another network), it gives up after two unanswered 15-second keepalives,
  so a command started meanwhile waits up to about 45 seconds and then opens
  a new connection. Sessions that were open on the old one end.
- `bash scripts/disconnect.sh` closes the shared connection right away, and
  with it every session running on it.
- The `Match exec` lines run a shell for every ssh command, about 13 ms, even
  while the mode is off.

Why not chisel's own SOCKS proxy (`--socks5`)? A chisel client's forwards are
fixed when it starts, so one client can reach any host only through a SOCKS
remote, which needs `--socks5` on the server. The shared SSH connection gives
`ssh <your-machine>` sessions without a handshake each, does not need a
change on the server, and keeps access tied to your SSH key: its control socket sits in a
directory only you can open, where chisel's SOCKS listener is a local port
that any program on the computer can use without credentials, and
`--socks5` lets chisel users exit through your machine (with an authfile, in
chisel 1.12 and later, those with a `socks` entry). For other hosts,
both add the same round trip to your machine per handshake step. ssh cannot
carry UDP, which needs chisel's `udpsocks` remote (1.13 and later on both
ends).

## Browser control

The optional `7799` forward pairs this with the **`cdp-daemon`** skill: start
cdp-daemon on your machine, and drive your real logged-in Chrome from the sandbox
through `$REMOTE_CDP`. Set `CDP_LOCAL_PORT=""` in `config.sh` to disable the CDP
forward entirely if you only want a shell.

## Testing

`test/selftest.sh` runs both modes against the machine in `config.sh`, over
the network: workstation mode when ssh picks up the lines from
`ssh-config.sh`, sandbox mode when the config sets `SSH_KEY`. It checks that
sessions started together share one connection without warnings, that later
sessions reuse it, that a killed or idle connection is replaced, and that
`connect.sh` rebuilds a dead tunnel. `--slow` adds a test that freezes the
chisel client under a shared connection, which takes up to a minute and a
half. The tests end shared connections, so the script refuses to start while
one is up. To test sandbox mode on a workstation, point
`CHISEL_REMOTE_CONFIG` at a config that sources `config.sh` and sets
`SSH_KEY`, a free `SSH_LOCAL_PORT` and its own `STATE_DIR`.

## Security

This is, by design, remote shell access to your machine. Treat it accordingly:

- Use a **dedicated key** for sandbox mode (above), not a personal one, so
  you can revoke it independently by removing one `authorized_keys` line.
- The chisel credentials decide who can open the tunnel; keep them secret and
  rotate them if leaked. An `--authfile` (above) also limits which addresses
  they can reach.
- The chisel server binds to loopback behind your proxy, so the machine isn't
  exposing a new public port beyond your existing HTTPS vhost.
- A shared connection's control socket gives a shell on your machine to
  whoever can open it, so the sockets live in `STATE_DIR` (mode 700), or in a
  private directory in `$XDG_RUNTIME_DIR` or `/tmp` that must belong to you
  and be closed to everyone else.
- `config.sh`, keys, and token files are gitignored. Double-check `git status`
  before committing: secrets should never enter the repo.
