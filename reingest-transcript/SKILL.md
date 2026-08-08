---
name: reingest-transcript
description: Reconstruct and re-read the full current-session conversation from its own Claude Code JSONL transcript (active path only), to recover detail that compaction flattened. Use when you need verbatim prior turns, exact strings/code you generated, decisions made, or the real action sequence rather than the lossy compacted summary. Produces a compact JSONL rendering you read back in slices.
---

# Reingest transcript

Rebuild the live conversation from the session's own JSONL and read it back, so detail the compaction summary dropped is recoverable verbatim.

## When to use
- After a compaction, when you need exact prior content (strings, code, decisions, the order things happened in) rather than the summary.
- The compaction note "read the full transcript at: `<path>.jsonl`" points straight at the source file.

## Steps
1. **Find the session JSONL.** It is the path in the compaction note, or:
   `~/.claude/projects/<slug>/<session-id>.jsonl`, where `<slug>` is the cwd with `/` and `.` turned into `-`. If unsure which file, the most recently modified `*.jsonl` in that project dir is the current session.
2. **Generate the rendering** (the script sits beside this SKILL.md):
   ```
   python3 ~/.claude/skills/reingest-transcript/extract.py <SESSION.jsonl> /tmp/turns.jsonl --mode text
   ```
   - `--mode text` (default-prefer): user + assistant prose only. Smallest. Use this unless you actually need the tool-call flow.
   - `--mode enriched`: also emits assistant tool calls (name + input, clipped via `--cap N`, default 300), and tags each record `u` so a clipped input is recoverable with `grep '"uuid":"<u>"' <SESSION.jsonl>`. Much larger, roughly double.
   - `--format jsonl` (default) or `--format md`. Prefer the default; md exists for reading by eye.
   - `--meta stub|drop|keep` (default `stub`): harness-injected `isMeta` user nodes (skill dumps, caveats) are stubbed to one line; `keep` emits them in full if the injected content is itself what you need.
3. **Read the COVERAGE block if it printed, then the slice the intervals point at.** Each interval states its own cost in tokens; the `Read` tool caps ~25k per call, so page a large interval with `offset`/`limit`.

## Output shape
One JSON record per line, no banners:

```json
{"i":12,"r":"user","t":"..."}
{"i":13,"r":"team","msgs":[{"from":"cli-ux","idle":"available"}]}
{"i":14,"r":"asst","t":"...","x":[{"n":"Bash","in":{"command":"..."}}]}
```

`r` says where the turn came from, which the markdown format could not: **user** (the operator typed it), **team** (a teammate message), **task** (a task notification), **asst**, **meta** (a harness injection), **sys** (interrupt markers and other harness-inserted text). Teammate and task notifications are parsed into fields rather than left as raw tagged blobs, and their repeated harness boilerplate is dropped. A teammate record keeps any surrounding prose that is *not* that boilerplate, so nothing real is dropped quietly.

## What the script does
Walks from the most-recent non-sidechain leaf towards the root, emitting **only the active path**: abandoned retry/interrupt branches and subagent sidechains are excluded (one real session had 22 leaves but a single live path). It prefers `logicalParentUuid` over `parentUuid`, which lets it **walk straight through every compaction boundary**: a `/compact` writes its summary against a synthetic boundary node whose `parentUuid` dead-ends, but that node stores the true pre-compaction tip in `logicalParentUuid`. Keeps `text` blocks (stripping `<system-reminder>...</system-reminder>`); enriched mode also keeps `tool_use`; `tool_result` and `thinking` are always dropped.

**The walk does not always reach the root, and that is the failure mode to watch.** A killed process leaves a `last-prompt` naming an entry it never wrote, and the next boot parents its first entry onto that missing uuid. A strict walk stops dead there, mid-file, with most of the session still sitting in the same file unreached. The script therefore rejoins the newest conversation entry before the break and keeps going. That join is **inferred, not recorded**: it gets its own `{"r":"break"}` record at the exact spot, and is named in the COVERAGE report. Records either side of it may not be consecutive.

