# Archive conversation forks

Declutter the Claude Code session picker by grouping a project's session JSONLs
into fork families, keeping the canonical session per family, and moving the
redundant forks out to `~/claude-archive` - documented and fully recoverable,
never deleted.

## What it does

- Builds a deterministic map of every session: raw and prose distinct-content
  fingerprints, `logicalParentUuid` lineage, compaction boundaries, and
  cross-file / phantom ancestors.
- Picks the canonical (most complete) session per fork family and protects every
  load-bearing file a kept session needs for cross-file scrollback, so archiving
  never breaks a canonical's rendered history.
- Tells a true fork from a session that merely shares tool-edits by comparing
  prose fingerprints, not raw overlap.
- Archives only what is provably redundant (exact containment) or read-and-
  confirmed disposable. Substantive unique content is kept, and a single message
  is never archived on message-count alone (it could be a key or a proof).
- Optionally titles the retained sessions for a themed, chronological picker,
  mtime-neutrally so nothing is exposed to the retention sweep.

## Safety

- **Move, never delete.** Everything goes to `~/claude-archive` with a manifest
  that restores any file to its original path.
- **lpu-safe.** Never orphans a kept session's cross-file or phantom-backfill
  ancestors, so a moved file can't break another session's scrollback.
- Reads `~/.claude/sessions/*.json` first, so live sessions are never touched.
- Disables the mtime-keyed retention sweep and backs the store up out-of-tree
  before any mutation - that sweep can silently delete kept sessions, and any
  tool that touches a file's mtime can trip it.

See `SKILL.md` for the full procedure. Companion: `recover-deleted-sessions-ext4`,
for when sessions are already lost.
