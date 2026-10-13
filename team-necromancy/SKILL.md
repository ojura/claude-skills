---
name: team-necromancy
description: Resume dead Claude Code teammates and whole teams with their transcripts intact. Covers tmux panes, supervised background jobs with claude --bg --resume, and whole-team recovery by resuming the leader under the team-lead identity. Use when the user wants to resume a teammate, agent, swarm, or team after its process has died, or move a teammate between tmux, in-process, and bg execution modes.
---

# Team necromancy

SendMessage resumes a dead teammate in-process inside the leader. This skill
also covers pane recovery, supervised bg recovery, and whole-team recovery.
The procedures describe Claude Code 2.1.200 behavior; `architecture.md`
explains the machinery and `seams-and-bugs.md` records known defects.

## `agent-resume`

`agent-resume` automates pane and whole-team recovery. Resolver combines
identity from the roster, the agent transcript, and the lead transcript's spawn
records. Depending on the selector, it can also use a live process identity,
a promotion receipt, the dead lead's spawn records, and the lead-session lookup
for a missing parent. Dated observations use newest-first precedence, while an
explicit complete identity or promotion receipt intentionally assigns identity
until another explicit move replaces it. Spawn records rank below dated evidence;
a model name carrying a bracketed variant ranks above a newer plain name. The
session registry identifies the tmux server.
The tool rebuilds a missing team directory and launches the resumed transcript
with identity flags, team environment variables, and initialized inboxes.

### Resolver decisions

Each command selects its transcript, identity, roster entry, process facts, and
tmux destination once. Before it signals a process, writes a roster, moves a
transcript, archives mail, archives roster and inbox bytes, removes tool-managed
files, or launches a pane, it checks the recorded state again. A changed roster,
transcript, mailbox, or process identity stops the command and requires a new
selection.

`architecture.md` defines the Resolver records, tmux placement rules, title
provenance, public methods, caller restrictions, and generation checks.

Resolver selects transcripts in this order:

1. an explicit session UUID, or a session title (below)
2. a live process with a matching agent ID, using its `--resume` value
3. the roster's `leadSessionId`
4. a promotion receipt assigning the requested member identity
5. matching `agentName` and `teamName` transcript stamps
6. an `agentName` stamp that identifies one roster member
7. for a lead, a transcript with spawn records that name the team

A bare name shared by several rosters remains ambiguous. This commonly affects
`team-lead` because every team uses that name.

Lead resolution needs spawn records because lead transcript lines carry session
identity alone. A fresh lead's implicit team records the transcript ID in
`leadSessionId`. A resumed lead creates its implicit team from a new internal
session ID, represented by the live process and team-directory name. Its spawn
records also name the team.
Resolver corroborates lead transcript candidates with a live process, a roster
entry, member names shared by roster and spawn records, or the team-name block. A
sole candidate remains usable for ordinary dead-team recovery and is reported as
uncorroborated.

Live process arguments outrank roster entries and transcript stamps because argv
describes the current process identity, while files describe identity at their
write time. Resolver checks the selected transcript against the selected launch
identity and stops on disagreement. Every run prints the selected evidence
as `via`. An already-correct pane returns as running; an occupied pane gets a new
pane; a dead tmux server is recreated. A stopped process counts as alive and the
tool recommends `kill -CONT`, preventing a second writer on one transcript.

```bash
agent-resume teammate                        # bare name, newest generation
agent-resume NAME@session-xxxx               # exact agent id
agent-resume <session-uuid>                  # exact transcript
agent-resume session-xxxx                    # whole team: members into panes, lead last
agent-resume session-xxxx --no-lead          # members only, lead already alive
agent-resume NAME --into-team live           # rebind onto the team the lead now runs as
agent-resume <lead-uuid> --into-team TEAM    # move the lead itself; other members stay on the source team
agent-resume NAME --stop                     # stop it, listing what it is working on first
agent-resume session-xxxx --stop             # stop the whole team
agent-resume --doctor session-xxxx            # team verdict, then lead-first member dossiers
agent-resume --doctor NAME@session-xxxx       # team verdict, requested member first, then lead
agent-resume --doctor                         # summary of teams with something live or waiting
```

