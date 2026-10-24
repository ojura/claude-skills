---
name: statusline-cachequota
description: >
  Claude Code status line showing prompt-cache health, how many times over the
  conversation has been re-cached, how long the cache lives and when it expires, context
  use, cost, and the 5h/7d usage windows with their renewal times. A resident daemon
  renders it, so it can refresh every second for a few milliseconds. Use when the user
  wants to install or change their status line, add cache or quota indicators, diagnose a
  cache miss that silently re-wrote the whole conversation (health collapses, recached
  jumps), show the agent or teammate currently in view, or work out what forks, subtasks and
  teammates cost in cache terms.
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

```sh
./install.sh
```

That compiles `statusline-client.c` to `~/.claude/statusline-client`, links
`statusline-render.py` and `statusline-command.sh` into `~/.claude`, and links this
package into `~/.claude/skills/statusline-cachequota` for native mod discovery.
Run `/reload-plugins` in an existing Claude session. The installer prints the
settings block if it is not already configured:

```json
"statusLine": {
  "type": "command",
  "command": "~/.claude/statusline-client",
  "refreshInterval": 1
}
```

`refreshInterval: 1` is what makes the countdowns tick. It needs `python3` and a C
compiler, and nothing else: there are no packages to install and no services to run.

## Selected agent and teammate

Use the native mod in `hooks/register.js`, tested with Claude Code 2.1.309.
Terminal `AbovePrompt.props.view.agentId` identifies the selection; absent means
main. It also carries external teammate addresses such as `worker@session-…`.
Keep view selection separate from the agent making a `turn.step` call. A local
teammate can have both a resumable loop ID and a roster address, so preserve
`AgentInfo.teammateId` separately.

The mod emits version 2 `CLAUDE_STATUSLINE_VIEW` with the viewer's `session_id`,
`agent_id`, module `activation`, and available name, teammate ID, model and effort.
Its `settings.read` hook wraps only the effective merged `statusLine.command`
with an exported `CLAUDE_STATUSLINE_ACTIVE` token. The hook receives and returns
`{ value: Settings }`; the public `$.settings.read()` call unwraps it.
Source-specific reads and
settings files stay unchanged. Export the token before the original command so
pipelines and compound commands inherit it. The same module always uses the same
token; a reload creates another. The next command after unload carries none.

The C client wraps each native input with `statusline_view`,
`statusline_activation` and `statusline_process` (`CLAUDE_PID` and optional
`TMUX_PANE`). Validate activation, protocol and viewer session before memo lookup.
Reject version 1 owner handles. No `--view-owner` process, file lock, heartbeat or
extra persistent connection is needed. Do not add one: many sessions share this
renderer, and process and write costs matter at team scale.

Before applying selection, register a normal frame's original native metrics in
the shared server. Task metadata inputs are partial and must not replace it.
Ignore volatile durations when comparing peer frames; unchanged metrics cause no
database writes. A peer's own selected subagent must not replace its main frame.

Separate-process teammate lookup is currently Linux-based. Validate the submitted
PID and `/proc/<pid>/stat` start value. For socket requests, derive the client
PID from `SO_PEERCRED` and verify it descends from the claimed Claude process.
For one-shot rendering use the renderer's actual PID as the descendant. Never
accept a transport-origin PID from JSON. Check `<config>/sessions/<pid>.json`
and its session ID when present; separate-process teammates can have no native
registry entry. Cache only argv identity by `(pid, start)`: `/clear` can change
the session without changing the process. Extract the exact launch flags
`--agent-id`, `--team-name` and `--parent-session-id`. Resolve the roster address
against the current team's member and pane. Normalize the team directory with
`re.sub(r"[^a-zA-Z0-9]", "-", team).lower()` but compare roster names and addresses
exactly. A child's exact `--parent-session-id` can identify its current leader
when the roster's `leadSessionId` still names a session from before resume or
`/clear`. Require exactly one matching live process. Never pick whichever
session or transcript was newest. Missing,
reused or ambiguous identities show `teammate unavailable`, with no parent model,
context or cost. All peer processes must use this client and config directory.

Local agents use `<main transcript stem>/subagents/agent-<id>.jsonl`. A selected
local agent uses its own latest complete response and cache history. A selected
peer uses its own session transcript and native frame. Latest transcript usage
wins over a stale native frame; retain a native context limit or effort only when
its model matches. Unknown context limits show tokens without a percentage.
Keep a selected peer's native quota values and reset times together. Do not use
the viewer's quotas or the shared daemon's credential/proxy lookup for that peer;
omit unavailable account data. Local subagents retain parent account usage.
Cost is labeled `session $…`: the parent session for
a local subagent, or the peer's own session for a separate-process teammate.

`settings.json` supplies a silent `subagentStatusLine` metadata command. The
client's `--task-metadata` mode marks its native task rows with
`statusline_task_metadata`; ingest without producing task-row text. Native task
model, effort and context-window size belong only to that corresponding agent.
A local subagent's five-minute cache is normal; a teammate retains the main
session's TTL badge treatment.

