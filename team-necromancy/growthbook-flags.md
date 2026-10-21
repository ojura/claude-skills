# GrowthBook flag registry

What Claude Code's server-side feature flags do, annotated from the binary.
Companion to `upstream.md`. Scope is Claude-Code-wide, not this tool's, but it
lives here because it was started while tracing cross-session messaging and
`upstream.md` cross-references it.

## Why this exists

Any telemetry-off setting (`DISABLE_TELEMETRY`, `DO_NOT_TRACK`,
`CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`) disables GrowthBook, so Claude Code
does not fetch flags. A flag then reads the disk cache at
`cachedGrowthBookFeatures` in `~/.claude.json` only if
`CLAUDE_CODE_GB_DISK_CACHE_WHEN_TELEMETRY_OFF` is true and the provider is
`firstParty`. Otherwise every flag reads its default, and every feature behind
a default-false flag is off. Since 2.1.293 that variable is ignored, without a
warning, when it comes from a settings file, so it has to be in the environment
Claude Code is launched with; see
[How a flag read resolves](#how-a-flag-read-resolves-21296).

With telemetry off, ordinary flag reads therefore depend on the value in that
cache or on an env override. A feature can impose further requirements:
[native restart](#native-restart-tengu_fancy_wand-21296) requires a payload fetched
by the current process and does not accept a disk-cache value alone. This
registry records the flag reads and the conditions imposed by their callers.

Of the three telemetry-off settings, `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`
also disables `/feedback` and the `SendFeedback` tool.

A process with GrowthBook enabled replaces `cachedGrowthBookFeatures` with each
payload it fetches, so a hand edit of the cache lasts only until the next such
fetch.

A flag read is `k("name", default)` in the 2.1.296 and 2.1.304 bundles, `tt`
in 2.1.234 and `nt` in 2.1.226: first argument the flag name, second the value
returned when the flag cannot be read. `cj("name")` is the async boolean form,
default false.
`Vue("name", default, validator)` reads a config object. To see what a flag
does, find its read site and follow the value, never the name. The names are
deliberately opaque (`tengu_thrifty_sonic`, `tengu_hover_rest`), so the name
does not tell what the flag does. Record only what a read site proves.

## Two ways to turn one on

Some flags also read a `CLAUDE_CODE_*` environment variable and honour it over
the cache. Where one exists, the env var in the `env` block of
`~/.claude/settings.json` is the better switch: it is visible in a short file
rather than buried in the large `~/.claude.json`, it survives any future cache
refresh, and it does not need a hand edit of the cache. Prefer it. Fall back to
a cache edit only for a flag without an env override.

In 2.1.226 the env override reads as
`nt("flag",default) || Boolean(te.CLAUDE_CODE_X)`, so setting the env var to `1`
forces the feature on regardless of the cached value. In 2.1.296 two of them
read the other way: when `CLAUDE_CODE_HARBOR_KITE` or
`CLAUDE_CODE_LOOP_KEEPALIVE` is set, its value decides and the flag is not
read, so `0` turns the feature off; when it is unset, the flag decides. Most
flags do not have an env override and are read from the cache only. The pairs
the binary actually reads, from 2.1.226 unless a row says otherwise:

| env var (settings.json `env`) | flag it overrides |
|---|---|
| `CLAUDE_CODE_HARBOR_KITE` | `tengu_harbor_kite` (cross-session messaging; in 2.1.296 the variable decides when set, and the flag defaults to true) |
| `CLAUDE_CODE_LOOP_KEEPALIVE` | `tengu_kairos_loop_keepalive` (2.1.296, `/loop` keepalive wakeups; the variable decides when set, and the flag defaults to true) |
| `CLAUDE_CODE_FLEETVIEW_SIMPLE` | `tengu_fleetview_simple` |
| `CLAUDE_CODE_NEW_INIT` | `tengu_slate_harbor_experiment` |
| `CLAUDE_CODE_LANTERN_PRISM` | `tengu_lantern_prism` (in 2.1.296, `/skill-doctor`, `/plugin stats` and the plugin Usage menu) |
| `CLAUDE_CODE_ENABLE_MENU_KIND_LANES` | `tengu_mint_lanes` |
| `CLAUDE_CODE_PLUGIN_BINARY_ASSETS` | `tengu_plugin_binary_assets` |
| `CLAUDE_CODE_WALNUT_SPIRE` | `tengu_walnut_spire` |
| `CLAUDE_CODE_ARTIFACT_DIRECT_UPLOAD` | `tengu_cobalt_plinth_direct` |
| `CLAUDE_CODE_DISABLE_NONSTREAMING_FALLBACK` | `tengu_disable_streaming_to_non_streaming_fallback` |
| `CLAUDE_CODE_ENABLE_TODO_TOOLS` | `tengu_rosy_wren` (task tools; both new in 2.1.234) |
| `CLAUDE_CODE_DISABLE_DANGEROUS_RM_TIMEOUT` | `tengu_splendid_horizon` (2.1.296, only the `enabled` field: a true value turns the auto-deny timer off; an env variable cannot set the timer's length) |

These are only the `flag-OR-env` overrides. Env vars that control behaviour on
their own without a matching flag (`CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`,
`CLAUDE_CODE_GB_DISK_CACHE_WHEN_TELEMETRY_OFF`, `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS`,
`CLAUDE_CODE_FORCE_SESSION_PERSISTENCE`, `DISABLE_GROWTHBOOK`, and many more)
are a separate set and not listed here.

`CLAUDE_CODE_SEND_FEEDBACK` looks like one of these overrides but is not: in
2.1.296, `false` turns `SendFeedback` off, while `true` and unset both return
`k("tengu_juniper_relay", false)`, so it cannot turn the tool on.

## How to edit the cache

A cache edit has effect only in processes that read the disk cache, which with
telemetry off means processes launched with
`CLAUDE_CODE_GB_DISK_CACHE_WHEN_TELEMETRY_OFF` in their environment.

Replace the one occurrence as text instead of rewriting the whole JSON, so the
rest of that file stays unchanged:

1. back up `~/.claude.json` to `/tmp` first, 0600
2. confirm the exact `"flag": value` string occurs exactly once
3. replace, re-parse the result as JSON, check the byte count changed by only
   the expected delta
4. write to a temp file, `os.replace` into place

## How a flag read resolves (2.1.296)

`k(name, default)` returns `getFeatureValueWithSource(name, default).value`,
which checks in this order:

1. Overrides (source `override`). Public builds stub both override sources:
   `getEnvironmentOverrides()` returns `null` and `readConfigOverrides()`
   returns `undefined`, so this step never applies. The client is given a
   `readEnvironmentOverrides` dependency that returns
   `CLAUDE_INTERNAL_FC_OVERRIDES`, but it is never called, so that variable does
   not have an effect.
2. If GrowthBook is disabled and the disk cache is not readable while disabled,
   the default (source `disabled`).
3. The value in a payload this process fetched (source `payload`).
4. If this process holds a payload that lacks the name, the default.
5. For seven fixed `tengu_*` names that are never read from disk, the default.
6. `cachedGrowthBookFeatures[name]` from `~/.claude.json` (source `disk`).
7. Otherwise the default.

GrowthBook is enabled (`mg()`) only when all of these hold:

- `DISABLE_GROWTHBOOK` is not true;
- the provider is `firstParty`, or `CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST` is true;
- a host gateway credential is not present;
- the telemetry level is `default`: `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`
  and `DISABLE_TELEMETRY` are unset (any non-empty value counts, `0` included)
  and `DO_NOT_TRACK` is not true;
- `CLAUDE_CODE_CUSTOM_OAUTH_URL` is unset.

The disk cache is readable while GrowthBook is disabled (`nB()`) only when
`CLAUDE_CODE_GB_DISK_CACHE_WHEN_TELEMETRY_OFF` is true, `DISABLE_GROWTHBOOK` is
not true, the telemetry level is not `default`, and the provider is
`firstParty`.
`ANTHROPIC_BASE_URL` does not take part in either check, so a session routed
through a proxy reads flags the same way as a direct one.

### Since 2.1.293 the disk-cache variable must be in the launch environment

`filterSettingsEnv` drops, without a warning, every settings-file env key in a
fixed set of names that settings files may not set. 2.1.293 added
`CLAUDE_CODE_GB_DISK_CACHE_WHEN_TELEMETRY_OFF` to that set; the set in 2.1.292
does not contain it. `DISABLE_TELEMETRY` is not in the set. So when both come
only from `~/.claude/settings.json`, GrowthBook is disabled, the disk cache is
not read, and every flag reads its default (source `disabled`). The tools in
[Tools behind default-false flags](#tools-behind-default-false-flags-21296)
then disappear.

Measured on 2026-10-10 by starting each installed version in print mode and
reading the `tools` array of the stream-json `init` event. The table says
whether `Monitor` and `PushNotification` were in that array; in every run they
were present or absent together.

| version | both variables only in settings.json | both also in the launch environment |
|---|---|---|
| 2.1.292 | present | not run |
| 2.1.293 | absent | not run |
| 2.1.294 | absent | not run |
| 2.1.295 | absent | not run |
| 2.1.296 | absent | present |

Each run used a separate `CLAUDE_CONFIG_DIR`. Its `.claude.json` cache held
`tengu_amber_sentinel` and `tengu_kairos_push_notifications` as `true`, and its
`settings.json` set `DISABLE_TELEMETRY=1` and
`CLAUDE_CODE_GB_DISK_CACHE_WHEN_TELEMETRY_OFF=1`. The launch environment did
not have either variable, except in the last column, where it had both. Every
run also had `ANTHROPIC_BASE_URL=http://127.0.0.1:9` (a closed port, so requests
did not leave the machine; the base URL does not take part in the checks above),
`--settings '{"disableAllHooks":true}'`, `--strict-mcp-config` and
`--no-session-persistence`, and was killed after the `init` event. Print mode
runs as entrypoint `sdk-cli`, which `SendFeedback` and `EndConversation` exclude
in their `isEnabled` checks, so those two cannot be seen this way. `Monitor` and
`PushNotification` do not have an entrypoint condition, so their presence shows
whether the process reads the disk cache.

To keep disk reads, set `CLAUDE_CODE_GB_DISK_CACHE_WHEN_TELEMETRY_OFF=1` in the
environment of whatever starts `claude` (the shell or the launcher). Setting it
in a settings file still works in 2.1.292 and earlier.

## Feature fetching and local responses (2.1.296)

### Endpoint selection

`zBn()` returns the fixed host `https://api.anthropic.com/`, and `zO()` returns
client key `sdk-zAZezfDKGoZuXXKe` (`/tmp/claude-2.1.296.js:2243`).
`CLAUDE_CODE_GB_BASE_URL` is declared in the environment registry but is not read
by this public build's host selector. `ANTHROPIC_BASE_URL` does not redirect
GrowthBook requests either.

The normal request is `POST /api/eval/sdk-zAZezfDKGoZuXXKe`. The SDK builds its
JSON body from `attributes`, `forcedVariations`, `forcedFeatures`, and `url`
(`:2229`). When `tengu_gb_eval_authed_enable` is truthy, the client first tries
`/api/eval-authed/sdk-zAZezfDKGoZuXXKe`; a failed request falls back to the ordinary
endpoint (`:2243`). Authentication headers, when available, accompany these
requests. Attributes can include account, organization, device and session
identifiers and an email address.

A local response can represent an entry from `cachedGrowthBookFeatures` as
`"name": {"defaultValue": value}` inside a top-level `features` object.
`processRemoteEvalPayload()` also accepts `value` in place of `defaultValue`.
Boolean values written directly under `features` are rejected as non-object
entries. Include the intended set of cached flags, because a populated response
makes names missing from it use their defaults instead of disk-cache values.

### Transport and telemetry

The public build supports `HTTPS_PROXY`/`HTTP_PROXY` and `NODE_EXTRA_CA_CERTS`.
A TLS-intercepting local proxy could answer the fixed GrowthBook origin with
local data. This requires proxy routing and a certificate trusted by the client;
it is not a direct localhost endpoint setting. Redirecting DNS or operating-system
traffic would also need trusted TLS and can affect other applications. The
pre-fetch `mg()` check still applies under both approaches.

GrowthBook fetching requires the application's telemetry level to be `default`.
`DISABLE_TELEMETRY` must be unset or empty, rather than set to the nonempty strings
`"0"` or `"false"`; the other conditions under
[How a flag read resolves](#how-a-flag-read-resolves-21296) still apply.
Blocking telemetry transmission through a proxy is a separate configuration from
keeping that application opt-out enabled. Proxy filtering alone does not prove
that a network client which ignores the proxy cannot transmit data.

`tengu_frond_boric` is an object read by `mOt(pipeline)` with default `{}`.
A boolean `firstParty: true` stops first-party event emission and is checked by
that event exporter's `isKilled` callback (`:2274`). `datadog: true` stops Datadog
event dispatch (`:14582`). These fields do not disable the separate first-party
metrics reader. `Dn()` adds that reader independently of the user OpenTelemetry
exporters when `Ve()` permits it (`:36322` and `:36324`). Its exporter can request
`/api/claude_code/organizations/metrics_enabled` and post to
`/api/claude_code/metrics`. `OTEL_LOGS_EXPORTER` and `OTEL_METRICS_EXPORTER` do not
control those first-party event and metric exporters, and `OTEL_SDK_DISABLED`
is not referenced in this build's JavaScript. `DISABLE_ERROR_REPORTING` independently disables the
error-reporting check at `:8323`. When `Ve()` permits first-party metrics, these
controls do not disable that reader, so they do not replace the global
`DISABLE_TELEMETRY` opt-out.

### Initialization, refresh and cache writes

Missing authentication skips the initial SDK `init()` fetch but retains the
client. `initializeClient()` still starts periodic refresh, and a later
`refreshFeatures()` can make an unauthenticated request that a local responder
can accept. The default interval is 360 minutes. A positive numeric value of
`tengu_gb_refresh_interval_minutes` is clamped to a nominal 5 through 360 minutes,
then multiplied by a random factor from 0.9 to 1.1 and capped at 360 minutes
(`:2274`).

Calling `initialize()` while fetching is disabled stores promises that resolve
to `null`. Clearing the telemetry setting alone does not replace those promises.
`refreshAfterAuthChange()` resets the client before initializing it again
(`:2243`). Transport trust and routing must also be configured. A successful
later payload emits a refresh event; the interactive session subscribes with
`eu(this.refilterCommands)` and rebuilds its command list (`:40875`). Command
availability is therefore recalculated after a fetch, rather than fixed at
startup. This does not establish that a running native process can adopt a new
proxy or certificate configuration through environment changes alone.

The fetched response is written back to `cachedGrowthBookFeatures` and updates
`cachedGrowthBookFeaturesAt`. A response containing only `defaultValue` entries
also clears `cachedExperimentFeatures` and `cachedExperimentData`, because the
client rebuilds those from `source: "experiment"`, `experiment.key`, and
`experimentResult.variationId` in the response. The Slack-specific client mode
skips these disk writes. Serving static cached values does not perform fresh
server-side targeting.

The endpoint, initialization and command-refresh checks were matched against the
installed 2.1.296 executable. Thirteen isolated checks using extracted request
and client functions, test SDK dependencies, Bun 1.3.13, and a loopback-only TLS
proxy verified payload handling and refresh behavior. That is not an end-to-end
native transport test: the installed executable embeds Bun 1.4.3. Network
namespace isolation was unavailable in the test environment, so the native
application was not run with feature fetching enabled.

## Traced flags

| flag | default | what it does |
|---|---|---|
| `tengu_harbor_kite` | false | Controls cross-session messaging. `YE()` returns `nt("tengu_harbor_kite",false) or CLAUDE_CODE_HARBOR_KITE`, and every messaging entry point checks `YE()`: the `ListAgents` and `SendMessage` tools' `isEnabled`, the uds/bridge send paths, and the socket startup at boot. This one flag turns the whole feature on; `CLAUDE_CODE_HARBOR_KITE=1` forces it on. In 2.1.296 the check is `xi()`: `Qh().messagingBeyondOwnAgentsDisabled` turns messaging off first, then a set `CLAUDE_CODE_HARBOR_KITE` decides on its own, then on Windows `tengu_harbor_kite_win` (default true) must hold, and last `k("tengu_harbor_kite", true)` decides, so the default is true. |
| `tengu_rosy_wren` | false | Turns on `TaskCreate`/`TaskGet`/`TaskList`/`TaskUpdate`, which are otherwise absent on Opus 4.8+, Sonnet 5+, Fable 5+ and Mythos 5+. `CLAUDE_CODE_ENABLE_TODO_TOOLS=1` forces it on. Details below. |
| `tengu_fancy_wand` | false | Controls native `/restart` and its `/update` alias. In 2.1.296 it requires a fetched true value; the offline cache alone cannot enable the command. Teammates and background sessions are excluded. Details below. |
| `tengu_fancy_wand_copy` | `{}` | Replaces the update notice's restart text when `offer` is a nonempty string. It does not enable `/restart`. |
| `tengu_gb_eval_authed_enable` | false | Tries `/api/eval-authed/<clientKey>` before the ordinary `/api/eval/<clientKey>` endpoint. A failed authenticated request falls back to the ordinary endpoint. |
| `tengu_gb_refresh_interval_minutes` | `null`, meaning 360 minutes | Sets the nominal periodic feature-refresh interval. A positive numeric value is clamped to 5 through 360 minutes before jitter and a final 360-minute cap. |
| `tengu_frond_boric` | `{}` | Boolean fields `firstParty` and `datadog` stop those event pipelines. They do not disable the separate first-party metrics exporter. See the feature-fetching section above. |
| `tengu_fleetview_peers` | false | Adds other live sessions as rows in the agent-view UI (read behind `YE() && nt("tengu_fleetview_peers",false)`). Cosmetic to the fleet view; messaging works without it. Turn on only if the peer list in agent-view is wanted. |
| `tengu_harbor_kite_mode_emit` | false | Telemetry only: emits the session's messaging mode as an event (`Hoi()`). |
| `tengu_harbor_kite_limits` | `{}`, meaning built-in defaults | Rate-limit config for peer messaging (bucketCapacity, refillPerSecond, dedupWindowMs, maxSelfHops, maxChainLength, maxTrackedSenders, maxQueuedPeerMessages). The validator falls back to built-in defaults per field, so an absent or empty entry gives the defaults. Set it only to tighten a specific limit. |
| `tengu_splendid_horizon` | `{}`, meaning built-in defaults | The auto-deny timer on the permission prompt for a dangerous `rm` or `rmdir` in bypass and auto mode. `timeoutMs` (default 120000) is its length, and a disk-cache value for it takes effect. Details below. |
| `tengu_chair_sermon` | false | Broadens how attachment reminders are moved into tool results. It is not the switch for message-level system entries, and on the non-promoted fallback path ordinary text steering can already join a string-valued tool result while this flag is false. Leave it off unless its mixed-content relocation is specifically wanted; details below. |
| `tengu_moth_copse` | false | 2.1.296: `R7()` returns true when this flag is true or when `CLAUDE_MEMORY_STORES` is non-empty, even if that variable fails validation. While `R7()` is true, `Vie()` drops the auto-memory `MEMORY.md` from the context, the memory prompt no longer tells the model to add a pointer to `MEMORY.md`, and `JXr()` runs a per-turn side request that lists the memory files' frontmatter descriptions and attaches up to 5 selected files (first 4 KB each) as `relevant_memories`. Verified 2026-10-14 by capturing requests with `CLAUDE_MEMORY_STORES` set; on Juraj's Max account the selector request answered 429 `rate_limit_error` in 4 of 4 runs while the main request succeeded. |
| `tengu_mill_orange` | false | 2.1.296: adjusts the relevance recall that `R7()` turns on and does not turn local recall on by itself. Read in `JXr()` (account recall starts only while it is false), `QXr()` (when true, the main loop waits up to 2 s for the selector result), the single-token CJK query check, and `VDn()` (placement of `relevant_memories` attachments). |
| `tengu_haze_glass` | false | 2.1.296: required by `li()` and `zr()`. Both return false when `CLAUDE_MEMORY_STORES` is set, and `li()` also when `CLAUDE_CODE_DISABLE_ORG_MEMORY` is set, so they decide whether org memory is on; they do not affect the stores named in that variable. The callers of `zr()` were not followed. |
| `tengu_memory_store_resync_interval_minutes` | 60 | 2.1.296: periodic resync interval of memory-store sync, at least 1 minute. A non-positive value or `CLAUDE_CODE_DISABLE_MEMORY_PERIODIC_RESYNC` turns the periodic resync off. |
| `tengu_linked_cake` | true | 2.1.296: in `fw()`, while true, `autoMemoryDirectory` is read only from enabled setting sources, in the order policy, `--settings`, local and project (only when `lO()` holds), user. |
| `tengu_paper_halyard` | false | 2.1.296: when true, `YZn()` and `$3r()` leave Project and Local CLAUDE.md files out of the context. Read site only, not tested. |
| `tengu_plugin_hooks_modules` | true | 2.1.304: decides whether the hooks modules of plugins the person adds load: marketplace plugins, `--plugin-dir` and `CLAUDE_CODE_PLUGIN_DIRS` folders, and `~/.claude/skills/<name>` folders that hold `.claude-plugin/plugin.json`. Built-in plugins' modules load either way. The loader (`chunk-v7aynt7y.js`) tests `D_()`, which is `!Z() && !Sl() && gxr()`. `gxr()` reads `k("tengu_plugin_hooks_modules", true)` and returns true in place of a served false while `sL()` or `w()` holds (neither traced). `Sl()` is true inside a tool server's daemon; `Z()` returns `Lg()`, not traced. When the value changes in a running session the loader loads or unloads those modules and logs `installed plugins' hooks modules load` (or `unload`) `: the rollout flag (tengu_plugin_hooks_modules) reads on` (or `off`) `in a running session`. Juraj's cache held true on 2026-10-20; that day a `--plugin-dir` plugin's module loaded as `tier user` and its `prompt.compose` hook ran in a `claude -p` request. |
| `tengu_sunny_locket` | `null`, meaning mode `off` | 2.1.304, idle compaction (added in 2.1.299). `m5t()` in `chunk-rncrmfyt.js` reads `k("tengu_sunny_locket", null)` and takes three fields: `mode` (`log`, `compact` or `compact_on_return`; any other value is `off`), `minTokens` (`CLAUDE_CODE_IDLE_COMPACT_MIN_TOKENS` when set, else the field; at least 100000, default 200000) and `fireAtFraction` (clamped to 0.5 through 0.95, default 0.9). The variable sets only `minTokens`, so it cannot turn the feature on. The timer (class `y5t`, armed by `dUn()` each time the main thread's last-turn parameters are saved) runs only where `EK()` holds (an interactive session, or a non-interactive one whose entrypoint is in a set that was not traced; never a `bg`, `daemon` or `daemon-worker` session, nor one where `ab()`, not traced, returns a value) and only after a main-thread request that asked for 1-hour cache (`vY()` is 3600000). It fires at the last activity (the latest main request or the engine's own cache touch) plus `fireAtFraction` of the time from then until the cache expires, about 54 minutes. `m5o()` then does nothing, and logs the reason, when auto-compact is off (`autoCompactEnabled` false, `DISABLE_COMPACT`, `DISABLE_AUTO_COMPACT`), when the setting `idleCompaction` is false, when the saved last-turn parameters are no longer usable (`prefix_changed`), when the TTL is no longer 1 hour, when the timer fired more than 60 s late or the cache is no longer warm, when the context is below `minTokens`, when a newer main request went out, or when the rate-limit status is not `allowed`. Mode `compact` compacts the conversation; `compact_on_return` writes a summary while the session is idle and uses it when the person returns, and also needs `precomputeCompactionEnabled` and `A0()` (not traced); `log` only records `tengu_idle_compact_fired` with the outcome `would_compact`. `LSs()` adds the "Idle compaction" row to `/config` only while the mode is `compact` or `compact_on_return`, together with a second condition (`co`) that was not traced. Juraj's cache held `{"mode":"off"}` on 2026-10-20. With a mode on, a keepalive that wakes a session 59 minutes after its last request (cc-env-overrides) comes after this compaction. |
| `tengu_sequential_cloud` | false | 2.1.304, unchanged from 2.1.296: the settings of Claude Code's own renewal of the main thread's 1-hour cache. `Y3()` in `chunk-rncrmfyt.js` reads `k("tengu_sequential_cloud", false)`. A value that is not an object, or an object that fails the schema `Nio`, reads as off, so `false` is off. The schema: `enabled` must be `true` (`z.literal(true)`); `touchIntervalSeconds` 10 to 120, default 30; `activityWindowMinutes` 1 to 1440, default 60; `inFlightWindowMinutes` 1 to 1440, optional and new in 2.1.304, used in place of the activity window while a turn or a subagent runs when it is the larger; `resumeTouches`, default false. While the flag is on, a main-loop request whose TTL decision is the 1-hour subscriber default, and for which `RTt()` holds (at the first request of a chain, also outside usage overage and with `KD()`, not traced, false), carries the beta header `anthropic-cache-keepalive`, decided per chain of main requests in `ATt()`. The engine then posts `/v1/messages/cache_touch` every `touchIntervalSeconds` while the latest activity (a main request, prompt input, work starting or ending, waiting for the user starting or ending) lies within the window. `RTt()` has the same ten terms as 2.1.296's `jst()`, among them an unset `ANTHROPIC_UNIX_SOCKET`, `kt()` (not traced; `zbe()` gives 5 minutes when it is false, which matches a subscriber check) and the permission `allow_cache_keepalive`. `~/src/cc-env-overrides/docs/cc-cache.md`, section "Claude Code's TTL and its own renewal", gives the rules as traced in 2.1.294. Juraj's cache held `false` on 2026-10-20, so the renewal is off here. |
| `tengu_prompt_cache_1h_config` | `{"allowlist": ["repl_main_thread*", "sdk", "auto_mode", "memdir_relevance"]}` | 2.1.304, unchanged from 2.1.296 (`npe()` there): read in the TTL decision `zbe()` in `chunk-rncrmfyt.js`. It applies after the explicit rules (`FORCE_PROMPT_CACHING_5M`, `CLAUDE_CODE_PROMPT_CACHE_TTL` or `CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL`, the settings key `promptCacheTtl` or `subagentPromptCacheTtl`, an agent's frontmatter TTL, of which a 1-hour one is skipped in usage overage, and `ENABLE_PROMPT_CACHING_1H`), and only while `kt()` holds (not traced; read as the subscriber check) and the account is not in usage overage: a request whose query source matches an entry (a trailing `*` matches any source that starts with the rest) gets 1-hour cache, any other 5 minutes. The engine stores the first allowlist it reads in the host's `requestLatches` (`C5o()`, `R5o()`) and uses that one from then on; what resets the store was not traced. Juraj's cache held the allowlist `repl_main_thread*`, `sdk`, `auto_mode`, `rolling_compact`, `memdir_relevance`, `agent_classifier`, `prompt_suggestion`, `away_summary`, `extract_memories` on 2026-10-20. |
| `tengu_tidy_frost` | false | 2.1.304, unchanged from 2.1.296: read last in `zbe()`, only when the caller passes the main thread's model and the main thread's latest request asked for 5-minute cache and is still live for that model (`N4o()`). A request that would otherwise get the 1-hour subscriber default then gets 5 minutes, reason `live_5m_cache`. The callers that pass the model were not followed. Absent from Juraj's cache on 2026-10-20, so it reads false. |
| `tengu_saffron_lattice` | `{}` | 2.1.305: the Fable billing config, without an env override. `rg()` in `chunk-4msg6xg9.js` parses it with the schema `Zh`: `enabled`, `planLimitsEndDate` (a string), `overageConsentRequired` and `perModel` (model id to `{"enabled": true\|false}`; two spellings of one model with different values count as billed). `fGe(model)` returns the `enabled` of a `perModel` entry for the model when there is one, false when `enabled` is false, and otherwise `overageConsentRequired === true \|\| planLimitsEndDate !== undefined`, so any end date counts, past or future; session 37ecf7d2 read 2.1.220 as applying the rules only once the date had passed. Where `fGe()` is true the usage-credits consent dialog opens before a Fable request; see [Usage-credits consent dialog](#usage-credits-consent-dialog-21305). History: in August 2026 (2.1.220) the cached value `{"enabled": true, "planLimitsEndDate": "2026-07-20T07:00:00Z"}` became readable once `CLAUDE_CODE_GB_DISK_CACHE_WHEN_TELEMETRY_OFF` was set for `tengu_kairos_loop_dynamic`. It opened the consent dialog and replaced Fable with the first non-Fable model in the list, a forced Sonnet, and consenting could not help because `cachedExtraUsageDisabledReason` was `org_level_disabled`. The cached entry was edited by hand to `"enabled": false` (sessions 62f20830 and 37ecf7d2). Since a refetch on 2026-10-01 the server itself sends `{"enabled": false}`, which Juraj's cache still held on 2026-10-21; so in October 2026 this flag is not what opens the dialog. |
| `tengu_saffron_credits_only_tiers` | `["enterprise"]` | 2.1.305: `HUt()` is true for a subscription tier in this list, and for `enterprise` unless `upn()` (not traced). The consent check then reports `admin_billed` and none of the usage-credits dialogs opens. The tier is `gr()`: null unless `Qa()` holds, else the stored credential's `subscriptionType`, which with `CLAUDE_CODE_OAUTH_TOKEN` set is not filled from the claude.ai profile (`vN()` is false); with a null tier the list has no effect. Juraj's cache held `["enterprise"]` until 2026-10-21, when it was edited by hand to `["enterprise", "max", "pro", "team"]` to stop the dialogs. |
| `tengu_calm_seal` | `null` | 2.1.305: the families besides Fable that `Y4()` treats as usage-credits models, so the consent check covers them too. Juraj's cache held `["Mythos"]` on 2026-10-21. |
| `tengu-model-error-overrides` | `{}` | 2.1.305, read with `JR()`, not `k()`: a non-empty `block` string for a model makes `Y5n()` true, and the consent dialog does not open for that model. What the request does then was not traced. |
| `tengu_ember_latch` | false | 2.1.305: one term of `k5t()`, which decides whether the consent dialog offers to buy usage credits in Claude Code ("Yes, buy usage credits") instead of sending the person to claude.ai or to an admin. |
| `tengu_saffron_ladder` | false | 2.1.305: `J2s()` is `Wle() && flag` (`Wle()` not traced); with it the Fable consent dialog offers "Upgrade to Max". |

Reading a flag from a mod: 2.1.304 has a `flag.value` operation
(`$.flag.value(name, fallback)` in the engine's interface builder), but the
table of core nouns that `e$r()` in `chunk-0q47gehe.js` builds leaves `flag`
out (`xE()` is false for it), and the plugin API's types (`claude-code.d.ts`)
do not declare `$.flag`. Whether a mod's call is refused was not tested.
cc-env-overrides reads `cachedGrowthBookFeatures` from `~/.claude.json`
instead (`remoteFeatures()` in its `overrides.js`).

## Usage-credits consent dialog (2.1.305)

The dialog titled "You've reached your Fable limit", or "Fable 5.1 now uses usage credits", with the
choices "Continue with Fable 5.1" and "Not now", is the prompt `fable_overage_consent_prompt`. The engine
(`chunk-ntt9607d.js`) opens it before a request to Fable when `HUe(model)` holds: `Jke(model)`, no
`tengu-model-error-overrides` block for the model, and a session that can show the dialog. `Jke()` is
true when `Pcn(model)` in `chunk-4msg6xg9.js` returns `needs_consent` or `needs_credits`:

1. `Y4(model)` gives the family: Fable, or a family in `tengu_calm_seal`; any other model is
   `included_in_plan`.
2. `LW()` gives `pay_per_request`; it is true outside the first-party provider, when `wt()` is false,
   and for two other terms not traced.
3. `HUt()` (`tengu_saffron_credits_only_tiers`) gives `admin_billed`.
4. `ONr(model)` must be true, otherwise `included_in_plan`. It is `fGe(model)`
   (`tengu_saffron_lattice`) or `F5o()`, the latch `host.accountCreditLatches.usageCreditsRequired`.
5. No consent for the current organization (or account) in the config key `fableOverageConsentV2`
   (`{"<organization uuid>": true}`, or `"acct:<account uuid>"` without an organization; other families
   in `usageCreditsOnlyModelConsent.<family>`), nor for this session, gives `needs_consent`. The current
   organization is the one `oauthAccount` in the global config names, the stored /login account
   (`pn()`), and only while `Qa()` holds: no API key, `apiKeyHelper` or `ANTHROPIC_AUTH_TOKEN` in use,
   among other terms (`Gn()`, `ac()`, `e1()`, `av()` and `Rhe()` were not traced).
   `CLAUDE_CODE_OAUTH_TOKEN` is not one of those terms, so a cc-token override is checked against the
   stored login's organization, not the token's. With `ANTHROPIC_AUTH_TOKEN` set, as by the cc-token
   proxy, `Qa()` is false unless `av()` or `Rhe()` hold, and then only a consent given in the running
   process counts, which a write to the config file cannot supply.
6. `KMe()` false, read from the config key `cachedExtraUsageDisabledReason`, gives `needs_credits`;
   `out_of_credits` there gives `out_of_credits`, which does not open the dialog.

The latch is not a flag. Two places set it to true, and nothing in 2.1.305 sets it back except
`reset()` of its class, for which no caller was found; it lasts for the life of the process:

- the rate-limit tracker (`chunk-p2eb1prj.js`), when a response's headers report overage in use for a
  usage-credits model and the current organization has not consented, with further terms;
- the 429 handler (`chunk-pc3bqzpw.js`), for an error with code `credits_required` and without a
  rate-limit type.

The title is "You've reached your Fable limit" when `fGe()` is false, that is when the latch opened
the dialog, and "Fable 5.1 now uses usage credits" when the flag did.

Without `oauthAccount` in the global config there is no organization (`Aa()` is null), so step 5 reads
only the consent given in the running process and never `fableOverageConsentV2`. Juraj's
`~/.claude.json` had no `oauthAccount` on 2026-10-21, so each new process asked again, whatever token
it used.

A token switch inside a running session does not clear the latch. So after one account hit its weekly
Fable limit, the latch stays set after a switch to another account's token, and the dialog opens before
every Fable request whenever step 5 or step 6 fails: under the cc-token proxy because of step 5, and
otherwise because of step 6 when the cached reason does not count as enabled, although the new account
has Fable usage left. Starting a new Claude Code process clears the latch.

## Native restart: `tengu_fancy_wand` (2.1.296)

The flag controls native `/restart` and its `/update` alias. Both names refer to
one command: its `isEnabled` is `kjt()` and its `isHidden` returns `!kjt()`.
The command is disabled when the flag is missing or is not boolean `true`.

The checks in `/tmp/claude-2.1.296.js:12302`, formatted for reading:

```js
var b5 = "tengu_fancy_wand";
function Sjt() { return lct(b5) || (mg() && ya()); }
function _To() {
  if (!Sjt()) return false;
  return lct(b5) ? k(b5, false) === true : C7()[b5] === true;
}
function kjt() { return bjt() && _To(); }
```

`mg()` checks whether GrowthBook is enabled. `ya()` calls
`hasFreshFeatures()`, which requires a nonempty `remoteEvalFeatureValues` map
populated by a fetched payload. When that map is populated, `C7()` returns its
flag values as an object.

### Offline cache does not enable this command

With telemetry disabled, `mg()` is false. Enabling
`CLAUDE_CODE_GB_DISK_CACHE_WHEN_TELEMETRY_OFF` permits ordinary flag reads to use
the disk cache, but it does not satisfy `mg() && ya()`. Setting
`cachedGrowthBookFeatures.tengu_fancy_wand` to `true` therefore does not enable
`/restart` in an offline session.

### Environment override

The restart checks do not read a dedicated environment variable. `lct()` tests
for a generic feature override, but the public 2.1.296 build implements
`getEnvironmentOverrides()` as `return null` and `readConfigOverrides()` as
`return` without a value. `CLAUDE_INTERNAL_FC_OVERRIDES` therefore does not force
this flag on. These implementations are at `/tmp/claude-2.1.296.js:2243` and
were also matched in the installed 2.1.296 executable.

### Local feature-map injection

`processRemoteEvalPayload()` populates `remoteEvalFeatureValues` from a GrowthBook
client's payload. The internal `answerNextGrowthBookRemoteEvalWith()` function
registers an answerer for the next fetch; it does not populate the map itself.
Both `createClient()` and `refreshFeatures()` return before fetching when
GrowthBook is disabled. These methods are at `/tmp/claude-2.1.296.js:2243`; the
answerer is exported at `:25921`.

Filling the map directly would make `hasFreshFeatures()` true, but it would not
change `mg()`. The restart command would still be unavailable with telemetry
disabled. An isolated test of the extracted client and command functions
confirmed this after `processRemoteEvalPayload()` accepted a local payload
containing `tengu_fancy_wand: {value: true}`.

A partial map also changes ordinary flag lookups: once the map is nonempty,
`getFeatureValueWithSource()` returns the default for a name missing from that
map instead of consulting its disk-cache value. In the isolated test, inserting
only `tengu_fancy_wand` made `tengu_amber_sentinel` read its `false` default despite
its disk-cache value being `true`. The map-writing, disabled-fetch, and lookup
checks were also matched in the installed 2.1.296 executable.

### Session requirements

`bjt()` requires these conditions independently of the flag:

- standard input and standard output are terminals;
- the platform is Linux or macOS;
- the executable is a native installation recognized by `s5()`;
- `teammateAgentId()` is undefined;
- the entrypoint is not `bg`;
- the workspace is not remote and the process does not have an active remote
  connection;
- slash commands are not disabled.

A teammate therefore cannot use `/restart` even when the fetched flag is true.
The command's flag checks and teammate exclusion were also matched in the
installed 2.1.296 executable.

The separate `tengu_fancy_wand_copy` config, read at
`/tmp/claude-2.1.296.js:24856` with default `{}`, can replace the update notice's
restart text through a nonempty string in `offer`. It does not enable the
command.

## Tools behind default-false flags (2.1.296)

Each of these tools is absent whenever its flag reads the default.

| tool | flag | other conditions |
|---|---|---|
| `SendFeedback` | `tengu_juniper_relay` | listed below |
| `EndConversation` | `tengu_umber_kestrel` | entrypoint and model, listed below |
| `Monitor` | `tengu_amber_sentinel` | on Windows, Git Bash or PowerShell must be available |
| `PushNotification` | `tengu_kairos_push_notifications` | none |

`SendFeedback` (`isEnabled` is `zG()`) needs all of these:

- the `feedbackDrafts` setting is not `off` (unset means `notify`);
- `DISABLE_FEEDBACK_COMMAND` and `DISABLE_BUG_COMMAND` are not true, and
  `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` is unset;
- the policy `allow_product_feedback` allows it. The policy applies only to
  credentials that `K5()` treats as policy-eligible. OAuth credentials with a
  subscription type set to a value other than `team` or `enterprise` are not
  eligible, so the policy never blocks them. An API key, or OAuth credentials
  without a subscription type, are eligible, and for them
  `allow_product_feedback` is denied whenever the session has not fetched a
  policy and has not read one from `~/.claude/policy-limits.json`
  (`onCacheMiss: "deny"`; it is also denied under HIPAA and ZDR). With
  `CLAUDE_CODE_OAUTH_TOKEN`, the subscription type comes from
  `CLAUDE_CODE_SUBSCRIPTION_TYPE`;
- the entrypoint is not a remote, Slack or Teams one, and not `sdk-ts`,
  `sdk-py` or `sdk-cli`, so `claude -p` never has the tool;
- the provider is `firstParty`;
- `CLAUDE_CODE_SEND_FEEDBACK` is not `false`;
- `tengu_juniper_relay` is true.

`EndConversation` needs all of these:

- the main-loop model is Opus 4.8+, Sonnet 5+, Fable 5+ or Mythos 5+;
- `tengu_umber_kestrel` is `true` or an object. An object's `scope` sets a
  regex over entrypoint names; otherwise the regex is `^cli$`;
- the entrypoint matches that regex. With the value `true`, only interactive
  `cli` sessions qualify.

When the tool is offered and tool search is on, the system prompt also gets a
one-line hint section, `endconv_deferred_hint`, added only while the session's
tool list contains `EndConversation`.

The flag is the only way to turn the tool off. Permission settings cannot,
because `Lre()` is true only for `EndConversation`, and every place that
consults it exempts the tool:

- the deny and allow rule lookups (`Go()`, `y4e()`, `lc()`) do not return a rule
  for it, so a `permissions.deny` entry does not remove it. `--disallowedTools`
  adds deny rules to the same lookups, so by the code it does not either
  (not tested);
- the `--tools` launch list never withholds it (`dAt()`);
- calling it never asks for permission.

Checked live on 2.1.296 in an interactive session in tmux, with requests sent
to a local listener and tool search on. A `settings.json` deny list naming
`EndConversation`, `Monitor`, `NotebookEdit` and `Write` removed `Write` from
the loaded tools and `Monitor` and `NotebookEdit` from the deferred list, while
`EndConversation` and its hint section stayed. With `tengu_umber_kestrel`
false in the disk cache, both were gone. A running session also drops the
tool, without a restart, once it rereads the cache.

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

A response without a `tool_use` never reaches this code. Claude Code runs Stop
hooks and completes the turn before checking the queue. A message typed while
final prose is streaming, after the last tool-boundary snapshot, or while Stop
hooks run cannot change that response; it becomes a new top-level user turn.
This is the timing-dependent reason some messages act as immediate steering and
others appear one turn later.

Removal happens after each attachment has already been yielded to the transcript:

- If selected commands do not produce a `queued_command` attachment, they remain
  queued.
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
  message, or separate user messages when a user carrier does not precede them.

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
  merging; if the remaining blocks do not contain any text, the original error
  result is retained.
- Text blocks are trimmed and adjacent text is coalesced. Mixed media can
  therefore have different placement from the false path.

### Why this flag often does not affect current steering

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
reminders there. The two forms do not produce a separate steering item once the
text has entered the tool result.

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

## Task tools: `tengu_rosy_wren`

Traced in 2.1.234. Both the flag and its env override are new in that build:
the two strings do not occur in 2.1.232. The flag reader is
`tt("name", default)` in 2.1.234, the same function this file calls `nt` at
2.1.226 under a different minified name.

The flag decides whether `TaskCreate`, `TaskGet`, `TaskList` and `TaskUpdate`
exist at all on Opus 4.8+, Sonnet 5+, Fable 5+ and Mythos 5+. When a payload
and the disk cache both lack the flag, it reads its `false` default, and the
four tools are missing on those models. The env override turns them on
without the flag.

The read site, deminified:

```js
function gW(){ if (V.CLAUDE_CODE_ENABLE_TASKS === false) return false; return true }

function aJ(){
  if (PI() || Hbs()) return true;                     // bg session, or CLI opt-in
  let m = vxr();                                       // canonical main-loop model id
  if (m === undefined || !LLr(m, xuv)) return true;    // below threshold: always on
  if (V.CLAUDE_CODE_ENABLE_TODO_TOOLS === true) return true;
  return tt("tengu_rosy_wren", false) === true;
}

function yte(){ return gW() && aJ() }

xuv = [["opus",[4,8]], ["sonnet",[5]], ["fable",[5]], ["mythos",[5]]];
Voi = [TodoWrite, TaskCreate, TaskGet, TaskUpdate, TaskList];
```

`LLr` parses `claude-<family>-<version>` and returns true when the version is at
or above that family's entry in `xuv`. So the flag is only ever consulted on
Opus 4.8+, Sonnet 5+, Fable 5+ and Mythos 5+. A family absent from `xuv`, such
as Haiku, makes `LLr` false, `aJ` returns true two lines earlier, and the tools
are present by default.

Four things satisfy `aJ()`, only three of them local:

1. `CLAUDE_CODE_ENABLE_TODO_TOOLS` set to `1`/`true`/`yes`/`on` (parsed by `$n`).
2. Naming any of the five tools in `--allowedTools` or `--tools`. Startup runs
   `Tbs(Voi.some(p))` where `p` tests the CLI tool rules, so mentioning one
   enables all four.
3. A background session or job takeover: `PI()` is `As() || r5() !== null`.
4. The flag.

### Two flags, two jobs

`CLAUDE_CODE_ENABLE_TODO_TOOLS` decides whether a checklist tool exists.
`CLAUDE_CODE_ENABLE_TASKS` decides which one, and the two `isEnabled` bodies are
complements on it: the task tools are `gW() && aJ()`, TodoWrite is
`!gW() && aJ()`. You never get both, and `ENABLE_TASKS=false` on its own does
not give either, because `aJ()` is still unsatisfied. Verified by diffing the
`tools` array in the stream-json init event:

| env | tools present |
|---|---|
| both unset | none |
| `ENABLE_TODO_TOOLS=1` | TaskCreate, TaskGet, TaskList, TaskUpdate |
| `ENABLE_TASKS=false` | none |
| `ENABLE_TASKS=false` + `ENABLE_TODO_TOOLS=1` | TodoWrite |
| `ENABLE_TASKS=true` | none |

`CLAUDE_CODE_ENABLE_TASKS` is an off-switch only. `Ve.triBool()` parses `true`,
`false` and `undefined`, but `gW` tests `=== false` alone, so `true` and unset
take the identical branch. An unset `CLAUDE_CODE_ENABLE_TASKS` means "tasks on";
the on-switch is `ENABLE_TODO_TOOLS`, despite the name.

### Why this matters to this tool

The task tools are a persisted store, not TodoWrite's in-memory list. Each task
is a file at `~/.claude/tasks/<listId>/<taskId>.json` carrying `status`,
`owner`, `blocks`, `blockedBy` and `metadata`; creation fires `TaskCreated`
hooks, and a hook exiting 2 deletes the just-written task and throws. The list
id is `CLAUDE_CODE_TASK_LIST_ID`, else the team name, else the session id, so a
team shares one list and `owner` is how work is assigned across it. TodoWrite
does not have ids, dependencies, owners or hooks, replaces the whole list on
each call, and stores `[]` once every item is completed.

## Dangerous-rm prompt timer: `tengu_splendid_horizon`

Traced in 2.1.296 and checked live.

The Bash permission check flags an `rm` or `rmdir` with a target that can remove
far more than intended: a shell variable that leaves `/`, `/*` or a top-level
path when it is empty, the output of a command substitution, a relative glob
after `cd`, or a system, home or workspace directory. The decision reason it
returns has `circuitBreaker: "dangerousRemoval"`, and that entry in the table of
circuit breakers has `bypassImmune: true`. So the call asks for permission even
in bypass mode, and an allow rule cannot approve it.

In bypass mode and in auto mode, the prompt gets an auto-deny timer, configured
by this flag, but only when `Mpn()` holds: the context can request a dialog, an
SDK dialog host is not active, prompts are not suppressed
(`shouldAvoidPermissionPrompts`), and `TZt()` is false; `TZt()` is true for a
teammate and for a background (async) agent. `q9()` reads it
with `Fn("tengu_splendid_horizon", {})`, which returns the value together with
its source, and checks each field separately:

| field | default | where it is read from | effect |
|---|---|---|---|
| `enabled` | true | a fetched payload only; a cache value is ignored. A true `CLAUDE_CODE_DISABLE_DANGEROUS_RM_TIMEOUT` makes it false | false removes the timer, so the prompt waits for an answer |
| `showDialog` | true | any source | false denies the call at once, without a prompt |
| `timeoutMs` | 120000 | any source; a value outside 5000 to 3600000 is replaced by the default | how long the prompt stays open before the call is denied |
| `maxDialogTimeouts` | 3 | any source; an integer from 0 to 100 | after this many prompts in one process have timed out, later flagged removals are denied without a prompt; 0 means unlimited |

The other three fields apply only while `enabled` is true. The count for
`maxDialogTimeouts` goes back to zero whenever someone answers one of these
prompts or cancels its timer.

While the timer runs, the prompt shows a countdown: "Claude Code will
automatically deny this request in 0:14, to avoid blocking progress on an
unattended session". When it reaches zero, the model receives a tool result
saying that a built-in safety check denied the command, not the user, that the
command did not run, and that it must not work around the check. The result
ends with the check's own text, which usually suggests a rewrite such as
`"${VAR:?}"` or a literal path.

Any keyboard or mouse input except wheel scrolling cancels the timer, and so
does opening the transcript view while the prompt is open: both emit
`autoDenyPresence`. The prompt then stays open until someone answers it. The
timer therefore denies the request only if the terminal stays untouched for the
whole countdown.

Without the timer, what happens depends on the caller. Headless `claude -p`
denies the call at once, and the tool result is the check's own text with its
rewrite hint, without the preamble above. A background subagent of an
interactive session is not denied at all: in bypass mode, `_pn()` passes the
ask on unchanged when `shouldAvoidPermissionPrompts` is false, as it is for
such an agent. The parent session shows the prompt, marked with the agent's
type ("from the general-purpose agent"), without a countdown, and the prompt
stays open until someone answers it.

To shorten the timer, add the flag to the disk cache as
`"tengu_splendid_horizon": {"timeoutMs": 15000}`. The other fields can be left
out, because each one that is missing gets its default. The entry only takes
effect in processes that read the disk cache (see
[How a flag read resolves](#how-a-flag-read-resolves-21296)), and like any
cache edit it is lost when a process with GrowthBook enabled fetches a payload.

Checked live on 2026-10-10 with 2.1.296 in bypass mode. Each session had its
own `CLAUDE_CONFIG_DIR`, telemetry off with the disk cache readable, and a local
listener that answered with a Bash call to `rm -f $CC_RM_A/$CC_RM_B`, both
variables unset. Interactive sessions ran in tmux. For the subagent row the
listener first answered with an Agent call, and the general-purpose agent it
started made the Bash call:

| session | cache entry | input while the prompt was open | tool result |
|---|---|---|---|
| interactive | `{"timeoutMs": 15000}` | none | denied 15.1 s after the Bash call |
| interactive | none | none | denied 120.1 s after the Bash call |
| interactive | `{"timeoutMs": 15000}` | one Down arrow, 2.5 s after the Bash call | still pending after 142 s; the countdown line disappeared at the keypress and the prompt stayed open |
| `claude -p` | none | the prompt was not shown | denied 0.03 s after the Bash call, with the check's text alone |
| background subagent of an interactive session | none | none | still pending after 167 s; the prompt in the parent did not have a countdown |

In the same setup, `rm -f "$CC_RM_UNSET"/name` ran without a prompt, so a test
needs a target such as `$A/$B`. Auto mode was not tested.

## Messaging/teams cluster

Recorded here because they share the naming and matter to this tool. Values are
those GrowthBook served on 2026-08-07; they can differ by account and change
over time.

| flag | served value | what it is |
|---|---|---|
| `tengu_amber_flint` | true | Teams killswitch. Read `nt(...,true)`. With teams opted in via `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS` or `--agent-teams`, this being true is what lets `isAgentSwarmsEnabled` return true. Turning it false would disable agent teams even when opted in. |
| `tengu_harbor` | true | Not yet traced to a behaviour. |
| `tengu_harbor_permissions` | true | Not yet traced. |
| `tengu_harbor_prism` | true | Not yet traced. |
| `tengu_cobalt_harbor_notice` | true | Not yet traced. |
| `tengu_cobalt_harbor` | false | Read `nt("tengu_cobalt_harbor",false)`; a remote-control-at-startup source. Not traced further. |
| `tengu_harbor_ledger` | list of official plugins (discord, telegram, fakechat, imessage) | A plugin allowlist, not a boolean. |
| `tengu_flint_harbor` | true | Drives the onboarding-guide generator (`tengu_flint_harbor_prompt` carries its full prompt). Unrelated to messaging despite the name. |

The remaining `*_harbor` / `*_kite` flags (`tengu_slate_harbor_experiment`,
`tengu_quiet_harbor`, `tengu_woven_harbor`, `tengu_silent_harbor`,
`tengu_zinc_harbor`, `tengu_velvet_harbor`, `tengu_fennel_kite`,
`tengu_gypsum_kite`, `tengu_quill_harbor`, `tengu_harbor_willow`) have not been
traced. Add a row when a read site is read, not before.

## New flags in 2.1.226, not traced

These are the flags that appear as `nt(...)` reads in 2.1.226 and not in
2.1.220, minus the cluster above. Each is a real flag with a default, but the
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
emitter, not flags read for a value, so they are not listed.