`--doctor` uses the same selector resolution as resume. A direct team or agent
prints the ordinary team report followed by every member exactly once. It resolves
the addressed identity without inferring a launch destination. Resolver builds
one canonical member record per agent ID; the summary, team conclusions, and
dossiers reuse its exact runtime, transcript, parent, color, delivery, and ledger
facts without resolving them again. One ordered member list feeds both sections:
a requested member comes first, then the genuine lead; a team selector starts
with the lead; the rest follow stable roster order. The summary roster keeps its
PID, colored identity, and one-line title columns. Each
dossier repeats the identity color and adds explicit requested, membership, and
runtime labels. Its session UUID and title occupy separate rows. Lead identity,
operative team, polling, common delivery failure, and common remedies print once
in the team report. Dossiers retain the member's conversation, parent, seat,
recipient-specific ledger evidence, and member-specific actions. `--no-color`
changes no spacing. The team verdict remains the command's exit status. The
empty-hand sweep stays at team-summary level.

The selector may sit anywhere among the flags. The forwarded flags are an
allowlist that records which of them take a value, so a bare token is either
that value or the selector, independent of position; `--model x NAME`,
`NAME --model x` and `--stop NAME` are the same command. Two bare tokens are an
error naming both, since one would otherwise reach the resumed session as a
prompt the user did not mean to send. Put anything genuinely meant for claude
after `--`.

Team identity survives deletion of its directory in transcript stamps and lead
spawn records. The tool reconstructs the roster with the recorded members,
colors, and models. `leadSessionId` receives only corroborated evidence because
later resolution treats that field as exact. An uncorroborated lead remains
reachable through the lower-priority evidence and produces the same warning on
each run.

A bare name selects one transcript. When several distinct transcripts match,
the tool lists them and asks for a qualified selector. If several promotion
receipts assign one `name@team` to different conversations, it prints one full
UUID-and-title row per conversation and requires `--resume`. If the same UUID
exists in several project directories, it prints those rows and refuses rather
than choosing by file modification time.

`--resume` also takes a session title, as `claude --resume <title>` does, and
so does the bare selector when the token is not a session ID, an agent ID, a
team or an agent name, and does not have the form of an implicit team's name
(`session-` and eight hex digits). A session's title is its custom title, or
its AI title when it does not have a custom title, and a value matches it in
any letter case and without surrounding whitespace. The lookup covers each
project directory under `~/.claude/projects/`, where claude covers the
current project and its worktrees, and leaves out a transcript that starts
with a sidechain record. When several sessions have the title, the tool
prints one row per session, with its ID, project directory and modified
time, and refuses.

Three flags name destinations:

- `--into-team` selects the team used at launch.
- `--into-session` selects the parent embedded by demotion.
- `--as-agent` selects the identity used by promotion.

`--team` selects the set of agents operated on. Promotion derives the name from
the side transcript and the team from its current parent session. When identity
was inferred or defaulted and a final demote receipt from another team records an
exact inverse, promotion offers that inverse as `Y/n`; declining uses the current
parent's team. Explicit `--as-agent` suppresses the offer. Completion inserts
`name@`, then lists teams with the current parent's team first.

Promotion also makes known `origin.kind: human` persistence records visible.
The default keeps Claude Code's complete wrapper text. `--unwrap` instead stores
only the inner human text in the promoted record. Both variants record the edit
in the transcript's schema-version-1 conversion receipt, so demotion restores
the original side transcript bytes. `visible_human_wrapper_edits` stores UUID
intervals; unwrapped intervals reference the original wrapper payloads needed for
the inverse, while complete-wrapper intervals copy no payload text. When
promotion applies the exact inverse recorded by a carried demote receipt and
`--unwrap` is omitted, it preserves that receipt's recorded mode; an exact inverse
replays only the recorded intervals. `side_meta_b64` and `side_meta_mode` store the
original `.meta.json` bytes and file mode when promotion removes that sidecar, so
demotion
restores them exactly. Missing receipt fields and unsupported schema versions are
refused rather than inferred. Unknown wrappers and coordinator, peer,
task-notification, hook, image, and tool-result records remain unchanged. The
generated `last-prompt` uses the latest human message's inner text in either
variant.

A member promotion stamps its own root with the assigned name and team so plain
Claude Code resume restores member identity. The receipt records the root's prior
field presence and values for exact demotion. Lead promotion does not apply the
member-root edit. A fork-reference promotion keeps its copied parent prefix as
model context, but those UUIDs contribute neither transcript identity nor spawn,
lead, or roster-recovery actions.

