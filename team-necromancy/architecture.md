# How teammates actually work

Companion to SKILL.md. Everything here was verified against the shipped
Claude Code 2.1.200 binary during one long night of spelunking (July 2026),
mostly by extracting the relevant functions from the bundle and then testing
the conclusions live. The leaked TypeScript tree is useful for orientation,
but it lags the binary by months; every load-bearing claim below was checked
in the binary. Where a claim rests on a specific build, assume it can drift.

## The three substrates

"Teammate" is not an execution model. It is a communication layer (roster +
file mailboxes + idle protocol) that can be draped over three different ways
of running an agent:

| | plain subagent | in-process teammate | tmux/iterm2 teammate | bg job (hand-rolled) |
|---|---|---|---|---|
| process | leader's | leader's | own, child of tmux server | own, child of daemon's pty host |
| transcript | sidechain (`agent-<id>.jsonl`) | sidechain | **main session JSONL** | main session JSONL, **but turns never flush** |
| survives leader exit | no | no | no (reaped) | yes (reaper skips it) |
| talks via | tool result | mailbox + in-memory queue | mailbox | mailbox |
| colors | n/a | n/a | 256-cube, downsampled by the app | native truecolor |

`teammateMode` in 2.1.200 is an enum of `auto, tmux, iterm2, in-process`.
The bg substrate is not in it; we assembled it by hand from shipped parts
(`claude --bg` accepts the teammate identity flags and the mailbox does not
care who reads it). It arguably beats the tmux backend on every axis except
transcript flushing, and would make a fine fifth mode.

A subagent can never get a pane: the Agent tool without `name` runs a query
loop inside the leader, and its sidechain transcript is not a session, so no
CLI could adopt it. The SendMessage resurrection path converts a dead tmux
teammate into an in-process one for the same reason in reverse: replaying a
JSONL into an internal query loop is cheap, booting a CLI around a sidechain
is not built.

## Transcripts are stamped by teammates only

Every JSONL line records `sessionKind`, and teammate processes additionally
stamp `agentName` and `teamName` on each line. Leader sessions stamp nothing.
Consequences:

- The /resume picker takes its metadata from the **first line** of the file.
  A session born as a bg fork wears the `bg` badge forever, even after being
  resumed interactively (behavior follows the live process's env, not the
  badge). `claude -c` skips any session whose first line carries a
  non-interactive kind. Forking a session clears the stamps on the copied
  lines, so the fork sheds the badge.
- Reconnection (the boot-time module that answers "am I in a team, and as
  whom?") has two triggers: identity flags on the CLI, or stamps in the
  resumed transcript. Leaders have neither: the flag triple forces teammate
  classification, and their transcripts are unstamped. That is the structural
  reason a dead leader cannot be resumed *as* a leader, and why the masquerade
  goes through the teammate branch.

## The implicit team and its death warrant

A leader mints its team lazily, named `session-<its internal live session
id>`. That internal id is not the transcript filename and changes across
resumes, so the team name cannot be predicted before boot (learned the hard
way; discover it post hoc from which team file a spawn just wrote, or skip the
problem entirely with the masquerade, which sets the team name by flag).

Registration happens at session startup: the implicit-team initializer
(`initializeSessionTeam`) ends with `registerTeamForSessionCleanup`, adding
the team to an in-memory set. The whole branch is gated by `!agentId`, so any
flag-bound launch (teammates, the masquerade) never registers. On graceful
leader exit, `cleanupSessionTeams` walks the set in two passes: the reaper
kills every member whose `backendType` is a pane type (in-process members
carry `backendType`/`tmuxPaneId` of `"in-process"` and are skipped), then the
undertaker removes member worktrees and `rm -rf`s the whole team directory.
The log line calls them "orphan team dir(s)": to the harness, a team without
its leader process is garbage by definition. (Verified in the 2.1.202 bundle;
the original 2.1.200 analysis attributed registration to TeamCreate, which
was close but one level off.)

So teams die with their leader **on purpose**, but only on the graceful path.
A crashed or SIGKILLed leader leaves everything standing. Bg-substrate members
have no pane id, so the reaper skips them even on graceful exit. And a
masqueraded lead never registered the cleanup, so its exit disbands nothing.

## The mailbox and the roster

All cross-process team state rides the filesystem, because there is no other
channel between the leader and an out-of-process teammate:

- `~/.claude/teams/<team>/config.json` is the roster. `SendMessage` resolves
  recipients from the in-memory teammates map and agent registry first, then
  falls back to reading this file at send time; that fallback is why
  hand-injected members are tool-addressable immediately. The reverse
  direction does not exist: nothing imports the roster file into leader
  memory, so file-only members never appear in mention completion, colors,
  or the tasks pill. Spawn and shutdown paths also read-modify-write it.
  Every spawn backend writes a roster entry through the same locked
  reservation helper (`updateTeamFile`, lockfile at `config.json.lock`):
  in-process teammates included, written twice (a bare reservation, then a
  patch setting `tmuxPaneId` and `backendType` to `"in-process"`). The exit
  reaper skips them (its filter wants a pane-type backend), but the
  undertaker's `rm -rf` of the team dir takes their registration with it, so
  after a graceful leader exit an in-process teammate's name resolves
  nowhere and only its transcript remains.
- `~/.claude/teams/<team>/inboxes/<name>.json` is a JSON array of messages
  (`{id, from, text, timestamp, status}`). It must contain `[]` when empty;
  a zero-byte file is a parse error that wedges both reader and writer.
- Teammates resolve `team-lead` through their *own* recorded team name. If
  that team is dead, the mail lands in a dir nobody reads. `@main` bypasses
  teams entirely (background-agent channel to the parent session), which makes
  it the reliable escape hatch for agents grafted across team generations.

The leader's in-memory picture (`teamContext` in AppState) is a different
animal: an event-sourced projection updated only by the leader's own actions
plus death announcements arriving in the mail. Nothing ever reconciles it
against the roster file, and rendering reads only the memory. Hence every
observed divergence: chips keep stale colors, Ctrl+C'd teammates stay
"active", counters for tmux teammates tick until a formal goodbye, because
turn-state and death only reach the leader if the teammate announces them.
One sentence covers all of it: actions consult the file, pixels consult a
snapshot.

Binding matters as much as the file: a leader whose `teamContext` is unset
cannot resolve any name, no matter what the roster says. Real TeamCreate
(first teammate spawn) or identity flags at boot are the only binders.

## Session id vs transcript id

The pid files in `~/.claude/sessions/` give each live process's current
internal session id. That id diverges from the transcript filename across
resume and clear cycles, and team names are minted from the internal id.
When something team-related does not add up, check both ids before reasoning.

## Adopt, in one paragraph

Adopt is the orphaned-work recovery system: on exit or backgrounding, running
shells are detached with their pid and start-time recorded, and an
`adopt.json` checkpoint describes everything the session owned (shells,
agents, workflows, cron). A later resume claims the checkpoint by atomic
rename, re-attaches still-running shells after verifying the pid is really
the same process, and restarts agents that have no completion record from
their saved transcripts. `CLAUDE_DISABLE_ADOPT=1` turns all of it off.
Teammates are not in adopt's inventory, which is the missing piece that would
make team resume a real feature instead of this skill.
