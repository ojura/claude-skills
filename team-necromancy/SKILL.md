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

## Choosing the team

Every `<team>` is a directory name under `~/.claude/teams/`. Every recipe
needs one. Two cases:

- **A leader is alive and you are it (or driving it):** use its current team.
  Find it as the team file its last spawn wrote, or the newest
  `~/.claude/teams/session-*` dir. Do not try to predict it: the implicit team
  is named after the leader's internal live session id, which changes on every
  resume and does not match any transcript filename.
- **No live leader (full team resurrection):** invent a name and forge the
  team file yourself; Recipe 3's identity flags bind the resumed leader to
  whatever name you chose.

## Forging a team file

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
tmux -S $SOCK set-environment -gu ANTHROPIC_BASE_URL
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
only. The one channel that does work is the SendMessage tool itself: its
resolver checks the in-memory map, then the agent registry, then falls back
to reading the roster file at send time, so a hand-registered member is
tool-addressable by exact name and inbox delivery works. Drive 1b members by
SendMessage only; @name from the prompt will not reach them. No tint is
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

Best substrate in almost every way: no tmux, native truecolor, daemon
supervision, `claude agents` / `attach` / `logs` / `stop` management, and it
survives the leader's exit (the exit reaper only kills pane-backed members).

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

3. Bring the members back with Recipe 2 (or Recipe 1), pointing their
   `--team-name` at this team.

Limits of the masquerade, both verified:
- It runs on the teammate code path, so it cannot spawn new teammates
  ("teammates cannot spawn teammates"). Hire externally via the recipes above.
- It never registers the team for exit cleanup (only the real TeamCreate path
  does), so when the impostor exits, the team dir and the members survive.
  A bug upstream; a feature here.

Do not bother with these dead ends, they are tested: `--agent-id ""` fails the
same falsy check as omitting it. Pre-forging a team named after the leader's
transcript id fails because the implicit team is named after the internal live
session id, which changes on every resume and cannot be predicted before boot.

## Driving and observing resurrected agents

- To a live teammate, talk normally, but know the two channels differ: the
  SendMessage tool resolves recipients from leader memory first, then falls
  back to the roster file at send time, so even a hand-registered member is
  reachable by exact name. The `@name` prompt path and every UI surface
  (mention completion, colors, tasks pill) run on the in-memory roster that
  only real spawns populate; those work for spawned members only.
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
