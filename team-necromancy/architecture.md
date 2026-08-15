# How teammates actually work

Companion to SKILL.md. The claims describe Claude Code 2.1.200 bundle functions
and live behavior. The leaked TypeScript tree helps locate those functions but
lags the binary by months. Build-specific behavior needs verification against
the installed release.

## Execution arrangements

A teammate combines a roster, file mailboxes, and an idle protocol with one of
several execution arrangements:

| | plain subagent | in-process teammate | tmux/iterm2 teammate | bg job (hand-built) |
|---|---|---|---|---|
| process | leader's | leader's | own, child of tmux server | own, child of daemon's pty host |
| transcript | sidechain (`agent-<id>.jsonl`) | sidechain | **main session JSONL** | main session JSONL; turns remain in process memory |
| leader exit | exits | exits | reaped | survives |
| talks via | tool result | mailbox + in-memory queue | mailbox | mailbox |
| colors | n/a | n/a | 256-cube, downsampled by the app | native truecolor |

`teammateMode` in 2.1.200 covers `auto`, `tmux`, `iterm2`, and `in-process`.
A hand-built bg arrangement works because `claude --bg` accepts teammate
identity flags and mailboxes use files. It provides supervision, truecolor, and
survival across leader exit. Transcript turns remain in process memory, so
durable turn flushing is the requirement for a supported bg mode.

`agent-resume` reports seating separately from team health. `--doctor` prints
the team verdict followed by one dossier per member for a directly named team or
agent. `run_doctor` computes one member order and passes it to the summary roster
and dossier loop. A requested member comes first, then the genuine lead; a
directly named team starts with the lead. `team_runtime` resolves each member's
process answer, full title, identity color, and the shared lead and polling facts
once. The summary uses the existing PID, identity, and title renderer. That
renderer clips each roster title to the terminal space left on its row; the full
title remains available to the dossier. ANSI is inserted around the identity
after plain-text layout, so `--no-color` changes no spacing. Dossier headers carry
explicit requested, membership, and runtime labels. Session UUID and title use
separate labeled rows, followed by parent and live seat. Lead identity, operative
team, polling, common delivery failure, and common remedies print once in the
team report. Recipient-specific ledger evidence and member-specific remedies stay
in the matching dossier. Direct diagnosis uses the ordinary selector and
immutable target, but does not infer a launch destination. The team verdict still
measures whether a live lead reads the team, and remains the command's exit status.

The unnamed Agent tool runs its query loop inside the leader and writes a
sidechain transcript. Its work therefore resumes through an internal query
loop. SendMessage uses this path to resume a dead tmux teammate in-process by
replaying its JSONL.

## Teammate transcript stamps

Every JSONL line records `sessionKind`. Teammate processes also record
`agentName` and `teamName`; leader records carry session identity alone. These
fields control two behaviors:

- The /resume picker reads metadata from the **first line**. A session created
  as a bg fork therefore keeps the `bg` badge across interactive resumes, while
  the live process environment determines its behavior. `claude -c` selects
  transcripts whose first line carries an interactive kind. Forking clears the
  copied identity stamps and gives the fork its own picker classification.
- Reconnection binds team identity from CLI identity flags or transcript
  stamps. Plain leads launch with session arguments, and their transcripts
  carry session identity alone, so recovery binds a dead lead through the
  teammate path. `isAgentSwarmsEnabled` activates identity-flag binding in
  2.1.220 and 2.1.226. Every exec and pane launch therefore carries `TEAM_ENV`
  inline, independent of the invoking shell's environment.

## Implicit-team cleanup

A leader names its lazily created team `session-<internal-live-session-id>`.
The internal ID changes across resumes and differs from the transcript filename,
so the team file written by a spawn provides the discoverable name. A
masquerade can also set the name explicitly.

