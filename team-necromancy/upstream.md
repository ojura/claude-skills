# Following Claude Code upstream

Companion to `architecture.md`. That file records how teammates work and what
this tool concluded from the binary; this one tracks what changes between
Claude Code releases, what it costs this tool, and what is still owed.

Baseline for everything below: **2.1.220 → 2.1.226**, both linux-x64 native
builds, unpacked with `~/claude-patches/util/bun_handler.py extract`. The
2.1.226 checksum matches its published manifest. Extracted bundles kept at
`/tmp/claude-2.1.226.js` and `/tmp/claude-2.1.220.js`; the 2.1.220 one comes
from the patched install, which is fine for everything here because the lock
region compared byte-identical against the freshly downloaded 2.1.226.

## How to do this comparison

Two mistakes were made the first time and both are easy to repeat.

**Minified names differ between builds.** Any diff keyed on an identifier is
worthless. A flag-name diff run through the gate-reading function's minified
name reported 318 new flags, because that function is called `nt` in 2.1.226
and something else in 2.1.220, so 2.1.220 matched nothing and every flag
looked new. The real diff, taken on `"tengu_*"` string literals, is 90 new and
33 gone. Diff string literals, never identifiers.

**String literals containing minified names are not prose.** A diff of
literals mentioning teammates appeared to show that resurrection through
SendMessage was new in 2.1.226. It is present in 2.1.220 word for word; the
literals that looked new were ones with minified identifiers embedded, so
renaming moved them. Restrict a prose diff to literals of letters,
spaces and punctuation only.

The comparison that does work, for a region rather than a name: anchor on a
stable string, expand outward token by token, and require identifiers to
correspond one-to-one and consistently in both directions. Pin property names
(preceded by `.`) and object keys (followed by `:`) so they must match
literally, otherwise a `.mkdir` can pair with a `.readFile` and the region
still reads as identical.

## What did not change

**The roster lock.** 14,274 characters, identical in both builds, 143 minified
names renamed and nothing else. Filesystem calls counted separately in each
build and matching exactly: `mkdir` 2, `rmdir` 4, `utimes` 3, `stat` 6, and
zero each of `readFile`, `open`, `writeFile`, `unlink` and `readdir`. The
roster lock's own options and its path are unchanged too.

What those facts mean for this tool is in `architecture.md`, which owns the
lock and now states the range they were measured over. Restating the mechanism
here would give it two homes to keep in step, and the point of this entry is
only that the comparison found nothing moved.

**The session registry schema.** `~/.claude/sessions/<pid>.json` reads the same
fields in both builds. In particular there is still no record of the internal
session id a boot minted, so the ask in `architecture.md` is still open and the
team-to-pid join is still temporal. Searched and absent in both builds:
`internalSessionId`, `mintedSessionId`, `liveSessionId`, `currentSessionId`.

**Registry reaping.** Claude Code already deleted dead sessions' `<pid>.json`
in 2.1.220, logging "Prior session exited uncleanly" for interactive ones. Not
new, no change needed.

**How teams are enabled.** `isAgentSwarmsEnabled` is byte-identical modulo renaming:
the `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS` env var or `--agent-teams`, then the
`tengu_amber_flint` killswitch. Note both shipped builds have dropped the
`USER_TYPE === 'ant'` arm that the leaked source still shows.

## What changed: cross-session messaging went public

This is the release that matters. Anthropic's docs page for it says it needs
2.1.224 or later, so it landed between our two builds.

It is a different feature from agent teams, which is why a teammate-focused
diff nearly missed it. Teams are a lead spawning and supervising members;
this is messaging between independent sessions you started yourself, with
`ListAgents` to discover and `SendMessage` to deliver.

The check that turns it on is new in 2.1.226 and reads: not Windows, and either the server-side
flag `tengu_harbor_kite` (default false in the binary) or the
`CLAUDE_CODE_HARBOR_KITE` environment variable. So it ships off and is turned
on from the server, which is consistent with the docs saying it is on with
nothing to enable.

What it puts on the machine when enabled:

- a unix socket per session at `$XDG_RUNTIME_DIR/cc-socks/<pid>.sock`, falling
  back to `/tmp/cc-socks-<uid>/<pid>.sock` when the first path would exceed the
  unix socket length limit, chmod 0600
- `CLAUDE_CODE_MESSAGING_SOCKET` in that session's environment, holding the path
- `messagingSocketPath` written into that session's `sessions/<pid>.json`
- a reaper that unlinks registry entries whose owner is gone, deciding
  liveness from pid plus process start time, skipped on WSL. It walks the
  registry, not the socket directory: the socket file itself is unlinked by
  its owner's graceful shutdown, so a SIGKILLed session's socket outlives
  everything and only the runtime dir clearing at logout removes it

