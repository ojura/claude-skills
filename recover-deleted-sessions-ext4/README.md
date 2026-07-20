# Recover deleted Claude Code sessions (ext4)

Recover session transcripts (`~/.claude/projects/<slug>/<sessionId>.jsonl`) that were deleted by
`rm`, `find -delete`, or a retention sweep. The recovery rate is dominated by what decays fastest,
so the order of operations is the whole game.

## What it does

- Stops writes to the affected filesystem (every freed block is a candidate for the next write)
  and pulls the deleted-UUID list from a surviving session's own log.
- Grabs the volatile sources first: open file descriptors, live `claude --resume` process memory
  (`/proc/PID/mem`), and the webview renderer's in-memory session state over CDP - all gone the
  moment those processes exit.
- Takes the cheap byte-perfect wins (out-of-tree backups, the ext4 journal) before any carve.
- Carves the raw block device for surviving JSONL lines by content pattern, then recovers the
  id-less records the grep anchor misses via ext4 journal inode extents.
- Merges, dedupes (with the footguns that cost real passes called out), validates, repairs
  app-truncated survivors, and restores only on explicit OK with no-clobber.

## Scripts

`scripts/` holds the proven toolkit, scrubbed for reuse - each lifts machine-specific values
(affected device, scratch dir, uid, project dir) to CONFIG constants / env vars at the top. They
are forensic one-offs, not a turnkey CLI: read the matching step in `SKILL.md`, set the config,
then run.

## Never write to the affected filesystem

Not scratch, not dumps, not the recovered output. Use
`/dev/shm` or a separate disk, and dump the volatile sources (process memory, webview) before the
long disk carve, not after.

Prevention, so you never need this: the companion skill `archive-conversation-forks` covers
disabling the mtime-keyed retention sweep and keeping an out-of-tree backup. A backup turns this
whole carve into a one-line restore.
