# GrowthBook flag registry

What Claude Code's server-side feature flags do, annotated from the binary.
Companion to `upstream.md`. Scope is Claude-Code-wide, not this tool's, but it
lives here because it was started while tracing cross-session messaging and
`upstream.md` cross-references it.

## Why this exists

This box runs with `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` and
`CLAUDE_CODE_GB_DISK_CACHE_WHEN_TELEMETRY_OFF=1` in `~/.claude/settings.json`.
With nonessential traffic off, the binary never fetches flags from GrowthBook;
it reads only the disk cache at `cachedGrowthBookFeatures` in `~/.claude.json`.
So a feature the server would have turned on stays off here unless its flag is
written into that cache by hand. The binary must not rewrite `~/.claude.json`
on its own for this; we choose which flags to add.

A flag read is `nt("name", default)` in the 2.1.226 minified bundle: first
argument the flag name, second the fallback used when the cache has no entry.
`cj("name")` is the async boolean form, default false. `Vue("name", default,
validator)` reads a config object. To see what a flag does, find its read site
and follow the value, never the name — the names are deliberately opaque
(`tengu_thrifty_sonic`, `tengu_hover_rest`), so a guess from the name is worth
nothing. Record only what a read site proves.

## Two ways to turn one on

Some flags also read a `CLAUDE_CODE_*` environment variable and honour it over
the cache. Where one exists, the env var in the `env` block of
`~/.claude/settings.json` is the better switch: it is visible in a 70-line
file rather than buried in a 188KB one, it survives any future cache refresh,
and it needs no textual surgery. Prefer it. Fall back to a cache edit only for
a flag that has no env override.

The env override reads as `nt("flag",default) || Boolean(te.CLAUDE_CODE_X)`, so
setting the env var to `1` forces the feature on regardless of the cached
value. Not every flag has one; most are cache-only. The pairs the 2.1.226
binary actually reads this way:

| env var (settings.json `env`) | flag it forces on |
|---|---|
| `CLAUDE_CODE_HARBOR_KITE` | `tengu_harbor_kite` (cross-session messaging) |
| `CLAUDE_CODE_FLEETVIEW_SIMPLE` | `tengu_fleetview_simple` |
| `CLAUDE_CODE_NEW_INIT` | `tengu_slate_harbor_experiment` |
| `CLAUDE_CODE_LANTERN_PRISM` | `tengu_lantern_prism` |
| `CLAUDE_CODE_ENABLE_MENU_KIND_LANES` | `tengu_mint_lanes` |
| `CLAUDE_CODE_PLUGIN_BINARY_ASSETS` | `tengu_plugin_binary_assets` |
| `CLAUDE_CODE_WALNUT_SPIRE` | `tengu_walnut_spire` |
| `CLAUDE_CODE_ARTIFACT_DIRECT_UPLOAD` | `tengu_cobalt_plinth_direct` |
| `CLAUDE_CODE_DISABLE_NONSTREAMING_FALLBACK` | `tengu_disable_streaming_to_non_streaming_fallback` |

These are only the `flag-OR-env` overrides. Env vars that gate behaviour on
their own without a matching flag (`CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`,
`CLAUDE_CODE_GB_DISK_CACHE_WHEN_TELEMETRY_OFF`, `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS`,
`CLAUDE_CODE_FORCE_SESSION_PERSISTENCE`, `DISABLE_GROWTHBOOK`, and many more)
are a separate set and not listed here.

## How to edit the cache

Textual single-occurrence replace, never a full JSON rewrite, so nothing else
in that 188KB file moves:

1. back up `~/.claude.json` to `/tmp` first, 0600
2. confirm the exact `"flag": value` string occurs exactly once
3. replace, re-parse the result as JSON, check the byte count changed by only
   the expected delta
4. write to a temp file, `os.replace` into place

Done this way for `tengu_harbor_kite` on 2026-08-10; backup
`/tmp/claude.json.bak-1786351982`.

## Enabled here

