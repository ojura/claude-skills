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
  mtime-neutrally - titling restores each file's own prior mtime, so it never
  *worsens* a file's sweep exposure (it doesn't make an already-old file safe;
  it just doesn't make a recent one look old).

## Safety

- **Move, never delete.** Everything goes to `~/claude-archive` with a manifest
  that restores any file to its original path.
- **lpu-safe.** Never orphans a kept session's cross-file or phantom-backfill
  ancestors, so a moved file can't break another session's scrollback.
- Reads `~/.claude/sessions/*.json` first, so live sessions are never touched.
- Has you disable the mtime-keyed retention sweep (a user-gated step) and back the
  store up out-of-tree before any mutation - that sweep can silently delete kept
  sessions, and any tool that touches a file's mtime can trip it. Disabling the
  sweep is necessary but not sufficient on its own (it's bypassable), so the
  out-of-tree backup is the real guard.

See `SKILL.md` for the full procedure. Companion: `recover-deleted-sessions-ext4`,
for when sessions are already lost.
