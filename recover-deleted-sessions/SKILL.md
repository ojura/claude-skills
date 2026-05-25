---
name: recover-deleted-sessions
description: >
  Recover Claude Code session transcripts that were deleted / lost / rm'd / find -delete'd
  / vanished from ~/.claude/projects/<slug>/. Volatility-ordered triage: stop writes to the
  affected fs, check existing backups + the ext4 journal, dump LIVE claude --resume process
  memory and webview/CDP state before those processes exit, then raw-disk carve, then
  journal-extent recovery, then merge + dedupe and restore only on explicit OK. Trigger when
  the user says session JSONLs were deleted, a cleanup/rm wiped ~/.claude/projects, the
  session picker lost conversations, or "get my .claude back".
---

# Recover deleted Claude Code sessions

Session transcripts live as `~/.claude/projects/<slug>/<sessionId>.jsonl` (plus per-session
`<sessionId>/subagents/` and `<sessionId>/tool-results/` dirs). When a batch is deleted with
`find -delete` / `rm -rf` (no trash, ext4 zeroes the inode on unlink), the only paths back are
backups, the ext4 journal, live process / webview memory, and raw-disk block carving. **The
recovery rate is dominated by volatility: every block freed by the delete is a candidate for the
next write, and every running `claude --resume` process is holding session content in RAM that
evaporates the moment it exits.** Order the work by what decays fastest.

This skill is distilled from one real incident: 55 session JSONLs (~592 MB) deleted from
`~/.claude/projects/<slug>/`, recovered to 60/66 sessions (91% session-count, ~100%
conversation-content for backed-up sessions). The proven scripts are bundled in `scripts/` (see
"Toolkit" below); the steps below are the pipeline that recovered that store, with the ordering
mistakes it made the first time corrected.

**How the deletion happened (the actual root cause).** The deleter was Claude itself. The user
asked to "clean up all side tracks **in the jsonl** other than this one" - meaning prune dead
in-file branches within the *current* session JSONL. Claude misread it as "delete all the OTHER
session JSONL files," asked "Deleting all but ours?", then **executed `find -delete` in the same
turn without waiting for the answer** (a self-answered rhetorical confirm). The lesson that seeds
everything below: a confirmation question you answer yourself is not a confirmation. Never run a
batch `find -delete` / `rm -rf` against `~/.claude/projects/` on your own initiative.

## STOP - the order is the whole game. Read this before touching anything.

The prior recovery ran its passes in the **wrong order** and lost content to it. The journal-extent
carve, the live-process `/proc/PID/mem` dumps, and the webview/CDP extraction were all bolted on
*after* the raw-disk carve had already been running for an hour. By then some `claude --resume`
processes had context that could have been dumped earlier, and ~1.3 GB of the recoverer's own
scratch writes had landed on the affected filesystem. **Two rules dominate everything else:**

1.  **Never write to the affected filesystem.** Not scratch, not dumps, not the recovered output,
    not a journal dump, not even "harmless" probe files. Every write claims freed blocks and can
    overwrite the very data you are carving. In the real run, dumping a 1.27 GB journal text dump and
    inode tables into a scratch dir in `$HOME` (same nvme as the deleted files) was a self-inflicted
    wound caught two hours later. `/tmp` is **not** automatically safe: on that machine `/tmp` was on
    the same ext4, not tmpfs. Verify with `df -T /tmp`. The genuinely-safe scratch targets are
    `/dev/shm` (tmpfs, RAM-backed) or a **physically separate disk** (a different block device, e.g.
    a mount on `sda3` when the loss was on `nvme0n1p2`).

2.  **Dump the volatile sources first, durable last.** Live process memory and live webview state
    vanish on process exit; disk blocks degrade slowly. So the moment you've contained the bleeding,
    grab the volatile sources *before* the long raw-disk carve, not after.

### What the full record says should have happened sooner (the ordering the real run got wrong)

The real run discovered the high-value moves late and re-derived the right order in hindsight. If
you do nothing else, front-load these five:

1.  **Stop writing to the affected fs (and never start).** The first hour's scratch went to the same
    nvme (`~/recovery/`, a 1.27 GB journal text dump included); `/tmp` was *also* on it. `/dev/shm`
    or a separate disk from the very first command.