`isolatePeerMachines` in settings requires approval before a message leaves the
machine, and in 2.1.226 it became immune to bypass permission mode.

### What this costs this tool

**Nothing collides on sockets.** `socket_dir()` answers tmux's directory,
`$TMUX_TMPDIR` or `/tmp/tmux-<uid>`. The messaging sockets are under
`cc-socks`, a different directory, so `--cleanup-sockets` cannot touch them.
Worth stating in a comment so nobody widens that answer later and quietly
starts reaping Claude Code's sockets.

**The naming space now has a second occupant.** A session answers to a name
from `/rename` or `--name`, derived from the working directory's folder name
when unset, such as `myapp-3f`. Those names are what `SendMessage` addresses,
alongside teammate names from the roster. This tool has its own naming rules
for agents. Two questions follow, and neither is answered yet: whether a
resurrected teammate should carry a session name at all, and what happens when
a session name and a roster name collide in the same listing.

**A live process now advertises facts this tool derives.** The registry record
carries `cwd`, `kind`, `entrypoint`, `name`, `nameSource`, `status`,
`statusUpdatedAt`, `version`, `peerProtocol`, and in the 2.1.226 reader also
`tmux`, `agent`, `logPath`, `jobId`, `parkedJobId`, `bridgeSessionId`,
`waitingFor`, `state`, `detail`, `tempo`, `needs`. `session_registry()` parses
the file, and `live_sessions()` consumes `pid`, `sessionId`, `procStart`,
`updatedAt` and `startedAt`. Everything else is read by nobody.

## Work items

Ordered by whether the tool is wrong today, then by cost.

### 1. Confirm the lock findings against 2.1.226 in architecture.md

`architecture.md` dates its lock paragraph to 2.1.200 and warns that a claim
resting on a build can drift. The lock is now confirmed unchanged through
2.1.226. One sentence, naming both builds, so the next reader knows the range
rather than re-deriving it from a 275MB binary. Cheap and purely documentation.

### 2. Decide what the registry's unread fields are worth

Each of these is a question the tool answers some other way today. For each,
decide: adopt the registry as a source, ignore it deliberately, or record why
it cannot be trusted. Do not adopt any of them by default; the registry's
`sessionId` is already known to mean the resumed-from transcript rather than
the running identity, so its other fields deserve the same suspicion.

- `cwd` — the tool computes a project directory to find transcripts under.
  The registry holds the live process's actual working directory. These can
  disagree after a session changes directory, and the registry updates its
  record when cwd changes, so it is the better answer for a live process and
  says nothing at all about a dead one.
- `tmux` — the tool joins panes to processes itself. If this field means what
  it appears to, it is a direct answer for live sessions. Establish what
  writes it and in which substrates before trusting it; a field that is only
  populated for some launches is worse than no field, because absence would
  read as "no pane".
- `status` and `statusUpdatedAt` — busy, idle, shell, waiting. The doctor
  reports whether a lead is alive but not whether it is doing anything. A
  stale `statusUpdatedAt` on a live pid is also evidence of a wedged session.
- `kind` — interactive, bg, daemon, daemon-worker. The bg substrate is a
  documented arrangement this tool deliberately supports, and this field names
  it without inference.
- `name` and `nameSource` — see the naming collision above.
- `version` — which build wrote the record. Directly useful once behaviour
  starts differing by version, which is the whole point of this file.
- `agent` — needs establishing. If it carries the agent identity for
  flag-bound launches, it would be a second answer to a question
  `live_sessions()` currently takes from argv, and two answers that can
  disagree is exactly what the engine rule exists to prevent.

Whatever is adopted belongs behind an engine answer, not read at call sites,
and each adoption needs its own named check.

### 3. What happens to a resurrected agent under messaging: measured

Answered on the 2.1.226 binary against a scratch `CLAUDE_CONFIG_DIR`, gate
forced on with `CLAUDE_CODE_HARBOR_KITE=1`, real credentials lifted from a
live lead's environ by the same mechanism `inherit_credentials` uses, both
launches reaching the full REPL:

- a plain launch registers `sessions/<pid>.json`, records its
  `messagingSocketPath` in it, and binds the socket
- the same launch wearing `--agent-id` binds the socket but **never
  registers**. The register function refuses any process wearing an agentId,
  the same `!agentId` condition that keeps flag-bound launches out of
  implicit-team registration.

Getting the measurement right had a trap worth keeping: run the binary from a
tool-driven shell and it inherits `CLAUDE_CODE_CHILD_SESSION=1`, which the
register function also refuses, so a plain launch appears not to register and
the flag-bound result looks unremarkable. The env must be built with `env -i`.