| flag | default | set to | why |
|---|---|---|---|
| `tengu_harbor_kite` | false | **true** | Master gate for cross-session messaging. `YE()` returns `nt("tengu_harbor_kite",false) or CLAUDE_CODE_HARBOR_KITE`, and every messaging entry point checks `YE()`: the `ListAgents` and `SendMessage` tools' `isEnabled`, the uds/bridge send paths, and the socket startup at boot. This one flag turns the whole feature on. |

## Considered and left off

| flag | default | left at | why not |
|---|---|---|---|
| `tengu_fleetview_peers` | false | false | Adds other live sessions as rows in the agent-view UI (read behind `YE() && nt("tengu_fleetview_peers",false)`). Cosmetic to the fleet view; messaging works without it. Turn on only if the peer list in agent-view is wanted. |
| `tengu_harbor_kite_mode_emit` | false | false | Telemetry only: emits the session's messaging mode as an event (`Hoi()`). Off is correct with nonessential traffic disabled. |
| `tengu_harbor_kite_limits` | `{}` → built-in defaults | absent | Rate-limit config for peer messaging (bucketCapacity, refillPerSecond, dedupWindowMs, maxSelfHops, maxChainLength, maxTrackedSenders, maxQueuedPeerMessages). The validator falls back to built-in defaults per field, so an absent or empty entry is the working default. The cache already holds `{}`. Only override to tighten a specific limit. |
| `tengu_chair_sermon` | false | false | Broadens how attachment reminders are moved into tool results. It is not the switch for message-level system entries, and on the non-promoted fallback path ordinary text steering can already join a string-valued tool result while this flag is false. Leave it off unless its mixed-content relocation is specifically wanted; details below. |

## Mid-turn attachments: `tengu_chair_sermon`

Traced in both 2.1.220 and 2.1.226. The implementation is unchanged apart
from minified symbol names.

Once absorbed into a running turn, a queued mid-turn command is recorded in the
transcript as an `attachment` of type `queued_command`. Its usual transcript
order after a tool call is:

```text
assistant tool_use
user tool_result
attachment queued_command
```

### When a queued command is absorbed into the running turn

The query does not inspect the queue continuously. It takes one snapshot only
after the model has emitted at least one `tool_use`, every tool result has
finished, and `PostToolBatch` hooks have run. Abort, deferred-tool, hook-stop,
requested-end-turn, dynamic-loop-end, max-turn, and suspended-fold paths all
return or skip the snapshot first.

The snapshot asks for commands through priority `next`, which includes `now`
and `next` but excludes `later`. On the main thread the eligibility filter keeps
entries addressed to the main agent; in a subagent it only keeps targeted task
notifications. Slash-like inputs are excluded unless slash processing was
explicitly skipped. The attachment generator then emits `queued_command` only
for modes `prompt` and `task-notification`; any other selected mode is ignored by
that generator and excluded from the command-removal batch.

Selected entries with UUIDs are marked fold-in-flight while their
`queued_command` attachments are generated. The attachments are appended after
the tool results, and the model is called recursively with the combined history.
Commands that arrive after the snapshot wait for a later opportunity or for the
outer queue drain.

A response with no `tool_use` never reaches this code. Claude Code runs Stop
hooks and completes the turn before checking the queue. A message typed while
final prose is streaming, after the last tool-boundary snapshot, or while Stop
hooks run cannot change that response; it becomes a new top-level user turn.
This is the timing-dependent reason some messages act as immediate steering and
others appear one turn later.

Removal happens after each attachment has already been yielded to the transcript:

- If selected commands produce no `queued_command` attachment, they remain queued.
- A partial emission logs an error but removes the whole selected batch.
- If the outer abort signal is observed after the yield and before removal, the
  commands remain queued. The attachment may already be in the aborted
  transcript, so the same text can appear again when the command later runs.
- An abort after the removal check does not restore the command.
- `cancel_async_message` cannot remove a UUID while it is fold-in-flight; it
  reports `cancelled:false`. An interrupt with `cancel_queued:true` is the
  separate operation that also cancels fold-in-flight UUIDs.

