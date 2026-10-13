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
agent. `doctor_answer` returns one immutable diagnosis for a selected team or
agent. Its roster and process readers build one member record per agent ID, ordered
requested member first, genuine lead second, then stable roster order. Each record
contains the runtime, transcript `Resolution` and transcript fact, parent result,
identity color, delivery evidence, and addressed ledger rows. Resolver derives the
shared facts, team verdict, and remedies from those records. `run_doctor` calls
`doctor_answer` once; the summary, team conclusions, and dossier loop use that
answer without calling Resolver or reading evidence again. The privacy rename
makes `_team_diagnosis`, `_known_teams`, `_teams_with_live_evidence`, and
`_nameless_receipts` private. The AST caller check rejects module-level calls to
private Resolver methods.

The summary uses the existing PID, identity, and title renderer. That renderer
clips each roster title to the terminal space left on its row; the full title
remains available to the dossier. ANSI is inserted around the identity after
plain-text layout, so `--no-color` changes no spacing. Dossier headers carry
explicit requested, membership, and runtime labels. Session UUID and title use
separate labeled rows, followed by parent and live seat. Lead identity, operative
team, polling, common delivery failure, and common remedies print once in the
team report. Recipient-specific ledger evidence and member-specific remedies stay
in the matching dossier. A named diagnosis resolves the deeper parent, delivery,
and ledger evidence; the empty-hand sweep omits that work. Direct diagnosis uses
the ordinary selector and immutable target, but does not infer a launch
destination. The team verdict still measures whether a live lead reads the team,
and remains the command's exit status.

The unnamed Agent tool runs its query loop inside the leader and writes a
sidechain transcript. Its work therefore resumes through an internal query
loop. SendMessage uses this path to resume a dead tmux teammate in-process by
replaying its JSONL.

## Cross-session UDS coordination

This protocol is separate from team rosters and their file mailboxes. The
transport statements in this section are from the unpatched Claude Code 2.1.232
binary at `/home/juraj/.local/share/claude/versions/2.1.232.orig-unpatched`.
The local `claude-code/src/` tree corroborates task and UI behavior, but is not
assumed to be an exact 2.1.232 source checkout.

### Endpoint, authentication, and discovery

A session normally listens at:

```text
$XDG_RUNTIME_DIR/cc-socks/<pid>.sock
```

The default path uses the fallback when `Buffer.byteLength(path)` exceeds 103.
If binding still fails with `ENAMETOOLONG`, the error reports a maximum of about
104 bytes. Startup rejects a socket directory that is relative, owned by another
user, not a directory, or a symlink. It walks ancestor directories, rejects a
symlink owned by another user or a loop deeper than 16 links, and requires each
non-symlink ancestor to be private or sticky and owned by this user or root. It
creates or fixes the leaf directory to mode `0700`, binds the socket, changes the
socket to mode `0600`, publishes the peer key, then exports the socket path and
child token for descendants. Shutdown destroys clients, closes and unlinks the
socket, removes the active peer key, clears `CLAUDE_CODE_MESSAGING_SOCKET` and
`CLAUDE_CODE_MESSAGING_TOKEN`, and settles held prompts as described below.

Startup generates two independent random 16-byte tokens, rendered as 32
lowercase hexadecimal characters. The peer token is published in exactly one
key file:

```text
~/.claude/sessions/<pid>.<sha256(canonical-socket-path)>.key
```

The JSON key record contains `peerToken` and may contain `procStart` and
`procStartFt`. Key files have mode `0600`; `~/.claude/sessions` and the socket
directory have mode `0700`. The child token has no key file. It is passed to
child processes as `CLAUDE_CODE_MESSAGING_TOKEN`.

The first authenticated frame is `{"type":"auth","token":"..."}` followed by a
newline. A matching token is classified as either `peer` or `child`. Required
authentication rejects a blank, malformed, non-auth, or bad first frame. On this
Linux host authentication is optional by default, so a missing or bad auth frame
is not an authenticated identity. A claimed sender address must never be treated
as authenticated merely because it arrived over a local socket.

Session discovery and direct addressing are different. Registry records under
`~/.claude/sessions/` are PID-named JSON files read with a 256 KiB cap. They carry
socket, PID, session ID, cwd, process-start identity, name, kind, status, and
bridge facts. The registry reader accepts `interactive`, `bg`, `daemon`, and
`daemon-worker` kinds and probes a candidate socket with a 250 ms connect. A
sender that already knows a local socket path and its peer key can send directly;
it need not find the peer through a registry record. Non-local IPC paths are
refused.

`ListAgents` reflects these discoverable session records, not every task model.
An `in_process_teammate` is a team worker with a prompt-wait loop and mailbox
integration, not one of the registry peer kinds above. Team workers can therefore
be absent from `ListAgents` even while their task and team mailbox records exist.

### Frames, sender identity, and attachments

The stream carries UTF-8 JSON. Newlines normally delimit frames; blank lines are
ignored, and a nonempty final fragment is parsed when a connection ends. The
server first appends decoded text to an unparsed buffer and destroys the
connection once it exceeds 1 MiB (1,048,576 bytes). This is an unparsed-buffer
limit, not an independent per-message limit: a large chunk that contains a
newline can still exceed it before splitting.

A raw client optionally writes the auth frame, writes one JSON frame plus a
newline, then ends the connection. Its five-second operation resolves when the
connection closes, not when the receiver approves, queues, or processes the
prompt. The server sends no general positive acknowledgement. Consequently a
successful raw send or `SendMessage` result means only that the sender did not
observe a transport error before close.

A normal raw user frame has this form:

```json
{
  "type": "user",
  "msg_id": "<sender-generated UUID>",
  "message": {"role": "user", "content": "text"},
  "priority": "next",
  "from": "uds:<sender socket address>",
  "file_attachments": []
}
```

For a user frame, `type` must be a string and `message.content` must be a
nonempty string. `session_id` is optional but, when present, must equal the
receiver's session ID. `priority` accepts `now`, `next`, or `later` and otherwise
defaults to `next`; `uuid`, `file_attachments`, `from`, and `msg_id` are optional.
The server gives an absent `from` the value `unknown`, makes a prompt UUID when
none was supplied, and retains a syntactically valid `msg_id` in peer-origin
metadata. Accepted frames become prompt-queue items with `mode: "prompt"`,
`skipSlashCommands: true`, and `isMeta: true`.

`msg_id` is generated by the sender and is only a transport-frame correlation
identifier. It is not a receiver delivery acknowledgement or evidence of model
consumption. Control receipts carry their own frame ID and refer to the original
user frame through `orig_msg_id`.

Cross-session text uses a canonical envelope:

```xml
<cross-session-message from="..." from-session="..." hop-chain="..."
  from-name="..." from-mode="bypass|prompting">
body
</cross-session-message>
```

All envelope attributes are optional. Canonical serialization orders them as
`from`, `from-session`, `hop-chain`, `from-name`, then `from-mode`; the parser
accepts an envelope only when reserialization exactly reproduces the input. The
raw `from` field and these attributes are sender claims. Authentication instead
comes from token class and, where available, verified Unix-peer PID metadata. The
receiver permits automatic replies only when a claimed UDS reply address is
well-formed and belongs to its own socket namespace.

A peer frame may carry at most 16 attachments. Each source or transfer file is
bounded to 31,457,280 bytes (30 MiB); staged transfer files expire after one day.
The sender copies a regular source file into the directory
`~/.claude/file-transfers`, mode `0700`, and writes the staged copy mode `0600`.
Attachment metadata includes `path`, `file_name`, `file_size`, `sha256`, and
optional `media_type`.

The receiver validates the attachment array and its 16-item limit; requires an
absolute path without the UNC form; checks that the source is a regular file and
its parent is a directory named `file-transfers`; bounded-reads the source; and
compares its declared size and SHA-256. It copies the content into a fresh
recipient upload using exclusive creation and mode `0600`, and only removes a
source spool file when that parent equals its own configured spool directory. The
parent-basename check does not establish the sender's full path ancestry. The size
and hash checks, plus exclusive creation of the recipient copy, establish the
properties described here.

### Holds, status receipts, and ordering

The two recognized control forms are:

```json
{"type":"control", "action":"rename", "name":"..."}
```

and:

```json
{
  "type": "control",
  "action": "peer_message_status",
  "status": "held|delivered|denied|expired",
  "orig_msg_id": "<original user-frame ID>"
}
```

`rename` invokes a rename callback only when `name` is a string. It is a
notification, not an authenticated identity change. It is sent through the raw
control sender, not ordinary `SendMessage`.

The receiver accepts, refuses, or holds each prompt. An accepted prompt enters
the queue immediately. A refused prompt is not queued. A held prompt waits for
user approval or a later permission-mode decision and produces a `held` status
when its reply address and original message ID can be used safely. Releasing a
held prompt queues it and sends `delivered`; refusing it later sends `denied`. A
full hold buffer evicts its oldest item as `expired`; shutdown and the
cross-session kill switch also settle held items as `expired`. The hold-buffer
capacity and receipt timeout are unknown.

Every status is sent over a new UDS control connection. Statuses are therefore
best-effort lifecycle notices, not universal acknowledgements: immediate
acceptance sends no `delivered` receipt, immediate refusal may send no receipt,
and an unsafe reply address or an untracked ID prevents a status from reaching
the sender. The receiver tracks up to 200 outbound IDs; `held` moves a matching
ID to an awaiting-terminal set, while a later terminal status removes it.

The observed status **Cross-session message held for approval** means the receiver
accepted the transport frame but withheld the prompt because the permission modes
differ. The recipient user must approve it before Claude sees it. **Cross-session
message released after approval** means that approval occurred and the receiver
released the prompt to the recipient session. The session's prompt queue still
controls when Claude takes it. Neither status is an ordinary positive
acknowledgement, proof that the prompt ran, or proof of model consumption.

1. **Transport write success** means the sender saw no local connection error
   before close.
2. **Held status** means the receiver parked the prompt for approval.
3. **Delivered status** means an earlier held prompt was released into the prompt
   queue.
4. **Model consumption** happens only when the receiving session later takes the
   queued prompt. No UDS acknowledgement proves it happened.

Ordinary inbound work enters a processing chain. Control frames and
attachment-free `priority: "now"` user frames can bypass it, so the protocol does
not promise global FIFO order across connections. The local source tree implements
`now > next > later`, FIFO within each priority. This ordering was not independently
confirmed in the 2.1.232 binary.

### Wrappers, teammate delivery, and tasks

`SendMessage` accepts nonempty plain text for `uds:` recipients and delegates it
to the raw sender. Its reported `msg_id` is the sender-generated correlation ID,
not receiver delivery confirmation. Cross-session `SendMessage` rejects the
structured experimental-team message form; that form belongs to the team mailbox
path rather than the UDS text transport.

UDS is also distinct from local task delivery. The task model distinguishes
`local_agent` tasks, which have IDs beginning with `a`, from
`in_process_teammate` tasks, which have IDs beginning with `t`. An in-process
teammate has isolated context, independent idle tracking, a prompt-wait loop,
and team mailbox integration. A message to a live in-process teammate waits for
its next tool round; it is not sent through the cross-session socket.

`TaskUpdate` persists each task as JSON at:

```text
~/.claude/tasks/<task-list-id>/<task-id>.json
```

The update path locks and rewrites that record. When agent-swarm ownership changes,
it also appends a durable message to the team mailbox, and the message text
contains a `task_assignment` object with `taskId`, `subject`, `description`,
`assignedBy`, and `timestamp`. The mailbox is a JSON array at:

```text
~/.claude/teams/<team>/inboxes/<agent>.json
```

with lock-protected append and rewrite behavior. The colored assignment box is a
cyan task-assignment rendering, not a teal transport frame and not a UDS receipt.

## Teammate transcript stamps

Every JSONL line records `sessionKind`. Teammate processes also record
`agentName` and `teamName`; leader records carry session identity alone. A
record's `agentName` and `teamName` are its stamp: the record is stamped with
the team its `teamName` names, and restamping a transcript rewrites those two
fields in its records. These fields control two behaviors:

- The /resume picker reads metadata from the **first line**. A session created
  as a bg fork therefore keeps the `bg` badge across interactive resumes, while
  the live process environment determines its behavior. `claude -c` selects
  transcripts that carry an interactive kind on their first line. Forking clears
  the copied identity stamps and gives the fork its own picker classification.
- Reconnection binds team identity from CLI identity flags or transcript
  stamps. Plain leads launch with session arguments, and their transcripts
  carry session identity alone, so recovery binds a dead lead through the
  teammate path. `isAgentSwarmsEnabled` activates identity-flag binding in
  2.1.220 and 2.1.226. Every exec and pane launch therefore carries `TEAM_ENV`
  inline, independent of the invoking shell's environment.

### The team a live process operates as

