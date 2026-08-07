---
name: team-necromancy
description: Resurrect dead Claude Code teammates and whole teams, transcripts intact. Covers reviving a teammate into a tmux pane (spawn-then-swap), reviving one as a supervised background job (claude --bg --resume), and resuming an entire dead team by resuming its leader session under the team-lead identity (the masquerade). Use when the user wants to bring back, revive, resurrect, or resume a teammate, agent, swarm, or team whose process has died, or to move a teammate between tmux / in-process / bg substrates.
---

# Team necromancy

The harness only offers one resurrection path: SendMessage to a dead teammate
brings it back in-process, inside the leader, with no pane and no independent
life. Everything else here is hand-rolled, verified end to end against
Claude Code 2.1.200 (see architecture.md for how the machinery works and
seams-and-bugs.md for what is broken around it).

## agent-resume: Recipes 1b and 3, scripted

`agent-resume` in this directory automates the
common cases: finding the soul, merging identity from three sources (the
roster, the agent's own transcript, and the lead transcript's record of
spawning it) with the most recent winning, except that spawn records always
lose ties and a bracketed model variant beats a plainer newer one, resolving
the swarm socket (the session registry feeds that, not identity), forging the
team dir if a graceful lead exit deleted it (see Forging below), and
relaunching into a pane with the identity flags, team env vars, and seeded
inboxes.

Finding the soul is one engine with a stated evidence ladder, not a pile of
fallbacks. Strongest first: a session uuid you typed; a live process wearing
that agent id, whose `--resume` value is what it is working on right now; the
roster's `leadSessionId`; both stamps (`agentName` AND `teamName` on the
transcript's own lines); `agentName` alone, refused when the name lives in more
than one roster, which on any box with several teams is exactly what
`team-lead` does; and, for a lead only, the transcript whose spawn payloads name
this team.

The last rung is there because a lead writes no stamps, and `leadSessionId`
sometimes names nothing resumable. A team minted on a fresh boot is named after
its lead's transcript and records it, so that lead resolves at the roster rung. A
lead already resumed when it minted the team carries the *internal* session id of
that later life instead, and that id has no file.

Neither shape is the default to reason from, and counting a machine will not
tell you which is: its rosters were written by the harness, by `agent-resume`,
and by the hand-forging recipes below, so a census of them measures what has been
done to that machine as much as what the harness does. What a lead leaves behind
either way is a spawn payload per teammate, each naming the team it was running
as. Several transcripts can claim
one team, so that rung corroborates before it answers, against a live process
running as the team, a roster naming the transcript, the payload names matching
the roster's members, or the team-name block; a sole uncorroborated claimant is
still taken, and says so, because the ordinary dead-team case is exactly that.

A live process outranks the roster and the stamps because argv is frozen at exec
and a migration is kill-and-restart: a roster entry or a stamp records what was
true when it was written, while argv records what is true. Whatever rung fires, the conclusion is
then checked against the identity about to be claimed, and a mismatch stops
the run: launching `--agent-id X` on Y's conversation is the worst thing this
tool could do. The chosen rung is printed as `via` on every run. It noops when the same agent already runs in its recorded pane,
splits a new pane when something else squats there, rebuilds a dead
socket/session, and treats a ctrl-z'd process as alive (nudges you to
`kill -CONT` instead of double-spawning).

```bash
agent-resume teammate                        # bare name, newest generation
agent-resume NAME@session-xxxx               # exact agent id
agent-resume <session-uuid>                  # exact transcript
agent-resume session-xxxx                    # whole team: members into panes, lead last
agent-resume session-xxxx --no-lead          # members only, lead already alive
agent-resume NAME --to-team live             # rebind onto the team the lead now runs as
agent-resume <lead-uuid> --to-team TEAM      # move the lead itself; its crew stays put
agent-resume NAME --stop                     # stop it, listing what it is working on first
agent-resume session-xxxx --stop             # stop the whole team
```

