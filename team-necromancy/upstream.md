# Claude Code release comparison

Companion to `architecture.md`. This file compares Claude Code releases that
change assumptions used by `agent-resume`.

The current comparison covers **2.1.220 to 2.1.226**, using linux-x64 native
builds extracted with `~/claude-patches/util/bun_handler.py extract`. The 2.1.226
checksum matches its published manifest. The extracted bundles are
`/tmp/claude-2.1.220.js` and `/tmp/claude-2.1.226.js`. The roster-lock region in
the patched 2.1.220 install matches the downloaded 2.1.226 build after minified
identifier mapping.

## Comparison method

Minified identifiers change between builds, so comparisons use stable content:

- Literal comparisons operate on string values. The `"tengu_*"` literal set has
  90 additions and 33 removals from 2.1.220 to 2.1.226. Matching through a
  minified function name produced 318 false additions because that identifier
  changed.
- Prose comparisons accept literals made from letters, spaces, and punctuation.
  This excludes strings containing minified identifiers, since their apparent
  changes come from identifier renaming.
- Region comparisons start from a stable string, expand token by token, and
  require a one-to-one identifier mapping in both directions. Property names
  preceded by `.` and object keys followed by `:` match literally, preventing a
  call such as `.mkdir` from pairing with `.readFile`.
- Filesystem-operation counts use two stable anchors. Explicit bounds keep calls
  from neighboring functions out of the count.

`~/claude-patches/util/cc-release-diff.py` implements these comparisons. Its
subcommands are:

- `fetch <version|stable|latest>`: download and extract a build under `/tmp`
- `literals <tengu|env|flags|prose> A.js B.js [keyword]`: compare literal sets
- `region <anchor> A.js B.js`: report corresponding token spans and both
  divergence points
- `fs-ops <start> <end> A.js B.js`: count filesystem calls between anchors

For this pair, the region comparison finds 6,382 corresponding tokens and 143
identifier renames in the roster-lock module. Divergence begins in unrelated
neighboring code, and the filesystem-call census matches.

## Stable behavior across both builds

### Roster locking

The 14,274-character roster-lock module is identical after mapping 143 minified
identifiers. Across the complete module, each build calls `mkdir` twice, `rmdir`
four times, `utimes` three times, and `stat` five times. Calls to `readFile`,
`open`, `writeFile`, `unlink`, and `readdir` total zero. The module boundaries
run from the tail of `graceful-fs` to the next caller's options. A narrower
`ELOCKED` to `onCompromised` interval contains one `rmdir` and two `stat` calls.
The roster-lock path and options also match.

`architecture.md` records what this directory-lock protocol means for the
regular-file lock used by `agent-resume`.

### Session registry and reaping

Both builds read the same fields from `~/.claude/sessions/<pid>.json`. The
registry records the resumed transcript ID. The process's internal session ID
remains represented by the live process and implicit team name, which keeps the
resumed-team join dependent on additional evidence. Literal searches return zero
matches for `internalSessionId`, `mintedSessionId`, `liveSessionId`, and
`currentSessionId`.

Both builds remove registry files for exited processes and log “Prior session
exited uncleanly” for interactive sessions.

### Team enablement and identity flags

`isAgentSwarmsEnabled` is identical after identifier mapping. It reads
`CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS` or `--agent-teams`, then applies the
`tengu_amber_flint` server switch. The leaked source includes an additional
`USER_TYPE === 'ant'` branch.

The `--agent-id`, `--agent-name`, and `--team-name` triple binds inside this
condition. A teams-enabled environment activates the identity; other launches
resume as plain sessions. Harness teammate commands therefore embed
`CLAUDECODE=1` and the teams variable inline. In 2.1.226 the child sets
`CLAUDE_CODE_ENTRYPOINT` itself. `agent-resume` follows the inline-environment
rule through `TEAM_ENV`.

## Cross-session messaging in 2.1.226

Anthropic documents cross-session messaging for 2.1.224 and later. It connects
independent user-started sessions through `ListAgents` and `SendMessage`,
separately from lead-managed teammate rosters.

The 2.1.226 enablement condition requires a non-Windows platform plus either the
`tengu_harbor_kite` server switch or `CLAUDE_CODE_HARBOR_KITE`. The binary
default for the server switch is false. On this machine,
`CLAUDE_CODE_HARBOR_KITE=1` has been present in `settings.json` since 2026-08-10,
so each new session enables the feature locally.

An enabled session creates:

- a mode-0600 Unix socket at `$XDG_RUNTIME_DIR/cc-socks/<pid>.sock`
- `/tmp/cc-socks-<uid>/<pid>.sock` as the long-path fallback
- `CLAUDE_CODE_MESSAGING_SOCKET` in the process environment
- `messagingSocketPath` in `sessions/<pid>.json`
- registry cleanup based on PID and process start time, except on WSL

Graceful shutdown unlinks the socket. Registry cleanup walks registry entries,
so a socket left by SIGKILL remains in the runtime directory until logout clears
that directory.