Claude Code 2.1.292 decides a process's team context once, when it mounts.
`computeInitialTeamContext` takes the argv team when that team's config loads,
and `initializeTeammateContextFromSession` then adopts the team stamped on the
window start of the resumed transcript when that config loads, keeping the argv
context otherwise. The window start is the first record of the compaction
window: the part of a conversation since it was last compacted, which a resume
restores. A config loads when it parses as a JSON object (`readTeamFileAsync`);
an absent or unparsable file does not. The inbox poller reads from that
operating team. `SendMessage` and the idle-notification hook use the argv team.
So a teammate reads from its operating team and sends to its argv team, and a
redirect is consistent only when the argv team equals the team the stamps on the
window start will load, or the stamped team cannot load at that moment. Lines
the process writes are stamped with its operating team, and a lead's spawn
payloads name it.

Resolver answers this once, in `_operating_team`, for every reader: the lead
runtime's `runs_as`, the lead process rungs, lead corroboration, retirement
objections, and a parent's team for promotion. Lines and spawn payloads the
process wrote after it started decide, and only that answer is exact; the lines
written after the process started come from it only if it is the only live
holder of its transcript. Those lines are read backward from the end of the
transcript, stopping at the first record written before the process started, so
a long conversation costs only what was written since; the window start, which
takes a walk of the whole transcript, is read only when the mount rule is
needed. Spawns written by earlier processes on the same transcript are not
evidence about the current one. Without such lines the mount rule is applied to
whether each candidate config could have loaded when the process started: yes
when the config parses now and its bytes or its `createdAt` predate the start,
no when its `createdAt` is later than the start by more than
`TEAM_MINT_WINDOW_MS`, and unknown otherwise, including a config absent or
unparsable now, which may have loaded and been removed since. A config created
after the start is a new one, and an earlier config of the team may have been
there at the mount: when this tool's reap receipt for the team shows it retired
the team after the process started, the answer is unknown rather than no. A
process with no argv team, and no stamped team that loaded when it started,
takes an implicit team created within `TEAM_MINT_WINDOW_MS` before it started,
when it is the only process that started that soon after the team was created
and the team is the only one it could have created in that time. These answers
are inferred. When the evidence does not decide, the answer leaves the team
unknown and lists the teams still open. The start time comes from the kernel,
read from `/proc/<pid>/stat` and `/proc/uptime`, with the session registry's
`startedAt` as the fallback.

A launch that has not started yet is a different question. `restore_answer`
says whether its window start's team loads in the census the launch was planned
with, so that team takes over from argv. Launch answers do not store it:
`enforce_restore` asks at the action boundary and `run_one` asks when it
reports the launch. In a team run that census holds every change the run makes
to its teams, because the run makes them all before its first member, a
rebuild or one retirement, and no member's step creates, rebuilds or retires a
team (`in_team_run`). A launch that moved is not asked: the retirement or
restamp it went through decides what it comes back as.

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
  it avoids deleting a lock while its writer may still be alive. An atomic
  variant can write the record to a temporary file and claim the lock name with
  a hard link, removing that empty-record interval while keeping the protocol.

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

1. `--agent-id` and `--team-name` in a flag-bound process identify exactly the
   team it sends to and the identity it was launched with. The team it reads
   from is the operating answer above.
2. Lines and spawn payloads a process wrote after it started identify the team
   it operates as exactly; process start time distinguishes them from records
   written by earlier agent instances (earlier processes) on the same
   transcript.
3. A team's `leadSessionId` identifies a fresh lead's conversation exactly
   because the fresh internal ID equals its transcript filename.
4. A unique, bidirectional match between team `createdAt` and process
   `startedAt` infers the implicit team a plain process created at boot.

Timing needs bidirectional uniqueness because several processes can start in one
team's interval and one process can fit several team intervals. It also covers
only eager team creation: a team created at first spawn may appear minutes after
boot. `agent-resume` therefore asks before applying a redirect to an inferred
destination.

The `SessionStart` hook carries the transcript ID, which associates a process
with its resumed transcript. Team inference needs the internal ID used in the
implicit team name. In the measured corpus, one of eight resumed hook IDs mapped
to an implicit team name, and four of 23 team directories used IDs that existed
only for one process's run.

Resolver returns an exact result for durable identity and for the transcript
records a process wrote since it started, an inferred result for the mount rule
and a unique timing match, and an unknown result for the remaining cases. An
inferred redirect destination is offered with confirmation and defaults to yes.
A durable internal-session record would turn late-created teams into exact
results as well.

## Adopt

Adopt recovers work after a process exits or moves to the background. It records
running shells with PID and start time, and `adopt.json` checkpoints the
session's shells, agents, workflows, and cron jobs. A later resume claims the
checkpoint by atomic rename, verifies process identity before reattaching
shells, and restarts agents when their transcripts show unfinished work.
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

### Public methods and private readers

Resolver has exactly 36 public methods and no public instance attributes. Its
answers use read-only records. The retained observation types and config
constructors establish ownership of their nested values; a read-only outer
mapping alone does not copy a caller's referenced objects. In particular, rebuild
rejection retains the exact launch supplied by its caller. `refresh_transcript_evidence`
is the one notification: transcript writers use it to discard cached evidence
after a mutation attempt. The AST checks compare this method and caller table
exactly, reject public aliases and component access, reject module calls to
private Resolver methods, and require an empty `PUBLIC_RESOLVER_ATTRIBUTES` map.

| Public method | Module caller or callers |
|---|---|
| `attach_decision` | `run_agents` |
| `audit_answer` | `audited` |
| `census_answer` | `run_census` |
| `cleanup_plan` | `run_cleanup` |
| `complete_lead_migration` | `lead_rebind` |
| `complete_member_migration` | `apply_member_migration` |
| `completion_answer` | `_complete` |
| `demotion_plan` | `run_demote` |
| `doctor_answer` | `run_doctor` |
| `launch_observation` | `launch_survived` |
| `launch_plan` | `build` |
| `lead_migration_plan` | `run_one` |
| `mail_answer` | `run_mail_ledger` |
| `member_redirect_plan` | `auto_redirect`, `run_one` |
| `membership_plan` | `record_membership`, `release_membership`, `reserve_membership`, `restore_member` |
| `promotion_plan` | `run_promote` |
| `rebuild_plan` | `apply_rebuild_plan`, `rebuild_roster`, `rebuild_team` |
| `refresh_transcript_evidence` | `apply_restamp`, `embed`, `materialize` |
| `restore_answer` | `enforce_restore`, `run_one` |
| `resume_answer` | `apply_resume_title` |
| `revalidate_cleanup` | `run_cleanup` |
| `revalidate_cleanup_cut` | `remove_cleanup_action` |
| `revalidate_conversion` | `embed`, `materialize` |
| `revalidate_launch` | `run_one` |
| `revalidate_lead_migration` | `lead_rebind` |
| `revalidate_member_redirect` | `apply_member_migration` |
| `revalidate_membership` | `apply_membership`, `team_lock` |
| `revalidate_reap` | `archive_inbox`, `reap_team` |
| `revalidate_rebuild` | `apply_rebuild_plan` |
| `revalidate_stop` | `stopped` |
| `revalidate_team_run` | `run_team` |
| `selector_answer` | `apply_selector` |
| `sides_answer` | `run_sides` |
| `socket_control_answer` | `run_poison` |
| `stop_plan` | `confirm_attach`, `stop_targets` |
| `team_run_plan` | `run_team` |
| `teammate_mode_answer` | `check_teammate_mode` |