The attachment converter produces one or more user messages. The normalizer
then decides whether their content stays as user content, is placed inside the
preceding tool result, or is promoted to a message-level system entry. The
chair-sermon flag controls only part of the user/tool-result path.

With `tengu_chair_sermon=false` (the default):

- If the preceding user content ends in a `tool_result`, that result's content
  is a string, and every new attachment block is text, the text is appended to
  the `tool_result.content` string, separated by blank lines.
- Otherwise the attachment blocks remain separate blocks in the same user
  message, or separate user messages if there is no preceding user carrier.

So false does **not** mean "never fold steering into a tool result". On this
non-promoted path, plain-text steering next to an ordinary string-valued tool
result is already folded.

With `tengu_chair_sermon=true`:

- Attachment text not already wrapped as a `<system-reminder>` is wrapped first.
- When the preceding user carrier ends in a `tool_result`, non-`tool_result`
  attachment blocks are offered to that result even when its existing content
  is an array or mixed content. Attachment `tool_result` blocks remain after it.
- A final pass first merges adjacent user messages, then moves any remaining
  top-level `<system-reminder>` text blocks into the last `tool_result` in that
  user message.

The broader merge has deliberate exclusions and lossy cases:

- A tool result containing a `tool_reference` block refuses the relocation.
- For an error tool result, non-text attachment blocks are discarded before
  merging; if no text remains, the original error result is retained.
- Text blocks are trimmed and adjacent text is coalesced. Mixed media can
  therefore have different placement from the false path.

### Why this flag often has no effect on current steering

Before the chair-sermon branch, the normalizer checks whether the selected
model supports Anthropic's mid-conversation system messages. If support is on
and the attachment conversion is text-only, the attachment is consumed by an
earlier branch and accumulated as `api_system`; execution never reaches the
chair-sermon code for that attachment.

For the normal post-tool case, the outbound order is then:

```text
assistant tool_use
user tool_result
system steering reminder
```

The stable Claude Code harness remains in the request's top-level `system`
field. The later reminder is a separate message-level `role: "system"`; Claude
Code does not splice it into the top-level system prompt.

This promotion is not controlled by GrowthBook. The capability check, called
`Ser` in 2.1.220 and `wDr` in 2.1.226, runs in this order:

1. HIPAA mode disables it.
2. `CLAUDE_CODE_FORCE_MID_CONVERSATION_SYSTEM=1` forces it on otherwise.
3. Per-model metadata feature `mid_conversation_system`, when present, wins.
4. Explicit older Claude model families are denied.
5. Model capability `mid_conv_system`, Mythos 5, or one of the eligible provider
   routes (`firstParty`, Anthropic AWS/Google Cloud, Foundry, or Mantle) enables
   it.

When enabled, Claude Code sends the beta
`mid-conversation-system-2026-04-07`. Sonnet 5 is a special case that retains a
`<system-reminder>` wrapper inside the message-level system content; other
eligible models receive the unwrapped text. Only text-only attachments qualify
for promotion.

If the endpoint rejects the system role or beta, the `midConvFallback` path
normalizes the request again with promotion disabled, returning the reminder to
the user/tool-result representation described above.

### Direct CLIProxyAPI behaviour

CLIProxyAPI 7.2.116 already handles Claude's two kinds of system content
separately on its direct Claude-to-Codex route:

- top-level `system` text becomes a Responses `role: "developer"` message;
- a message-level `role: "system"` reminder becomes a `role: "user"` message at
  the same history position, wrapped in `<system-reminder>`.

For the promoted text-only case, the resulting order is therefore:

```text
assistant function_call
function_call_output
user steering reminder
```

This matches native Codex's output-then-user ordering, although native Codex
uses plain user text rather than Claude's reminder wrapper and explanatory
scaffold.

When mid-conversation-system promotion is unavailable, the fallback
user/tool-result rules matter. With the flag false, all-text input following a
string-valued tool result is already appended to `function_call_output`. With
the flag true, the broader merge and final sweep place more mixed-content
reminders there. Neither form produces a separate steering item once the text
has entered the tool result.