`isolatePeerMachines` requires approval before a message leaves the machine. In
2.1.226, bypass permission mode still requires that approval.

## Consequences for `agent-resume`

### Socket directories stay separate

`socket_dir()` returns the tmux directory from `$TMUX_TMPDIR` or
`/tmp/tmux-<uid>`. Peer-messaging sockets use `cc-socks`. The
`--cleanup-sockets` action therefore remains confined to tmux server sockets.
The code comment should retain this directory distinction.

### Session names and teammate names use separate registries

A user session can receive a name from `/rename`, `--name`, or its cwd-derived
default such as `myapp-3f`. Peer SendMessage addresses those session names.
Teammate SendMessage addresses roster member names.

A flag-bound teammate launch binds its peer socket but skips the session registry
because registration selects only processes with an empty `agentId`. It
therefore participates in teammate naming and remains outside session-name
discovery. This also prevents a collision between a resumed teammate name and a
session name in the peer listing.

### Registry fields exceed current consumers

The 2.1.226 registry reader recognizes `cwd`, `kind`, `entrypoint`, `name`,
`nameSource`, `status`, `statusUpdatedAt`, `version`, `peerProtocol`, `tmux`,
`agent`, `logPath`, `jobId`, `parkedJobId`, `bridgeSessionId`, `waitingFor`,
`state`, `detail`, `tempo`, and `needs`.

`Resolver._session_registry()` parses the file for the canonical immutable
runtime snapshot. Its registry observations currently consume `pid`, `sessionId`,
`procStart`, `updatedAt`, and `startedAt`; the snapshot joins live observations to
the same PID records used by its other indexes. Candidate additions need
verification before entering that Resolver-owned answer:

- `cwd`: compare with the live process cwd; it can improve live transcript lookup
- `tmux`: establish which launch modes write it before using it as pane evidence
- `status` and `statusUpdatedAt`: compare with process activity and use stale
  timestamps as possible wedged-session evidence
- `kind`: use the explicit interactive, bg, daemon, or daemon-worker value
- `name` and `nameSource`: account for peer-session naming
- `version`: identify the build that wrote a registry record
- `agent`: compare with argv identity before adding a second identity source

Each adopted field belongs behind a named Resolver method and a structural test.

### Measured behavior of flag-bound launches

A scratch `CLAUDE_CONFIG_DIR` test on 2.1.226 used
`CLAUDE_CODE_HARBOR_KITE=1`, credentials copied from a live lead, and full REPL
startup for both processes. The test uses `env -i` because a tool-driven shell
carries `CLAUDE_CODE_CHILD_SESSION=1`, which also selects the
non-registering path.

The plain process writes `sessions/<pid>.json`, records its
`messagingSocketPath`, and binds the socket. The process launched with
`--agent-id` binds its socket and skips the registry record.

For members, roster messaging provides the intended address path. A lead resumed
through teammate identity can discover and message other peer sessions. Peer
discovery returns an empty result for that lead because the socket does not have
a registry record. Graceful exit removes it; SIGKILL leaves the file until
runtime-directory cleanup.

### Cleanup ownership

`agent-resume` leaves Claude Code registry entries and peer sockets to Claude
Code and runtime-directory cleanup. Claude Code reaps registry entries from PID
and process start time. A recordless peer socket is unreachable and disappears
at logout. Removing either artifact earlier only advances cleanup time.

Roster locks require different ownership. Claude Code applies `rmdir` to its
lock directory and receives `ENOTDIR` for the regular file created by
`agent-resume`. That file blocks roster updates until this tool removes it.

## Storage v5 watch

Claude Code 2.1.226 contains about 25 storage-v5 call sites. The interface uses
namespaced keys, byte-result codes such as `NotFound` and `Unavailable`, and
cursor-based listings. Callers receive the backend as a parameter and retain
file-path fallbacks. Transcript code still constructs
`<projectKey>/<sessionId>.jsonl`. Searches for a database engine or service name
tied to this interface return empty, and the top-level backend binding remains
unresolved.

A real storage-v5 backend would affect transcripts, mailboxes, team configs,
settings, sessions, tasks, memory, plans, and jobs. Watch for these changes:

1. a namespace mapping from keys to paths
2. construction of an object implementing `read`, `write`, `stat`, `delete`, and
   `listEntries`
3. a `storageV5` call site that lacks a file fallback
4. team config moving behind the existing `<config>.v5-lock-anchor` branch

The 2.1.226 team branch keeps the lock artifact at `<config>.lock`, so current
lock contention remains file-based even when the caller passes the v5 flag.

## Open decision

Registry fields divide into externally checkable facts and process self-reports.
`pid`, `procStart`, `cwd`, and `tmux` can be compared with `/proc` and pane state.
`status`, `tempo`, `name`, `waitingFor`, and `state` are self-reports, and their
stale values can look valid on a wedged process. Any adoption should state which
class the field belongs to and how Resolver ranks it.