2.  **Dump the live `claude --resume` subprocess memory and the 9222 webview iframes immediately**,
    while they're still running - this was done dead last and only after the user pointed at it
    ("It is still running, pre-deletion ... 9222 is webview, 9229 is exthost"). Three deleted-UUID
    subprocesses were holding content the moment they were dumped.
3.  **Capture the ext4 journal as early as possible** - the earliest dump (8 min post-delete) was the
    only one that yielded anything; the fresh one had wrapped past the deletion.
4.  **Run the webview session-list scan early** for `summary`/`fileSize` per UUID - it answers "what
    was this?" and stops you from mislabeling a 14 MB session as an empty ghost.
5.  **Don't restart the app** until volatile extraction is done - restart truncates survivors and
    kills the subprocesses (Step 6.3 + the last hard-limit).

## Step 0 - stop the bleeding (do this literally first)

- **Identify and stop whatever is deleting.** If a script / sweep / your own next command is the
  deleter, halt it. Do not run another destructive command to "clean up".
- **Quiesce writes to the affected fs.** You usually cannot unmount `/`, but you can stop spawning
  writes to it: move all subsequent scratch off the device (next bullet), and avoid restarting apps
  that write under `~/.claude` (the running extension keeps appending to live session files).
- **Pick a safe scratch dir on a different device.** `df -T /tmp` first (it may be on the affected
  fs). Use `/dev/shm` or a separate-disk mount. Everything below writes only there.
- **Do NOT copy or restore anything onto the affected fs yet.** Restoration is the very last step,
  after the user OKs it. Copying recovered files back mid-recovery overwrites freed blocks you still
  need.
- **Get the deleted UUID list.** The current (surviving) session's own JSONL records every sibling
  filename it ever `ls`'d. Grep it:

    ```sh
    grep -oE '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.jsonl' \
      ~/.claude/projects/<slug>/<live-session>.jsonl | sort -u > known_uuids.txt
    ```

  Split into `deleted_uuids.txt` (gone) vs the survivors still on disk. This list seeds every
  later pass.

**Why first:** writes to the freed blocks are the single biggest controllable loss factor, and the
deleter may still be running. In the real run the recoverer spent the first hour writing scratch to
the affected nvme; the bracket-overlap analysis later showed those writes landed in session-adjacent
free regions. Containing this is cheap and irreversible-if-skipped.

**Don't waste time on `debugfs lsdel` / `extundelete` first - it was tried and failed.** The very
first recovery attempt was `debugfs -c -R 'lsdel'`. It returned only old March/April inodes (capped
at 100 results) and nothing from the just-deleted batch, because (a) ext4 zeroes the inode's extent
tree on unlink so there's nothing to follow, and (b) the filesystem was **96% full**, so the freed
blocks were already prime reuse candidates. Treat `lsdel` as a 30-second confirmation that undelete
is dead, then move to content-pattern carving. Do not burn the volatile window on it.

## Step 1 - cheap wins: existing backups and the ext4 journal (a backup can moot the carve)

Before any expensive carve, check for a full pre-deletion copy. A single backup recovers a session
**byte-perfectly**, which no carve can match.

- **Out-of-tree archive / backup dirs.** `~/claude_archive`, `~/claude-archive`, any user backup
  disk, btrfs/zfs snapshots (`btrfs subvolume list`, `zfs list -t snapshot`). In the real run,
  `~/claude_archive/` happened to hold 8 byte-perfect session copies (a side effect of an unrelated
  redact-script run) plus 2 `.pre-stitch-bak` files - those 8 are the only sessions that came back
  at 100%. **Also check `*.bak`, `*.preredact*`, `*.pre-stitch-bak` siblings.**
- **App-specific exports.** If the sessions correspond to another tool's storage (e.g. Antigravity
  `.pb` conversation files), those may be a full backup of the same content under a different format.
  In the real run this was a **dead end checked twice**: the Antigravity `.pb` files (AES-256-GCM,
  hardcoded key) were decrypted and grepped for the deleted UUIDs - **zero** hits. Antigravity keeps
  its own trajectory storage and never absorbs claude-code sessions. Worth the quick check; don't
  expect a hit. (First attempt used CBC and produced garbage / a false "0 hits" - re-verify with the
  *correct* cipher before concluding a clean miss; see `[workflow.verify-outcome]`.)
