---
name: statusline-cachequota
description: >
  Claude Code status line showing prompt-cache health, how many times over the
  conversation has been re-cached, how long the cache lives and when it expires, context
  use, cost, and the 5h/7d usage windows with their renewal times. A resident daemon
  renders it, so it can refresh every second for a few milliseconds. Use when the user
  wants to install or change their status line, add cache or quota indicators, diagnose a
  cache miss that silently re-wrote the whole conversation (health collapses, recached
  jumps), or work out what forks, subtasks and teammates cost in cache terms.
---

# Status line: cache health and quota

A status line for Claude Code showing how much of the conversation is still cached, how
many times over it has been re-cached, and when the cache and the usage windows renew.

```
Fable 5 · effort:max · ctx ████░░░░░░ 39% · cache 1h █████████░ 92% → 03:41 (57:12) ·
recached 0.08x · $12.40 · 5h ██░░░░░░░░ 23% → 10:43 (4:59:58) · 7d ████░░░░░░ 41% →
pet 31.7. 12:00 · b4fbf9e8-263d-4930-afec-0b633bb7dbb3
```

The segments, left to right, are the model, the effort level, the org (only when the
anthropic-proxy is in the request path), the context window, cache health, the re-cache
multiplier, session cost, the 5h and 7d usage windows, and the session id. Every segment hides itself when its input
is missing, so a bare harness renders a short line rather than empty fields.

## Install

### macOS and Linux

```sh
./install.sh
```

That compiles `statusline-client.c` to `~/.claude/statusline-client`, links
`statusline-render.py` and `statusline-command.sh` into `~/.claude`, and prints the
settings block if it is not already wired:

```json
"statusLine": {
  "type": "command",
  "command": "~/.claude/statusline-client",
  "refreshInterval": 1
}
```

This path needs `python3` and a C compiler.

### Windows

```powershell
.\install.ps1
```

This path needs Python 3. The installer copies `statusline-render.py` into the Claude
config directory, smoke-tests it, and prints a Windows-safe settings block. It accepts
`-ClaudeConfigDir` and `-PythonExe` overrides. Windows invokes the renderer directly in
one-shot mode; the resident Unix-socket optimization remains specific to macOS and
Linux.

`refreshInterval: 1` is what makes the countdowns tick. There are no Python packages or
services to install on either path.

## Where things live

| Path | What it is |
| --- | --- |
| `statusline-render.py` | The whole renderer: metric engine, formatting, and POSIX daemon |
| `statusline-client.c` | Source of the macOS/Linux client the harness runs on every refresh |
| `statusline-command.sh` | POSIX wrapper for anyone whose settings still point at the old shell entry point |
| `install.ps1` | Windows installer for the one-shot renderer path |
| `~/.claude/statusline-client` | Compiled macOS/Linux client (a build artifact, never committed) |
| `~/.claude/statusline-render.py` | Installed renderer (a link on POSIX, a copy on Windows) |
| `~/.claude/statusline-cache-health.db` | Per-message state in sqlite, rebuildable from the transcripts |
| `~/.claude/statusline-cache-health.state.json` | Memo used by the one-shot path |
| `~/.claude/statusline.sock` | POSIX daemon socket, mode 0600 |

The database, sidecar and socket are disposable: delete any of them and the next render
rebuilds what it needs. The compiled POSIX client is not, since nothing rebuilds it but
`install.sh`.

Everything resolves through `CLAUDE_CONFIG_DIR` when that is set, falling back to
`~/.claude`. The install script, the client and the renderer all agree on it, so a
non-default config directory gets its own socket, database and daemon.

## The displayed metrics

The harness hands the status line a JSON blob on stdin containing
`context_window.current_usage`, but that is only the most recent API call. Health and
the multiplier describe the whole chain of messages, so the renderer reads the session
transcript instead, incrementally: it stores per-message state in sqlite and parses only
the bytes appended since the last render.

Per API call in the chain:

```
redundancy = max(0, parent context footprint - cache_read_input_tokens)
footprint  = cache_read + cache_creation + input_tokens
```

Redundancy is cached content the call failed to read and therefore had to write again.
Writing genuinely new content, however large, is not redundancy: a skill load that adds
220k tokens reads its parent's footprint in full and scores zero.