At startup, the implicit-team initializer (`initializeSessionTeam`) calls
`registerTeamForSessionCleanup` and adds the team to an in-memory set. Plain
lead launches take this initializer path. Identity-bound launches take the
reconnection path and retain their team directory across exit. On graceful
lead exit, `cleanupSessionTeams` walks the registered set in two passes. Its
backend filter selects pane members for termination; in-process members carry
`backendType` or `tmuxPaneId` of `"in-process"` and continue until their query
loop ends. Cleanup then removes member worktrees and the team directory with
`rm -rf`. The 2.1.202 bundle places registration in the implicit-team
initializer, assigning cleanup ownership to plain lead startup.

Graceful exit runs this cleanup. A crash or SIGKILL bypasses it and leaves the
team state intact. Bg processes use a daemon pty backend outside the pane-member
filter and therefore keep running. A masqueraded lead uses the identity-bound
path, so its exit also leaves the team directory intact.

## The mailbox and the roster

Cross-process teammates communicate through files:

- `~/.claude/teams/<team>/config.json` is the roster. `SendMessage` resolves
  recipients from the in-memory teammates map and agent registry first, then
  reads this file at send time. A hand-inserted roster member therefore becomes
  addressable immediately. Mention completion, colors, and the tasks pill use
  the leader's in-memory `teamContext`, which is updated by the leader's actions
  and mail events. Spawn and shutdown paths read, modify, and write the roster.

  Every spawn backend reserves its roster entry through `updateTeamFile`, with
  a lock at `config.json.lock`. This helper is the stock npm `proper-lockfile`
  package, so it treats the lock path as a directory and operates with `mkdir`,
  `stat`, `utimes`, and `rmdir`. Path type and timestamps carry its state. It
  retries for about 0.65 seconds (10 retries, from 5 ms to 100 ms), declares an
  unrefreshed lock stale after 10 seconds, and clears stale locks with `rmdir`.

  `agent-resume` uses an exclusive regular file at the same path. The two forms
  exclude each other through `EEXIST`. A regular-file lock makes `rmdir` return
  `ENOTDIR`, so `agent-resume --cleanup` owns removal of regular-file leftovers.
  `removeTeamMember`, `setMemberActive`, `setMemberMode`, and
  `removeMemberByAgentId` catch `ENOTDIR` and retain the current roster state.
  `reserveTeammateIdentity` propagates it, so the leftover blocks later spawns.

  Cleanup removes a regular-file lock when its record names a dead owner. A
  live owner, an unreadable or empty record, and a `proper-lockfile` directory
  each preserve the team directory. An empty record can result when a process
  exits between exclusive creation and writing its ownership record; preserving
  it avoids deleting a lock whose writer may still be alive. An atomic variant
  can write the record to a temporary file and claim the lock name with a hard
  link, removing that empty-record interval while keeping the protocol.

  Removal compares the record bytes it read with the bytes present immediately
  before unlinking. This catches another process taking the lock between those
  two operations. Device and inode identity can repeat after replacement because
  the filesystem reuses the freed inode immediately, so byte equality is the
  useful ownership check. Claude Code 2.1.200 and 2.1.226 contain the same
  locking module apart from minified names; `upstream.md` records the comparison.

  In-process teammates use the reservation helper twice: first for a bare
  reservation, then for a patch that sets `tmuxPaneId` and `backendType` to
  `"in-process"`. The exit filter selects pane-type backends, so their query
  loops can continue while team-directory cleanup removes their registration.
  Their transcript remains available for later recovery.
- `~/.claude/teams/<team>/inboxes/<name>.json` is a JSON array of messages
  (`{id, from, text, timestamp, status}`). An empty inbox contains `[]`; a
  zero-byte file causes parse failures in both reader and writer.
- Teammates resolve `team-lead` through their recorded team name. A stale team
  name routes mail to the inactive team's inbox. `@main` serves background
  subagents, while pane teammates use team mailboxes. Moving an agent across team
  generations therefore requires restamping its recorded team name.

The leader renders its event-sourced `teamContext`, updated by its own actions
and death announcements in mail. Roster-backed actions read `config.json`.
This division explains stale chip colors, Ctrl+C'd teammates remaining active,
and tmux counters continuing until a formal goodbye.

Name resolution requires an initialized `teamContext`. The first teammate spawn
or identity flags at boot initialize it.