`completion_answer` reads the candidates and supporting records for every
completion topic. `_complete` does not make another Resolver call, read a file, or
call an evidence helper to make a second candidate list. For team completion,
`completion_answer("teams")` returns immutable rows and the emitter prints those
rows.

A conversion plan records source and destination generations. A reap plan records
the selected team-directory generation; a mailbox archive plan records the inbox
generation; and a cleanup plan records the selected directory generation. Each
revalidation compares the current value with the recorded value. A writer does
not use a later read to choose a different source, destination, or deletion. The
root-stamp conversion fixture checks four cases through operation answers.

### Complete roster observations

`FileObservation` retains an immutable path, the existing file-generation tuple,
and the descriptor's permission bits masked to `0o777`. `_file_observation`
reads bytes and mode from the same open descriptor and compares its device,
inode, size, and nanosecond modification time with the named path. A mode of `0`
is retained; `None` means there was no physical mode to observe.
`_file_generation` projects the generation for readers that still require a tuple.

A physical generation is `(dev, ino, size, mtime_ns, raw_bytes)`. The reader also
preserves `("absent",)` and the diagnostic
`("changed-during-read", descriptor_signature, named_signature, raw)` result.
The existing reader treats every `OSError` as absent, including permission
failures. Census skips those records. An absent lookup therefore does not prove
that no file exists. An unstable read refuses census selection instead of being
parsed or treated as absent.

`Resolver.RosterSnapshot` owns the complete roster interpretation. It retains a
file observation, config data, and any parse problem. State, operative team name,
raw bytes, mode, and activity time are derived properties. An empty parsed object
is valid. A nonempty config `name` supplies the team name; otherwise the directory
name does. `_rosters`, `_scan_teams`, `_boot_join`, and `census_answer` use that
selected name even when it differs from the directory. Activity milliseconds are
`mtime_ns // 1_000_000`; floating-point seconds can round a timestamp immediately
below a millisecond boundary into the next millisecond.

An explicit preview owns a path and config, with a generation containing that
same owned config. It does not claim an on-disk write or physical permissions.
Physical generation tuples retain their identity. Absent records, invalid parse
records, and previews reject metadata that contradicts their construction.
Snapshot consumers use attributes. Parsed config objects, individual membership
records, and outer operation answers remain separate mappings.

Snapshot construction copies config values through
`immutable(json_value(data, strict=True))`. The supported tree is string-keyed
mappings, lists or tuples, and built-in string, boolean, integer, float, and
`None` values. NaN and infinities keep the parser's existing behavior. Unsupported
leaves, non-string keys, dataclasses, sets, and cycles are rejected. Shared
children that do not form cycles are allowed. Copying also detaches mapping-proxy
backing dictionaries, so freezing only the outer object is insufficient.

`_team_members` owns selection from a team name, the entire retained census, a
normalized runtime snapshot, the retained durable-assignment index, and the
enrichment setting. Census sequences become tuples of complete snapshots;
process rows and their argv values become immutable tuples. Already immutable
tuples and their records are retained. Cache reuse requires those retained input
objects to be identical, not merely equal. Target-roster lookup follows the cache
check, so repeated absent-team queries reuse their complete answer too.
`_team_runtime` projects the member answer, and `_team_description` caches by
member-answer identity. A change to another roster can change lead inference,
which is why the selected team's generation alone cannot identify these inputs.

`_update_census` inserts, replaces, or removes a record by its physical config
path. It preserves unrelated order and identity and returns the original tuple
for a no-op. Previews and accepted rebuild effects use this operation.
`_complete_routed_launch` also uses it when a completed member or lead rebind
retires a source team. Removing one path does not remove a second record that
happens to declare the same team name. A later real `run_agents` job asks for a
fresh census and process snapshot after either success or refusal. Dry jobs keep
their selected preview census because it was never written.

### Cache invalidation after mutations

Each `Cache` declaration names the events that discard its stored value. One
registry retains the descriptors, names, storage slots, empty-value factories,
and event dependencies. `_invalidate` removes matching slots without mutating
previously returned values, reading files, or exercising NEED declarations.
Subsequent readers reconstruct evidence through their existing loaders.

| Event | Caches discarded |
|---|---|
| Transcript | `_stamps`, `_carried`, `_carried_scope`, `_receipts`, `_spawns`, `_spawn_log`, `_by_agent`, `_by_team`, `_team_assignments`, `_transcript_facts`, `_transcript_snapshots`, `_copied_prefixes` |
| Process | `_procs`, `_seat_table`, `_seats`, `_registry` |
| Either | `_runtime_table`, `_runtime_snapshot`, `_team_member_answers`, `_team_descriptions`, `_operating_answers` |
| Neither | `_terminal`, `_boot_time` |

The active `_process_table(refresh=True)` uses this registry. Transcript writers
call `refresh_transcript_evidence`. Neither event resets configured directories,
`_needs`, `_exercised`, the cache-free rewriter, or previously selected answers.
This responds to this command's writes, not arbitrary external transcript edits.

`apply_restamp` refreshes immediately after atomic replacement succeeds, before
member or lead completion can refuse. Atomic writers capture and validate the
observation and close the descriptor before `os.replace`; successful replacement
returns immediately, with no intervening metadata read. A failure before
installation preserves the earlier evidence.

Every exclusive creation in `materialize` and `embed` refreshes in its own
`finally`, including the embedded metadata creation. A write, flush, chmod,
fsync, fstat, or close failure can leave readable bytes without returning a
writer result. A failed attempt before creation may also invalidate unchanged
evidence, without claiming that a write succeeded. Each successful source or
side-metadata unlink refreshes again before any later operation can fail.
Dry conversion and migration routes do not invoke these writes or notifications.

### Selected roster writes and observed effects

Resolver selects one roster write containing the path, original observation,
owned config, serialized bytes, mode, and filesystem action. Rebuild and all
membership operations share `_roster_write`. It obtains owned object config from
`RosterSnapshot.preview` and serializes it once; writers do not serialize the
roster again. The preview supplies owned data, not a claimed file observation.
An observed mode, including zero, is preserved. Only a record without an
observed mode selects the `0600` default. Inbox selections retain the original
observation and a create, replace-empty, or keep action. Their path and generation
come from that observation. Create and replace-empty also select intended bytes
and mode; keep needs only the unchanged observation. The name is diagnostic
metadata, not evidence that a write happened.