Run `python3 -m unittest discover -v`, `claude plugin validate .`, and
`claude plugin test .` after changing this integration. Native tests cover view
races and command decoration; real terminal fixtures must cover plugin
unload/reload, separate-process teammates, return to main, and shared-server
resource use. A test of `desktop`/`mobile` render surfaces does not test a peer
process.

## Where things live

| Path | What it is |
| --- | --- |
| `statusline-render.py` | The whole renderer: metric engine, formatting, daemon |
| `statusline-client.c` | Source of the client the harness runs on every refresh |
| `statusline-command.sh` | Thin wrapper for anyone whose settings still point at the old shell entry point |
| `hooks/register.js` | Native mod observing selected transcript and per-agent model/effort |
| `.claude-plugin/plugin.json` | Plugin manifest, including the existing skill |
| `settings.json` | Native task metadata feed; a user's own setting takes precedence |
| `~/.claude/statusline-client` | The compiled client (a build artifact, never committed) |
| `~/.claude/statusline-cache-health.db` | Per-message state in sqlite, rebuildable from the transcripts |
| `~/.claude/statusline-cache-health.state.json` | Memo used only by the one-shot fallback path |
| `~/.claude/statusline.sock` | Daemon socket, mode 0600 |

The database, sidecar and socket are disposable. Transcript state rebuilds on the
next render; live peer frames repopulate on their next native refresh. The compiled client is not, since nothing rebuilds it but
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
the session wasted. A compaction boundary keeps the redundant-token total and
starts a fresh cache-health chain.

**`cache miss Nm ago`** reports the latest substantial loss of reuse, using the
same greater-than-half threshold as health and a 2,048-token noise floor. It is
time since the event, not the idle gap before it. Do not infer eviction or expiry
from a fixed gap: usage counts do not establish the cause. Omit event age when
the event has no usable timestamp.

**The `1h` / `5m` badge** names which TTL class the last write landed in, read from
`usage.cache_creation.ephemeral_1h_input_tokens` / `ephemeral_5m_input_tokens`. It is
dimmed for the normal 1h and alarm-colored for a main session's 5m, which can mean overage.
A selected subagent's 5m badge is neutral because that TTL is normal there.
A call that wrote into both buckets reads `5m+1h`, also alarm-colored, and its expiry is
computed against the shorter window, since that is the one that bites first.

**`→ ~03:41 (57:12)`** estimates the TTL boundary from the last timestamp and known
TTL. The API starts its TTL at request start; an assistant transcript timestamp
does not establish that exact instant. After the estimate passes, use
`(TTL elapsed)`, or `(5m TTL elapsed)` for a mixed cache. That does not prove all
cached content has been removed. Unknown TTL gets no expiry estimate. See the
[current API documentation](https://platform.claude.com/docs/en/build-with-claude/prompt-caching).

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
Selected separate-process teammates bypass this lookup because the daemon's
credential is not established as theirs; use their original native quota fields.

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

The client is C because the harness runs it once per second per session, and the whole
path costs about 3ms per render. The daemon holds the sqlite connection and a
per-transcript memo. It exits after 10 idle minutes, and **also right after serving a
render whenever `statusline-render.py` changes on disk**, so editing the engine deploys
it (the next client spawns a fresh daemon). If the socket cannot be reached at all, the
client execs the one-shot renderer, so the line degrades to slow rather than blank.

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
| Line is blank | A render raised inside the daemon. Run the one-shot path to see the traceback: `echo '{}' \| python3 -ES ~/.claude/statusline-render.py` |
| Numbers look impossible | Delete `~/.claude/statusline-cache-health.db*` **and** `~/.claude/statusline.sock`, so the live daemon (which holds an open handle to the deleted file plus an in-memory memo) is replaced by one that rebuilds from the transcripts. `statusline-cache-health.state.json` is the one-shot path's memo and is worth deleting in the same breath |
| Edits do not show up | The daemon exits on source change; if one is wedged, `rm ~/.claude/statusline.sock` and render again |
| Countdown frozen | The line only re-renders when the harness refreshes it. Check `refreshInterval` is set, and remember an unfocused session does not tick |
| Health reset to 100% for no reason | The chain of messages broke. An entry whose `parentUuid` names a message that is not in the file continues the current chain by design; only a genuinely null parent starts a new count |
| `5m` badge on a main session | Almost always overage. Writes are cheaper but the window is twelve times shorter, so gaps get expensive |

## Prompt-cache facts behind these numbers

All measured from real transcripts, in a session on Claude Fable 5 (input $10/M, so a
cache read costs $1/M, a 5m write $12.50/M, a 1h write $20/M).

**Historical lookup-window experiment.** A measured batch of 10 parallel tool
calls was followed by 315,884 tokens of repeated work, about $6.30. The original
explanation counted its 22 individual blocks against the lookup window. Current
API documentation groups consecutive tool-use and tool-result blocks into one
position each. Keep the measurement as history; do not treat ten parallel calls
as proof of a lookup-window miss or infer that cause from the statusline alone.

**The TTL depends on what kind of context it is, not on the model.** A main session
writes at 1h, and so does a teammate, which is a session with its own mailbox. Plain
subagents and `/subtask` workers write at 5m, on Haiku and on Fable alike. A parked
subagent therefore has a five-minute TTL, while that teammate has a one-hour TTL.

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