Consequences, in order of who notices:

- **Members: no defect.** Teammates were never in peer discovery; the docs
  say they are messaged through the team's roster, not listed. A resurrected
  member is exactly as reachable as a native one, through the mailbox.
- **The masqueraded lead loses reachability the original had.** A normal
  lead registers and can be listed and messaged from the user's other
  sessions. The masquerade wears `--agent-id`, so a resurrected lead never
  registers: outbound messaging still works from inside it (discovery reads
  others' records), but no other session can discover or message it. This is
  the one place resurrection now subtracts a capability users can see.
- **The bound socket is inert and, after a SIGKILL, immortal.** Its path is
  recorded nowhere but the process's own environment, so nothing can dial it;
  graceful exit unlinks it, but the harness's reaper walks registry records
  and a recordless socket is invisible to it. It sits in the runtime dir
  until logout clears it. Same nuisance class as the tmux socket litter that
  motivated `--cleanup-sockets`; cleanup deferred per item 4.

The session-name half of the collision question dissolves with this: names
live in registry records, a flag-bound launch writes none, so a resurrected
agent never enters the session-name namespace at all.

### 4. Decide whether this tool should clean up after a kill

When this tool stops a process, Claude Code's registry entry and messaging
socket for that pid are left behind until some later Claude Code run reaps
them.

The roster lock invites a comparison here, since that is a leftover this tool
does clear. The deciding fact is not shared, and the two kinds of leftover
earn their answers differently. Claude Code cannot remove a lock file this
tool leaves, ever, because its recovery calls `rmdir` and gets ENOTDIR; if
this tool does not clear it, nothing will, and roster writes for that team
stay broken. Registry entries the harness does reap, from pid plus process
start time, so acting there would only make the removal sooner. Sockets the
measurement in item 3 corrected: the reaper never looks at the socket
directory, so after an ungraceful end the file sits there until logout clears
the runtime dir — but it is recorded nowhere and nothing can dial it, so what
remains is a dead file, not a broken capability. In every case the leftover
either gets removed without us or costs nothing by staying.

So the bar is higher, not equal. Decide deliberately, and do not drift into
deleting Claude Code's files because it is easy.

### 5. Watch the v5 storage layer

New in 2.1.226 and not yet real. It replaces file paths with namespaced keys
across nearly everything this tool reads: transcripts, mailboxes, team
configs, settings, sessions, tasks, memory, plans, jobs. Reads return bytes
through a result type with codes like NotFound and Unavailable; listings page
with cursors.

**It is called in about 25 places and implemented in none of them.** Every
call takes the backend as a parameter, every call site falls back to the
existing file paths, and the transcript code still builds
`<projectKey>/<sessionId>.jsonl` even on the v5 branch. There is no database
engine and no service name in the bundle. The honest limit: the top-level
value could not be traced to its binding, so this says nothing constructs a
backend here, not that one can never be supplied.

If it ever becomes real, every read the engine does stops working: each of the
eight realms it examines is a file today, and it would have to learn to ask a
key-value store instead. Nothing to build now. What to watch for, in order of
how early it would show:

- a namespace switch that maps a key to a path, which would mean the first
  backend is file-backed and the damage is containable
- any construction of an object with `read`, `write`, `stat`, `delete` and
  `listEntries` over keys
- `storageV5` reaching a call site without a file fallback beside it
- team files: 2.1.226 already has a branch that locks `<config>.v5-lock-anchor`
  while keeping the lock file at `<config>.lock`, taken when a caller passes a
  v5 flag. The lock artifact path is unchanged, so contention is unaffected
  today, but it is the first place the roster itself moves.

### 6. Keep the comparison reproducible

The two mistakes at the top of this file cost more than the comparison did.
The token-level region comparison and the literal diffs should become a small
script rather than being retyped each release, so the next comparison starts
from a known-good method. Not part of `agent-resume` and not in its suite; a
maintenance script, run by hand when a release lands.

## Open questions for Juraj

- Whether adopting registry fields is wanted at all. "Cannot verify" was the
  wrong axis: `pid`, `procStart`, `cwd` and `tmux` are checkable against
  `/proc` and a pane list, and adoption there is ordinary corroboration. The
  real line runs through the self-asserted fields, `status`, `tempo`, `name`,
  `waitingFor`, `state`, where nothing else on disk says the same thing and a
  wedged session's last self-report is indistinguishable from truth.
- Whether cleaning Claude Code's registry entries and sockets after a kill is
  in scope, per item 4. Ruled: deferred. The motivation that made tmux socket
  litter worth cleaning was tab completion growing a nuisance; nothing
  completes over `cc-socks`, so the pressure is absent until it isn't.
