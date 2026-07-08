# remote-shell-over-chisel

Run commands on a machine you own from a sandbox whose egress only allows
HTTPS. Many hosted code/agent sandboxes front their network with a TLS-MITM L7
proxy that passes valid HTTPS and rejects everything else, including raw SSH on
any port. This skill tunnels SSH (and optionally a Chrome DevTools port) inside
an HTTPS WebSocket using [chisel](https://github.com/jpillora/chisel), so the
assistant gets a real shell on your box without poking a hole for raw SSH.

Nothing machine-specific or secret lives in this repo. All host, user, and
credential details are read from a gitignored `config.sh`.

## How it works

```
sandbox  --HTTPS WebSocket-->  https://<your-host>/chisel  (nginx reverse proxy)
         --> chisel server (--reverse) running on your machine
         --> forwards loopback ports back into the sandbox:
               sandbox:2222 -> your-machine:22   (ssh)
               sandbox:7799 -> your-machine:7799  (cdp-daemon, optional)
```

The assistant then runs `ssh -p 2222 user@127.0.0.1 "..."` inside the sandbox,
which lands on your machine's sshd.

## Setup

### 1. Server side: chisel + reverse proxy on your machine

Install chisel (same binary is server and client):

```bash
curl -fsSL https://github.com/jpillora/chisel/releases/download/v1.10.1/chisel_1.10.1_linux_amd64.gz \
  | gunzip > /usr/local/bin/chisel && chmod +x /usr/local/bin/chisel
```

Run a reverse server bound to loopback, with a shared secret:

```bash
chisel server --reverse --auth "user:$(head -c24 /dev/urandom | base64)" \
  --host 127.0.0.1 --port 8080
```

Note the `user:secret` you chose. That's your `CHISEL_TOKEN`. Put the server
behind your existing HTTPS vhost so it's reachable as `https://<host>/chisel`.
nginx:

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
idle connections.

### 2. A dedicated SSH key

Generate a key used *only* for this, and authorize it on the machine:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/id_remote_shell -N "" -C "remote-shell-over-chisel"
cat ~/.ssh/id_remote_shell.pub >> ~/.ssh/authorized_keys
```

Keep the private key wherever you'll point `SSH_KEY` at, outside this repo.

### 3. Client config

```bash
cp config.sh.example config.sh
$EDITOR config.sh
```

Set at least `CHISEL_SERVER`, `REMOTE_USER`, `SSH_KEY`, and the token
(`CHISEL_TOKEN_FILE` pointing at a file with your `user:secret`, or
`CHISEL_TOKEN` inline). Stash the token outside the repo, e.g.:

```bash
mkdir -p ~/.config/chisel-remote
printf 'user:secret\n' > ~/.config/chisel-remote/token
chmod 600 ~/.config/chisel-remote/token
```

### 4. Customize the skill's triggers

Edit the `description:` in `SKILL.md` to name your machine and the phrases you'll
actually use, so the assistant knows when to reach for it.

## Using it (from the sandbox)

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

## Browser control

The optional `7799` forward pairs this with the **`cdp-daemon`** skill: start
cdp-daemon on your machine, and drive your real logged-in Chrome from the sandbox
through `$REMOTE_CDP`. Set `CDP_LOCAL_PORT=""` in `config.sh` to disable the CDP
forward entirely if you only want a shell.

## Security

This is, by design, remote shell access to your machine. Treat it accordingly:

- Use a **dedicated key** (above), not a personal one, so you can revoke it
  independently by removing one `authorized_keys` line.
- The chisel `--auth` secret gates who can open the tunnel; keep it secret and
  rotate it if leaked.
- chisel runs reverse, bound to loopback behind your proxy, so the machine isn't
  exposing a new public port beyond your existing HTTPS vhost.
- `config.sh`, keys, and token files are gitignored. Double-check `git status`
  before committing: secrets should never enter the repo.
