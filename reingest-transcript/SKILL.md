---
name: reingest-transcript
description: Reconstruct and re-read the full current-session conversation from its own Claude Code JSONL transcript (active path only), to recover detail that compaction flattened. Use when you need verbatim prior turns, exact strings/code you generated, decisions made, or the real action sequence — not the lossy compacted summary. Produces a text-only or tool-enriched markdown rendering you read back in chunks.
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
   python3 ~/.claude/skills/reingest-transcript/extract.py <SESSION.jsonl> /tmp/turns.md --mode text
   ```
   - `--mode text` (default-prefer): user + assistant prose only. Smallest. Use this unless you actually need the tool-call flow.
   - `--mode enriched`: also emits assistant tool calls (name + input, clipped via `--cap N`, default 300), and tags each turn `uuid=<id>` so a clipped input is recoverable with `grep '"uuid":"<id>"' <SESSION.jsonl>`. Much larger — roughly 250-300k tokens for a long session.
   - `--meta stub|drop|keep` (default `stub`): harness-injected `isMeta` user nodes (skill dumps, caveats) are stubbed to one line; `keep` emits them in full if the injected content is itself what you need.
3. **Read the output back in ~600-line chunks.** The `Read` tool caps ~25k tokens/call; 600 lines of this format ≈ 12-17k. `wc -l` first, then page with `offset`/`limit`.

## What the script does
Walks from the most-recent non-sidechain leaf to the root, emitting **only the active path** — abandoned retry/interrupt branches and subagent sidechains are excluded (one real session had 22 leaves but a single live path). It prefers `logicalParentUuid` over `parentUuid`, which lets it **walk straight through every compaction boundary**: a `/compact` writes its summary against a synthetic boundary node whose `parentUuid` dead-ends, but that node stores the true pre-compaction tip in `logicalParentUuid`. Keeps `text` blocks (stripping `<system-reminder>...</system-reminder>`); enriched mode also keeps `tool_use`; `tool_result` and `thinking` are always dropped.

Harness-injected user nodes (`isMeta: true` — skill content dumps, `<local-command-caveat>` wrappers, session-name reminders) are **stubbed to one line by default**: `[[meta injection stripped: <first line> (<N> lines | via <sourceToolUseID>)]]`. They are ordinary `text` blocks, not `tool_result`s, so block-type filtering alone would emit them in full — a single skill load can be ~8,500 lines. `--meta drop` removes them without a stub; `--meta keep` restores emit-in-full (use when the injected content itself is what you're recovering). Compact-summary nodes and short one-line meta (image placeholders) are never stubbed. Flags can only see this class: `<task-notification>`, `<teammate-message>`, `<command-name>` expansions, and `<local-command-stdout>` are `isMeta: false` — metadata-indistinguishable from typed prose — and are kept.

Because it runs to root, the stdout **reading guidance** is the part you act on: it prints the **read-back intervals** between compaction boundaries (line ranges with their compaction times), marks the one that is exactly what the latest compaction dropped and the tail already in your live context, and puts a **`+`** on any interval that contains a prior `/reingest-transcript` — so you can read the slice you need and skip a transcript-of-a-transcript instead of ingesting it twice. (The `+` errs loud: it fires on any sighting of the command, including a summary merely quoting it, so treat it as "look here" not "proven reingest.")

## Gotchas (don't re-learn these)
- **Read the guidance, not the whole file.** The walk goes all the way to root, but you rarely want all of it — read the one-boundary-back slice the report points at (what the latest compaction flattened), and skip the lines it marks as already-in-context or as a prior reingest.
- **Prefer `--mode text`.** Enriched roughly doubles the turn count (prose and tool calls are often separate nodes) and is token-heavy — use it only when the action sequence matters.
- The live JSONL keeps growing, so turn counts drift between runs.
- Don't count "user turns" by role alone: a node can carry a `tool_result` and a `text` block together, and `[Request interrupted by user]` / slash-command artifacts ride the user role — classify by block types if the count matters. Skill and command expansions are `text` blocks on `isMeta` nodes, not tool_results — block types alone won't catch them; that's what the `--meta` stubbing is for.
- A teleported session shows a tool-name seam (`bash_tool`->`Bash`, `view`->`Read`, `str_replace`->`Edit`, `create_file`->`Write`) — a handy phase marker for where the session changed machines.
