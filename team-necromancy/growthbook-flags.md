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