Demotion derives its parent from the latest promotion receipt. When parent
inference reaches current-team evidence, the tool offers that team's lead
session as `Y/n`; Enter accepts, and an unattended run takes the default.
`--into-session` overrides this choice. Completion lists the receipt parent first,
current-team sessions next, and remaining sessions last.

`--into-team TEAM|live` moves an agent to the team used by a live lead. A plain
`claude --resume` gives the lead a new implicit team, while existing members
remain registered under the previous team. Rebinding relaunches the member with
the destination identity, stops any process still holding the transcript,
registers the destination roster entry, and marks the source entry inactive.
The source row and its original metadata are preserved with `isActive` set to
`false`; the destination row becomes the current assignment. The inactive source
row does not enter ordinary selection, completion, or team-run population for
the source team.

`live` derives the destination from spawn records written since the current
process started. A lead resumed several times writes the records of several
agent instances, one process after another, into one transcript, so process
start time excludes records for earlier teams. Before the current agent instance
writes its first spawn, Resolver can corroborate an eagerly created team by
matching team `createdAt` with live-session `startedAt`.

It composes with `--team`:

```bash
agent-resume session-<previous-team> --into-team live   # redirect the whole roster
```

Rebinding has two operations:

- **redirect** selects identity flags for a pending launch and updates roster
  entries.
- **migrate** stops and restarts a live member under the destination identity.
  Identity is fixed in argv at exec, and stopping the holder first preserves one
  writer per transcript.

`--into-team TEAM` migrates a lead. The command asks before stopping the process
holding that conversation; `--yes` accepts in advance. It archives the source
team's roster and inbox bytes into a receipt, removes tool-managed files from the
source team directory, preserves unrecognized entries, and removes the directory
only when empty. It then execs the transcript as the destination lead. With
`--force`, it restamps the source team's identity fields in the transcript and
leaves that team's directory and processes in place. Other members keep their
existing identities, so the report lists them, their waiting mail, and the
per-member `--into-team live` command.

The team component in an agent ID addresses an identity. `--into-team` selects a
destination. When transcript stamps name another team, the command points to
`--into-team TEAM`; `--force` permits a split identity. An unstamped conversation
uses the plain lead launch; source-team archival applies to stamped source teams.

Dead members redirect automatically when exact registry evidence, or spawn
evidence written by the lead's current agent instance, identifies a live lead
under another team. `--keep-team` preserves the recorded team. Live members keep
their in-flight turn and receive a migration hint. Automatic redirect requires
exact registry evidence or spawn evidence from the current agent instance;
timing corroboration remains advisory.

`--keep-team` also supports whole-team recovery in stages: members first resume
under the recorded or reconstructed team, then the lead resumes into that team.
During the member-first stage, SendMessage calls use the lead's current team and
route there. `@main` serves background subagents; pane teammates
use the team mailbox.

The automation launches tmux panes and warns when `teammateMode` selects another
execution mode. Recipe 2 covers explicit bg recovery.

Lead recovery executes in the current terminal because the resumed lead is the
interactive session the user drives. Under `--team`, `--dry-run`, or a non-tty,
the tool prints the command. A pane-backed lead would be selected by the next
lead's exit cleanup. `--into-team` migrates it; the team component of an agent ID
only addresses it. Members launch first because mailbox directories accept
messages before the lead process starts.

`--dry-run` is read-only and previews every verb, including `--stop`, `--install`,
and `--poison`. `--explain` reports the source of each launch fact. `--selftest`
runs read-only invariant checks suitable for a hook, while `--selftest --live`
adds checks that create temporary processes, terminals, and directories and
then remove them. Resumed-session flags use an allowlist, so misspellings are
reported by `agent-resume`.

After launch, the tool attaches from an available user terminal outside tmux
when it becomes the watching client. `--attach` and `--no-attach` select
the behavior explicitly. From inside the target pane, it replaces that pane's
process instead of splitting another one.

Pane launches apply the harness tint: three pane options, a bold titled border,
and roster-color mapping to tmux color names. `--no-color` disables pane and
completion colors. `--force` permits liveness and identity conflicts, including
resuming a transcript under a different agent or team, and reports each
conflict.

`agent-resume --install` links the script into `~/.local/bin`, installs
`_agent-resume` on zsh's fpath, clears a stale completion dump, and adds an async
SessionStart hook running `agent-resume --cleanup`. It uses the oh-my-zsh custom
completion directory when present; otherwise it uses `~/.zsh/completions` and
prints the required fpath line. Repeated installation updates its own files and
preserves unrelated files.

