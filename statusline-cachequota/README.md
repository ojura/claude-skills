# Seeing what the prompt cache is doing

Claude Code re-sends your whole conversation to the model on every request, and the
prompt cache is what keeps that from costing full price each time. It works silently,
and it fails silently: when a request misses the cache, the conversation gets written
again at double rate, and the reply comes back looking exactly like every other reply.

All of it is recorded in the session transcript, though. Every API call leaves behind
how many tokens it read from cache and how many it had to write, which is enough to
reconstruct what the cache has been doing all session and put it on the status line.

![Historical example of an overnight cache miss. The older display called an elapsed TTL cold. The next request rewrites 420k tokens, and the following frame shows 1% health and a 1.00x recached factor.](docs/cold-resume.svg)

The conversation is intact in all three frames. In this recorded example, the next
request after an overnight gap missed the cache, costing about $8.40 rather than
the $0.42 a cache read would have cost. The third frame records that request: the
health bar empties, and the multiplier records the repeated work. The current
display says `TTL elapsed` where this older illustration says `cold`.

## Expiry and renewal times

Each window shows a clock time, then how much is left: `→ ~03:41 (57:12)`
for the cache's estimated TTL boundary, `→ 10:43 (4:59:58)` for the five-hour usage window, and a weekday and
date for the weekly one. The countdowns update once a second.

Putting the clock time first solves a small problem worth mentioning. A status line only
redraws while you are working, so a session you left open an hour ago is still showing
what it showed then, and any countdown in it is wrong by exactly the time you were away.
A clock time remains useful after a pause. Once the estimated boundary passes,
the cache segment says `(TTL elapsed)`. This is a timing estimate, not proof of
cache deletion: the TTL starts at request start, while the transcript supplies
an assistant timestamp. The `~` marks that approximation. Unknown TTLs have no
expiry estimate; a mixed cache identifies the earliest, five-minute boundary.

## The other segments

![The status line: model and effort, a context bar, the cache badge with its health bar and expiry stamp, the recached multiplier, session cost, and the two usage windows with their renewal times.](docs/statusline.svg)

The line also carries the model and effort level, how full the context window is, what
the session has cost so far, how much of each usage window is gone, and the session id.
The context bar embeds a fixed-width used/total label such as `245k/372k` or
`1.1M/1.0M`; without truecolor support it falls back to the same label as plain text.

### Terminal colors and narrow layouts

`NO_COLOR` disables ANSI styling. `CLAUDE_STATUSLINE_COLOR=always` forces truecolor
and `CLAUDE_STATUSLINE_COLOR=never` disables it; the default detects `COLORTERM` or a
`direct` terminfo entry. Set `CLAUDE_STATUSLINE_THEME=light` for the light palette
(the default is dark, or inferred from `COLORFGBG`). Palette entries can be overridden
with `CLAUDE_STATUSLINE_{ALARM,MUTED,BAR_DIM,BAR_ON_FILL,BAR_ON_DIM}_RGB`, using
`#rrggbb` or `r,g,b` values.

`CLAUDE_STATUSLINE_COMPACT=1` shortens separators and renewal stamps and omits the
session id. The embedded context value remains because it carries more information per
column than the lower-priority fields it replaces.

## Follow the selected agent

On Claude Code 2.1.309, selecting a local subagent or a teammate in the terminal
switches the status line to that agent. Returning to the main conversation
restores its status line on the next refresh. The selected agent's name or ID
remains visible in compact mode.

| Segment | Local subagent | Teammate in another process |
| --- | --- | --- |
| Model | Its latest completed response | Its latest completed response, or its own native frame before a response |
| Effort | Its observed setting, when available | Its own session's setting when it matches the response model |
| Context | Input, cache-read and cache-written tokens from its latest response | Its own transcript usage and native context limit |
| Cache health, expiry and recached factor | Its own transcript | Its own session transcript |
| Cost | The parent session's total, labeled `session $…` | The teammate session's total, labeled `session $…` |
| 5h/7d usage | Parent account usage | The teammate's own native quota values and reset times, when available |
| Organization | Parent account's proxy metadata | Omitted until peer-specific proxy identity is available |

When the context limit is unavailable, the context segment shows tokens without
a percentage. A missing transcript or unverified peer shows an unavailable
label. Main-conversation measurements are never substituted for a valid selected
agent whose data is missing.

The mod observes the terminal's `AbovePrompt.props.view` and passes selection
through `CLAUDE_STATUSLINE_VIEW`. Each C client request includes that value and
the effective command's activation token. The renderer checks the viewer's
session ID and activation before using it. Background activity cannot select a
different transcript.

A `settings.read` hook adds the module's activation token to the effective
statusline command. Claude rebuilds those settings when the plugin unloads or
reloads, so the next command rejects any leftover selection from the old module.
Settings files stay unchanged. There is no persistent helper per Claude process,
no ownership file, and no heartbeat. The only resident renderer remains the
existing shared Python server.

For separate-process teammates on Linux, that server registers the original
native statusline frames received from all sessions in the same
`CLAUDE_CONFIG_DIR`. It joins a selected roster address to one live process using
the kernel process start value, the launcher's team identity and the roster's
current tmux pane. Socket credentials prove that the submitting client descends
from the claimed Claude process. Claude's session registry is checked when it
exists; native teammates can legitimately have no registry entry. The running
peer's parent session takes precedence over an old roster lead ID after resume
or `/clear`. A teammate viewing its own subagent still contributes its original
session frame. Ambiguous or stale identities are
unavailable, and idle duration changes do not rewrite the database.