Harness-injected user nodes marked `isMeta: true` (skill content dumps, `<local-command-caveat>` wrappers, session-name reminders) are **stubbed to one line by default**: `[[meta injection stripped: <first line> (<N> lines | via <sourceToolUseID>)]]`. They are ordinary `text` blocks, not `tool_result`s, so block-type filtering alone would emit them in full; a single skill load can be ~8,500 lines. `--meta drop` removes them without a stub; `--meta keep` restores emit-in-full (use when the injected content itself is what you're recovering). Compact-summary nodes and short one-line meta (image placeholders) are never stubbed.

The stdout is what you act on, in two parts:

- **COVERAGE**, printed only when something is wrong, and never silent when it is. It names every chain break (bridged or terminal), every compaction boundary in the file that the walk never reached, and how many of the file's conversation entries were actually walked. A boundary listed here is **not** in the intervals below, so more history exists than they describe.
- **READ-BACK INTERVALS**, the line ranges between the compaction boundaries the walk did reach, with their times and their token cost. It marks the interval that is exactly what the latest compaction dropped, and the tail already in your live context, and puts a **`+`** on any interval containing a prior `/reingest-transcript`, so you can read the slice you need and skip a transcript-of-a-transcript instead of ingesting it twice. (The `+` errs loud: it fires on any sighting of the command, including a summary merely quoting it, so treat it as "look here" rather than "proven reingest.")

## pseudocompact.py: graft a synthetic compaction leaf

The inverse companion to reingesting: when a session's context is exhausted (or its tail is dead cruft), pseudocompact it so the next resume starts with an empty window while `logicalParentUuid` keeps the full ancestry walkable.

```
python3 ~/.claude/skills/reingest-transcript/pseudocompact.py <SESSION.jsonl | session-id> [--leaf UUID] [--message TEXT] [--dry-run]
```

Appends a rootless `isCompactSummary` user entry (`parentUuid: null`, `logicalParentUuid` = chosen leaf, default text "The conversation was compacted.") **plus a trailing `last-prompt` line pointing at it. That line is what makes it work: on resume the harness picks its leaf from the file's final `last-prompt` line, and silently ignores the new entry without it** (found empirically 2026-08-03). `--leaf UUID` also trims everything after that entry (a dead tail of failed turns), keeping an adjacent `last-prompt` that already points at it. Backs up first, verifies no newly-dangling refs (pre-existing cross-file lpu danglers are tolerated), and warns about live processes.

**The default leaf is the file's last conversation entry, not the trailing `last-prompt`'s `leafUuid`.** A `last-prompt` is written only when you submit a prompt, so a session whose final turns were driven by **teammate messages** appends entries without one and that pointer falls behind. Grafting onto the stale pointer orphans every turn after it: they stop being ancestors of the new boundary, so no `logicalParentUuid` walk reaches them again and they are gone without a word. When the two disagree the tool says so and grafts at the real tip.

After running it: **kill and re-resume** the session. A live process keeps its old in-memory leaf, and prompting it appends entries parented on uuids that may no longer exist. A cancelled `/resume` in the fresh process writes a cruft turn plus a `last-prompt` re-aiming the leaf at it; delete those trailing lines (the correct `last-prompt` is right above them) rather than re-running the tool.

## Gotchas (don't re-learn these)
- **Read COVERAGE before trusting the intervals.** If it printed at all, the intervals describe less than the whole file, and "lines 1-N before `<ts>`" does not mean line 1 is the start of the conversation.
- **Read the guidance, not the whole file.** Read the one-boundary-back slice the report points at (what the latest compaction flattened), and skip the lines it marks as already-in-context or as a prior reingest.
- **A line is a whole message, so line count no longer predicts read size.** Use the per-interval token cost, not the line span.
- **Prefer `--mode text`.** Enriched roughly doubles the record count (prose and tool calls are often separate nodes) and is token-heavy, so use it only when the action sequence matters.
- The live JSONL keeps growing, so counts drift between runs.
- **The harness does not mark teammate messages.** A task notification carries `origin.kind = "task-notification"` and prose you typed carries `origin.kind = "human"`, but a teammate message has no `origin` at all and is otherwise identical to typed prose: `type: user`, `userType: external`, no `isMeta`, plain string content. The `<teammate-message>` block in the body is the only thing that identifies it, which is why the script parses those blocks rather than trusting a field.
- Don't count "user turns" by role alone: a node can carry a `tool_result` and a `text` block together, and `[Request interrupted by user]` / slash-command artifacts ride the user role. The `r` field settles this; classify by that rather than by role.
- A teleported session shows a tool-name seam (`bash_tool`->`Bash`, `view`->`Read`, `str_replace`->`Edit`, `create_file`->`Write`), a handy phase marker for where the session changed machines.