Completion caches each emitter briefly in memory and on disk. This zstyle runs
the uncached path while leaving cache files intact:

```zsh
zstyle ':completion:*:*:agent-resume:*' use-cache false
```

Unset the style or set it to `true` to enable caching.

### Cleanup hook

The cleanup hook removes dead `claude-swarm-*` sockets and empty team
directories. Socket cleanup uses a `connect()` probe and gives new sockets 60
seconds to finish binding. A socket the probe cannot classify, such as one with
a path of 108 bytes or more or a server that does not take the connection, is
kept, and a launch that needs its server refuses with the reason.
Team-directory removal requires every condition below:

- process evidence classifies the team as stopped
- Resolver's transcript search returns an empty result for the lead and every
  member
- the directory contains a parseable roster and JSON inbox arrays, with every
  message in them delivered
- its roster lock permits removal; a live owner, an unreadable owner record, or a
  `proper-lockfile` directory preserves the team
- the directory is older than 300 seconds, covering the interval between team
  creation and session discovery

Cleanup repeats these checks immediately before deletion and preserves entries
that appeared between the checks. It removes `config.json` last so an interrupted
run remains eligible for the next cleanup. Deletion is confined to `teams/` and
`tasks/`.

For a retained team, the hook removes a regular-file `config.json.lock` when its
owner record belongs to this tool and names a stopped process. Claude Code uses
a lock directory and clears it with `rmdir`; that operation returns `ENOTDIR` on
the regular file. Its roster updates then retain their previous state, and
teammate spawn remains blocked. Unreadable lock records, lock directories, and
live owners preserve the lock. `agent-resume --cleanup --dry-run` lists every
decision and reason.

Completion candidates and flag specifications come from the script's hidden
`--complete <topic>` emitters. `_agent-resume` formats those answers for zsh, so
parser flags and completion flags share one definition.

For Recipe 1, `agent-resume --poison` and `--unpoison` set and clear a
server-level `ANTHROPIC_BASE_URL` value on the selected tmux server. A launch
removes that value only when it matches the tool's poison, preserving a base URL
set by the user. Manual recipes cover custom team configs, bg execution, and
argv replacement.

A lead launched under teammate identity follows the teammate code path and can
message existing members. A running lead's mention completion and UI use its
in-memory roster; hand-registered members remain addressable through
SendMessage once `teamContext` is initialized. Recipe 1b describes this limit.
Finish recovery with the memory check under “Verify the resurrection.”

Recovery depends on the teammate transcript. A tmux or bg teammate is a full
`claude` process, and its conversation is a main-session JSONL under the project
directory for its cwd. Team configs, panes, tmux servers, and job directories
are runtime state that can be reconstructed around that transcript. The JSONL
allows the agent to resume in any supported execution mode with its memory
intact.

## Find the transcript

