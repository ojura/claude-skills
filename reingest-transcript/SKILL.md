---
name: reingest-transcript
description: Reconstruct and re-read either the active main conversation or one worker's local sidechain from a Claude Code JSONL transcript. Use after compaction, or when a resumed fork needs its predecessor's exact local decisions, commands, findings, and checkpoints without reloading inherited main-session history.
---

# Reingest transcript

Rebuild either the lead's active conversation or a worker's local sidechain and read back the exact records that summaries or replacement forks omitted.

## When to use
- After a compaction, when you need exact prior content such as strings, code, decisions, or action order.
- When a replacement or resumed fork needs the predecessor worker's local decisions and checkpoints without ingesting the inherited lead conversation again.
- The compaction note "read the full transcript at: `<path>.jsonl`" points straight at a main-session source file. Worker transcripts live under `<session-id>/subagents/agent-<id>.jsonl`.

## Steps
1. **Find the session JSONL.** It is the path in the compaction note, or:
   `~/.claude/projects/<slug>/<session-id>.jsonl`, where `<slug>` is the cwd with `/` and `.` turned into `-`. If unsure which file, the most recently modified `*.jsonl` in that project dir is the current session.
2. **Generate the rendering** (the script sits beside this SKILL.md):
   ```
   # Lead conversation
   python3 ~/.claude/skills/reingest-transcript/extract.py <SESSION.jsonl> /tmp/turns.jsonl --scope main --mode text

   # One worker's local records only
   python3 ~/.claude/skills/reingest-transcript/extract.py <agent-ID.jsonl> /tmp/sidechain.jsonl --scope sidechain --mode text
   ```
   - `--scope main` (default) walks the active lead path and excludes worker records.
   - `--scope sidechain` walks only one worker's local `isSidechain` path. It reports the matching `fork-context-ref` as the boundary and does not emit the inherited lead conversation. A general-purpose worker may have no reference; its parentless local entry is then the boundary.
   - `--agent-id ID` selects the worker when a file contains more than one sidechain identity. A normal worker transcript contains one, so the script infers it.
   - `--mode text` (default-prefer): user + assistant prose only. Smallest. Use this unless you actually need the tool-call flow.
   - `--mode enriched`: also emits assistant tool calls (name + input, clipped via `--cap N`, default 300), and tags each record `u` so a clipped input is recoverable with `grep '"uuid":"<u>"' <SESSION.jsonl>`. Much larger, roughly double.
   - `--format jsonl` (default) or `--format md`. Prefer the default; md exists for reading by eye.
   - `--meta stub|drop|keep` (default `stub`): harness-injected `isMeta` user nodes (skill dumps, caveats) are stubbed to one line; `keep` emits them in full if the injected content is itself what you need.
3. **Read the COVERAGE block if it printed, then the slice the intervals point at.** Each interval states its own cost in tokens; the `Read` tool caps ~25k per call, so page a large interval with `offset`/`limit`.

## Output shape
One JSON record per line, no banners:

```json
{"r":"head","scope":"sidechain","agent":"a123...","fork":{"parentSessionId":"...","parentLastUuid":"..."}}
{"i":12,"r":"user","t":"..."}
{"i":13,"r":"team","msgs":[{"from":"cli-ux","idle":"available"}]}
{"i":14,"r":"asst","t":"...","x":[{"n":"Bash","in":{"command":"..."}}]}
{"i":15,"r":"user","t":"...","queued":true}
{"i":16,"r":"team","queued":true,"msgs":[{"from":"navines-lead","addr":"uds:/run/user/1000/cc-socks/42.sock","mode":"bypass","t":"..."}]}
```