`write_new_bytes`, `replace_json`, `replace_text`, `replace_bytes`, and
`replace_retained_path` return `FileObservation`. Exclusive creation and atomic
replacement share descriptor writing and observation capture. The returned mode
comes from `fstat`, not the requested value. Forwarding helpers return the same
observation, and retained-path replacement reports the installed inode.
Conversion revalidation still accepts generation tuples: `materialize` and
`embed` explicitly pass each writer observation's `.generation`. That projection
does not add permission-aware conversion revalidation.

Rebuild completion checks the `Applied` operation, launch and plan association,
confirmation, dry-run state, durable action, written selection, path, bytes,
config, physical observation, and mode. It validates operand types before value
comparison. Operation and action names must be built-in strings with supported
values. Both rollback operands must be `FileObservation` values; the retained
selection must be physical and the reported observation must equal it. Every
selected inbox needs exactly one associated effect with its action,
before-observation, and correct result. Both inbox collections must be present,
even when empty. Diagnostic names are optional; only string names are formatted.

Completion checks these retained values without filesystem reads or another
serialization. Create requires an absent before-observation, replace-empty a
physical empty file, and keep a physical nonempty file with a reported result
equal to the original observation. Equal observations need not be the same Python
object. The shared written-result comparison first requires a physical
`FileObservation` and checks each intended operand's exact type against its
observed counterpart. It then compares destination, bytes, and mode. A float or
boolean cannot stand for integer permissions, nor a mutable buffer for bytes.
The comparator does not interpret bytes as config data. Invalid JSON is legitimate
in a backup of an unreadable roster.

Both selected and reported config values pass through `RosterSnapshot` before
their owned data is compared. Its expected type and value errors become
objections. The selected config's identity association and the reported config's
value comparison remain different checks. Supported list and tuple arrays are
normalized by the existing constructor; completion adds no serialization or
special equality rules for config leaves. The selected candidate is retained
for publication after every effect passes.

Incorrect or incomplete effects return exactly `launch`, `written_roster`, and
`objections`, with the original launch object and no written roster. This also
preserves an ordinary dictionary supplied as launch; it does not detach or deeply
freeze that caller's graph. Successful output contains exactly `launch`,
`written_roster`, `original_generation`, `replacement_generation`, and
`objections`. The generation values come from the retained original and accepted
file observation. Neither result copies raw plan fields or returns the `Applied`
input: only that input's outer dataclass is frozen, and freezing it does not
establish ownership of its nested data. Executors and effect tests can retain
that input separately.

Only complete acceptance publishes the owned candidate as both the completed
destination and the census entry. Creation inserts it into an empty census as
well as an existing one. Previously retained snapshots remain unchanged.

Rebuild revalidation compares the complete expected observations for its current
phase. Backup creation, retaining the rollback inode, and installing the new
config each update those expectations. After replacement, the rollback inode and
the new config are separate observations. Before restoration or cleanup, both
must still match, including permissions. An inbox failure restores the retained
original inode, bytes, and mode, or removes a newly created config. The next check
expects the restored config and absence of the rollback path that was renamed.

Membership reserve, record, release, and restore use the same selected byte
writer. Initial validation checks every selected roster before effects; each
roster is checked again under its lock. A cross-team update retains separate
source and destination modes. Successful backup history is passed to Resolver
as an immutable set of config paths. The first backup in an invocation may
replace an older invocation's `.bak-agent-resume`; later writes leave it alone.
This differs from rebuild, which requires an absent invalid-roster backup path.
There is no transaction-wide rollback across several membership writes: a late
refusal may follow an inbox change or a completed write to the first roster.
The existing lock publication procedure is unchanged.

### Operation behavior

`DoctorRequest` is frozen. Its fields are `team`, `agent_id`, and `session_uuid`,
of which at most one may be set, plus `include_dormant`. `__post_init__` refuses
more than one selector. `doctor_answer(request)` retains that request, the
selected target, ordered
`TeamDiagnosis` records, the shared snapshot and generations, counts, structured
facts about promoted transcripts without a name, rules that decide which dossiers
are included, objections or refusal, and the exit code. The eight-check doctor
unit verifies recursive immutability, exclusive `run_doctor` access, private
evidence readers, answer-only rendering, and use of credentials held by the answer
without a later `/proc` or Resolver read.

`MemberRedirectRequest` is immutable and has `kind` `explicit`, `live`, or
automatic. The explicit branch names a team. The live branch uses the member's
live lead team. The automatic branch runs only when the required live-lead facts
are available. `member_redirect_plan(request)` chooses the source and
destination teams, target identity and parent, confirmation, warnings, objections,
and any rebind before any write. `auto_redirect` and `run_one` are its only
callers.

Where members go and whether the team their stamps restore them as is retired
is one decision per run, made before any member launches by the private
`_team_redirect`. A team run makes it in `team_run_plan` for every member it
starts; a run of one member makes it inside `member_redirect_plan`. The decision
names the destination and its selected lead session, whether a person must
confirm (an inferred destination), and the retirement: reap the source team when
no process that sends or reads as it is left, restamp each moving member's own
conversation under `--force`, or blocked. `run_team` carries out the team-wide
part once in `apply_team_redirect`, in the order a single member's move keeps:
the one question, then every stop (`settle_stops`), then at most one reap, or
instead one decline. Each running member a move restarts is asked about in
turn before any process is ended; a declined stop, or one that cannot be asked
about because there is no terminal and `--yes` was not passed, refuses the whole
move with nothing stopped, retired or started. The processes are then ended, the
plan is revalidated with them gone, and only then is the team retired. Each
member's plan receives the decision and the run's answer to it in
`MemberRedirectRequest.team` and `team_answer`, skips the stops the run settled,
and carries the rest out without deciding again. The decision is the only
authority over which team directories exist while the run lasts: `run_team`
rebuilds a missing roster before the first member only when the members stay,
and no member's step creates, rebuilds or retires a team, so no roster is
rebuilt for a team the members leave. A restamp touches one transcript, so a
live process holding that transcript is its only conflict. A reap counts each
moving member's other holders as ended, since the run ends them first. A moving
member held by this run's own process or one of its ancestors cannot be ended,
so the decision refuses the move before anything changes, with `--force` or
without. A running member has already moved when all its live processes operate
as the destination, whatever roster still lists it: the decision marks it
`arrived`, and its step reports it running there and registers it in the
destination's roster, without starting, stopping or rebuilding anything.