The selector may sit anywhere among the flags. The forwarded flags are an
allowlist that records which of them take a value, so a bare token is either
that value or the selector and position carries no meaning; `--model x NAME`,
`NAME --model x` and `--stop NAME` are the same command. Two bare tokens are an
error naming both, since one would otherwise reach the resumed session as a
prompt nobody typed. Put anything genuinely meant for claude after `--`.

A team whose directory a graceful lead exit removed is still a team: it is
recognised from the payloads or the stamps, and rebuilt from its lead's
transcript with the members, colours and models those payloads recorded. A
rebuild driven by an uncorroborated claimant leaves `leadSessionId` empty rather
than recording a guess in a field later read as fact, so the evidence is
re-derived, and re-warned about, on every resume until it genuinely improves.

A bare name is resolved only when it is unambiguous; if several rosters carry
it (every team has a `team-lead`) the tool lists the candidates and refuses
rather than picking one for you.

`--to-team TEAM|live` exists because a plain `claude --resume` boots the lead
into a **brand new implicit team**, orphaning the members registered under the
old one: the lead's SendMessage resolves against the team it is now running as,
so the old roster is unreachable. The rebind relaunches the member with the
destination team's identity, replaces whichever process still holds its
transcript (killing any holder it cannot reach in a pane), registers it in the
destination roster and retires the source entry. `live` finds the destination from the lead's own transcript: every teammate it
spawned records the team name it was running as, so the newest such record
**written since this process started** names the team it runs as now. The life
filter matters: a resumed lead keeps appending to one transcript, so an older
life's record would name a team it has already left. Where this life has
spawned nobody, the fallback is a time join of team `createdAt` against live
sessions' `startedAt`, which only covers teams minted at boot.

It composes with `--team`, which is the one-command answer to "I resumed my
lead and my team is orphaned":

```bash
agent-resume session-<old-team> --to-team live   # whole roster, redirected
```

Two operations hide behind the word rebind, and the tool separates them:

- **redirect** picks the identity flags for a launch that is happening anyway
  and writes the roster entries. It kills nothing.
- **migrate** changes a *live* member's team, which is necessarily kill and
  restart: identity is frozen in argv at exec, and two processes must never
  append to one transcript. That is what `--to-team`'s kills are, the mechanism
  rather than a policy.

A lead moves the same way, and `--to-team TEAM` on one is the same operation as
the masquerade spelling `agent-resume team-lead@TEAM --resume <uuid>`: whichever
you type, the run ends whoever is running that conversation (asking first,
`--yes` to skip), retires the team its window-start stamps name so the flags win
after the next restart, and execs it as TEAM's lead. Under `--force` against a
team something is still running as, it restamps those two fields instead and the
old team stays up. The lead moves alone: no member is restarted or re-stamped,
so the report names who stays behind, what mail waits for them there, and that
`--to-team live` on each member is what moves one.

So the default may redirect, and only `--to-team` may migrate. Reviving a dead
member while its lead is live under a different team redirects automatically,
saying so, and `--keep-team` opts out. A member that is still running is left
alone with a hint, because restarting it costs its in-flight turn. Redirect
also demands exact evidence (the registry hop, or a spawn payload from this
life); the mint-window join is a heuristic and never triggers it.

Without a redirect, reviving into an old roster still works and still lands on
the right socket, but the member is **mailbox-orphaned**: the live lead's
SendMessage resolves against the team it runs as now, so mail to that member
goes to an inbox directory nobody reads. There is no fallback channel: `@main`
is background-subagent only, so a pane-backed member has no way out. That is a
real workflow, not only an accident: Recipe 3 rebuilds members under the old or
forged team first and masquerades the lead into it afterwards, which is what
`--keep-team` is for.

This automation implements the tmux substrate only, and warns if your
`teammateMode` says otherwise; for a bg resurrection use Recipe 2 by hand.