**`cache NN%` (health)** is the share of calls that have run since the last one to lose
more than half of what was cached: `(total - last_rewrite + 1) / total`. That losing call
restarts the count, so a full rewrite drops health to `1/total` and every clean call
afterwards raises it again. Compaction boundaries and `isCompactSummary` entries reset the
count to zero: a compacted conversation is a fresh start, and `logicalParentUuid` (which
links a compacted conversation to what came before it) is deliberately not followed.

**`recached N.NNx`** is cumulative redundancy over the current footprint. Unlike health
it does not heal: it amortizes as the conversation grows, and passes 1.00x once a
conversation has been re-bought more than once over. This is the number that says what
the session wasted. It does reset at a compaction boundary, along with everything else
about the chain, so it measures waste since the last compaction rather than since the
session began.

**The `1h` / `5m` badge** names which TTL class the last write landed in, read from
`usage.cache_creation.ephemeral_1h_input_tokens` / `ephemeral_5m_input_tokens`. It is
dimmed for the normal 1h and alarm-colored for 5m, which on a main session usually means overage.
A call that wrote into both buckets reads `5m+1h`, also alarm-colored, and its expiry is
computed against the shorter window, since that is the one that bites first.

**`→ 03:41 (57:12)`** is the last call plus the TTL window, so it says when this cache
expires and how long is left. The clock time comes first because an idle session stops re-rendering,
which would leave a relative countdown showing a stale value, while a clock time stays
correct on a frozen line. Once lapsed it reads `→ 03:41 (cold)`.

**`5h` / `7d`** come from `used_percentage` and `resets_at` on stdin. The 5h window shows
wall clock plus a countdown, and the 7d window shows a weekday and date (Croatian day names, hardcoded
so the stamp does not depend on locale; `HR_DAYS` is the list to change).

**`org`** appears only when an anthropic-proxy is in the request path. The renderer reads the
OAuth token from `CLAUDE_CODE_OAUTH_TOKEN` or `<config dir>/.credentials.json` purely to
hash it into the proxy's per-credential filename,
`<config dir>/proxy-state/<first 16 hex of sha256("Bearer " + token)>.json`, and reads
`org`, `h5` and `d7` from it (the last two are fractions, so 0.5 means 50%). The token
is never sent anywhere, and when that file does not exist, which is the normal case, the
segment is simply absent and the quota bars fall back to the stdin values.

Bars share one azure to red ramp. Health inverts its color sample (`100 - health`) so a
full bar reads azure and a drained one red, while the fill still tracks the number.

## Architecture

```
harness --stdin JSON--> statusline-client --socket--> daemon (statusline-render.py)
                              |                            |
                              | no daemon answers          | holds sqlite + memo
                              +--> spawns one, retries     |
                              +--> else execs one-shot renderer
```

The POSIX client is C because the harness runs it once per second per session, and the
whole path costs about 3ms per render. Its daemon holds the sqlite connection and a
per-transcript memo. It exits after 10 idle minutes, and **also right after serving a
render whenever `statusline-render.py` changes on disk**, so editing the engine deploys
it (the next client spawns a fresh daemon). If the socket cannot be reached at all, the
client execs the one-shot renderer, so the line degrades to slow rather than blank.

Windows uses that one-shot renderer directly. Starting Python for every refresh is slower
than the resident POSIX path, but avoids platform-specific socket, daemon, and symlink
machinery while retaining every metric and the persistent sqlite/sidecar cache.

A render is split around its time-dependent fragments: `render_core` returns a template
plus a list of slots, and every serve refills the slots. That is how a cached line can
tick its countdowns each second without re-running the engine.

## Editing it

**Adding a segment.** Append to `parts` in `render_core`. If it is sourced from a stdin
field, also add that field to the tuple in `_fingerprint`, or the daemon's memo will not
notice it changing and the segment will freeze.

**Adding something time-dependent.** Do not inline the clock. Append `SLOT % len(slots)`
to the segment and push `(kind, epoch)` onto `slots`, then format it in `slot_str`.
Anything inlined instead will be frozen into the memo until some other input changes.