Whether a member can be moved onto a team comes from one private answer,
`_destination_answer`: the team needs a valid roster and a selected lead session
to be the moved member's parent. Its `refusal` says why not, and its
objections refuse a move asked for with `--into-team`, saying to drop that flag
and pass `--keep-team` to stay, or to name a team that has a lead session. An
explicit `--into-team <name>` asks it about the named team, and
`_lead_destination` about the team the live lead runs as, for `--into-team
live` and the automatic decision; for the automatic move a missing lead session
blocks, so `decline_unreachable` asks before starting the member where its lead
does not read its mail.

A report offers a move only as the planner would make it. `_move_offer` asks
`_team_redirect` for the command it would offer, as given and then with
`--with-mail`, and offers the first under which the decision moves someone,
with those flags. When a team's lead runs elsewhere, the doctor's remedies for
the team come from `_team_moves`: the team run with `--all`, which starts the
stopped members on the lead's team, and the run with `--into-team live --all`,
which restarts the running ones too, each judged on the members `team_run_plan`
would start. So the doctor names `--with-mail` when only waiting mail keeps the
team from being retired, offers no automatic move when the lead is not known
exactly or running members stay behind, offers stopping the processes that run
as the team when they alone stand in the way, and always names `--keep-team`.
A lead move's crew report asks `_crew_move` the same about moving the crew onto
the lead's destination, in the world the lead move leaves: the processes it
ends gone, and the source roster gone when it retires that team.

A member redirect's revalidation compares the facts its plan used: the
destination roster observation, the parent transcript's identity (device and
inode, which appending leaves unchanged, since a working lead writes to it all
the time), the destination lead's runtime generation, the selected holders, the
rebind, and the redirected-launch object and placement.

`apply_member_migration` is the only caller of
`revalidate_member_redirect(plan)` and
`complete_member_migration(applied)`. The
`member-migration-completion-reuses-route-selection` API test verifies that
completion preserves the explicit `--into-team` provenance selected by
`member_redirect_plan`, preserves the selected identity, and reuses the selected
objects. `apply_membership` preserves nonempty inbox bytes, replaces a selected
zero-byte inbox with exactly `[]`, creates an absent selected inbox with `[]`, and
refuses generation drift before a write while retaining the changed bytes. The
applied member-completion test verifies the selected source clearing and
completion result. The applied member-restamp API check restores the selected
destination with expected bytes and reuses the selected object and provenance. The
unstamped-lead API check refuses restamping before any mutation.

`LeadMigrationRequest` is frozen and contains `destination`, `force`, and
`archive_mail`. It requires a nonempty destination, and `run_one` asks for a lead
migration only when `--into-team` names one: a team named inside an agent id
says which lead this is, and when the stamps disagree `enforce_identity` refuses
and names `--into-team` as the move. `complete_member_migration`
and `complete_lead_migration` receive an `Applied` record, which is frozen and
contains `operation` and `plan`. `run_one` is the only caller of
`lead_migration_plan(request)`. `lead_rebind` is the only caller of
`revalidate_lead_migration(plan)` and `complete_lead_migration(applied)`.
`_retarget_launch` and generic rebind helpers are private to Resolver plan and
completion builders, with no module callers. API tests accept lead route selection,
revalidation, exact caller ownership, the absence of generic or raw module calls,
and matching dry completion. A non-mutating live check correctly reports that the
selected effects were not applied. The applied lead-migration test verifies the
selected route and retained crew facts after the migration. Module code does not
call `lead_session_for` or `roster_census` to finish either routing operation. The
team-run entrypoint test reuses the exact destination and inbox generations from
`team_run_plan` and refuses generation drift. The attach entrypoint test verifies
that `run_agents` uses `attach_decision` without reading fresh terminal or server
state.

Renderers and executors do not call process-table, runtime-index, PID-liveness,
tmux socket or pane, roster-path, mailbox-path, receipt, transcript-selection,
spawn-payload, or title readers after receiving an operation record. They do not
combine additional records into another decision.

The runtime reader parses direct argv into structured option values once.
Identity, process classification, and placement decisions use those values, not
substrings in rendered argv or explanatory text. A decision never branches on the
rendered `via`, `how`, or `detail` fields.

Renderers receive immutable records and produce text only. Writers receive a
selected, revalidated plan and perform its named update, signal, launch, or
transcript move. A writer may make the final check required by that plan, but it
does not choose a new identity, parent, destination, or member set.

`selector_answer` selects a conversation without selecting a tmux destination.
`resume_answer` returns what a `--resume` value selects: a session by its ID or
transcript file name, or the sessions that have it as their title.
`launch_plan` adds identity, settings, cwd, provenance, the roster
record to write, terminal context, lead and member runtime facts, `CrewPlacement`,
`LaunchDestination`, and `RosterPaneReference`. `_retarget_launch` rebuilds those
coupled facts after an identity or route change. It does not retain a roster,
process, or destination record selected for the prior identity.

A `Seat` is an observed pair of socket and pane. A `LaunchDestination` is a
selected socket and optional pane reference. They are deliberately distinct: a
recorded pane is meaningful only on the socket of the roster record that
supplied it, and an observed crew seat can select a destination without
authorizing reuse of that occupied pane. `CrewPlacement` records all observed
crew seats, their per-socket grouping, whether they are split, and the selected
destination with its evidence. `TerminalContext` records the current terminal's
seat, or a tmux socket without its pane ID; it is captured once for a launch or
team run and is not read again while rendering or attaching.

A `SocketLiveness` is what one connection attempt found at a tmux socket path:
`alive`, `absent`, or `unknown` with a reason worded for the user. A path of
`SOCKET_PATH_LIMIT` (108) bytes or more is unknown without an attempt, since
neither the probe nor tmux can connect through it. An unknown socket counts as
neither live nor dead: cleanup keeps it, `_newest_live_socket` and the seat index
pass it over, `--poison` and `--unpoison` stop, and attach is not offered. For a
launch destination on an unknown socket, `_tmux_launch_action` selects `refuse`,
and `launch` raises `LaunchFailed` with the reason before any tmux command runs.
A `RosterPaneReference` for a pane recorded on that socket has the state
`unknown`, and the plan reports the pane as not checked. A launch that runs in
the current pane does not read the server and is not refused. A
`SocketLiveness` raises `TypeError` when read as a truth value, so a caller
compares its state.