The peer must run this client in the same config directory and have supplied a
native frame. Claude's own blank transcript area for a separate process does not
prevent the statusline from using the peer's transcript and metrics. Local
subagents continue to use the main session's `subagents` directory.

The plugin's `subagentStatusLine` command silently supplies native per-task model,
effort and context-limit metadata. It leaves task-row text alone. If your own
setting overrides that command, selected local transcripts still work and
unknown context limits show a token count.

## Cache health

`cache 92%` is the share of this conversation's requests that have run since the last
time the cache was lost. It sits near 100% while things are working. When a request
fails to find the cache and has to send the conversation from scratch, it drops to
almost nothing and then climbs back as you keep working. Below 25%, the percentage is
alarm-colored so a failing cache outranks moderate context usage visually.

The `1h` beside it is how long this cache lives. On the main conversation, `5m` is
shown in red because the shorter cache lifetime can indicate overage. Subagents
normally use the five-minute cache, so their `5m` badge has a neutral color.

### What a cache miss means

`cache miss 12m ago` reports the latest substantial drop in cache reuse: more than
half of the comparable prior input footprint was not read from cache, after
ignoring differences below 2,048 tokens. The age is time since that request.
It does not diagnose a cause. The previous `hot`, `evicted` and `expired` labels
were based on fixed ten-minute and two-hour gaps, which did not account for the
actual TTL and could mislabel ordinary expiry.

Cache reuse can change because of elapsed retention time, a changed prefix or
configuration, or lookup behavior. Usage counters establish the amount reused.
See Anthropic's [prompt-caching documentation](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)
for the current cache lifetime and invalidation rules.

## The re-cache multiplier

Health recovers, which makes it good for "is this working right now" and useless for
"what has this session wasted". So there is a second number that does not recover.

`recached 0.08x` counts the tokens this conversation has paid for more than once,
measured against its current size. At `0.08x` nearly everything has been bought once. At
`1.00x` the conversation has been paid for twice over. It falls only as the conversation
grows. Compaction starts a fresh cache-health chain while retaining the redundant
tokens already paid for.

## Mid-session cache misses

![Two consecutive renders: before, the cache reads 100% and nothing has been re-cached. After one batch of ten tool calls, health has collapsed to 6% and the multiplier has jumped to 0.89x.](docs/stranded-cache.svg)

The picture above is a historical measurement: cache reuse fell after a batch of
tool calls, with about $6.30 of repeated work. Its explanation of individual
blocks reflects that earlier experiment. The current API groups consecutive
tool-use blocks and consecutive tool-result blocks when searching backward, so
ten parallel calls alone do not establish that the lookup window was exceeded.
The indicator reports the measured miss without attributing it to that mechanism.

## How it runs

Redrawing every second means the renderer has to be cheap, and this one is: about three
milliseconds a redraw, for a line that parses transcripts and keeps a database. A small
C client hands the harness's JSON to a resident Python renderer over a socket, and the
renderer answers most redraws from memory, since usually the only thing that changed is
the clock. It exits after ten idle minutes and restarts itself whenever you edit
it. If it cannot be reached at all, the client renders the line the slow way, so the
line never goes blank.

Your transcripts are only read, never written. The tool's own state is one small
database. Transcript state rebuilds from the transcripts; peer frames repopulate
on their next native refresh. If you run a local proxy, your OAuth token is read and
hashed into a filename to find that proxy's usage file; the token is not sent anywhere,
and without a proxy that path never runs. Selected teammates use their own native
quotas and skip this lookup because the daemon's token may belong to another
account.

## Get started

```sh
./install.sh
```

That compiles the client, links the renderer into `~/.claude`, and links the
package into `~/.claude/skills/statusline-cachequota` so Claude Code loads its mod.
In an existing session, run `/reload-plugins`. The installer prints the settings
block for `~/.claude/settings.json` if the client is not configured:

```json
"statusLine": {
  "type": "command",
  "command": "~/.claude/statusline-client",
  "refreshInterval": 1
}
```

You need `python3`, a C compiler, and Claude Code 2.1.309 for the selected-view
integration. Everything honours `CLAUDE_CONFIG_DIR` if your Claude Code config
lives somewhere other than `~/.claude`. The package also provides its skill so
you can ask Claude Code to change the line:

> add a segment to my status line showing how long the session has been running

### Checks

Run these from this directory:

```sh
python3 -m unittest discover -v
claude plugin validate .
claude plugin test .
```

The Python tests cover the renderer and the compiled client's socket and
fallback paths, peer identity changes and idle registry reuse. Native mod tests
exercise selection races, terminal-surface isolation, teammate identity and the
effective-command wrapper. Live fixtures additionally exercise separate Claude
processes and plugin unload/reload.

## Under the hood

[`SKILL.md`](SKILL.md) has the exact definitions of both cache numbers, the layout of
the daemon, how to add a segment without freezing it, and the measured cache facts
behind all of it: what forking a large conversation costs, why a parked subagent goes
cold in five minutes while a teammate lasts an hour, and when the long-lived cache is
worth its higher write price.
