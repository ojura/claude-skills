---
name: sudo-listener
description: >
  Self-contained, PSK-authenticated localhost root-command channel in one Python file
  (server + client, no socat, no dependencies). Run as root, the server binds 127.0.0.1 and, for
  each request, checks an HMAC-SHA256 signature made with a 0600 pre-shared key (auto-generated,
  never sent over the socket). It rejects replays by requiring each request to carry a newer
  timestamp than the last one it ran, then runs the command as root and streams stdout, stderr,
  and the exit code back, logging every request and its output. It is a loopback-only, auditable
  alternative to passwordless (NOPASSWD) sudo for letting a local non-root process or AI agent run
  controlled root commands. Use it when something unprivileged needs root without NOPASSWD sudo,
  or when you want an audited root channel on a box. SECURITY: anyone who can read the PSK file can
  run any command as root through it, so it is exactly as strong as that file's 0600 permissions
  plus the loopback bind.
---

# sudo-listener

One file, both ends, no extra processes: an authenticated root command channel over
`127.0.0.1:9999`. `sudo_listener.py serve` (run as root) is the listener; `sudo_listener.py run
'<cmd>'` (run as you) is the client.

## Deploy

```bash
# Server: run as root. "serve" is the default; the audit log goes to its stderr.
sudo python3 sudo_listener.py                       # foreground, log in this terminal
sudo setsid python3 sudo_listener.py >>/tmp/rootlistener.log 2>&1 &   # detached + logfile
```

On first run it generates a pre-shared key at `~/.rootrun_psk` (the invoking user's home, via
`$SUDO_USER`), `chmod 0600`, chowned to that user so the unprivileged client can read it. It is
**not** a systemd service; it does not survive reboot. To auto-start, wrap it in a unit:

```ini
# /etc/systemd/system/sudo-listener.service
[Service]
ExecStart=/usr/bin/python3 /path/to/sudo_listener.py
Restart=always
[Install]
WantedBy=multi-user.target
```

## Use

```bash
python3 sudo_listener.py run 'id -un; whoami'        # -> root
python3 sudo_listener.py run 'apt-get update'        # stdout/stderr stream back; real exit code
python3 sudo_listener.py --port 9000 run '...'       # non-default port
python3 sudo_listener.py -h                           # flags + env + defaults
```

The client exits with the command's real exit code, and stdout/stderr arrive on the client's
stdout/stderr separately. Wrap it in a 3-line shim if you want a short alias:

```bash
#!/bin/bash
exec python3 /path/to/sudo_listener.py run "$@"
```

## How it works

- **Auth:** request line is `<ts_ns> <nonce_hex> <hmac_hex> <b64cmd>`; the server recomputes
  `HMAC-SHA256(psk, "<ts_ns>.<nonce_hex>.<b64cmd>")` and `compare_digest`s it. The PSK never
  crosses the wire. The command is base64 so exact bytes are authenticated and shell quoting
  can't survive transport.
- **Anti-replay:** the server remembers the newest timestamp it has accepted (one number in
  memory, set to the time the server started) and accepts a request only if its timestamp is
  newer. So if someone captures a request and sends it again, its timestamp is no longer the
  newest and the server rejects it. This needs no list of past requests and nothing on disk,
  because the client and server run on the same host and read the same clock. If two requests
  race and the one with the later timestamp is handled first, the other now looks stale and is
  rejected; the client just re-signs with a fresh timestamp and retries.
- **Transport:** length+tag framing - tag 1 stdout, 2 stderr, 3 exit (4-byte signed rc), 4
  reject(reason). Reading the request as a single newline-terminated line means the client never
  half-closes, so there are no socket linger/timeout games.
- **Concurrency:** one thread per connection; the command runs as a `bash -c` subprocess with
  `stdin=/dev/null`. Clean shutdown on SIGINT/SIGTERM.
- **Audit:** every request logs `RUN: <cmd>` and `DONE: exit=.. in ..s (out/err bytes)`, plus
  the command's full output (suppress with `--quiet`).

## Config

Flags `--host --port --psk --quiet` (and `-h`); env `ROOTRUN_HOST / ROOTRUN_PORT / ROOTRUN_PSK /
ROOTRUN_QUIET`. Defaults: `127.0.0.1:9999`, psk `~/.rootrun_psk`.

## Security model & caveats

- **The PSK is a root credential.** Anyone who can read it can run any command as root over the
  socket. Security rests on the `0600` file perms + the `127.0.0.1`-only bind. Do not widen the
  bind; do not loosen the perms.
- Loopback-only: an off-box attacker can't reach it; a local attacker needs the PSK to forge a
  request (capturing one off loopback already requires root). The signed line is never logged.
- It is a deliberate root-execution channel - deploy it only where passwordless-sudo-equivalent
  access for the PSK holder is acceptable.
- Output is streamed through pipes, so a command's stdout is block-buffered (~4-8 KB) unless it
  flushes; output is never lost, only batched. Fine for run-and-exit/capture; for live progress
  UIs of long-running commands, a program may need `stdbuf -oL`/`-u` or a pty (not built in).