- **Sidecar logs that index sessions by ID (cheap, and they double as a "was this session even
  non-empty?" oracle).** Two files log per-session activity and survive in `~/.claude`:
  `~/.claude/history.jsonl` (every user prompt, tagged by sessionId) and
  `~/.cache/claude-cli-nodejs/.../mcp-logs-*` (every MCP tool call, tagged by sessionId). They rarely
  hold full conversation prose, but they're the fastest way to (a) discover sessionIds you didn't
  know existed, and (b) **prove a fully-lost UUID was genuinely empty** - in the real run the 3
  truly-unrecoverable sessions had zero mentions in both, confirming they were aborted/near-empty
  rather than lost-with-content. Also check VS Code edit history `~/.config/Code/User/History/`
  (timestamped per-file backups if the user ever opened a jsonl in an editor tab) and the IDE
  globalState `state.vscdb` (sqlite; holds session-list metadata, sometimes titles).
- **ext4 journal, captured ASAP.** The journal holds recent metadata transactions including
  pre-deletion inode snapshots (with extent trees pointing at the original data blocks). It wraps
  fast, so an **earlier** dump is strictly better - but dump it to safe scratch, never the affected
  fs. `debugfs -c -R 'stat <ino>'` and `logdump` are read-only and fast. (Full parse is Step 5;
  here you just preserve the bytes while they're fresh.)

If a backup covers everything the user cares about, you may be done. Otherwise continue.

## Step 2 - VOLATILE sources: live process memory + webview, before processes exit

This is the step that was done LAST in the real run and should have been near-first. At the time of
deletion, the Claude Code extension was still running with multiple `claude --resume <sid>`
subprocesses - each had loaded a (now-deleted) session at startup and held its content in memory.
Three of those subprocesses were for *deleted* UUIDs. They were only dumped at the very end, after
the user pointed at them ("It is still running, pre-deletion ... 9222 is webview, 9229 is exthost").
**A process that exits between deletion and your dump takes its session content with it.**

Order within this step, fastest-decaying first:

1.  **`/proc/PID/fd` open handles (seconds-to-minutes).** A still-open fd to a `(deleted)` file
    reads the full original content via `cp /proc/PID/fd/N out`. Usually already closed for session
    JSONLs (Claude closes them after parse), but free to check and a jackpot when it hits:

        ```sh
        lsof +L1 2>/dev/null | grep -i claude          # deleted-but-open files
        find /proc/*/fd -lname '*<slug>*' 2>/dev/null   # fds into the project dir
        ```

2.  **`claude --resume` subprocess memory (until the process exits).** Find the live subprocesses,
    map each PID to its `--resume <sid>`, and dump its anonymous `rw-p` regions from `/proc/PID/mem`
    to safe scratch:

        ```sh
        ps -ef | grep -E 'claude.*--resume' | grep -v grep   # PID -> sessionId
        cat /proc/sys/kernel/yama/ptrace_scope                # 0 = unprivileged read OK; else sudo
        # then dump rw-p ranges per /proc/PID/maps via /proc/PID/mem
        ```

    Parse with `scripts/parse_proc_dumps.py` (regex `"sessionId":"<uuid>"` +
    brace-balance line extraction). **Caveats:** (a) process memory holds only what that process
    loaded, and live in-flight messages are in a **reshaped (React/camelCase) schema** that does
    **not** carry `sessionId` per record - so this yields a *subset*, plus post-deletion turns the
    running process produced after the delete. (b) **Sizing surprise:** RSS reads ~194 MB but a naive
    `/proc/PID/maps`-walk dump came out **3.5 GB per process** because V8 reserves huge anonymous
    ranges that are mostly unbacked zero pages; cap each range read (the real run used a 2 GB/range
    cap, ~16-23 GB across all processes). Reading `/proc/PID/mem` is non-destructive - it does not
    disturb the process. This source is uniquely valuable for sessions with no disk copy.

3.  **Webview renderer CDP state (port 9222) - this is the rich source, not the exthost.** Per the
    playbook in `github.com/ojura/claude-patches` (`docs/debugging.md`): the renderer is CDP port 9222, the
    extension host 9229 (+ ephemeral per-window node-inspector ports). **Sequencing correction from
    the full record:** the exthost (9229) singleton manager's `messages` Map was nearly empty (it
    held ~6 messages for one session, not the flat all-sessions map the playbook implied). The full
    rendered conversation lives in the **chat-panel iframes inside the 9222 renderer**, one iframe
    per open panel. Each iframe's React app has a session manager; React-fiber-walk for it
    (`looksLikeMgr` = has `getSession`/`sendRequest`/`listSessions`), then dump
    `mgr.sessions[*].messages.peek()` for each *already-loaded* session. In the real run this is what
    actually recovered the 3 deleted-UUID sessions that were open (35 / 3 / 500 live messages).

    **The session-list scan is separately priceless.** `mgr.sessions[]` carries metadata for *all*
    sessions (46+ unloaded ones too): per-entry `summary` (the session's first user prompt),
    `fileSize`, `lastModifiedTime`, git branch. This is where the "what was this lost session about?"
    answer comes from - and it **overturned a wrong conclusion**: 3 of the "6 fully lost" UUIDs
    turned out to have 12-15 MB `fileSize` and real summaries (substantive `timepoint.h` / `angle.h`
    sessions), not the near-empty ghosts an earlier pass had assumed. Always run the session-list
    scan before declaring a UUID empty. (These summaries are webview-memory-only Preact signals; no
    on-disk cache mirrors them.)

    **Order-sensitive gotcha (still applies):** do NOT call the loader (`getSession`) on a session
    whose file is deleted *while you might still recover it by other means*. `getSession` invokes the
    loader (`Wz4`) which reads from disk with no intermediate cache; worst case it clears/resets the
    in-memory state for that session *before* failing on the missing file, wiping the very content
    you're extracting. Extract already-loaded state first; only call `getSession` once the user
    confirms it's safe (in the real run the user explicitly said "now it's safe"), and even then it
    won't conjure content for a deleted file with no running subprocess.

4.  **Transport detail for CDP scripts.** Write the `.mjs`/`.js` CDP-eval helpers and their JSON
    output to `/dev/shm` (tmpfs), never the affected fs - the playbook's `/tmp` examples are unsafe
    when `/tmp` is on the deleted-from device. Helper scripts (`cdp-eval.mjs`,
    `eval_in_inner_frame.mjs`) and the fiber-walk recipe are in `github.com/ojura/claude-patches` (`docs/`).

**Why before the carve:** these sources are the only ones with a deadline measured in
process-lifetime, not block-overwrite-rate. Dump them while the long disk carve runs in the
background, but *start* them first.

## Step 3 - raw-disk carve (the workhorse; sooner = more, as blocks degrade)

Inodes are zeroed on ext4 delete, so there is no undelete and no extent tree to follow. The carve
is purely content-pattern-based: every surviving JSONL line still contains `"sessionId":"<uuid>"`,
so grep the raw block device for those byte patterns, read a window around each hit, and extract
whole JSONL lines.

1.  **Build the pattern file** (`sessionId":"<uuid>` per deleted UUID) and benchmark grep speed on a
    few GB to estimate total time (a 1.8 TB nvme carved at ~1.6-2 GB/s ≈ 15-20 min).
2.  **Full-device scan to byte offsets**, output to safe scratch:

        ```sh
        sudo dd if=/dev/<affected-part> bs=4M status=progress \
          | rg --null-data -aob -f patterns.txt > raw_matches.txt
        ```

3.  **Carve** with `scripts/recover.py`: cluster nearby offsets, `dd`-read
    `±512 KB` windows, split on `\n`, JSON-parse each line, keep lines whose `sessionId` matches,
    dedupe by `.uuid` (md5 fallback for id-less lines), sort by timestamp.

Notes from the real run: a **5.4x on-disk replication factor** (journal + page cache + block reuse)
is why carve coverage hit ~94% of lines even after a delete - many lines survive in several copies.
`recover_v2.py` tried a wider 4 MB window + parent-uuid linking to grab `sessionId`-less records;
it gained essentially zero over v1 (those records are recovered by Step 5 instead). Use v1.

## Step 4 - other block sources (cheap to add, usually low yield, but check)

Same `dd | rg | recover.py` pipeline pointed at: the **swap partition** and **swapfile** (freed
pages aren't zeroed). In the real run both `/swapfile` (128 GB) and the swap partition (64 GB)
yielded **0 matches** - low probability, but the scan is cheap and parallelizes with Step 3.
`scan_swap.sh` / `scan_swappart.sh` drive these. Skip btrfs/nextcloud/disk-image mounts unless a
filename or `.claude` dir actually turns up there.

## Step 5 - journal-extent recovery (recovers the sessionId-less records the grep anchor misses)

The grep anchor (`"sessionId":"<uuid>"`) misses records that carry no `sessionId`: chiefly
`file-history-snapshot` lines (IDE file-tracking; ~6% of lines, no `sessionId`/`parentUuid`/`lpu` at
all) and some `system`/`compact_boundary` meta. The ext4 journal holds pre-deletion **inode
snapshots** whose extent trees point at the original, contiguous data blocks of each deleted file -
so you can read a whole file's blocks in order, not just the grep-hit windows, and pick up the
id-less lines by position.

Pipeline (all in `scripts/`):

1.  `parse_journal.py` - scan the journal dump for ext4 inode patterns
    (`uid=1001`, regular file, extent-header magic `0xf30a`, plausible size). **Gotcha that cost a
    pass in the real run:** session JSONLs are mode **0600** (`-rw-------`), not 0644 - a filter
    hardcoded to `0x81a4` (0644) rejected every session inode. Verify the actual mode of a surviving
    session file first and match it.
2.  `journal_match_to_session.py` - cross-reference each candidate inode's extents against the
    carved match offsets per UUID; attribute the highest-overlap inode to each UUID.
3.  `dump_journal_extents.py` - `dd` each attributed inode's extents once into
    `journal_extents_raw/<uuid>.bin` (to safe scratch).
4.  `carve_journal_extents.py` - parse those bins, keeping `sessionId == uuid` **or
    `sessionId == null`** records (the id-less ones), merged into `recovered_v4/<uuid>.jsonl`.

This brought the 8 backup-backed sessions from ~94% to ~100% line coverage (all 59
file-history-snapshots per session) and added ~700 snapshots + ~800 conversation records across 21
sessions. **Prefer the earliest journal dump you took** - in the real run a 15 MB dump captured ~8
min after deletion was closer to the pre-delete state than the fresh one (the user had to prompt:
"Haven't you observed the old journal dump?"). Watch for a botched dump: `dd ... skip=40011` against
the *device* reads FS block 40011, not journal block 40011 - the journal lives at the FS block the
superblock's journal inode points to (`debugfs -R 'stat <8>'`), often hundreds of millions of blocks
in. Confirm JBD2 magic `0xc03b3998` at 4 KB boundaries before trusting a "journal" dump.

**Two more things the journal pass taught:**

- **The attributed inode's original size is your per-session recovery ceiling.** Once you've matched
  a journal inode to a UUID, its `i_size` is the pre-deletion file size. Comparing it to the carve
  tells you how much is truly gone vs. just not-yet-carved: `b49eacf8` showed 64 MB original vs.
  7.8 MB carved (8.6x overwritten), `498c5362` 15.6 MB original vs. 2 KB carved. This is how you
  honestly answer "how complete is this session?" without a backup baseline.
- **A live journal that has wrapped past the deletion event yields nothing**, even on an idle
  system. The fresh 1 GB dump produced 79k-478k inode snapshots but **zero** attributable to deleted
  sessions - intervening hours of normal metadata churn (services, atime updates, even the carve's
  own reads) had overwritten the deletion-era transactions. The win came only from the *older* dump.
  And: ext4 ORDERED journaling records metadata, not file *contents*; the deletion transaction itself
  captures the zeroed inode, not the pre-deletion data - you need a snapshot from *before* the
  delete, which is exactly why the earliest dump matters.

## Step 6 - merge, dedupe, validate, then restore only on explicit OK

1.  `merge.py` - per UUID, prefer the byte-perfect archive copy as canonical, then fold in carved +
    journal-extent + live-webview records not already present; sort by timestamp.

    **Dedup-key footgun (cost a whole bad pass - v5).** Records with a `.uuid` dedupe by `.uuid`.
    But `file-history-snapshot` records have **no `.uuid`** - and you cannot fall back to their
    `.messageId`, because claude-code sets a snapshot's `.messageId` equal to the *triggering*
    message's `.uuid`. A dedup keyed `.uuid // .messageId` therefore collides each snapshot with its
    trigger message and silently collapses distinct records (one session dropped from 5826 to 658
    lines). **Dedup `file-history-snapshot` by full-line md5**, never by any id field. Also: when the
    same logical `.uuid` appears as multiple byte-different on-disk copies (different write
    generations / partial overwrite), keeping the *longest* copy preserves the most content.

2.  **Validate**: every output line parses as JSON (half-overwritten lines fail JSON parse and are
    silently dropped by every parser above - that is expected and unavoidable, but confirm the *kept*
    lines are 100% valid). Compute line coverage against any backup-backed session as ground truth.
    **Sandbox gotcha:** under the bash sandbox `jq file.jsonl` hits permission-denied opening files
    directly; pipe via `cat file.jsonl | jq -c .` instead.

3.  **Repair surviving sessions that the running app truncated - they are NOT safe to skip.** This is
    the step the truncated note missed entirely. If the app was restarted at any point (or a
    `claude --resume <sid>` subprocess kept running through the deletion), that subprocess **rewrites
    its own session file from its compacted in-memory state** - producing a *truncated stub* on disk
    (e.g. one substantive session went from 412 live messages to a 17-line on-disk stub). These survivors were never
    in your deleted-UUID set, so a blind no-clobber restore leaves the truncated stub in place and
    loses content. Repair them: keep every record currently on disk (post-restart turns you don't
    have elsewhere), then fold in proc-memory + live-webview records by `.uuid`. The proc-memory
    records are drop-in JSONL; the webview records are reshaped camelCase (tag them `_source:
    "live_webview"`, they render but aren't byte-identical). Drivers: `merge_survivors.py` (proc +
    reshaped webview) and `merge_survivors_safe.py` (proc-only, conservative). Back up the truncated
    stub to safe scratch first. Verify which survivors actually lost content vs. which were correctly
    clobber-skipped (in the real run 2 of 5 survivors needed repair, 3 were clean).

4.  **Restore is the LAST action and needs explicit user OK.** Copy `final/*.jsonl` back with
    **no-clobber** (`cp -n`), never overwriting a surviving session or the still-live session file.
    List conflicts and the new-write set, get a go/no-go, then copy. Match the original mode
    (sessions are typically 0600). **chmod-clobber warning:** do NOT blanket `chmod 644 *.jsonl` in
    the destination - it relaxes the live session and surviving files from 0600 to 0644 (harmless to
    operation but changes perms you didn't mean to). chmod only the files you wrote. The running
    patched extension resolves cross-file `logicalParentUuid` lineage at render time (Patches D/J/K),
    so restoring forks as-is is sufficient - **do not** bake sibling content into the JSONLs. (If the
    extension is *vanilla*, i.e. no `pfg-v*` markers in `extension.js`, cross-fork backfill won't
    happen at render time; the `.jsonl` files are still correct, the user just installs the patches
    later to get the tree-spanning view.)

## Toolkit (bundled in `scripts/`)

The working scripts from the real recovery, scrubbed for reuse. Each lifts its machine-specific
values (the affected device, a scratch `RECOVERY_DIR`, your uid, your project dir) to CONFIG
constants / env vars at the top - set those before running. They are forensic one-offs, not a
turnkey CLI: read the matching Step above, then run the script and adjust.

- `recover.py` - raw-disk carve (multi-source: main fs / swap / swappart)
- `recover_v2.py` - wider-window + parent-uuid-linking carve; ~zero gain over v1, kept for reference
- `parse_journal.py` - find ext4 inode snapshots in the journal dump
- `journal_match_to_session.py` - attribute journal inodes to deleted UUIDs by extent overlap
- `dump_journal_extents.py` - `dd` attributed extents to `<uuid>.bin`
- `carve_journal_extents.py` - extract sessionId-less records from the extent bins (v4)
- `carve_v5_from_bins.py` - re-parse pre-dumped extent bins offline (no sudo); longest-copy dedup
  experiment - it's where the `.messageId`-collision dedup footgun was found, kept as a cautionary
- `parse_proc_dumps.py` - extract session records from `/proc/PID/mem` dumps (sessionId-anchored)
- `parse_replicated.py` - re-parse all process dumps anchored on `.uuid` (not sessionId) to catch
  records the sessionId anchor misses; net gain was ~1 record for deleted sessions (the rest were
  the live session's own in-flight messages)
- `merge.py` - merge archive + carved + journal + live into `final/<uuid>.jsonl`
- `merge_survivors.py` / `merge_survivors_safe.py` - repair restart-truncated surviving sessions
  (proc + reshaped webview / proc-only)
- `scan_swap.sh`, `scan_swappart.sh` - swap carve drivers

Working dirs the pipeline produces under `RECOVERY_DIR` (keep it on safe scratch - `/dev/shm` or a
separate disk): `process_dumps/` (`/proc/PID/mem` dumps), `journal.bin` (the captured ext4 journal),
`journal_extents_raw/` (`<uuid>.bin` extent dumps), `recovered/` + `recovered_v4/` (carved JSONL),
`final/` (merged per-UUID output), `pre_merge_backups/` (truncated survivor stubs, backed up before repair).

## Gotchas and hard limits

- **ext4 zeroes the inode on delete - there is NO undelete.** `debugfs lsdel` / `extundelete` find
  the inode numbers but the extent trees are gone (`Corrupt extent header`), so they can't
  reconstruct files. Carving by content pattern is the only path; tools like testdisk/photorec are
  not the answer for JSONL.
- **Extent-boundary lines can't be stitched without inode metadata.** A JSONL line whose bytes span
  two physically-discontiguous extents (first half at disk offset 1 GB, second at 200 GB) can't be
  rejoined by the windowed carve; only the journal inode's extent map could locate the second half,
  and only if that inode snapshot survived.
- **Half-overwritten lines fail JSON parse and are silently dropped.** A line whose blocks were
  partially reused post-deletion won't parse and is fundamentally lost. This is the main source of
  the per-session gap.
- **`/proc/PID/mem` holds only loaded sessions, in a reshaped schema.** Live in-flight messages are
  React-camelCase and carry no per-record `sessionId`; the dump is a subset of disk content, valuable
  mainly for sessions with no disk copy and for post-deletion turns.
- **Swap/swapfile yielded 0** in the real run. Cheap to scan, low expected yield.
- **`/tmp` may be on the affected fs.** Always `df -T /tmp`. Safe scratch = `/dev/shm` (tmpfs) or a
  separate physical disk.
- **`ripgrep --null-data` can silently truncate matches.** NUL is the record separator; raw free
  space has huge NUL-free stretches that can exceed rg's per-record heap, dropping matches without an
  obvious error. Check the running rg's stderr for buffer warnings; the carve coverage was fine in
  the real run but this is a verify-don't-assume point.
- **"Fully lost" is not the same as "was empty" - run the webview session-list scan before
  concluding either.** An earlier pass declared 6 UUIDs lost-and-near-empty; the webview manager's
  `mgr.sessions[].fileSize/summary` then showed 3 of them were 12-15 MB substantive sessions whose
  blocks were simply overwritten. Only the *other* 3 (zero mentions in `history.jsonl` + mcp-logs +
  webview list) were genuinely near-empty. Don't downgrade a loss to "didn't matter anyway" without
  that cross-check.
- **You can quantify your own self-inflicted damage with a bracket-overlap test.** If you wrote
  scratch to the affected fs (Rule 1 violation), get your scratch files' physical extents
  (`debugfs stat <ino>`) and check whether deleted-session match offsets *bracket* a scratch extent
  (matches densely before AND after, gap inside = you overwrote the middle). In the real run 1,756
  matches sat within ±1 MB of scratch extents but **zero deleted sessions showed the bracket
  pattern** - the ext4 allocator had placed the scratch in fresh free runs adjacent to, not through,
  session data, so actual damage was <1%. Useful for an honest post-mortem, not a substitute for not
  writing in the first place.
- **The running extension keeps appending to the live session file** during the whole recovery -
  that's normal and must not be interrupted; just don't restart it or open the deleted sessions in it
  while volatile extraction is pending. **Restarting the app is actively harmful mid-recovery**: it
  kills the `claude --resume` subprocesses (taking their in-RAM session content with them) AND makes
  surviving sessions get rewritten as truncated compaction stubs (see Step 6.3). Dump volatile
  sources *before* any restart.

## Prevention pointer

The default retention sweep (`cleanupPeriodDays`, default 30) silently deletes session JSONLs whose
file mtime is older than the window - the most common *non*-accidental cause of this exact loss.
Disable it in `~/.claude/settings.json` (`{ "cleanupPeriodDays": 3650000 }`; never `0`, which deletes
everything) and keep an out-of-tree backup of `~/.claude/projects/` on a separate device. A backup is
the only thing that turns this whole carve into a one-line restore.