**Changing the stored columns.** Add the column and update `NODE_COLS` / `FILE_COLS`. The
mismatch check drops and rebuilds both tables on the next render; there is no migration
path and there should not be one, because the transcripts are the source of truth.

**After any edit, the running daemon serves one more render before exiting**, because
the source check happens after the response goes out. The first line you see following
an edit is still the old code, which is worth remembering before chasing a phantom bug.

## Troubleshooting

Paths below are written as `~/.claude`; read them as your `CLAUDE_CONFIG_DIR` if you set
one.

| Symptom | Cause and fix |
| --- | --- |
| Line is blank | Run the one-shot path to see the traceback: `echo '{}' \| python3 -ES ~/.claude/statusline-render.py` on POSIX, or `'{}' \| python -X utf8 -ES $HOME\.claude\statusline-render.py` in PowerShell |
| Numbers look impossible | Delete `~/.claude/statusline-cache-health.db*` and the sidecar. On POSIX also delete `~/.claude/statusline.sock`, so the live daemon is replaced by one that rebuilds from the transcripts |
| Edits do not show up | POSIX: the daemon exits on source change; if one is wedged, `rm ~/.claude/statusline.sock` and render again. Windows: rerun `install.ps1` to copy the edited renderer |
| Countdown frozen | The line only re-renders when the harness refreshes it. Check `refreshInterval` is set, and remember an unfocused session does not tick |
| Health reset to 100% for no reason | The chain of messages broke. An entry whose `parentUuid` names a message that is not in the file continues the current chain by design; only a genuinely null parent starts a new count |
| `5m` badge on a main session | Almost always overage. Writes are cheaper but the window is twelve times shorter, so gaps get expensive |

## Prompt-cache facts behind these numbers

All measured from real transcripts, in a session on Claude Fable 5 (input $10/M, so a
cache read costs $1/M, a 5m write $12.50/M, a 1h write $20/M).

**Reads are found by a bounded walk, not an index.** Each `cache_control` marker probes
at most 20 content blocks backwards for an existing entry, and a request may carry at
most 4 markers. A single round trip that appends more than 20 blocks therefore strands
the previous entry out of reach, and the server rewrites the whole prefix at the write
rate even though the entry is sitting there valid. Measured: a batch of 10 parallel tool
calls appended 22 blocks (thinking, text, 10 `tool_use`, 10 `tool_result`), stranding the
previous entry by two blocks and re-writing 315,884 tokens, about $6.30. The fix is
client-side and documented by Anthropic (an intermediate breakpoint every ~15 blocks in
long turns); the harness does not do it, which is why the line reports it.

**The TTL depends on what kind of context it is, not on the model.** A main session
writes at 1h, and so does a teammate, which is a session with its own mailbox. Plain
subagents and `/subtask` workers write at 5m, on Haiku and on Fable alike. A parked
subagent therefore goes cold in five minutes where a parked teammate lasts an hour.

**Forking and subtasking cost very different amounts.** A `/subtask` keeps the parent's prefix
byte-identical and appends its directive, so it reads the parent's cache (measured: 431k
read for about $0.43, 2.3k written) and incidentally refreshes the parent's TTL. A
`/fork` pays for the whole prefix on its very first call: its own identity (worktree,
cwd, job id) diverges from the parent's about 22k tokens in, so it read 22,031 tokens
and rewrote 407,325, about $8.15 at the time. Fork early, while the prefix is small.

Read a fork's transcript carefully. The file opens with a verbatim copy of the
parent's history, including the parent's `message.id` values and original timestamps, so
the early entries look like fork activity with healthy cache reads. The fork's own calls
are the ones whose `message.id` does not appear in the parent transcript. Filter on that,
not on timestamps.

**1h versus 5m is decided by idle gaps, not by write volume.** On a measured session
(80 calls, 713,730 tokens written, five gaps longer than five minutes), the 1h window's
write bill came to $14.27, against $8.92 of cheaper writes plus $2.70 of keepalive reads
for a 5m plus heartbeat scheme: about $2.66 apart, and the difference was almost
entirely one stranding event. A single hour-long absence swings it decisively back to 1h. Keepalives
are a proxy feature, not something the harness can do: they must replay the request body
byte for byte with `max_tokens: 0`.
