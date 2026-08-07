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

Because that is a deliberate way to run a member rather than a fault,
`agent-resume` states a live member that is in no tmux pane and never demotes a
verdict for it: the doctor adds a warning line naming the member and what
changes operationally, and `--standing` prints its seat. A verdict answers only
whether a live lead reads the team, so demoting one for an unseated member would
report a working arrangement as broken.

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
  that team is dead, the mail lands in a dir nobody reads, and there is no
  escape hatch: `@main` is the background-agent channel to the parent session
  and is documented as background subagents ONLY, so a pane-backed teammate
  cannot use it. An agent grafted across team generations is mute until its
  recorded team name matches a team the lead is actually running as.

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

The pid files in `~/.claude/sessions/` record the session id the process was
**resumed from**, i.e. its transcript filename, NOT its current internal id
(verified live on 2.1.220: a lead resumed from a52cb2af... shows sessionId
a52cb2af... in the registry while running internally as 1d040460..., proven by
the team it minted). Team names come from the internal id, so after a resume no
file on disk maps team name to pid, and the join has to be temporal: the team
dir is created a few hundred ms before the session file (measured 281, 252 and
402ms). Before the first resume the two ids are the same, so a fresh lead's
team name does match a registry sessionId and an exact join is available.
When something team-related does not add up, check both ids before reasoning.

## Wanted from the harness: a record of the id a life minted

One structural record would close the gap above: the internal session id a
boot minted, written where a later reader can find it. Either shape works, a
line in the boot's own transcript (which already carries identity stamps) or
the internal id alongside the resumed-transcript id in
`~/.claude/sessions/<pid>.json`. Nothing else records it, so today it exists
only in the running process and in the name of the team directory.

Without it, "which team is this live process running as" has two exact rungs
and a guess. The exact ones are `--team-name` in argv, which only a
flag-bound agent has, and a spawn payload written by this life, which needs
the lead to have spawned someone since it resumed and needs a process start
time to tell this life's payloads from an earlier life's. Everything else
falls to pairing the team's `createdAt` against a live process's `startedAt`.

That pairing is weaker than a tolerance suggests, because the mint is not one
event. Minted eagerly at boot it lands a few hundred ms before the session
file (measured 281, 252 and 402ms), but a team can instead be minted at the
first spawn, minutes later, which no window centred on boot covers at all. So
the join does not merely lose precision on the second shape, it does not
apply, and a pairing that survives the window still has to be shown unique in
both directions before it means anything: several processes can start inside
one team's window, and one process can start inside several teams' windows.

Downstream this is why `agent-resume` will not rebind a member onto a team it
inferred from timing without asking.

This tool tried to keep the record for itself and failed, which is worth
recording so nobody tries the same way twice. `SessionStart` is handed a
`session_id`, and a hook wrote it down each boot. That payload carries the
id of the transcript, not the id the team is named after: measured against
the live processes on one machine, the resulting ledger claimed teams that
had never existed for seven of eight resumed sessions, and it outranked the
timing join, so it answered confidently and wrongly exactly where the guess
was right. The ledger is deleted. Deriving the mint from the session id is
the same mistake in another spelling, and is not there either: for a fresh
boot the minted id is the transcript filename, but for a resumed life it is
generated at that boot and appears nowhere on disk (4 of 23 team
directories here name such an id).

What is provable from disk bounds the guess, which was the missing half.
A team records its minting boot's session id as `leadSessionId`. For a fresh
boot that id is also the transcript's filename, so the team is derivable and
confirmable and needs no window at all. For a resumed life the id names no
file, which is a signature only a resumed life leaves. So a resumed process
could only have minted a team of that second shape (4 of 23 here), and every
fresh-boot team is provably somebody else's however the timing falls.

That leaves three grades rather than two: exact for a fresh boot, corroborated
for a resumed life uniquely paired, both directions, with a resumed-shape team,
and nothing at all when neither holds. Corroborated is offered rather than
taken, and the offer defaults to yes. The residual is coverage, not ambiguity:
a team minted at the first spawn falls outside any window centred on boot, so
it never enters the pool to be chosen between. A harness-side record is what
would close that, and it is still the ask:
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

## The engine owns the environment

The environment this tool reasons about is eight realms:

- transcripts, both main sessions and the side transcripts under `subagents/`
- team configs, the rosters at `teams/<team>/config.json`
- mailboxes, the message files at `teams/<team>/inboxes/<name>.json`
- claude sessions
- running processes
- tmux servers, sessions and panes
- settings, `~/.claude/settings.json`
- the claude binary, both which one a launch uses and the pattern that decides
  whether a process on the table is a claude at all

Only the engine may examine a realm and draw a conclusion from what it finds.
Everything else asks the engine a question and consumes the answer.

Mailboxes are their own realm rather than part of a team's config, because they
answer a different question and answer it worse: the file is pruned the moment a
busy recipient takes a message into memory, so an empty mailbox is not evidence
that nothing was sent. Reading one and concluding is exactly where this tool has
already been wrong, which is why `mail_ledger` exists to answer from the
transcripts instead.

The tool's own receipts are deliberately not a realm. A realm is something the
world owns and this tool observes; a receipt is this tool's own writing. The
engine does read receipts and conclude membership from them, so the exclusion is
a decision rather than an oversight.