A transcript title is a `TitleFact`, not a string. It contains the selected title
text, the event kind, transcript path, timestamp, and source record UUID. Every
reporting answer carries that one object, so a title's text and the event that
provided it cannot diverge between a summary, dossier, completion candidate, or
launch report. The transcript fact also carries, as a string, the title that a
`--resume` value or a bare selector is compared with. `_session_title` reads it
in the same pass from `custom-title` and `ai-title` records and from
`custom-title.json`, taking the sources in the order `claude --resume` takes
them: the custom title from the last 64 KiB of the transcript, else from
`custom-title.json` in the session's directory, else from the first 64 KiB;
without a custom title, the AI title from the last 64 KiB, else from the
first. Titles from records are taken as written, without the filter of the
shown title, which skips harness text, one-word AI titles and the AI title
"new session". `custom-title.json` is decoded as UTF-8, with U+FFFD in place
of each invalid sequence, and its title is cleaned as claude cleans it:
trimmed, each run of control or format characters (Unicode categories Cc and
Cf), U+2028 or U+2029 replaced by one space, cut to 200 code points and
trimmed again. A file that gives an empty title is passed over, and so is one
that is not JSON by `JSON.parse`'s rules.

### Verification scope

A read-only, synthetic, mutation, or unit test establishes only the fact it
checks. Final verification also requires one agreed read-only timing measurement
on the post-live-fixture source; mutation coverage; live process and tmux checks;
routing and placement checks; launch, attach, and stop checks; conversion, mail,
and doctor checks; and c4 verification. Do not treat an isolated fixture, a
source-level check, or a compile result as evidence for one of those other
behaviors.

### Generations, revalidation, and conversion

A team-run plan retains its member answer's process snapshot, census, durable
assignments, and selected complete roster. It chooses the member order,
former-member set, destination, and the run's redirect decision from that
evidence. `revalidate_team_run` compares the selected roster observation,
including permissions, and the redirect destination's roster before the run
starts. A change stops execution rather than choosing another member list.
Launch observation records whether the selected launch appeared, survived, and
wrote its transcript.

Mutations select a plan, print or confirm it, compare the recorded evidence,
then act. Roster writes compare complete file observations; transcript conversion
compares source generations and destination collision state; reaping and mailbox
archiving compare the bytes immediately before removal; process actions identify
a PID with its kernel start time. A mismatch stops the action rather than
choosing from later state.

AST checks list the permitted production callers of each public Resolver method.
They reject a module-level call to a private Resolver reader or
`RESOLVER.rewriter`, a renderer that builds a second record, and a writer that
reads new evidence after plan selection. The checks compare exact caller sets and
shared immutable object identity; a search for method names cannot establish
either condition.

`TranscriptRewriter` changes transcript records and verifies that applying the
reverse conversion restores the source bytes. Resolver passes the selected
identity, parent, side grammar, mailbox, destination, collision, and generation
facts to the rewriter. The rewriter does not query Resolver or inspect Resolver
paths. Module-level commands do not reach through `RESOLVER.rewriter`; promotion,
demotion, restamping, and archive executors use their selected operation plans.

A mailbox records messages waiting on disk and is pruned when a busy recipient
takes them into memory. `mail_answer` therefore derives delivery state from
transcript events, which stay in the transcript after the recipient takes the
messages from the mailbox. Team config and mailbox data answer separate
questions and carry separate evidence.

Conversion receipts are versioned, tool-written evidence carried by the
converted transcript. Resolver reads them to recover team membership, copied
parent-record ownership, and the intentional identity assigned by promotion.
The corpus census retains the main transcript paths it examined, so a path with
no matching receipt is a known absence until transcript evidence is invalidated.
A new path or one outside that census still receives direct receipt validation.
Ordinary later stamps and roster observations do not silently replace the assigned identity. An
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

While the selftest runs, Resolver's `/proc` readers (`_proc_stat`, `_cmdline`
and `_proc_env`, and through them `_pid_alive`, `_proc_start` and the tmux
server fact) ask a process fence before reading, in the selftest process and in
every tool run one of its units starts. The fence's root is the selftest process
that runs the units: a read-only run, a serial live run, one batch of a split
run (a batch of live-tier test jobs, which one worker runs), or a live run's
read-only child. A unit not declared with `machine=True` sees the root and the
processes under it, and nothing else: `_process_table` narrows the `/proc`
listing to them before it reads any pid, the session registry is empty, and a
reader refuses every other pid. A unit declares `machine=True` when its engines,
or the tool runs it starts, must read on purpose, through the engine's process
inputs (`_FakeTmux.PROCESS_INPUTS`), processes outside that tree or the real
session registry, and says why beside the declaration. The declaration decides
what those inputs answer and nothing else: a declared unit's table lists the
whole machine and its registry is the real one. The selftest's own process
readers on `_ProcessFence` read the same either way. In both worlds a reader may
read any pid from `PID_CEILING` (1 << 22) up, which no process can hold and from
which fixtures number the processes they invent. Every other pid the run admits,
whether a declared unit's table or registry named it, Popen started it or a walk
found it under the root, is under one rule: it is read while its start time is
the one recorded when the run learned of it, or once no process holds its
number, and refused while another process holds it. A pid that a process under
the root holds now is admitted as well, since the walk that finds it there is
how the run learns it. A value that is not a whole number above zero is refused.
A refused read is answered as a process that does not exist and is written down
with the unit, the reader and the pid. To refuse a pid, the fence reads only the
start time it compares for a pid a declared unit's table or registry named or
one the run knows. The run goes on, and the tier's row
`read-only-tier-reads-only-admitted-processes` or
`live-tier-reads-only-admitted-processes` names every refusal, and every fault
of the fence itself.

`ran_unit` hands a unit's rule to the tool runs the unit starts through
`AGENT_RESUME_SELFTEST_UNIT`: the root's pid and start time, the fence's token,
the declaration and the unit's name. `main` installs that fence before anything
else, so a tool run's root is the selftest process that runs its unit, never the
tool run itself. It checks the root's start time before walking the root's
children, so a root that has gone leaves it no processes, and it writes its
refusals to the file `AGENT_RESUME_SELFTEST_FENCE` names, which the tier's row
reads once the tool run has exited. The pids the run knows are kept in one
record for the whole run, a file beside the refusal record: each of the run's
fences adds the pids it learns there, and reads what the others added before it
counts a pid as unknown, so a tool run admits a pid the selftest process learned
by the same rule. A fence fault is a failure in the fence's own work: the
refusal record cannot take a refusal, the record of known pids cannot take a pid
or cannot be read, the refusal record cannot be read for the row, or a walk has
not settled (below). In the selftest process each fault is kept for the tier's
row, which fails naming it; a refusal the record could not take is kept there as
the refusal it is. A tool run has no row of its own, so there a fault ends the
run: `fence_fault` names it on stderr and raises `FenceFault`, which `answered`
turns into exit status 70. `FenceFault` is not a `Refusal`, so the product's own
handlers, which catch a refusal to bring up the rest of a team in `run_agents`
or to print a roster warning in a rollback, let it through, and no code between
the read and `answered` absorbs it. A tool run ends with a fence fault before
anything else runs when its `AGENT_RESUME_SELFTEST_UNIT` does not parse. A tool
run that is a selftest itself roots its own units at itself. `ran_unit` drops
RESOLVER's process caches as each unit starts and again in its `finally`, so a
table a declared unit read reaches no other unit. `proc_reader_audit.py` runs
the tiers with a record file of its own and lists every refusal by unit, reader
and pid, those of the tool runs included. A `claude` that records its arguments
and fails sits first on `PATH` beside the tmux one for the whole run, so a unit
or tool run that launches an agent reaches it instead of the machine's claude,
and the tier's `read-only-tier-runs-no-claude` or `live-tier-runs-no-claude` row
says whether anything did.