CLIProxyAPI's message-level-system helper always adds an outer reminder wrapper.
Sonnet 5's special already-wrapped system content would therefore acquire nested
`<system-reminder>` tags on this route; the helper does not currently detect a
complete existing wrapper.

Controlled Codex checks followed the correction in every tested direct form:
plain user text, the explanatory scaffold, a full reminder wrapper, a
message-level system reminder, and reminder text appended to tool output. This
is not evidence that the representations are equivalent. When promotion is
available, the chair-sermon flag is bypassed and both flag values take the same
path closest to native user-message ordering; leaving it false avoids the
broader tool-output merge only on fallback and mixed-content paths.

## Already in the cache, messaging/teams cluster

Recorded here because they share the naming and matter to this tool. Values as
found in `cachedGrowthBookFeatures` on 2026-08-07; not changed.

| flag | cached value | what it is |
|---|---|---|
| `tengu_amber_flint` | true | Teams killswitch. Read `nt(...,true)`. With teams opted in via `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS` or `--agent-teams`, this being true is what lets `isAgentSwarmsEnabled` return true. Turning it false would disable agent teams even when opted in. |
| `tengu_harbor` | true | Present and true; not yet traced to a behaviour. |
| `tengu_harbor_permissions` | true | Present and true; not yet traced. |
| `tengu_harbor_prism` | true | Present and true; not yet traced. |
| `tengu_cobalt_harbor_notice` | true | Present and true; not yet traced. |
| `tengu_cobalt_harbor` | false | Read `nt("tengu_cobalt_harbor",false)`; a remote-control-at-startup source. Not traced further. |
| `tengu_harbor_ledger` | list of official plugins (discord, telegram, fakechat, imessage) | A plugin allowlist, not a boolean. |
| `tengu_flint_harbor` | true | Drives the onboarding-guide generator (`tengu_flint_harbor_prompt` carries its full prompt). Unrelated to messaging despite the name. |

The remaining `*_harbor` / `*_kite` entries in the cache
(`tengu_slate_harbor_experiment`, `tengu_quiet_harbor`, `tengu_woven_harbor`,
`tengu_silent_harbor`, `tengu_zinc_harbor`, `tengu_velvet_harbor`,
`tengu_fennel_kite`, `tengu_gypsum_kite`, `tengu_quill_harbor`,
`tengu_harbor_willow`) are present but not traced. Add a row when a read site
is read, not before.

## New gates in 2.1.226, not traced

These are the flags that appear as `nt(...)` reads in 2.1.226 and not in
2.1.220, minus the cluster above. Each is a real gate with a default, but the
behaviour behind it has not been followed to a read site yet. Listed so the
next pass has a worklist; do not annotate one until its value is followed.

`tengu_amber_quill_moth`, `tengu_basalt_loom`, `tengu_bridge_auth_revive`,
`tengu_bridge_selfheal_heartbeats`, `tengu_ccr_stream_event_flush_ms`,
`tengu_ccr_subagent_skip_on_delta`, `tengu_composed_quail` (default true),
`tengu_gb_refresh_interval_minutes`, `tengu_hazel_quire`, `tengu_hover_rest`,
`tengu_luminous_seal`, `tengu_marbled_teal`, `tengu_mcp_discovery_cache`,
`tengu_mcp_issuer_strict_echo`, `tengu_mcp_protocol_negotiation_claudeai`,
`tengu_mcp_protocol_negotiation_http`, `tengu_mcp_protocol_negotiation_stdio`,
`tengu_quartz_thimble`, `tengu_remote_auto_mode_include_destructive_mcp`,
`tengu_sorrel_trellis`, `tengu_teal_corbel`, `tengu_thrifty_sonic`,
`tengu_umber_lattice`, `tengu_violin_wood` (async bool).

62 further new names in 2.1.226 are event names passed to the telemetry
emitter, not flags read for a value. They are not gates and are not listed.
