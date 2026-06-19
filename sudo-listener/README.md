# sudo-listener

An authenticated **root command channel** over loopback, in a single Python file
that is both the server and the client. The server runs as root and binds `127.0.0.1:9999`; an
unprivileged client signs a command with a shared key, sends it, and gets the command's
stdout/stderr/exit code streamed back. It is an **auditable alternative to passwordless
(`NOPASSWD`) sudo** for letting a local non-root process - for example an AI coding agent - run
controlled root commands.

No dependencies, no socat, no helper processes: just `python3` and one file.

## What it does

- **Server** (`sudo python3 sudo_listener.py`): binds loopback, serves each connection in a
  thread, authenticates it, runs the command as root, streams the reply, and logs every request
  (and its output) to stderr as an audit trail.
- **Client** (`python3 sudo_listener.py run '<cmd>'`): signs and sends the command, prints its
  stdout/stderr, and exits with the command's real exit code.
- **Auth**: HMAC-SHA256 over a pre-shared key kept in a `0600` file that the server
  auto-generates on first run. The key is never transmitted - only an HMAC of each request.
- **Anti-replay**: each request carries a timestamp, and the server accepts it only if it is
  newer than the last request the server ran. A captured request, sent again later, carries an
  older timestamp, so the server rejects it. This needs no stored list of past requests and
  nothing on disk, because the client and server are on the same machine and read the same clock.

## When to use it

When an unprivileged process needs to run root commands and you don't want to hand it blanket
`NOPASSWD` sudo - you get a single, auditable choke point (every command is logged), a key you
can rotate by deleting one file, and a loopback-only attack surface. Equally useful as a
lightweight, dependency-free root RPC for automation on a single host.

## Quick start

```bash
# 1. Start the listener as root (first run generates ~/.rootrun_psk, 0600, owned by you)
sudo setsid python3 sudo_listener.py >>/tmp/rootlistener.log 2>&1 &

# 2. Run commands as yourself
python3 sudo_listener.py run 'id -un'            # -> root
python3 sudo_listener.py run 'systemctl restart nginx; echo done'
echo $?                                           # the command's real exit code

# 3. Watch the audit log
tail -f /tmp/rootlistener.log
```

`-h` prints all flags (`--host --port --psk --quiet`) and the corresponding
`ROOTRUN_HOST/ROOTRUN_PORT/ROOTRUN_PSK/ROOTRUN_QUIET` env vars.

## Protocol (so you can write your own client)

Request, one newline-terminated line:

```
<ts_ns> <nonce_hex> <hmac_hex> <base64(command)>\n
hmac = HMAC-SHA256(psk, "<ts_ns>.<nonce_hex>.<base64(command)>")
```

Reply, a stream of `tag(1 byte) | length(4-byte big-endian) | payload` frames:

| tag | meaning                                   |
|-----|-------------------------------------------|
| 1   | stdout bytes                              |
| 2   | stderr bytes                              |
| 3   | exit: payload is a 4-byte signed int rc   |
| 4   | rejected: payload is an ASCII reason      |

A reject reason that starts with `stale` means another request with a newer timestamp was handled
first; it is safe to retry, and the bundled client re-signs with a fresh timestamp and does so
automatically. Other reasons (`auth-failed`, `malformed-request`, ...) are final.

## Security model

This is a **root-execution channel by design**. Its safety rests on two things:

1. **The PSK file is `0600`** (owner + root only). Anyone who can read it can run any command as
   root through the socket, so treat the key as a root credential. Rotate it by deleting the file
   (the server regenerates it on next start).
2. **The bind is `127.0.0.1` only.** Off-box hosts cannot reach it. A local attacker has two ways
   in, and both are closed: forging a fresh request needs the PSK (a root-equivalent credential),
   and replaying a captured one needs a copy of a valid request - but capturing it off the loopback
   interface already requires root, and the signed line is never written to the log.

Do not widen the bind address or loosen the key permissions. Only deploy it where granting the
PSK holder power equivalent to passwordless sudo is acceptable.

## Notes & limitations

- **Not a service / not reboot-persistent.** It's a plain process; wrap it in a systemd unit
  (see `SKILL.md`) if you want auto-start and restart-on-crash.
- **Output buffering.** Output flows through pipes, so a command that uses default stdio
  buffering and doesn't flush will have its stdout batched (~4-8 KB) rather than streamed
  line-by-line; nothing is lost, only delayed. For live progress UIs of long-running commands a
  program may need `stdbuf -oL` / `-u`, or a pty (intentionally not built in, to keep captured
  output clean and stdout/stderr separate).
- **Single host.** Client and server must share the same machine (loopback bind + shared wall
  clock for replay protection).