Leads are never spawned into a pane. Selecting one builds the Recipe 3
masquerade command and execs it in your terminal (or prints it, under
`--team`, `--dry-run`, or a non-tty), because a masqueraded lead is an
interactive session you drive, and a pane-backed one would be reaped by the
next lead's exit. Naming a team it did not lead, either with `--to-team` or
inside the agent id, moves it there rather than launching it split between the
two (see the rebind section below). Members come up first: the mailbox is a directory, not a
process, so nobody has to wait for the lead.

`--dry-run` previews (it does not cover `--install`/`--poison`, which refuse
it), `--explain` shows per-field provenance, and `--selftest` checks the tool's own
invariants while changing nothing, which is what lets it run from a hook.
`--selftest --live` adds the checks that need a real process, a terminal or a
directory change; each undoes itself. Flags for the resumed session are an
allowlist rather than a passthrough, so a misspelling of one of ours is caught
instead of reaching claude as an unknown argument. After a
launch it attaches you to the swarm unless a Claude Code tool is driving, there
is no tty, you are already inside tmux, or somebody is watching it already;
`--attach` and `--no-attach` force either way. Run from inside the target pane,
it execs the agent there instead of splitting a new one.

It also tints the pane the way the harness tints its own (three pane options
plus the bold titled border, with the roster colour mapped to tmux's names), so
a revived member is not the one grey pane in a coloured swarm; only a real
spawn gets a tint otherwise. `--no-color` skips the tint and drops the colours
from completion output. `--force` overrides not only the liveness guards but the identity
ones: with it the tool will resume a transcript stamped for a different agent
or team, saying so loudly each time. It is the one flag that can produce the
outcome the tool exists to prevent.
`agent-resume --install` sets everything up on a new box: symlinks the script
into `~/.local/bin` and the `_agent-resume` zsh completion onto fpath
(oh-my-zsh `custom/completions` when present, `~/.zsh/completions` plus a
printed fpath line otherwise, clearing any stale compdump), and adds an async
SessionStart hook running `agent-resume --cleanup`. All of it is idempotent,
refuses to clobber foreign files, and updates an older hook of its own in place
rather than adding a second.

**That hook deletes things**, so its conditions are worth knowing. It reaps
`claude-swarm-*` socket files whose tmux server is gone (a connect() probe, no
tmux spawns, skipping sockets younger than 60s so a server mid-bind survives),
and team directories that hold nothing resurrectable. A team is only removed
when every one of these holds:

- nothing live is running as it, by any of the four rungs (registry, argv,
  spawn payload, mint window)
- neither its lead nor any member resolves to a transcript, asked through the
  same resolver that would resurrect them, so a false positive errs toward
  keeping
- the directory contains only this tool's own scaffolding: a parseable roster,
  and inboxes that are `.json` arrays holding no undelivered mail
- it is older than 300s, covering the gap between a team being minted and its
  session becoming visible

The check is re-proved immediately before deletion, refuses anything that
appeared since, removes `config.json` last so an interrupted sweep is retried
rather than orphaned, and never touches a path outside `teams/` and `tasks/`.
`agent-resume --cleanup --dry-run` lists every decision with its reason.
Completion candidates (names, teams, sockets, sessions with their titles)
come from the script's hidden `--complete <topic>` emitter, so the candidate
lists cannot drift from reality. The flag list in `_agent-resume` is
hand-maintained and has drifted before; add new flags there too. For Recipe 1's
placeholder ritual, `agent-resume --poison` / `--unpoison` set and clear the
server-level ANTHROPIC_BASE_URL poison (newest live swarm socket by default,
`--socket` to aim). A launch agent-resume performs drops
ANTHROPIC_BASE_URL only when it holds that poison, so an interrupted run cannot
starve a real resurrection while a base url you set on purpose still reaches the
agent. A stripped poison is announced; anything else is passed through, since
the swarm's own agents are running with it. Use the manual
recipes below when the automation's assumptions break (forged teams, bg
substrate, argv surgery) or when you need to understand what just went wrong.
Known limits, shared with the recipes: a masqueraded lead cannot spawn
teammates, and revived members are invisible to a running lead's in-memory
roster (@-mentions and UI surfaces never see them; SendMessage sees them only
from a lead whose teamContext is bound, see Recipe 1b's caveat). Close with
the memory ping as always.

The one fact everything rests on: **a teammate's identity is its transcript.**
A tmux or bg teammate is a full `claude` CLI process, so its conversation is a
normal main-session JSONL in the project dir of its cwd. Teams, rosters, panes,
swarm tmux servers and job dirs are all scaffolding that the harness rebuilds
on demand. If you have the JSONL, you can bring the agent back on any
substrate, with its memory intact.

## Finding the soul

Teammate transcripts live in `~/.claude/projects/<flattened-cwd>/` (teammates
usually run in `/tmp` or the leader's cwd). Every line carries the identity:

```bash
for f in ~/.claude/projects/-tmp/*.jsonl; do
  head -c 4000 "$f" | grep -aoE '"agentName":"[^"]*"|"teamName":"[^"]*"' | sort -u | tr '\n' ' ' | sed "s|^|$(basename $f) |"
  echo
done
```

One name can have several generations (each respawn under the same name minted
a new session file). Pick the generation whose life you want back; check
content, not just mtime. A resurrection resumes that file and keeps appending
to it, so the lineage stays in one place.

Do not trust the harness's backend labels when hunting for the soul.
`TaskStop` reports `task_type: "in_process_teammate"` (and rosters say
`backendType: "in-process"`) for teammates that are in fact full `claude`
CLI processes; the labels are unreliable. The transcript's location is the
only reliable discriminator: a main-session JSONL in
`~/.claude/projects/<flattened-cwd>/` means a full CLI process (resumable by
every recipe here), while a truly in-process agent's transcript lives under
the leader session's `subagents/` dir (`agent-*.jsonl`) and has no
independent life to resume. Also mind the cwd: an agent whose cwd was the
leader's scratchpad flattens to a project dir named after that scratchpad
path, not `/tmp`. Search all of `~/.claude/projects/` for the
`"agentName"` stamp before concluding a transcript does not exist
(verified 2026-07-19: a TaskStop-killed "in_process_teammate" left a normal
205-line main transcript under
`projects/-tmp-claude-1000--tmp-<session>-scratchpad/`).

## Choosing the team

Every `<team>` is a directory name under `~/.claude/teams/`. Every recipe
needs one. Two cases:

- **A leader is alive and you are it (or driving it):** use its current team.
  Find it as the team file its last spawn wrote, or the newest
  `~/.claude/teams/session-*` dir. Do not try to predict it: the implicit team is
  named after the leader's internal live session id. On a first boot that is also
  its transcript id, which is why most teams do match their lead's transcript, but
  a resume mints a new internal id and the two part company from then on.
- **No live leader (full team resurrection):** invent a name and forge the
  team file yourself; Recipe 3's identity flags bind the resumed leader to
  whatever name you chose.

## Forging a team file

`agent-resume` forges automatically when it finds a transcript whose team dir is
gone. Resuming a member forges the lead entry plus that member. Resuming a team
or its lead rebuilds the whole roster from the lead's transcript, every teammate
its payloads name, so forging by hand is now only for a custom team name in a
masquerade. Colour and the model's `[1m]` suffix are not lost: they live only in
the roster, but the lead's transcript keeps the harness's spawn payload and the
forge recovers them from there. Nothing recovers them if the lead's transcript
is gone too.

`leadSessionId` is written only when the lead was identified by evidence the
engine will later honour at that field's strength. Rung 1 reads it as exact and
asks nothing further, so a rebuild resting on a sole uncorroborated claimant
leaves it empty instead: the lead is still reachable, through the corroboration
ladder, and the run says every time that nothing corroborates it. A roster with
no `leadSessionId` also cannot corroborate a claim about the lead, or a roster
derived from a lead's payloads would vouch for those same payloads.

The complete shape, learned by copying real ones. Every field shown is
consumed somewhere; do not trim:

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

This template is a manual recipe for hand-building a team, not a spec of what
`agent-resume` forges: the tool writes the member as `backendType: "tmux"` with
its real pane and cwd, since it is about to put a live process in one.

## Recipe 1: resurrect into a tmux pane (spawn-then-swap)

Best when you want the teammate visible in the swarm view and fully registered
with the leader. The trick: let the harness do all its registration around a
placeholder, then swap the process inside the pane. Every piece of harness
state points at the pane id, which survives the swap.

The placeholder must never reach the API. Its one turn buys nothing: all the
registration happens at spawn time, the swap needs only the pane id and the
argv, and nothing ever reads its answer. What the turn does cost is a cache
write of the shared prefix, and on a fresh org it is a billed request that
starts that org's 5-hour window clock. Kill the call at the network, not the
process: the Agent tool offers no per-spawn env, but new panes inherit the
tmux server environment, and the harness's spawn command only re-exports the
vars it embeds (CLAUDECODE etc.), so a server-level poison reaches the
placeholder's claude untouched.

0. Poison the swarm server, so the placeholder's API attempts die at TCP
   connect ($0 billed, no window anchored; it just sits in the pane wearing
   an API error, which is exactly what a corpse-to-be should do):

```bash
agent-resume --poison            # newest live swarm socket; or --socket claude-swarm-<pid>
# equivalent by hand:
SOCK=$(ls -t /tmp/tmux-1000/claude-swarm-* | head -1)
tmux -S $SOCK set-environment -g ANTHROPIC_BASE_URL http://127.0.0.1:9
```

   If no swarm socket exists yet, the first spawn is what creates it: on a
   virgin server, either spawn one sacrificial placeholder first or accept
   one paid stub turn.

1. Spawn a placeholder teammate under the dead teammate's name, via the Agent
   tool with `name:` set, and pass `model: haiku` (belt and braces for the
   case where the poison misses). The harness creates the team if needed,
   writes the roster entry, creates the pane, applies the tint, wires the
   mailbox. Do not wait for the placeholder to answer anything; it can't.
2. Unpoison immediately, before the swap, so the respawned process and every
   future real spawn get a working API back (the live placeholder keeps its
   poisoned env regardless; env is fixed at exec):

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

4. Replace the process in the same pane, resuming the old transcript, with an
   explicit scrub in case the pane still carries the poison:

```bash
tmux -S $SOCK respawn-pane -k -c /tmp -t "$PANE" \
  "env -u ANTHROPIC_BASE_URL CLAUDECODE=1 CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1 CLAUDE_CODE_ENTRYPOINT=cli $ARGS --resume <old-session-id>"
```

The poison steps (0, 2, the `env -u`) are derived from verified parts (seam 6
in seams-and-bugs.md: spawn commands embed their own env; tmux server env
reaches panes otherwise) but the poisoned flow as a whole is not yet
battle-tested; the failure mode is the old behavior, one cheap haiku turn.

`-c` must match the transcript's project dir, or resume will not find the
session. You can edit the argv on the way through: `--agent-color`, `--model`,
`--effort`, `--permission-mode` all take effect for the new incarnation.

Costs and caveats:
- The placeholder still leaves a tiny orphan session file; with the poison in
  place it holds one failed turn and zero billed tokens.
- The kill is rude, so the placeholder never announces its death; the leader
  never notices the swap (that blindness is structural, see architecture.md).
- The leader's in-memory color/roster beliefs keep whatever the placeholder
  had. Disk edits will not update a running leader's UI.

## Recipe 1b: pane resurrection with no placeholder at all

When the leader's in-memory registration does not matter (post-masquerade
rebuilds, teams driven purely through the roster file), skip the spawn ritual:
nothing is born, nothing is billed, no swap race. Hand-build what the harness
would have built: a pane running the resumed claude, plus a roster entry
pointing at it.

```bash
SOCK=$(ls -t /tmp/tmux-1000/claude-swarm-* | head -1)
PANE=$(tmux -S $SOCK split-window -d -P -F '#{pane_id}' -c /tmp \
  "env CLAUDECODE=1 CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1 CLAUDE_CODE_ENTRYPOINT=cli \
   claude --resume <old-session-id> --agent-id NAME@<team> --agent-name NAME \
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

Trade-offs vs Recipe 1: zero birth cost; transcripts still flush (it is a
pane life, not a bg life); with `backendType: tmux` and a real pane id the
graceful-exit reaper covers it like a real spawn. What you lose is everything
that lives in the leader's runtime memory, because only real teammate
creation writes it and nothing ever imports config.json back into it
(verified in the 2.1.202 bundle): no `teamContext.teammates` entry, no
registered task. Concretely: absent from the tasks pill and swarm view, no
color, and the @-mention DM machinery cannot see it, since the completion
list is built from `teamContext.teammates` plus the agent registry, memory
only. The one channel that can work is the SendMessage tool itself: its
resolver checks the in-memory map, then the agent registry, then falls back
to reading the roster file at send time. But the fallback only fires in a
leader whose teamContext is bound at all (see architecture.md): a leader that
has spawned at least one real teammate in its current life. A restarted
leader that has not spawned since resolves nothing, roster or no roster
(verified live on 2.1.220: "No agent named 'X' is reachable", and the full
agent-id form is rejected by the tool schema). Bind first: spawn one real
teammate via the Agent tool (a poisoned placeholder per Recipe 1 makes that
free), after which hand-registered members resolve by bare name. Without
binding, drive the member's pane by keystrokes (see Driving below) and read
its replies from the team's inbox files directly. @name from the prompt
never reaches 1b members either way. No tint is
applied either; for visual parity with spawned teammates, apply it the way
the harness does (the tint is worth keeping, see bug 2 in seams-and-bugs.md):

```bash
C=blue  # tmux color, not roster color: purple->magenta, orange->colour208,
        # pink->colour205; red/blue/green/yellow/cyan pass through (2.1.202 map)
tmux -S $SOCK set-option -p -t "$PANE" window-style "bg=default,fg=$C"
tmux -S $SOCK set-option -p -t "$PANE" pane-border-style "fg=$C"
tmux -S $SOCK set-option -p -t "$PANE" pane-active-border-style "fg=$C"
tmux -S $SOCK select-pane -t "$PANE" -T NAME
tmux -S $SOCK set-option -p -t "$PANE" pane-border-format "#[fg=$C,bold] #{pane_title} #[default]"
```

That replicates the harness exactly: its tint is three set-options plus the
bold colored border title, not just window-style.

Status: assembled from individually
verified parts (hand-registration from Recipe 2, identity-flag resume from
Recipes 2/3, inline-env pane spawn per seam 6, resolver precedence read from
the bundle); the combination is not yet battle-tested. Close with the memory
ping as always.

## Recipe 2: resurrect as a background job (one command)

Substrate choice is not yours to make: check `teammateMode` in
`~/.claude/settings.json` first: if it says `tmux`, resurrect into a tmux
pane (Recipe 1 or 1b), and use this recipe only when the user explicitly
asks for a background job. Beyond honoring the setting, the bg caveat below
(no transcript flush) means a bg resurrection is amnesiac, and the wrong
default for an agent whose memory you just went to the trouble of
recovering. What bg does offer when asked for: no tmux, native truecolor,
daemon supervision, `claude agents` / `attach` / `logs` / `stop` management,
and it survives the leader's exit (the exit reaper only kills pane-backed
members).

```bash
cd /tmp && claude --bg --resume <old-session-id> \
  --agent-id NAME@<team> --agent-name NAME --team-name <team> \
  --agent-color blue --agent-type claude \
  --dangerously-skip-permissions --effort low --model <model>
```

The child does its half of the wiring (it watches the right inbox), but the
harness does not know a bg job can be a teammate, so you register it yourself:

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

The big caveat: **bg incarnations do not flush their turns back to the
transcript.** A teammate that lives three lives as a bg job and is then
resurrected will remember none of them; only pane and interactive lives leave
memories. If the work of a bg life matters, have the agent write results
somewhere durable before it dies, or treat bg lives as stateless workers.

## Recipe 3: resume a whole dead team (the leader masquerade)

Leaders cannot be resumed as leaders directly. Their transcripts carry no team
identity (only teammate processes stamp `agentName`/`teamName` onto lines), the
CLI rejects `--team-name` without `--agent-id` (the three flags are
all-or-nothing), and a graceful leader exit deletes the team dir and kills the
pane-backed members anyway. The way in is the teammate branch, wearing the
lead's own id:

1. Make sure the team file exists. Forge it if the original was deleted:

```bash
mkdir -p ~/.claude/teams/<team>/inboxes
# config.json with "leadAgentId": "team-lead@<team>" and your member list
echo '[]' > ~/.claude/teams/<team>/inboxes/team-lead.json
```

2. Resume the dead leader session claiming the team-lead identity:

```bash
claude --resume <dead-main-session-id> \
  --agent-id team-lead@<team> --agent-name team-lead --team-name <team>
```

This boots as a "teammate" whose id equals `leadAgentId`, so it reads the
right roster, watches the leader's inbox, and can SendMessage every member
immediately. No spawn ritual, no waiting for an implicit team.

3. Bring the members back with Recipe 1/1b (or Recipe 2 if explicitly
   requested, honoring `teammateMode` as above), pointing their `--team-name`
   at this team.

Limits of the masquerade, both verified:
- It runs on the teammate code path, so it cannot spawn new teammates
  ("teammates cannot spawn teammates"). Hire externally via the recipes above.
- It never registers the team for exit cleanup (only the real TeamCreate path
  does), so when the impostor exits, the team dir and the members survive.
  A bug upstream; a feature here.

Do not bother with these dead ends, they are tested: `--agent-id ""` fails the
same falsy check as omitting it. Pre-forging a team named after the leader's
transcript id is a coin toss: the implicit team is named after the internal live
session id, which equals the transcript id on a first boot and diverges from it
after any resume, and which cannot be predicted before boot either way.

## Driving and observing resurrected agents

- To a live teammate, talk normally, but know the two channels differ: the
  SendMessage tool resolves recipients from leader memory first, then falls
  back to the roster file at send time; both require the leader's teamContext
  to be bound (Recipe 1b's caveat), and reach members by bare name only. The
  `@name` prompt path and every UI surface (mention completion, colors, tasks
  pill) run on the in-memory roster that only real spawns populate; those work
  for spawned members only.
- Driving an interactive pane by keystrokes: send the text and the Enter as
  two separate `send-keys` calls, with a beat between them. A single call with
  a trailing Enter pastes a newline into the input instead of submitting.
- Reading panes: `capture-pane -p` renders the prompt's dim ghost suggestion
  as if someone typed it. Before believing any input line, re-capture with
  `-e` and check for the dim SGR code (`\x1b[2m`) in front of it.
- Do not trust the roster or the leader's task list for liveness. The leader
  only learns about deaths that announce themselves; a Ctrl+C'd teammate stays
  "active" forever. Ground truth is the pane, the process, and the job state.

## Verify the resurrection

Always close with a memory ping: SendMessage the teammate a question only the
resumed transcript can answer, and check the reply arrives through the
mailbox. That one round trip proves the transcript loaded, the identity flags
took, the roster resolves, and both inbox directions work.