Teammate transcripts live in `~/.claude/projects/<flattened-cwd>/` (teammates
usually run in `/tmp` or the leader's cwd). Every line carries the identity:

```bash
for f in ~/.claude/projects/-tmp/*.jsonl; do
  head -c 4000 "$f" | grep -aoE '"agentName":"[^"]*"|"teamName":"[^"]*"' | sort -u | tr '\n' ' ' | sed "s|^|$(basename $f) |"
  echo
done
```

One name can have several generations because each respawn creates a session
file. Choose by transcript content as well as mtime. Resume appends to the chosen
file and keeps that conversation in one lineage.

Transcript location distinguishes full CLI teammates from in-process agents.
A main-session JSONL in `~/.claude/projects/<flattened-cwd>/` belongs to a full
CLI process and works with every recipe here. An `agent-*.jsonl` under the lead
session's `subagents/` directory belongs to an in-process query loop. Backend
labels can disagree with this distinction: `TaskStop` may report
`task_type: "in_process_teammate"`, and a roster may report
`backendType: "in-process"`, for a full CLI process.

The flattened project path follows the process cwd. An agent launched from the
lead's scratchpad therefore appears under that scratchpad's flattened path. A
measured 205-line main transcript carrying the `in_process_teammate` task label
lived under `projects/-tmp-claude-1000--tmp-<session>-scratchpad/`. Search all of
`~/.claude/projects/` for the `agentName` stamp.

## Choose the team

Every `<team>` is a directory name under `~/.claude/teams/`.

- With a live lead, use its current team. Its latest spawn record or team config
  provides the name. Implicit team names use the lead's internal live session
  ID. A fresh lead shares that ID with its transcript; a resumed lead receives a
  new internal ID.
- For whole-team recovery with a stopped lead, choose a team name and create its
  config. Recipe 3 binds the resumed lead to that name through identity flags.

## Rebuild a team config

`agent-resume` rebuilds a missing team directory automatically. Member recovery
creates the lead and selected member entries. Team or lead recovery reconstructs
the full roster from lead spawn records. Manual construction is useful for a
custom team name.

Colors and bracketed model variants live in the roster and in the lead's spawn
records, so reconstruction preserves them when the lead transcript is
available. `leadSessionId` receives only evidence that Resolver treats as exact.
An uncorroborated lead remains reachable through spawn-record evidence and is
reported as such on each run. This prevents a roster reconstructed from one
transcript's evidence from corroborating that same transcript.

The complete team config follows. Every field is read by the harness or this tool:

```bash
mkdir -p ~/.claude/teams/<team>/inboxes
cat > ~/.claude/teams/<team>/config.json <<EOF
{
  "name": "<team>",
  "createdAt": $(date +%s%3N),
  "leadAgentId": "team-lead@<team>",
  "leadSessionId": "<leader session id, or the transcript id you will resume>",
  "members": [
    {"agentId": "team-lead@<team>", "name": "team-lead", "agentType": "team-lead",
     "joinedAt": $(date +%s%3N), "tmuxPaneId": "leader", "cwd": "$HOME",
     "subscriptions": [], "backendType": "in-process"},
    {"agentId": "NAME@<team>", "name": "NAME", "color": "blue",
     "joinedAt": $(date +%s%3N), "tmuxPaneId": "", "subscriptions": [],
     "agentType": "claude", "planModeRequired": false, "cwd": "/tmp",
     "backendType": "in-process", "isActive": true}
  ]
}
EOF
echo '[]' > ~/.claude/teams/<team>/inboxes/team-lead.json
echo '[]' > ~/.claude/teams/<team>/inboxes/NAME.json
```

Inbox files must contain `[]`. A zero-byte file is invalid JSON and wedges
the mailbox code on both ends.

This manual template uses `backendType: "in-process"`. `agent-resume` writes
`backendType: "tmux"` with the actual pane and cwd because it launches a pane
process immediately.

## Recipe 1: resume into a tmux pane with spawn-then-swap

Use this recipe when the teammate should appear in the lead's in-memory team
view. A placeholder Agent spawn creates the roster entry, pane, tint, mailbox,
and runtime registration. Replacing the process inside that pane preserves the
pane ID referenced by those records.

Block the placeholder's API request because registration completes at spawn
time and the replacement needs only its pane ID and argv. An API turn would
write the shared prompt prefix to cache and can start the organization's billing
window. New panes inherit tmux server environment, while the spawn command
re-exports only its embedded variables. A server-level `ANTHROPIC_BASE_URL`
therefore blocks the placeholder before the request reaches the API.

0. Set the server poison so the placeholder gets a TCP connection error before
   billing:

```bash
agent-resume --poison            # newest live swarm socket; or --socket claude-swarm-<pid>
# equivalent by hand:
SOCK=$(ls -t /tmp/tmux-1000/claude-swarm-* | head -1)
tmux -S $SOCK set-environment -g ANTHROPIC_BASE_URL http://127.0.0.1:9
```

   The first teammate spawn creates the swarm socket. On a new server, that
   initial spawn incurs one stub turn before the socket can carry the poison.

1. Spawn a placeholder teammate under the dead teammate's name with `name:` set
   and `model: haiku`. Haiku limits the fallback cost if the network block misses.
   Continue as soon as the team, roster entry, pane, tint, and mailbox exist.
2. Clear the poison before replacement so the resumed process and later spawns
   receive the normal API configuration. The placeholder keeps its launch-time
   environment until replacement:

```bash
agent-resume --unpoison          # or: tmux -S $SOCK set-environment -gu ANTHROPIC_BASE_URL
```

3. Read the pane id from the team config, and the exact argv from the
   placeholder process:

```bash
CFG=~/.claude/teams/<team>/config.json
PANE=$(python3 -c "import json;print([m['tmuxPaneId'] for m in json.load(open('$CFG'))['members'] if m['name']=='NAME'][0])")
PID=$(tmux -S $SOCK list-panes -a -F '#{pane_id} #{pane_pid}' | awk -v p="$PANE" '$1==p{print $2}')
ARGS=$(ps -o args --no-headers -p $PID)
```

4. Replace the process in the same pane and resume the selected transcript, with an
   explicit scrub in case the pane still carries the poison:

```bash
tmux -S $SOCK respawn-pane -k -c /tmp -t "$PANE" \
  "env -u ANTHROPIC_BASE_URL CLAUDECODE=1 CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1 CLAUDE_CODE_ENTRYPOINT=cli $ARGS --resume <session-id>"
```

Validation covers the environment propagation used by steps 0, 2, and
`env -u`: spawn commands embed selected variables and panes inherit the server
environment. Haiku limits a missed poison to one inexpensive turn.

`-c` selects the project directory containing the transcript. The replacement
argv can also change `--agent-color`, `--model`, `--effort`, and
`--permission-mode`.

Costs and caveats:
- The placeholder leaves a small session file containing one failed turn and
  zero billed tokens when the poison works.
- Abrupt process replacement leaves the placeholder's runtime entry in the lead
  because death announcements run during graceful exit.
- The lead's in-memory color and roster state keep the placeholder values.
  Roster edits affect actions; the current UI follows memory.

## Recipe 1b: resume into a tmux pane by roster registration

Use this recipe when roster-backed messaging is enough. Create a pane running
the resumed process and add a roster entry that names its pane. It avoids the
placeholder request and process replacement.

```bash
SOCK=$(ls -t /tmp/tmux-1000/claude-swarm-* | head -1)
PANE=$(tmux -S $SOCK split-window -d -P -F '#{pane_id}' -c /tmp \
  "env CLAUDECODE=1 CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1 CLAUDE_CODE_ENTRYPOINT=cli \
   claude --resume <session-id> --agent-id NAME@<team> --agent-name NAME \
   --team-name <team> --agent-color blue --agent-type claude \
   --dangerously-skip-permissions")
python3 - "$PANE" <<'PY'
import json,sys,time
p='/home/juraj/.claude/teams/<team>/config.json'
cfg=json.load(open(p))
cfg['members'].append({"agentId":"NAME@<team>","name":"NAME","color":"blue",
 "joinedAt":int(time.time()*1000),"tmuxPaneId":sys.argv[1],"subscriptions":[],
 "agentType":"claude","planModeRequired":False,"cwd":"/tmp",
 "backendType":"tmux","isActive":True})
json.dump(cfg,open(p,'w'),indent=2)
PY
echo '[]' > ~/.claude/teams/<team>/inboxes/NAME.json
```

This pane process flushes transcript turns and participates in graceful
pane-member cleanup through `backendType: "tmux"` and its pane ID. The lead's
runtime memory contains Agent-tool spawns, while this recipe writes only the
roster. The tasks pill, swarm view, colors, and `@name` completion therefore
show spawned members.

SendMessage resolves through the in-memory map, agent registry, and finally the
roster file. The roster lookup requires an initialized `teamContext`, which a
real teammate spawn creates for the lead's current agent instance. After that
initialization, hand-registered members resolve by bare name. Before it, drive
the pane with keystrokes and read replies from inbox files. Apply the tint
explicitly for the same visual identity as an Agent-tool spawn:

```bash
C=blue  # tmux color; map roster purple->magenta, orange->colour208,
        # pink->colour205; red/blue/green/yellow/cyan pass through (2.1.202 map)
tmux -S $SOCK set-option -p -t "$PANE" window-style "bg=default,fg=$C"
tmux -S $SOCK set-option -p -t "$PANE" pane-border-style "fg=$C"
tmux -S $SOCK set-option -p -t "$PANE" pane-active-border-style "fg=$C"
tmux -S $SOCK select-pane -t "$PANE" -T NAME
tmux -S $SOCK set-option -p -t "$PANE" pane-border-format "#[fg=$C,bold] #{pane_title} #[default]"
```

The harness tint consists of these three pane styles and the bold colored title.

Validation covers roster registration, identity-flag resume, inline pane
environment, and Resolver precedence. Finish with the memory check below.

## Recipe 2: resume as a background job

Check `teammateMode` in `~/.claude/settings.json` first. A `tmux` setting selects
Recipe 1 or 1b. Use bg when the user explicitly requests it. Bg provides native
truecolor, daemon supervision, `claude agents`, `attach`, `logs`, `stop`, and
survival across lead exit. Transcript turns remain in process memory, so bg is
appropriate for stateless work or work that writes its result elsewhere.

```bash
cd /tmp && claude --bg --resume <session-id> \
  --agent-id NAME@<team> --agent-name NAME --team-name <team> \
  --agent-color blue --agent-type claude \
  --dangerously-skip-permissions --effort low --model <model>
```

The bg process watches the selected team inbox. Add its roster entry manually so
team tools can address it:

```bash
python3 - <<'PY'
import json,time
p='/home/juraj/.claude/teams/<team>/config.json'
cfg=json.load(open(p))
cfg['members'].append({"agentId":"NAME@<team>","name":"NAME","color":"blue",
 "joinedAt":int(time.time()*1000),"tmuxPaneId":"","subscriptions":[],
 "agentType":"claude","planModeRequired":False,"cwd":"/tmp",
 "backendType":"in-process","isActive":True})
json.dump(cfg,open(p,'w'),indent=2)
PY
```

Inbox files must contain `[]`. An empty file is invalid JSON and wedges the
mailbox code on both ends:

```bash
echo '[]' > ~/.claude/teams/<team>/inboxes/NAME.json
```

**Bg turns remain in process memory.** Pane and interactive processes append
their turns to the transcript. A bg worker should write important results to a
durable file before exit.

## Recipe 3: resume a whole team through the lead identity

Lead transcript lines carry session identity, while teammate lines also carry
`agentName` and `teamName`. The CLI binds team identity from the complete
`--agent-id`, `--agent-name`, and `--team-name` triple. Resume the lead through
that teammate-identity path using `team-lead@<team>`:

1. Create the team config and leader inbox when cleanup has removed them:

```bash
mkdir -p ~/.claude/teams/<team>/inboxes
# config.json with "leadAgentId": "team-lead@<team>" and your member list
echo '[]' > ~/.claude/teams/<team>/inboxes/team-lead.json
```

2. Resume the dead leader session with explicit team-lead identity flags:

```bash
env CLAUDECODE=1 CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1 CLAUDE_CODE_ENTRYPOINT=cli \
  claude --resume <dead-main-session-id> \
  --agent-id team-lead@<team> --agent-name team-lead --team-name <team>
```

`isAgentSwarmsEnabled` activates identity-flag binding in 2.1.220 and 2.1.226.
The environment prefix makes that activation explicit for interactive and tmux
launches. A tmux pane started with an explicit command runs that command
directly, so the prefix supplies its team environment.

The resumed process takes the `leadAgentId` identity, reads the selected roster,
watches the lead inbox, and can SendMessage each member immediately.

3. Resume members with Recipe 1 or 1b. Use Recipe 2 for an explicitly requested
   bg process. Point every member's `--team-name` at this team.

The teammate code path rejects nested teammate spawns, so additional members
must be launched externally. Identity-bound launches use the reconnection path;
plain-lead startup owns exit-cleanup registration. The team directory and member
processes therefore remain after this lead exits.

`--agent-id` requires a nonempty value. Implicit team names use the process's
internal live session ID, which equals the transcript ID for a new agent and
changes on resume. Read or set the team name explicitly.

## Driving and observing resurrected agents

- SendMessage resolves a bare name through lead memory, the agent registry, and
  the roster file. These paths require an initialized `teamContext`. The `@name`
  prompt path, mention completion, colors, and tasks pill use the in-memory
  roster populated by Agent-tool spawns.
- To drive an interactive pane by keystrokes, send the text and Enter as two
  `send-keys` calls with a short delay. One call with trailing Enter pastes a
  newline into the input.
- `capture-pane -p` renders the prompt's dim autosuggestion as input text.
  Re-capture with `-e` and check for the dim SGR code (`\x1b[2m`) before
  attributing that text to a user.
- The roster and lead task list reflect announced lifecycle events. Pane,
  process, and job state provide current liveness for abrupt exits such as
  Ctrl+C.

## Verify the resurrection

Finish with a memory check: SendMessage a question about a specific fact in the
resumed history and confirm the reply arrives through the mailbox. This round
trip verifies transcript loading, identity binding, roster resolution, and both
inbox directions.