## Session ID and transcript filename

The PID files in `~/.claude/sessions/` record the resumed transcript ID. Each
process also has an internal session ID, and implicit team names use that
internal ID. A fresh process starts with equal transcript and internal IDs, so
its team name joins exactly to the registry. A resumed process receives a new
internal ID while its registry record keeps the transcript ID. Resolver must
therefore keep these identities separate.

An eagerly created team appears shortly before the session registry file; three
measurements gave 281, 252, and 402 ms. This timing supports corroboration for a
resumed lead, while exact joins come from explicit identity or durable records.

## Required harness record

Team-to-process resolution needs the internal session ID that each boot creates.
A transcript identity line or an `internalSessionId` beside the transcript ID in
`~/.claude/sessions/<pid>.json` would preserve it. The running process and its
implicit team-directory name currently carry that value.

Resolver ranks available evidence as follows:

1. `--team-name` in a flag-bound process identifies the team exactly.
2. A spawn payload from the current process life identifies the lead exactly;
   process start time distinguishes it from payloads written by earlier lives.
3. A team's `leadSessionId` identifies a fresh lead exactly because the fresh
   internal ID equals its transcript filename.
4. A unique, bidirectional match between team `createdAt` and process
   `startedAt` corroborates a resumed lead.

Timing needs bidirectional uniqueness because several processes can start in one
team's interval and one process can fit several team intervals. It also covers
only eager team creation: a team created at first spawn may appear minutes after
boot. `agent-resume` therefore asks before applying a timing-based rebind.

The `SessionStart` hook carries the transcript ID, which associates a process
with its resumed transcript. Team inference needs the internal ID used in the
implicit team name. In the measured corpus, one of eight resumed hook IDs mapped
to an implicit team name, and four of 23 team directories used IDs unique to a
process life.

Resolver returns an exact result for durable identity, a corroborated result for
a unique timing match, and an unknown result for the remaining cases. A
corroborated result is offered with confirmation and defaults to yes. A durable
internal-session record would turn late-created teams into exact results as
well.

## Adopt

Adopt recovers work after a process exits or moves to the background. It records
running shells with PID and start time, and `adopt.json` checkpoints the
session's shells, agents, workflows, and cron jobs. A later resume claims the
checkpoint by atomic rename, verifies process identity before reattaching
shells, and restarts agents whose transcripts show unfinished work.
`CLAUDE_DISABLE_ADOPT=1` disables this behavior. Adding teammate identity and
team state to the checkpoint would make team resume part of Adopt.

## Resolver inputs

Resolver reads nine kinds of state:

- transcripts, both main sessions and the side transcripts under `subagents/`
- schema-versioned conversion receipts carried inside converted transcripts
- team configs, the rosters at `teams/<team>/config.json`
- mailboxes, the message files at `teams/<team>/inboxes/<name>.json`
- claude sessions
- running processes
- tmux servers, sessions and panes
- settings, `~/.claude/settings.json`
- the claude binary, both which one a launch uses and the pattern that decides
  whether a process on the table is a claude at all

Resolver owns inspection and interpretation of this state. Other code receives
structured answers from Resolver.

A mailbox records messages waiting on disk and is pruned when a busy recipient
takes them into memory. `mail_ledger` therefore derives delivery state from
transcript events, whose history remains available after mailbox uptake. Team
config and mailbox data answer separate questions and carry separate evidence.

Conversion receipts are versioned, tool-written evidence carried by the
converted transcript. Resolver reads them to recover team membership, copied
parent-record ownership, and the intentional identity assigned by promotion.
The corpus census retains the main transcript paths it examined, so a path with
no matching receipt is a known absence for that Resolver lifetime. A new path or
one outside that census still receives direct receipt validation. Ordinary later
stamps and roster observations do not silently replace the assigned identity. An
explicit complete identity or an intentional move can replace it.