The rule is about reading and concluding, not about acting. A launch runs
`tmux split-window`, a reap removes a directory, a stop signals a pid: none of
those is an examination. What may not happen outside the engine is looking at a
realm and deciding from it what is true, because then two places answer the same
question and they drift apart. That drift is most of this file's bug list: a
report that judged mail from the inbox file while the ledger judged it from the
transcripts, a liveness gate that could not see its own caller, a redirect aimed
by a timing coincidence while the stamps said otherwise.

Enforcement is per realm, and every realm now has a check that fails by name.
Transcripts and sessions are held by `transcript-pick-engine-only`,
`tree-reached-via-engine` and their siblings: the tree's names cannot be loaded
outside the engine, and the picking happens in one place. Team configs and
mailboxes are held together by `team-dir-read-engine-only`, because both live in
one directory; it follows the engine's own path answers through local variables
and attribute chains, and fails on a filesystem read applied to one, with
`roster-read-one-place` still keeping the parse in a single reader. Settings are
held by `settings-read-engine-only`, which bars the file's name and the key's.
Processes are held by `proc-read-engine-only`, which bars `/proc` paths and
signal-0 kills, the two ways a second liveness test gets written. tmux is held
by two checks rather than one, because a server nobody can find and a seat
nobody confirmed are different bugs: `server-liveness-engine-only` bars an
AF_UNIX probe and the subcommands that answer whether a server, a session or a
client is there, and `pane-question-engine-only` bars the subcommands that
answer which panes exist and the environment variables that say which pane we
are in. The binary is held by `claude-pattern-engine-only`, which bars
`CLAUDE_PROC` outside the engine; that realm never had a violation, so the check
records a state rather than repairing one.

Every check stops where acting begins, and the line is worth naming because that
is what each of them is drawn around. `claude_binary` still chooses what a
launch runs. `launch` still picks its tmux subcommand, from a pane list the
engine handed it. A reap still stages a directory, re-proves it and unlinks it,
asking the engine for the verdict both times. An archive still copies a roster's
or a mailbox's bytes into a receipt before deleting them, and the installer
still reads `settings.json`, adds its hook and writes it back. An actor that
kills a process still checks it died, by asking the engine the same question
every other liveness answer comes from.

Each check is narrower than the rule above it, and the gaps are worth stating. A
team path handed to a helper as an argument and read there is not seen. A
matcher written out by hand rather than reached for by name is not seen. What
makes the narrow checks worth having is that every route this file actually has
runs through the engine's own answers, so a second reader would have to be
written oddly on purpose to avoid them.

## Method

Carried out of this file's construction rather than designed up front.
Prove the inverse before acting: a conversion computes its own undo in
memory and byte-compares against the source before any writer may touch
disk. And its process-level analogue: design before field contact, field
contact before consumers. A design that waits for specimens never ships;
one that reaches consumers before specimens ships its collapse into their
misdiagnoses. The middle is to ship with a field acceptance attached, so a
wrong vocabulary collapses on the first live team while the correction is
still cheap. It has paid twice: a mail ledger's two unmatched states became
five on first contact, and a promote's roster write dissolved into the
receipt being the membership.

A fourth, cheaper to state than to learn: a suite run is evidence only about
a tree nobody is editing. Python reads a file to execute it and again to
render a traceback, and this suite reads its own source for the structural
checks, so an edit landing between those reads produces one run whose
behavior and whose assertions disagree, and then vanishes. That is the
quiescent-specimen rule from the conversion battery, one level up: do not
write a file another writer owns, and do not trust a green from a tree in
motion.

A third rule earned its place the hard way: a refusal that names an unbuilt
remedy is making a claim about that remedy, and claims in refusal strings
deserve the same verification as claims in code. One refusal presented a
conversation-wiping graft as the durable fix for a whole day, because no
check runs on unbuilt designs; a single angry question did what the suite
could not.

A fifth, earned in one afternoon of readability work that kept striking
engine rot: a reporter renders engine answers and gathers nothing, and a
caller declares to the engine what it needs, so the engine resolves no more.
Both directions bite. A renderer reaching for a path helper has found an
engine gap, and the fix is a new engine answer (transcript_title), never a
widened exemption on the fence that keeps pickers out of the tree. And an
engine answer fatter than the declared need taxes every caller: the mail
ledger joined a whole team to render one agent's line, one typed side
classified nine hundred, the doctor swept a census only the sweep branch
reads, and the standing verdict walked the spawn corpus for a field its
caller discarded. Per-method toggles were the first cut and did not
survive review; the contract that did: a run declares its needs to the
constructor (Resolver(needs=...), Maybe(NEED.X) for a genuinely
conditional path), an undeclared compute refuses at the loader by name,
and a hard need never exercised refuses when the verb completes, so a fat
declaration dies as loudly as a missing one. Narrowing within a question
stays a question parameter (mail_ledger's to=), parentage became its own
question (spawn_parent_of) that only the verb printing it asks, and each
narrowing was proven answer-identical and checked by name. The cost of a
field is measured, never presumed cached: "approximately free" turned out
to mean "someone else usually pays" the one time it went unmeasured.