The head record names the selected scope. In sidechain mode it also names the worker and, when present, the `fork-context-ref` boundary. `r` says where the turn came from, which the markdown format could not: **user** (the operator typed it), **team** (a message from another agent: a teammate, a subagent, another session, or the coordinator that started a fork), **task** (a task notification), **asst**, **meta** (a harness injection), **sys** (interrupt markers and other harness-inserted text). `queued: true` marks a message that arrived while a turn was running; the markdown banner says `queued`. Agent messages and task notifications are parsed into fields rather than left as raw tagged blobs, and their repeated harness boilerplate is dropped. Each entry in `msgs` has `from` (the sender's name, or `coordinator`) and either its text `t` or, for an idle notification, `idle` with `why` and `at` when given. A teammate's message can add its `summary`; a message from another session adds `addr`, the address a reply goes to, and `mode`, the sender's permission mode; `agent` is the sender's task id when the record names one that differs from `from`. A team record keeps any surrounding prose that is *not* that boilerplate, so nothing real is dropped quietly.

## What the script does
For `--scope main`, the script walks from the most-recent non-sidechain leaf towards the root and emits only the active lead path. Abandoned retries and worker sidechains are excluded. For `--scope sidechain`, it selects one `agentId`, walks only that worker's `isSidechain` records, and stops at the local root. A matching `fork-context-ref` is reported in the header and on stdout, but the inherited main-session records named by it are not loaded or emitted.

Both scopes prefer `logicalParentUuid` over `parentUuid`, which lets the walk continue through compaction boundaries. Subagents can compact independently of the lead; sidechain mode follows those worker-local boundaries in the same way. A `/compact` summary can have a parent that ends locally while its `logicalParentUuid` records the real pre-compaction tip. The renderer keeps `text` blocks after stripping `<system-reminder>...</system-reminder>`; enriched mode also keeps `tool_use`; `tool_result` and `thinking` are always dropped.

A message that arrives while a turn is running is not written as a user message. The harness writes it as an `attachment` record of type `queued_command`, with the text in `attachment.prompt` and the sender in `attachment.origin`, and shows it to the model inside a system-reminder. That attachment is the only record of the message (checked 2026-10-07), so the walk and the renderer treat it as a turn, and when it is the newest record, such as the operator's last word to a worker that then died, it is the leaf.

**The walk does not always reach the root, and that is the failure mode to watch.** A killed process leaves a `last-prompt` naming an entry it never wrote, and the next boot parents its first entry onto that missing uuid. A strict walk stops dead there, mid-file, with most of the session still sitting in the same file unreached. The script therefore rejoins the newest conversation entry before the break and keeps going. That join is **inferred, not recorded**: it gets its own `{"r":"break"}` record at the exact spot, and is named in the COVERAGE report. Records either side of it may not be consecutive.

**A compaction can also point its boundary into a cycle.** The boundary's `logicalParentUuid` names the tail of the compaction's own preserved window (the last records it keeps verbatim, listed in `compactMetadata.preservedMessages`), but that tail was written after the boundary, under the new summary, so following it leads back to the boundary. Before this repair the walk stopped there and lost everything before that compaction. The script now repairs that one shape the way `~/claude-patches` step P does (`src/d1e.js`; the rule is in its `docs/invariant.md`): it restores the window's recorded order and connects the window's head to the nearest record of the same chain written before the boundary. It renders the compaction summary at that point, followed by the window, which is what the session held after the compaction. A `{"r":"break"}` record marks each repair, and the REPAIRED report lists them. Each join is labelled **recorded** (the head's own record names that parent; the compaction re-parented only the tail), **proven** (that record walks to the chain's one origin), or **UNPROVEN** (write position alone; also named in COVERAGE). Any other cycle ends the walk with a `CHAIN CYCLE` break and a COVERAGE line, because a join by position would be a guess.

Harness injections, meaning user nodes marked `isMeta: true` that no sender's `origin` claims (skill content dumps, `<local-command-caveat>` wrappers, session-name reminders), are **stubbed to one line by default**: `[[meta injection stripped: <first line> (<N> lines | via <sourceToolUseID>)]]`. The stub follows the classified source, not the flag: a peer's message is `isMeta: true` as well, and renders in full as a team record. Injections are ordinary `text` blocks, not `tool_result`s, so block-type filtering alone would emit them in full; a single skill load can be ~8,500 lines. `--meta drop` removes them without a stub; `--meta keep` emits them in full (use when the injected content itself is what you're recovering). Compact-summary nodes and short one-line meta (image placeholders) are never stubbed.

The stdout is what you act on, in two parts:

- **COVERAGE**, printed only when something is wrong, and never silent when it is. It names every chain break (bridged or terminal), every cycle the walk stopped at, every unproven join of a repaired compaction cycle, every compaction boundary in the file that the walk never reached, and how many of the file's conversation entries were actually walked. A boundary listed here is **not** in the intervals below, so more history exists than they describe.
- **REPAIRED**, printed when the walk crossed a compaction cycle: one line per repaired boundary, with the record its window was joined to and how that join is known.
- **READ-BACK INTERVALS**, the line ranges between the compaction boundaries the walk did reach, with their times and their token cost. It marks the interval that is exactly what the latest compaction dropped, and the tail already in your live context, and puts a **`+`** on any interval containing a prior `/reingest-transcript`, so you can read the slice you need and skip a transcript-of-a-transcript instead of ingesting it twice. (The `+` errs loud: it fires on any sighting of the command, including a summary merely quoting it, so treat it as "look here" rather than "proven reingest.")

## pseudocompact.py: graft a synthetic compaction leaf

The inverse companion to reingesting: when a session's context is exhausted (or its tail is dead cruft), pseudocompact it so the next resume starts with an empty window while `logicalParentUuid` keeps the full ancestry walkable.

```
python3 ~/.claude/skills/reingest-transcript/pseudocompact.py <SESSION.jsonl | session-id> [--leaf UUID] [--message TEXT] [--dry-run]
```

Appends a rootless `isCompactSummary` user entry (`parentUuid: null`, `logicalParentUuid` = chosen leaf, default text "The conversation was compacted.") **plus a trailing `last-prompt` line pointing at it. That line is what makes it work: on resume the harness picks its leaf from the file's final `last-prompt` line, and silently ignores the new entry without it** (found empirically 2026-08-03). `--leaf UUID` also trims everything after that entry (a dead tail of failed turns), keeping an adjacent `last-prompt` that already points at it. Backs up first, verifies no newly-dangling refs (pre-existing cross-file lpu danglers are tolerated), and warns about live processes.

**The default leaf is the end of the chain that continues from the file's last user or assistant entry, not the trailing `last-prompt`'s `leafUuid`.** A `last-prompt` is written only when you submit a prompt, so a session whose final turns were driven by **teammate messages** appends entries without one and that pointer falls behind. Grafting onto the stale pointer would leave every turn after it out of the new boundary's ancestry, where no `logicalParentUuid` walk reaches them. The records that follow the last turn (its `turn_duration`, a local command such as `/model` and its output, a queued message nobody answered) are its descendants, so the leaf follows them and they stay on the ancestry. It follows parent links, not file order, because the harness can write a record above its parent: a `turn_duration` one line above the reply it times, a "Not logged in" reply above the prompt it answers. File order only chooses among sibling branches, taking the one written last. When the `last-prompt` names a different entry, the tool prints that entry's type and line, and says whether the leaf is further along its chain (and by how many entries) or on another branch, as after a rewound or resubmitted prompt.

After running it: **kill and re-resume** the session. A live process keeps its old in-memory leaf, and prompting it appends entries parented on uuids that may no longer exist. A cancelled `/resume` in the fresh process writes a cruft turn plus a `last-prompt` re-aiming the leaf at it; delete those trailing lines (the correct `last-prompt` is right above them) rather than re-running the tool.

## Gotchas (don't re-learn these)
- **Read COVERAGE before trusting the intervals.** If it printed at all, the intervals describe less than the whole file, and "lines 1-N before `<ts>`" does not mean line 1 is the start of the conversation.
- **Read the guidance, not the whole file.** Read the one-boundary-back slice the report points at (what the latest compaction flattened), and skip the lines it marks as already-in-context or as a prior reingest.
- **A line is a whole message, so line count no longer predicts read size.** Use the per-interval token cost, not the line span.
- **Prefer `--mode text`.** Enriched roughly doubles the record count because prose and tool calls are often separate nodes, so use it only when the action sequence matters.
- **Sidechain mode does not compare workers.** Render the predecessor and replacement transcripts separately, then compare decisions, file paths, hashes, unfinished checks, rejected approaches, and pending messages. The replacement's inherited lead history is deliberately absent from its sidechain rendering.
- A `fork-context-ref` has no UUID and is not linked through `parentUuid`; it is boundary metadata before the sidechain records. The script reports it instead of trying to walk through it.
- The live JSONL keeps growing, so counts drift between runs.
- **Each sender is marked in a different field, and a teammate in none.** Prose you typed carries `origin.kind = "human"` and stays yours when it quotes an agent's message block. A task notification carries `origin.kind = "task-notification"`; an older queued one carries only `commandMode: "task-notification"`. A message from a subagent or teammate of this session, or from another session, carries `origin.kind = "peer"` and `isMeta: true`, with its words in an `<agent-message from=...>` block (this session's agents) or a `<cross-session-message from=... from-name=... from-mode=...>` block (another session). A fork's coordinator carries `origin.kind = "coordinator"` and no block. A `<teammate-message>` record has no `origin` and no `isMeta` and is otherwise identical to typed prose (`type: user`, `userType: external`, plain string content), so the block is the only thing that identifies it. The script parses blocks only where a sender can have written them, never in a compaction summary or a harness injection, since those quote them.
- **A message delivered mid-turn exists only as a `queued_command` attachment.** Searching user records for something the operator or a peer said while a worker was busy finds nothing; search the attachments, or read the rendering, where the record says `queued`.
- Don't count "user turns" by role alone: a node can carry a `tool_result` and a `text` block together, and `[Request interrupted by user]` / slash-command artifacts ride the user role. The `r` field settles this; classify by that rather than by role.
- A teleported session shows a tool-name seam (`bash_tool`->`Bash`, `view`->`Read`, `str_replace`->`Edit`, `create_file`->`Write`), a handy phase marker for where the session changed machines.