The selftest ends the processes it starts through readers of its own on
`_ProcessFence`, which do not ask the fence and are limited instead by how they
find what they read. A run is the subreaper of every process it starts, so what
its fixtures orphan stays under it, and `finish_run` ends what is still running
when the run finishes. Only the outermost `run_selftest` call in a process does
that: a call made while another is in progress, as a unit's fixture makes one,
neither switches adoption on nor ends what is left. The tier's
`read-only-tier-adopts-orphans` or `live-tier-adopts-orphans` row checks
adoption by what happens: an orphan made on purpose before the first unit, and
another after the last, must each have the run as its parent. The tier's
`read-only-tier-leaves-nothing-running` or `live-tier-leaves-nothing-running`
row states what `finish_run` found once the units were done, so a leak no unit
caught fails the run. The run that merges the batches of a split live run adds
what its own two orphans showed, what its own `finish_run` ended, and its own
fence's refusals and faults to the rows it folds from the batches. A unit that
finds a process a tool run left behind ends it there, through `end_processes` as
`finish_run` does, so no later unit has it in view. `run_processes` walks down
from the run by the `children` files of each process's threads and reads the
stat file of each process it finds. Each process's parent is the process with
the `children` file that listed it in the walk's settled pass, not the parent
its stat file names, so `subtree`, which `end_process_tree` and
`end_left_behind` ask, and, under a fence, the engine's process table, which
`started_here` asks through `_descendants_of`, take the walk's answer.
`identity` reads one pid's stat file only when that pid is the run itself or the
same walk finds it under the run, so a pid the run knows that has left the run's
tree and still runs is refused there, though `admit` admits it. A pass of the
walk reads those files one after another, so when a process's parent exits
meanwhile, the process can move to a subreaper inside the run between two of
them and be in neither. The subreaper is the run or under it, so the pass reads
its file too, and misses the process when it read that file before the process
got there. After each pass the walk reads every children file the pass read once
more, and passes again, up to four passes (`WALK_PASSES`), while one of them
lists a pid the pass did not find. A walk that still finds one after its last
pass has not settled, and the reader reports that as a fault of the fence rather
than judge from it. `run_processes`, `identity` and the fence's `admit` all walk
that way. `ancestors` reads the stat files up the chain of parents from the
run's parent, so that the run never signals one of them. Any other pid is
reported as not running, without being read; a refused pid that a process holds,
and a value that is not a whole number, are written down as the fence writes its
refusals, in a unit or outside one, and a pid `pidfd_open` cannot judge is
written down with the error, so the tier's row names it and
`proc_reader_audit.py` counts it. `end_processes` and the functions built on it
signal each process through a pidfd opened while its start time still matched,
and never signal pid 1, the run, an ancestor, a process named systemd, or a
process that did not start after the run. The smoke check's stand-alone process
is outside the run on purpose, and the cleanup never reads it: the stand-alone
process holds the only write end of a pipe, the run keeps the pipe's read end,
its exit pipe, and the run ends the stand-alone process through a pidfd opened
while that pipe had not yet reached end of file.

Mutation scripts import `probe` and call `probe.expect(...)`. The function runs
one clean suite and isolated mutants, then raises `ProbeFailure` if the clean run
fails, a mutant crashes, no check bites, or the expected check does not bite.
The probe has no command-line interface, so no exit status can be separated from
its report by redirection or a surrounding shell command. A positive operation
fixture reports an unexpected refusal or missing result as a failed named check,
including the exception type and message. It restores patched globals before
later checks run; the runner still rejects an aborted unit.

Cache dependency checks compare actual readers before and after transcript
operations, with a cold Resolver as a control. The registry test establishes that
invalidation follows declarations, not that every required dependency was
declared. Assignment selection is tested separately with actual old and new
assignment answers while the selecting Resolver's census, runtime, and enrichment stay
fixed; this private-reader substitution does not claim that public refresh leaves
the member cache intact. Dry-run checks retain directory entries as well as file
bytes, so creating an empty directory counts as a change.

Every remedy named in a refusal needs an implementation and a test because the
refusal promises that the remedy preserves the user's data.

Reporters receive completed immutable answers and render them. Resume reporters
receive the immutable target. Doctor reporters receive the canonical member
records from `doctor_answer`; summary and dossier title rows therefore retain the
same transcript fact by object identity. The canonical transcript fact carries
path, optional titles, and activity time together, and the reporters that
show a title and the `--resume` title lookup read it from that fact.
A full transcript snapshot is keyed by device, inode, size, and
nanosecond modification time. It derives the window start, the newest team stamp
and, when requested, compact mail evidence from one stable read; window-start,
operating-team and mail questions reuse that answer until any key field changes.
Runtime reads produce one immutable snapshot, and its PID records are the exact
objects referenced by its process, agent, transcript, pane, and session indexes.
A question that receives a process table carries that snapshot through every
helper instead of reading the default table partway through. Callers declare
optional `REGISTRY`, `SEATS`, and launch `PLACEMENT` work through `NEED` rather
than selecting among alternate runtime APIs. Diagnosis does not declare
`PLACEMENT`.

Callers also declare the computations they need. `Resolver(needs=...)` receives
hard requirements and a `maybe` set for computations reached on selected paths.
Loaders accept declared computations, and the end-of-run audit requires every
hard requirement to be used. A parameter can narrow a complete operation without
changing its evidence model. A caller that needs different facts asks a different
complete operation question; it does not publish an evidence helper as a second
public view. Evidence helpers such as `_spawn_parent_of` remain private.
`TranscriptRewriter` receives the selected parent and conversion input instead of
combining stamp and spawn evidence itself. Tests compare narrowed and complete
answers where they should be identical. Measure each computation directly so its
declaration matches its cost.