Resolution produces one immutable target containing the exact session, canonical
transcript fact and path, resolved cwd, effective identity and role, provenance,
selection reason, and identity objections. Identity-bearing launch flags, reports,
roster and mailbox writes, liveness checks, and migration decisions read identity,
role, conversation, and cwd from that target rather than accepting those facts a
second time as parameters. Resolver keeps process, seat, roster, and conversion
evidence beside it for decisions that need those distinct records.

Resolver supplies the facts used by actions. A launch can run
`tmux split-window`, cleanup can remove a directory, and stop can signal a PID
after Resolver identifies the target. One reader keeps delivery, liveness, and
identity precedence consistent across all callers.

Each state class has a named structural check that requires its reads to occur
in Resolver:

- `transcript-pick-engine-only`, `tree-reached-via-engine`, and their siblings
  require transcript discovery and selection to use Resolver's transcript tree.
- `team-dir-read-engine-only` follows team and mailbox paths through local
  variables and attribute chains and requires filesystem reads on those paths to
  occur in Resolver. `roster-read-one-place` keeps roster parsing in one reader.
- `settings-read-engine-only` assigns settings inspection to Resolver.
- `proc-read-engine-only` assigns `/proc` reads and signal-0 liveness probes to
  Resolver.
- `server-liveness-engine-only` assigns AF_UNIX probes and tmux server, session,
  and client queries to Resolver. `pane-question-engine-only` assigns pane lists
  and pane-identifying environment reads to Resolver. These are separate checks
  because server availability and member seating answer different questions.
- `claude-pattern-engine-only` assigns the `CLAUDE_PROC` process matcher to
  Resolver.

The structural checks distinguish reads from actions. `claude_binary` chooses
an executable. `launch` chooses a tmux command from Resolver's pane answer. A
cleanup action stages a directory, asks Resolver to check it again, and unlinks
it. An archive copies roster or mailbox bytes into a receipt before deletion.
The installer edits `settings.json`. Process termination asks Resolver to
confirm exit.

The checks follow established helpers, path values, and named matchers. Current
read paths use those forms; a new form requires a corresponding check update.

## Implementation rules

Verify the conversion round trip in memory before writing. Apply the
conversion, apply its reverse, and compare the result byte-for-byte with the
source. Writers act only after that comparison succeeds.

Define the expected runtime fields before reading live specimens, then test the
reader against those specimens before callers depend on it. The mail ledger
distinguishes five unmatched states because inbox, process, and transcript
evidence describe different delivery stages. A promotion receipt records
membership before the first launch, so identity resolution can recover it before
the roster entry is written. Replacing a record includes updating every reader.

Run the suite only while the source tree is unchanged. Python reads the file to
execute it and may read it again for tracebacks, while structural checks parse
the source separately. An edit between those reads can make the executed code
and the assertions describe different files. Accept a passing run only when the
tree remained unchanged.

Every remedy named in a refusal needs an implementation and a test because the
refusal promises that the remedy preserves the user's data.

Reporters receive the immutable resume target and render it. The canonical
transcript fact carries path, optional title, and activity time together; there
is no separate title lookup. A full transcript snapshot is keyed by device,
inode, size, and nanosecond modification time. It derives the restored window
root and, when requested, compact mail evidence from one stable read;
restored-identity and mail questions reuse that answer until any key field
changes. Runtime reads produce
one immutable snapshot whose PID records are the exact objects referenced by its
process, agent, transcript, pane, and session indexes. A question that receives
a process table carries that snapshot through every helper instead of reading the
default table partway through. Callers declare optional `REGISTRY`, `SEATS`, and
launch `PLACEMENT` work through `NEED` rather than selecting among alternate
runtime APIs. Diagnosis does not declare `PLACEMENT`.

Callers also declare the computations they need. `Resolver(needs=...)` receives
hard requirements and a `maybe` set for computations reached on selected paths.
Loaders accept declared computations, and the end-of-run audit requires
every hard requirement to be used. Narrowing one question remains a parameter,
such as `mail_ledger(to=...)`; a distinct question gets a distinct method, such
as `spawn_parent_of`. Tests compare narrowed and complete answers where they
should be identical. Measure each computation directly so its declaration
matches its cost.
