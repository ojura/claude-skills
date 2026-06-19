---
name: archive-conversation-forks
description: >
  Categorise a project's Claude Code session JSONLs into themes, find the canonical
  fork per family, thoroughly document what the non-canonical forks uniquely hold, then
  MOVE (never delete) the non-canonical forks to ~/claude-archive. lpu-safe: never
  orphans a kept session's cross-file logicalParentUuid ancestors, including phantom-lpu
  backfill sources. Trigger when the user asks to clean up / categorise / dedupe their
  conversations and forks, or declutter the session picker.
---

# Archive conversation forks

Group a project's session JSONLs into themes, pick the canonical session per fork family,
document the non-canonical forks (with concrete jsonl uuids), and move the non-canonical
ones to `~/claude-archive`. Move, never delete; everything stays recoverable.

## STOP - disable the retention sweep first, or this skill will delete sessions

Claude Code silently deletes session transcripts whose **file mtime** is older than
`cleanupPeriodDays` (default **30**). The deleter is `unlinkIfOld` in `src/utils/cleanup.ts`
(`cleanupOldSessionFiles` walks `~/.claude/projects/<slug>/` and `unlink`s any `.jsonl` / `.cast`
with `stat.mtime < now - cleanupPeriodDays`). No prompt, no trash, no recovery.

Setting a session's mtime to its **true last-activity date** (to keep the recency-sorted picker
chronological) is what makes this lethal: for any session older than the retention window, that
makes it look old enough to be swept, and it is **deleted on the next cleanup**. This is not
hypothetical - doing exactly this deleted 11 sessions in a real run, one unrecoverable. Step 7
therefore titles **mtime-neutrally by default** (it restores each file's own prior mtime, so
titling never changes a file's deletion exposure); the mtime-equalisation pass that writes true
last-activity dates is **opt-in and requires this sweep disabled** (and the user's say-so).

**Before any titling / mtime / archiving work, disable the sweep** in `~/.claude/settings.json`:

```json
{ "cleanupPeriodDays": 3650000 }
```

`3650000` (~10,000 years) pushes the cutoff so far back that `mtime < cutoff` is never true; the
schema is `z.number().nonnegative().int()` with no max, so it validates. **Do NOT use `0`** -
`cleanupPeriodDays: 0` sets the cutoff to *now* (deletes ALL transcripts) and flips
`shouldSkipPersistence` (stops writing new transcripts); it is the most destructive value, not
"off". A long-running session caches settings, so reload the window after changing it. The
default mtime-neutral titling is safe even with the sweep active; only the opt-in
mtime-equalisation pass needs the sweep disabled, so if you cannot disable it, do not equalise.

**The large-value workaround is necessary but NOT sufficient - it can be bypassed.** As of Claude
Code **2.1.x** (issues #41458 / #45735, confirmed against decompiled source; re-verify against the
current version, since an upstream fix would make this claim version-specific while leaving the
backup hook valid as defence-in-depth), any process started with **restricted setting-sources**
runs the sweep with the **default 30**, ignoring `cleanupPeriodDays` from
`~/.claude/settings.json` entirely. The bypass paths are
`--setting-sources local`, and the SDK's `settingSources: []` - **which includes autonomously
spawned subagents.** This skill itself spawns subagents (Steps 4-5), so a bypassing sweep can
fire *during a run* and delete old-mtime sessions even with the setting maxed. Two further
hazards in the same bug cluster: the sweep keys on file mtime, so any tool touching mtime (a
restore, a sync, Step 7's opt-in equalisation pass) can re-arm deletion; and a transcript restored into
`~/.claude/projects/` without its session-index metadata entry can be treated as an orphan and
re-deleted. **Do not trust the setting as your only guard.** Use both belt-and-suspenders
mitigations below, each independent of the buggy setting:

1.  The out-of-tree backup in Safety invariants (back up the whole project dir to a separate device *before* any mtime / titling / move work).

2.  A `SessionStart` backup hook that copies transcripts - **every `*.jsonl`, however small; small does not mean unimportant** - out of the deletion path on every session start, so even a bypassing sweep cannot cause permanent loss. The on-disk JSONL is **append-only**: `/rewind` does not truncate the file, it orphans a dead branch and keeps appending (the *rendered* conversation shrinks, the file does not; verified in source - `recordTranscript` UUID-dedup skips already-written messages, and the comment at `sessionStorage.ts` is explicit that rewind "leaves an orphaned chain branch in the append-only JSONL forever"). So the latest file is a superset of every earlier state, and **one high-water-mark backup per file is lossless**: copy only when the live file is missing from the backup or has **grown**. Keying on grew-only (not "differs") is what makes it mtime-immune and shrink-safe - the skill's equalisation changes only mtime (size unchanged → skip), and the lone shrink path in the source (`ftruncate` tombstoning of *failed* streaming attempts, never real content) can't clobber the larger backup. In `~/.claude/hooks/backup-sessions.sh`:

    ```bash
    #!/usr/bin/env bash
    BACKUP_DIR="$HOME/.claude-session-backups"
    sz() { stat -c%s "$1" 2>/dev/null || stat -f%z "$1" 2>/dev/null; }   # GNU || BSD/macOS
    find "$HOME/.claude/projects" -name '*.jsonl' 2>/dev/null | while read -r f; do
      dst="$BACKUP_DIR/${f#$HOME/.claude/}"
      s=$(sz "$f"); d=$(sz "$dst")        # ${:-0} guards a file that vanished mid-walk (caught next run)
      # copy iff the backup is missing or the live file GREW (append-only -> grew = new content)
      if [ ! -f "$dst" ] || [ "${s:-0}" -gt "${d:-0}" ]; then
        mkdir -p "$(dirname "$dst")"; cp "$f" "$dst"
      fi
    done
    ```

    wired in `~/.claude/settings.json` as `{"hooks":{"SessionStart":[{"hooks":[{"type":"command","command":"~/.claude/hooks/backup-sessions.sh"}]}]}}`. Because it never overwrites with a smaller file, no rewind, compaction, or mtime edit can shrink the backup below the largest state ever captured - which, for an append-only file, contains all real content. (A content-hash-per-state variant - one backup file per distinct hash, never overwrite - is maximally defensive if you distrust the append-only guarantee, but it costs a full copy of the (growing) file at every session-start it changed - total storage O(distinct states x size), which balloons for a long session. Under the verified append-only behaviour it preserves nothing high-water doesn't, so it is not the default.) **Re-verify the append-only premise against your Claude Code version**: this entire high-water guarantee rests on the JSONL being append-only (largest state = superset of every earlier one), cited to `patches.md` and true for current versions. If a future version ever compacts *in place*, high-water backup would silently lose content - so treat append-only as a version-specific claim to re-check (exactly like the setting-sources bypass above), not a permanent invariant; when in doubt, use the content-hash variant. **Cost note:** the hook `find`s ALL of `~/.claude/projects` (every project) on every session start once wired - defence-in-depth, but a standing per-launch cost; scope the `find` to the project dir you're working if that matters.

---

## READ THIS FIRST - the safety rule that makes the task non-trivial

Sessions link across files via `logicalParentUuid` (lpu) on `compact_boundary` records
(`type:"system", subtype:"compact_boundary"`, written with `parentUuid:null`). An lpu
resolves **in-file**, **cross-file** (the target uuid lives in a sibling JSONL), or
**phantom** (the target uuid was never persisted anywhere, a write-side bug). If the user
runs the `claude-patches` set (Patches D/J/K), the extension reconstructs a session's
rendered history by reading sibling files cross-file, and even backfills a session's origin
from a sibling that shares the same phantom lpu.

**So a short or old "non-canonical" file is often the structural ancestor a newer canonical
session needs for scrollback or origin display. Moving it out of the project dir silently
breaks the canonical's transcript.** This is the "dropping lpus" failure the skill exists to
prevent. Do NOT decide archival by content length alone.

**Nor by containment percentage.** Archive is **a different axis from the markers, and exclusive of
all of them**: a file is either RETAINED (and then carries exactly one of `[main]` / `[fork]` /
`[scroll-dep]` / none) **or** ARCHIVED (moved out of the picker) - never both. The retain-vs-archive
split comes **first**; a marker is assigned only to what's kept. That first split is gated by
load-bearing status: **a load-bearing file can never be archived** - a kept or live session
reconstructs scrollback from it cross-file, so moving it orphans that session, the worst outcome.
Containment % is necessary-but-not-sufficient and never the gate: a 99.6%-contained file can still
be load-bearing. Only a file that is *both* not-load-bearing *and* redundant is an archive
candidate; a load-bearing ~0-unique file is kept and marked `[scroll-dep]`. (Real miss: an operator
offered "archive OR mark `[scroll-dep]`" as interchangeable choices for a 99.6%-contained file - they
are not choices for the same file; load-bearing decides retain-vs-archive first, and that file was
load-bearing for the *live* session. "99.6% contained" never answers "is it load-bearing"; only lpu /
phantom dependency does.)

There are **two** classes of load-bearing file, and you must protect both:

1.  **Cross-file lpu targets.** File B is load-bearing if some other file references (via lpu) a uuid that B owns. Easy to compute from `global_uuid`.
2.  **Phantom-lpu backfill sources.** When a kept session has a `compact_boundary` whose lpu is phantom and which sits at the chain root (no in-file pre-content), Patch K reconstructs that session's origin from a **sibling that shares the same phantom lpu AND has real messages before its own first boundary with that phantom lpu**. That sibling is load-bearing even though no uuid links to it. Miss this and you silently truncate the kept session's deep scrollback.

   **But do not over-correct.** Locking *every* file that merely references a phantom lpu is wrong: a byte-identical duplicate fork whose phantom boundary sits at the chain head (`parentUuid:null`, zero in-file messages before it) holds no *in-file* pre-content to backfill from, so it is not a load-bearing **source** and is safe to archive. Lock a phantom-sibling only if it is a viable source (has pre-content before its first boundary with that phantom lpu), and you only need to retain **at least one** source per phantom a kept file needs.

**HARD RULE: a `compact_boundary` is never an origin.** It is always a stitch with a real predecessor - in-file, cross-file (Patch J), or phantom-shared (Patch K). A boundary at `parentUuid:null` with nothing before it does NOT mean the session is rootless / standalone / originless; it means its origin is **entirely external** and must be reconstructed - which is the case that most needs lineage work, not the one to dismiss. Concluding "this file carries no origin / is a standalone" from a leading boundary is a hard error: in a real run it produced a wrong family map (forks of one big conversation got mis-read as separate standalones, and a fork's `[main]` was nearly dropped). In code, `nb==0` at a boundary means "origin is external," never "no origin." Safety to archive must come from **content-redundancy** (0 unique vs the kept set), never from a file "looking rootless."

**HARD RULE: keep, `[scroll-dep]`, and archive are ONE decision, made by measurement + read - identical rigor for all three.** Every file gets the same two things: the unique-vs-kept set-difference (a number) and a verbatim read of that unique residue. The skill's archive path already does this; the trap is exempting the *locked* set because it is load-bearing. **Load-bearing protects a file from being *moved*, never from being *measured*.** Three corollaries:

- **Measure the locked set too; never infer content from size.** Run the unique-vs-kept set-difference on every load-bearing file. Load-bearing AND ~zero-unique ⇒ `[scroll-dep]`, established by the number, not by length. (Real miss: a 2142-message locked file was eyeballed as "must be content"; measured, it held 8 unique messages, all policy-refusals and banter.)
- **Redundant structural-role files are archive candidates, not auto-keeps.** When several files can serve one structural role (e.g. two phantom-backfill sources for the same phantom), keep the richest and run the rest through the archive judgment on their read residue. (Real miss: a redundant source survived only by over-keeping; its 45 "unique" were operational noise plus two findings already present in a kept file.)
- **A wrong seed silently skips measurement.** A file wrongly seeded as live (Step 0) or wrongly locked never gets measured - which is how a session 99% contained in the *real* live session stays in the picker. So if a "live" or load-bearing file *measures* ~zero-unique against another, suspect the seed/lock, re-check Step 0, and treat it as the redundant copy it is. "It's big" and "it's load-bearing" never substitute for the number or the read.

The fork/compaction data model (Patches A, D, F, H, J, K) is documented in `github.com/ojura/claude-patches` (`docs/patches.md`); read it first if you have it. If not, the rules above are the operative summary. **Keep the phantom-source locking even on a vanilla install (no `claude-patches`).** A phantom source holds real user/assistant messages that the *current* stock renderer simply fails to stitch into the needer's scrollback - **missing user messages are a rendering bug, not data absence** - and cross-file backfill should land upstream eventually (`claude-patches` does it today). Preserving the sources keeps that content in the store and ready to reconstruct the moment backfill is available; archiving them because today's renderer ignores them would move out real, if currently-unrendered, history. So phantom-source locking is forward-looking insurance, not over-correction. (The "do not over-correct" rule above still holds, but it is narrower than "skip phantoms on vanilla": it frees only byte-identical *non-sources* - forks with zero pre-content before their phantom boundary, which hold nothing to backfill from.) **If sessions have already been lost/deleted, this skill does not recover them - see the companion `recover-deleted-sessions-ext4` skill.**

**HARD RULE: an empty fork-family result does NOT downscope the task.** When the deterministic pass finds
no fork families (every session a standalone single-file tree), the pull is to reframe the whole job as
smaller - "this is fundamentally just debris cleanup and titling, not consolidation" - and skip the
per-file rigor. Resist it. Absence of forks changes NOTHING about the standard: every retained file still
gets the unique-vs-kept measurement, a verbatim read of its residue, the cross-tree prose-overlap pass
(content-forks hide WITHOUT a shared lpu - Step 3), and a title that passes the Step-7 acceptance test.
The hedge words are the tell - "though", "just", "fundamentally", "only", "roughly N sessions" - and each
marks a place effort is about to be dropped. (Real run: two such framings in one session, both caught by
the user; one nearly skipped the prose-overlap pass that then found two real duplicate forks.) This is the
same proxy-substitution the Design rationale warns about: a qualitative goal ("clean up the picker")
quietly swapped for an easier proxy ("move the obvious junk"). Do the whole task at full rigor.

## Step 0 - read the live-session registry (authoritative, do this FIRST)

Before touching anything, read `~/.claude/sessions/*.json`: one JSON per **running** Claude
Code process, `{pid, sessionId, cwd, status, updatedAt}`. Each `sessionId` is a live
conversation; its `<uuid>.jsonl` is **never-touch** (no move, no file-edit, no retitle by
file edit). This registry is the source of truth for what is running right now.

**Do NOT infer live state from file mtime, "modified today," or the task-output tmp path.**
They mislead: `--fork-session` mints a new session id, so the live file can differ from what
recency or the tmp dir suggests (real case: heuristics pointed at `a24119fe` while the
registry showed the live session was `464771a1` - the failure was an agent NOT reading the
registry, never the registry missing a running session). The registry lists one entry per running
process and is authoritative; recent-mtime adds no liveness signal, only noise (opening an old chat to
inspect it bumps its mtime without making it live), so Step 2 uses NO mtime fallback - the registry
alone decides what is live, read here and re-read again at mutation time (Step 6). (Optionally
`os.kill(pid, 0)` to drop a stale `<pid>.json` from a crashed process; treating a stale entry as live merely over-protects, which is benign.)

**HARD GATE - identify the live set ONLY from the registry; never proceed on a guess.** Resolve
each running `sessionId` to its `<sessionId>.jsonl`, confirm the file exists, and take the union as
the never-touch live set. If the registry is missing/unreadable, or a running `sessionId` doesn't
resolve to a file, **STOP and ask** - do not fall back to recency, newest mtime, filename, or "the
one I think I'm in." A registry that reads cleanly but lists nothing running IS a valid empty live set:
proceed with `live={}` once you have confirmed it is genuinely empty (not unreadable). A wrong live set
is not a local error: it seeds the locks, the moves, and the titles, so a bad guess cascades downstream. **Why guessing is uniquely lethal
here:** a live conversation forked from a parent is **fork-shaped** - mostly-contained in that
parent plus a short live tail - so by *content* it is **indistinguishable from an archivable
`[fork]`**. The registry is the only signal that says "this fork-shaped file is live." Guess by
containment and you can mark the live session `[fork]` and **archive the very conversation you are
running in** (real near-miss: a 99.6%-contained file was almost archived that the live session
needed). Note the shape: a live session is **never** a `[scroll-dep]` (it is actively growing, not
a dead ~0-unique bridge) - at most it looks like a fork, which is exactly the trap. (This failure is
the wrong-seed miss made concrete: an agent that guessed its own conversation instead of reading the
registry locked the wrong file and the error cascaded.)

This goes first because every later step (the keep-lock seed, the moves, the titling) depends
on the never-touch set being right, and a fork/restore can revert in-memory or on-disk state
(it has clobbered `MEMORY.md` index lines mid-session), so do not trust assumptions about
what is live - read the registry.

## Step 1 - locate the data

Session store is `~/.claude/projects/<slug>/*.jsonl`, where `<slug>` is the cwd path with
`/` replaced by `-`. A git worktree may map to the parent repo's slug, and the memory dir
may live under a different slug than the sessions; **confirm the right dir by file count and
recent timestamps, not by name match**. Note total count and size. `~/claude-archive` is the
destination (create later).

## Step 2 - build a deterministic session_map (scripted, NOT delegated to an LLM)

This pass decides what is safe to move, so it must be exact. **It is an LLM-orchestrated procedure, not a
turnkey script:** the Python below computes the *structural* sets (fingerprints, the lpu DAG, the
keep-locked closure) deterministically, while the capitalized stubs (`judge`, `ARCHIVE`, `SONNET_CONFIRM`,
`HALT`, `confirm_no_live_or_HALT`, `nominate_debris`, `report_unsatisfiable_phantoms`) are the points where
the orchestrating model reads content and the user confirms - you wire them to your read / move / stop
steps (see the note after the code). It will NOT run as-is, by design. Gotchas confirmed in practice:

- `jq` is sometimes a broken snap. Use `python3`.
- Files start with large `file-history-snapshot` records (most of the bytes). Skip them: `if '"file-history-snapshot"' in line[:80]: continue`.
- `timestamp` is sometimes an epoch-ms **integer**, sometimes an ISO string. Normalise before comparing.
- Fork messages get **fresh UUIDs**, so grouping by a shared root uuid fails. Group by lpu, not by uuid.
- A long file is not the most complete one: fork-debugging sessions replay content, so compare **distinct** fingerprints, not raw message counts.
- `/rewind` and ctrl-z do **not** shrink the on-disk JSONL; they orphan a dead branch and keep appending (the rendered conversation shrinks, the file only grows). So a fingerprint pass over the raw file includes orphaned dead-branch messages: distinct-count reflects *all content the file ever held*, not just the live `parentUuid` chain. This is usually harmless (those messages are still real content, and containment/dedup handle them), but be aware a heavily-rewound session's distinct-count is inflated by dead branches when ranking canonicals.
- The judgment zone and the per-theme docs both need verbatim text, but the design forbids reopening multi-MB files downstream. So capture a capped **fingerprint -> text** map here, in the one pass.
- **Compute TWO fingerprint sets, both load-bearing.** A **raw** set (`fingerprints`, hashes `type` + full `content` via `json.dumps`, so it includes `tool_use` / `tool_result`) drives byte-redundancy and containment, i.e. archival **safety** - using prose here would over-archive (same discussion done with different tool work looks "contained"). A **prose** set (`fingerprints_prose`, user/assistant **text only**, no tool blocks) drives **family / theme grouping** - using raw here under-discriminates (two sessions that edited the same files look like one family). They are only half-distinguishable after the fact, so build both in this pass.

Per file extract: msg count, distinct-msg count, first plain user message, gitBranch,
first/last timestamp (normalised), set of owned uuids, lpu references, the `compact_boundary`
records as `(lpu, parentUuid, n_msgs_before_this_boundary)`, the **raw** and **prose** ordered
fingerprint lists, and the capped fingerprint->text map. Then build the lpu DAG and partition:

```python
import json, glob, os, hashlib, datetime
from collections import defaultdict
def f8(p): return os.path.basename(p)[:8]   # 8-char uuid prefix: readable, and used in every report
def norm(ts):
    # Normalise BOTH forms (epoch-ms int, or ISO string with/without Z/offset) to a naive-UTC ISO
    # string, so lexical max/compare is sound. The store mixes the two, and a raw `str(ts)` of a
    # `+02:00` value sorts after a `Z` value of a later instant - a real mis-sort for both the
    # first/last display and the canonical tiebreak. Parsing to UTC removes that.
    if ts is None: return ""
    try:
        if isinstance(ts,(int,float)):
            # not utcfromtimestamp() (deprecated in Python 3.12): build an aware UTC dt, then drop tzinfo
            dt=datetime.datetime.fromtimestamp(ts/1000, datetime.timezone.utc).replace(tzinfo=None)
        else:
            dt=datetime.datetime.fromisoformat(str(ts).replace("Z","+00:00"))
            if dt.tzinfo: dt=dt.astimezone(datetime.timezone.utc).replace(tzinfo=None)
        return dt.isoformat()
    except Exception: return ""   # unparseable -> "" sorts FIRST/earliest, i.e. treated as oldest: it won't win the recency tiebreak (the safe default). NOT a bare `except` (would swallow KeyboardInterrupt).

fingerprints={}; fingerprints_prose={}; ftext={}; owned={}; lref={}; bnd={}; nmsg={}; lts={}; firstmsg={}; global_uuid=defaultdict(set)
# STORE = the slug dir CONFIRMED in Step 1 (do NOT rely on the current working directory): glob THERE.
STORE=os.path.expanduser("~/.claude/projects/<slug>")   # e.g. ".../-home-you-proj"; full paths flow through below
files=sorted(glob.glob(os.path.join(STORE,"*.jsonl")))
# exact set-difference is the whole safety model, so a shared 8-char f8 prefix would silently overwrite one
# file's sets with another's. A bare `assert` is stripped under `python -O`, and this is a data-safety guard,
# so raise explicitly (if it ever fires, key the internal sets on the full uuid, not the 8-char prefix):
if len({f8(p) for p in files}) != len(files):
    raise RuntimeError("8-char uuid-prefix collision in the store; key internal sets on the full uuid")
for p in files:
    k=f8(p); F=[]; Fp=[]; us=set(); L=set(); B=[]; last=""; n=0; fu=""
    for line in open(p,encoding="utf-8",errors="replace"):
        if '"file-history-snapshot"' in line[:80]: continue
        try: o=json.loads(line)
        except: continue
        u=o.get("uuid")
        if u: us.add(u); global_uuid[u].add(k)
        lp=o.get("logicalParentUuid")
        if lp: L.add(lp)
        if o.get("type") in ("user","assistant"):
            c=o.get("message",{}).get("content")
            # RAW fingerprint: type + full content (json.dumps includes tool_use/tool_result).
            # Drives byte-redundancy + containment = archival SAFETY (prose here over-archives).
            raw=c if isinstance(c,str) else json.dumps(c,sort_keys=True) if c else ""
            h=hashlib.md5((o["type"]+raw).encode("utf-8","replace")).hexdigest()[:12]
            F.append(h); n+=1
            # PROSE fingerprint: user/assistant TEXT only, no tool blocks.
            # Drives family/theme grouping (raw here under-discriminates on shared file-edits).
            disp=c if isinstance(c,str) else " ".join(b.get("text","") for b in c if isinstance(b,dict) and b.get("type")=="text") if isinstance(c,list) else ""
            disp=(disp or "").strip()
            if disp: Fp.append(hashlib.md5((o["type"]+disp).encode("utf-8","replace")).hexdigest()[:12])
            if o["type"]=="user" and disp and not fu: fu=disp[:200]   # first plain user message (debris signal)
            if h not in ftext:   # capped raw-fingerprint -> text PREVIEW (+ truncation flag), for the doc/judgment
                # 2000 = a PREVIEW, not a full read. The truncation flag lets the judgment treat any message
                # that hit the cap as substantive (KEEP): a key/proof can hide past the cap, so a truncated
                # message is NEVER archived on the preview alone. (memory ~ distinct-fingerprints * 2000.)
                ftext[h]=(o["type"], disp.replace("\n"," ")[:2000], len(disp)>2000)
            t=norm(o.get("timestamp"));  last=max(last,t) if t else last
        if o.get("type")=="system" and o.get("subtype")=="compact_boundary":
            B.append((o.get("logicalParentUuid"), o.get("parentUuid"), n))  # n = msgs before boundary
    fingerprints[k]=F; fingerprints_prose[k]=Fp; owned[k]=us; lref[k]=L; bnd[k]=B; nmsg[k]=len(F); lts[k]=last; firstmsg[k]=fu

# phantom lpus = referenced but owned by no file
all_lpus={lp for k in bnd for (lp,_,_) in bnd[k] if lp}
phantom={lp for lp in all_lpus if lp not in global_uuid}
def sources(k):   # phantom lpus this file can BACKFILL (has pre-content before that phantom boundary)
    return {lp for (lp,par,nb) in bnd[k] if lp in phantom and (par is not None or nb>0)}
def needs(k):     # phantom lpus this file relies on a sibling for (boundary at root, no pre-content)
    return {lp for (lp,par,nb) in bnd[k] if lp in phantom and par is None and nb==0}
# DISCIPLINE: use sources()/needs() VERBATIM everywhere you verify orphans or backfill - never
# re-derive a quick approximation. A file is a source if it has ANY pre-content before its phantom
# boundary: `par is not None OR nb>0`. The lazy `par is not None`-only form (dropping the nb>0 half)
# was reimplemented loosely TWICE in a real run and produced false orphan flags both times. The
# precise predicate is cheap; the approximation is the bug. The Lean proof now DEFINES these predicates
# (proofs/Boundary.lean: `sourcesOf`/`needsOf` over the (lpu,par,nb) records) and MACHINE-CHECKS the
# par/nb classification - the per-record source/need partition (`rec_iff`) and THIS bug as a checked
# divergence (`lazy_flips_source_to_need`: a null-parent-but-nb>0 record is a SOURCE under the committed
# test, a NEED under the lazy one). It proves the par/nb LOGIC given the records; only the JSONL->records
# extraction stays fuzz-checked. So the proof certifies THIS committed set-builder, not a re-derived
# approximation: run it VERBATIM, never an eyeballed orphan check - the proof literally shows the lazy
# shortcut diverges, but only protects you if you actually run the committed definition.

# union-find trees: link files sharing an lpu value, or a cross-file dep edge
parent={k:k for k in fingerprints}
def find(x):
    while parent[x]!=x: parent[x]=parent[parent[x]]; x=parent[x]
    return x
bylpu=defaultdict(list)
for k in fingerprints:
    for lp in lref[k]: bylpu[lp].append(k)
for ks in bylpu.values():
    for k in ks[1:]: parent[find(k)]=find(ks[0])
dep={(a,b) for a in fingerprints for lp in lref[a] for b in global_uuid.get(lp,()) if b!=a}
for a,b in dep: parent[find(a)]=find(b)
trees=defaultdict(list)
for k in fingerprints: trees[find(k)].append(k)

# canonical: most DISTINCT content, recency as tiebreak, among non-debris (content floor)
DEBRIS_MAX=11
# DISTINCT count (len(set)), not raw len(F)=nmsg: a rewound/replayed fork-test inflates the raw count past
# the floor, but DISTINCT content is what the floor is about (the doc's own distinct-vs-raw discipline).
def is_debris(k): return len(set(fingerprints[k])) <= DEBRIS_MAX
def canonical(ks):
    cand=[k for k in ks if not is_debris(k)] or ks
    # final `, k` tiebreak: when distinct-count AND last-ts tie (e.g. byte-identical duplicate forks),
    # `max` resolves on `k`, picking the lexically-LAST filename - arbitrary but DETERMINISTIC (not glob/
    # dict iteration order), which is all the tiebreak needs once content and recency are equal.
    return max(cand, key=lambda k:(len(set(fingerprints[k])), lts[k], k))

# keep-locked closure: seed (canonicals + live) + transitive load-bearing over BOTH edge types.
# AUTHORITATIVE never-touch: live sessionIds from ~/.claude/sessions/*.json (Step 0). NO mtime term -
# recent-mtime is NOT a liveness signal (opening a chat to inspect it bumps its mtime; the registry,
# not mtime, says what is live). Liveness is "the process that owns this entry is still alive", which is
# NOT the `status` string: an open session sits in status "idle" (awaiting input) or "busy" (generating)
# and is essentially NEVER "running" in current Claude Code (verified on a live 2.1.x registry: entries
# carry idle/busy, not running). So do NOT gate on `status` - that silently drops EVERY live session.
# Gate on the PID being alive AND its /proc start-time matching the recorded `procStart` (which pins the
# entry to that exact process, so a reused PID cannot masquerade as the dead session it replaced). Both
# checks only ever OVER-protect (keep a stale entry), never under-protect, so the direction is always safe.
live=set(); registry_unreadable=False
def proc_starttime(pid):                                 # /proc/<pid>/stat field 22 (ticks since boot); the
    try:                                                 # registry's `procStart` is exactly this value. `comm`
        s=open(f"/proc/{pid}/stat").read()               # can contain spaces/parens, so split AFTER the final ")".
        return s[s.rindex(")")+1:].split()[19]
    except OSError: return None                          # non-Linux / process gone: fall back to os.kill alone
for sp in glob.glob(os.path.expanduser("~/.claude/sessions/*.json")):
    try:
        d=json.load(open(sp))
    except (OSError, ValueError):
        registry_unreadable=True; continue   # a CORRUPT <pid>.json may HIDE a live session -> HALT after
                                              # the loop, never silently skip (the prose promises STOP-on-unreadable)
    try: pid=int(d["pid"])
    except (KeyError, ValueError, TypeError):
        registry_unreadable=True; continue   # a malformed entry may HIDE a live session -> fail closed, HALT after
    # os.kill(pid,0) proves the PID is OCCUPIED, not that it is YOUR session (PIDs get reused); the procStart
    # match below proves it is the SAME process. EPERM = alive but another user = still live (keep).
    try: os.kill(pid,0)                                  # ProcessLookupError => process gone => stale entry
    except ProcessLookupError: continue
    except PermissionError: pass
    ps=str(d.get("procStart","")); st=proc_starttime(pid)
    if ps and st is not None and ps!=st: continue        # PID reused by an unrelated process => stale entry, skip
    sid=d.get("sessionId","")[:8]
    # The registry is GLOBAL (every running Claude process, all projects). A session running in ANOTHER
    # project has no file in THIS store, so `sid not in fingerprints` does NOT by itself mean an inconsistency.
    # OUTSIDE any try, so a raising HALT is NEVER swallowed:
    if sid not in fingerprints:
        # Could be (a) a session in a different project (safe to skip) or (b) a this-project session whose
        # file is genuinely missing (a real inconsistency). Do NOT auto-skip on a cwd guess - a worktree maps
        # to a different cwd yet a SHARED store (Step 1) - so STOP and let the operator decide from the cwd:
        HALT(f"running session {sid} (cwd {d.get('cwd')!r}) has no <{sid}>.jsonl in this store. If its cwd is "
             f"a DIFFERENT project, skip it; if THIS project, resolve before proceeding - never guess.")
    live.add(sid)
if registry_unreadable:
    HALT("a file under ~/.claude/sessions failed to parse - a running session may be hidden by it; resolve "
         "before proceeding, do not run on a partial live set")
if not live:
    # A READABLE registry with nothing running is a valid empty live set: proceed with live={} after the
    # operator confirms nothing is running. A MISSING/UNREADABLE registry is a HARD STOP. Either way,
    # never fall back to a recency/mtime guess.
    confirm_no_live_or_HALT()
# seed = multi-file-tree canonicals + live. Single-file trees need no seed: union-find already merged
# any file with a cross-file lpu target (a dep edge) or a co-referenced phantom (shared lpu), so a
# single-file tree has no cross-file/phantom obligation to close over. NO mtime term (see above).
seed={canonical(ks) for ks in trees.values() if len(ks)>1}
seed|={k for k in fingerprints if k in live}                                    # live (authoritative, Step 0)
unsatisfiable={}   # kept file -> {phantom lpus it needs but NO file can source}: origin truly gone
def locked_closure(seed):
    locked=set(seed); changed=True
    while changed:
        changed=False
        for k in list(locked):
            for lp in lref[k]:                          # (1) cross-file ancestors
                for b in global_uuid.get(lp,()):
                    if b not in locked: locked.add(b); changed=True
            for P in needs(k):                          # (2) phantom backfill: keep >=1 source
                srcs=[s for s in fingerprints if P in sources(s)]
                if not srcs:                            # needed phantom with NO viable source ->
                    unsatisfiable.setdefault(k,set()).add(P)   # origin truly gone; SURFACE, don't silently pass
                    continue
                if not (set(srcs)&locked):
                    best=max(srcs, key=lambda s:len(set(fingerprints[s])))  # richest origin
                    locked.add(best); changed=True
    return locked
locked=locked_closure(seed)   # PRELIMINARY (canonicals+live); the per-tree keep below uses it. The
                              # DEFINITIVE closure + the unsatisfiable report run after KEPT is known
                              # (the re-close below), so unsatisfiable reflects the full kept set.

canonicals={canonical(ks) for ks in trees.values()}   # defined once; used by KEPT, C5, and the marker loop

# Per-tree archive judgment: CLASSIFY each non-kept fork as keep-for-unique vs archive-candidate. The
# orchestrator reads uniq VERBATIM (ftext) INLINE here - an LLM-grade read by the operator running the
# procedure, not a delegated subagent - and it must finalize BEFORE the re-close (it feeds KEPT). Do NOT
# move anything yet: a fork judged redundant here can be the sole phantom-backfill SOURCE of a kept-unique
# fork we keep below, which the re-close protects. Collect candidates; archive after the re-close.
CEILING=50         # >= this many unique msgs => AUTO-KEEP. Below => JUDGE content, never auto-archive.
kept_unique_forks=set(); tree_archive_candidates=set()
for ks in trees.values():
    if len(ks)==1: continue
    canon=canonical(ks); keep=set(ks)&locked | {canon}
    kept_fp=set().union(*(set(fingerprints[k]) for k in keep)) if keep else set()
    for k in [x for x in ks if x not in keep]:
        uniq=set(fingerprints[k])-kept_fp           # unique vs THIS TREE's kept files (cross-tree dup -> recall pass)
        if len(uniq)>=CEILING:             # substantial tree-local unique -> AUTO-KEEP, don't even ask
            kept_unique_forks.add(k)
        else:                              # JUDGMENT ZONE: read uniq VERBATIM (ftext); a uniq message whose
            # ftext truncation flag is set is auto-substantive (KEEP). Archive ONLY if self-evidently
            # worthless (empty/tool-only turns, exact replays in the canonical, fork-test artifacts, trivial
            # one-shot Q&A). ANY substantive content (a decision, derivation, key, datum, code) -> KEEP.
            judge(k, uniq)                 # -> kept_unique_forks.add(k) OR tree_archive_candidates.add(k)
        # Count is NEVER the sole archive trigger; CEILING only auto-keeps (a 1-message fork can be a key).

# FULL kept set, THEN re-close so EVERY kept file's backfill source / cross-file ancestor is locked.
# ORPHAN FIX: the seed closure saw only canonicals+live, NOT the kept-unique forks just decided. A kept
# fork can itself NEED a phantom whose sole source is content-redundant; without this re-close that source
# is archived and the fork's deep origin orphaned. Re-closing over the FULL kept set folds the source into
# locked (hence KEPT), so it is protected, not archived.
KEPT = canonicals | set(locked) | kept_unique_forks
unsatisfiable={}                           # (re)compute on the DEFINITIVE closure over KEPT, not the seed one
locked = locked_closure(KEPT); KEPT |= locked
if unsatisfiable: report_unsatisfiable_phantoms(unsatisfiable)

# load-bearing is PURELY STRUCTURAL: k is load-bearing iff some KEPT/live file reconstructs scrollback
# from it - k owns a uuid referenced cross-file, OR k can source a phantom a kept file needs. Nothing
# about mtime (the registry, not mtime, is liveness) and nothing about `locked` per se.
consumers = KEPT | live
loadbearing  = {b for a in consumers for lp in lref[a] for b in global_uuid.get(lp,()) if b!=a}  # cross-file targets
needed       = set().union(*(needs(a) for a in consumers)) if consumers else set()                # phantoms they need
loadbearing |= {s for s in fingerprints if sources(s) & needed}                                            # files that source them
# loadbearing (ALL sources of a needed phantom) >= the ONE richest source the closure locks; the
# redundant extra sources stay archivable.

# DEBRIS-FIRST DISCARD - the one-window placement. Nominate throwaway singletons HERE: after `loadbearing`
# is frozen, but BEFORE C5/recall/markers measure containment. The window is exactly one. A debris file's
# own edges must still count when `loadbearing` is computed, or it could strip another file's protection;
# but the file must be GONE from KEPT before any residue / kept_union is measured, or a debris shell can be
# the sole kept container of another file's message - that file is then archived as "redundant" and the
# debris file is archived too, so the message survives only in archived files. Discarding in a LATE pass was
# exactly that loss (recall measuring against a KEPT that still held debris, and C5 reading a fork as
# 0-residue because a debris file duplicated it). This ordering loses no content; the proofs in
# proofs/ establish it (content_safe_post_debris, c5_demote_no_loss); a singleton debris file is provably non-loadbearing
# (the loadbearing-stability lemma), so discarding it changes no other file's loadbearing status.
def is_boilerplate(s):   # throwaway openers; conservative, tune to your store (see the debris subsection below)
    s=(s or "").strip().lower()
    return (not s or s.startswith(("<", "[request interrupted"))
            or s in ("test","hi","hello","ping","you here?","?"))
debris=set()
for ks in trees.values():
    if len(ks)!=1: continue                  # debris is ALWAYS a singleton tree -> debris is a subset of canonicals
    k=ks[0]
    if k in loadbearing or k in live: continue   # loadbearing (the frozen set), NEVER locked (see the trap below)
    fu=firstmsg.get(k,"")
    if nmsg[k]==0 or (is_debris(k) and (not fu or fu.lstrip().startswith(("/","cd ")) or fu.strip()=="cd" or is_boilerplate(fu))):
        debris.add(k)
for k in debris:
    KEPT.discard(k); kept_unique_forks.discard(k); locked.discard(k)   # shrink KEPT ONLY - canonicals stays tree-derived
    nominate_debris(k)                       # route to a debris/ theme; record in all_archived; user confirms at Step-6

# `loadbearing`/`needed` are computed ONCE, ABOVE (over consumers WITH debris still in), and deliberately NOT
# recomputed after the debris discard or the C5 demotion. The frozen value can only OVER-include - it never
# drops a real cross-target or source - which is the safe direction; and a singleton debris file is
# non-loadbearing anyway, so the discard changes no kept file's loadbearing status. (Recomputing after C5
# would instead strand a demoted fork's now-unneeded source as `~0-residue AND NOT loadbearing AND in KEPT`,
# reopening the marker hole C5 closes.) RESIDUE MONOTONICITY: removing ANY file from KEPT - a debris file
# (which may have NONZERO residue) or a C5 0-residue fork - drops it from the subtracted union, so every
# survivor's residue only GROWS. The static `residue` model never overstates a survivor's uniqueness, and the
# `~0-residue AND NOT loadbearing` marker hole cannot reopen (proofs/: residue_grows_on_shrink,
# nonzero_residue_survives_shrink). Monotonicity holds for removing ANY file from KEPT, not just a 0-residue
# one, so the model stays faithful whether the removed file is debris (possibly nonzero residue) or a C5
# 0-residue fork.
#
# A fork auto-kept on TREE-LOCAL uniqueness can be GLOBALLY redundant (its tree-unique content duplicated
# in another tree's kept file). Re-measure vs the full kept set: a kept-unique fork with EXACTLY 0 global
# residue that backs nothing had incomplete info -> drop it to the recall pass (which archives 0-unique
# safely). Keeps the taxonomy sound: every remaining ~0-residue KEPT file is then load-bearing -> [scroll-dep].
for k in sorted(kept_unique_forks):
    if k in loadbearing: continue
    if not (set(fingerprints[k]) - set().union(*(set(fingerprints[j]) for j in KEPT if j!=k))):
        KEPT.discard(k); kept_unique_forks.discard(k); locked.discard(k)   # keep `locked` a subset of KEPT

# Deferred per-tree archives: a candidate moves only if the re-close did NOT lock it (i.e. not load-bearing).
for k in sorted(tree_archive_candidates - KEPT):
    # SAFETY ASSERT (backstop to the re-close): archiving k must leave every needed phantom k sources with
    # ANOTHER kept source - NOT `sources(k) & needed == set()` (a redundant EXTRA source legitimately
    # sources a needed phantom yet is archivable). The right check: no phantom is left sourceless.
    for P in (sources(k) & needed):
        if not any(P in sources(s) for s in KEPT if s!=k):   # raise, not assert: a data-safety guard must
            raise RuntimeError(f"archiving {k} would orphan phantom {P}")   # not be elidable by `python -O`
    ARCHIVE(k, "redundant fork; unique content judged worthless after a verbatim read")

# MARK every retained file that is NOT a canonical and NOT live - ONE decision, the markers below.
# `KEPT - canonicals - live` is the union of cross-file ancestors, phantom sources, and the kept-unique
# forks. Do NOT assume "ancestor -> bridge": a kept-unique fork is NOT a reconstructed prefix of anything;
# it can be a disjoint substantial standalone, so `[main]` and `none` must be reachable here, not only
# `[scroll-dep]`/`[fork]`. (Canonicals are `[main]` from selection; live are never-touch. None of these is
# ever archived - all are in KEPT; load-bearing protects from MOVING, the loop only titles.)
for k in sorted(KEPT - canonicals - live):
    head=max(canonicals, key=lambda h: len(set(fingerprints_prose[k]) & set(fingerprints_prose[h])), default=None)
    # head is None only in a degenerate store with NO canonicals; then ov=0.0 routes to the low-ov branch
    # ([main]/none, no parent named), so the [fork]-of-head branch is never reached with head=None.
    ov=(len(set(fingerprints_prose[k]) & set(fingerprints_prose[head]))/max(1,len(set(fingerprints_prose[k])))) if head else 0.0
    residue=set(fingerprints[k]) - set().union(*(set(fingerprints[j]) for j in KEPT if j!=k))   # RAW residue, then READ it
    # 1) load-bearing AND ~0 residue -> [scroll-dep] of the canonical it backs, REGARDLESS of ov: a pure
    #    bridge can be prose-disjoint from every head (ov~0) yet raw-redundant. Only REGISTRY-live is never
    #    a scroll-dep (it is actively growing, not a dead ~0-unique bridge).
    # 2) else by PROSE containment vs the best-containing head (`head` ALWAYS resolves, so gate on `ov`;
    #    name a parent ONLY when k is genuinely mostly-contained in head's prose - the Step-3 family signal):
    #      NEAR-DISJOINT (low ov):  substantial residue -> [main] (a sub-lineage head, NO parent named)
    #                               minor                -> none   (a minor standalone)
    #      MOSTLY-CONTAINED (high ov), substantive residue -> [fork] of `head` (name it; title by residue)
    # There is NO "~0 residue AND NOT load-bearing" branch: recent-mtime is gone and C5 demoted the
    # globally-redundant kept-unique forks, so every ~0-residue file reaching here is load-bearing (case 1).
    # RAW residue catches tool-only content (a pure bridge has ~0 of ANY content). substantial-vs-minor and
    # trivial-vs-substantive are READ-and-JUDGE (the ftext truncation flag forces a long unique msg to KEEP),
    # never a CEILING cutoff (CEILING only auto-KEEPS a fork; reusing it as a title threshold is the proxy to avoid).
```

Record, per archive candidate: its unique message texts (verbatim, from `ftext`), its
relationship to the canonical (byte-identical duplicate / contained prefix / divergent at
msg K), and whether it is a phantom source (it must not be, by construction, but assert it).

### Cross-tree content-containment consolidation (the recall pass - do NOT skip)

The per-tree partition only consolidates forks that SHARE an lpu. It misses duplicates ACROSS
trees: a session resumed or forked without leaving an lpu link (phantom or in-file lpu, or an
older pre-`--fork-session` resume) carries the same messages under fresh uuids in a separate
tree. lpu-grouping keeps them apart; content shows them identical. Skip this and you leave
zero-unique redundant sessions in the picker - a recall failure (this happened in a real run:
five sessions that were 100% contained in a kept file survived because they were in their own
lpu-trees).

After the per-tree partition, run a global containment pass.

```python
# KEPT is the FULL retained set, defined ONCE in Step 2 above. CRITICAL: debris was already discarded from
# KEPT in the Step-2 debris-first window, so `kept_union` below is debris-free. Were debris still in KEPT, a
# debris shell could be the sole container that makes a candidate read 0-unique - the candidate would be
# archived and the debris file archived too, losing the message. Measuring containment over the debris-free
# KEPT is exactly what `content_safe_post_debris` certifies (proofs/).
keptset={k:set(fingerprints[k]) for k in KEPT}
kept_union=set().union(*keptset.values()) if keptset else set()
# `a not in KEPT` excludes every canonical (a subset of KEPT). Also exclude `debris`: those were archived in
# Step 2, so they must not re-enter as recall candidates.
for A in [a for a in fingerprints if a not in KEPT and a not in live and a not in debris]:
    # set-difference against the UNION of the whole kept set, NOT any single container - a session
    # whose content is split-contained across two smaller kept files is still 0-unique and redundant
    # (the single-container test `len(fingerprints[b])>=len(fingerprints[A])` had exactly that recall hole).
    missing = set(fingerprints[A]) - kept_union
    best = min(KEPT, key=lambda b: len(set(fingerprints[A])-keptset[b]), default=None)  # closest single file, for the doc only
    # SAFETY backstop, same shape as the C6 guard: content-redundancy and phantom-source status are
    # INDEPENDENT axes, so before archiving A confirm every phantom A sources that a kept file needs still
    # has a kept source. Safe by construction (the re-close locks a needed SOLE source into KEPT, so a recall
    # candidate is at most a REDUNDANT extra source), but both archive paths must enforce the same invariant.
    for P in (sources(A) & needed):
        if not any(P in sources(s) for s in KEPT):   # A is not in KEPT, so any kept source suffices
            raise RuntimeError(f"archiving {A} would orphan phantom {P} - recall-pass orphan guard")
    if not missing:                       # 0 unique vs the kept set: PROVEN redundant (every msg lives somewhere kept)
        ARCHIVE(A, f"0 unique vs the kept set as a whole (split-contained; closest single file {best}, which need not individually contain A)")
    elif len(missing) < CEILING:          # JUDGMENT ZONE
        SONNET_CONFIRM(A, best, missing)  # a Sonnet READS missing; archive iff trivial/preserved
    # else: substantial unique -> KEEP
```

Two rules learned the hard way:

- **`0 unique` is exact set-containment against the kept set as a whole, not a heuristic.** Every (type, content) message of A has an identical fingerprint somewhere in the kept *union* - possibly split across several kept files, NOT necessarily in `best` (which is only the single closest file, reported for the operator, and need not individually contain A). So archiving A loses nothing. Safe without a Sonnet. (The fingerprint is a 12-hex-char, i.e. 48-bit, MD5 prefix: read "identical" as "identical fingerprint", which is containment for any realistic store, not a literal byte-compare. If you want byte-certainty, compare the raw messages of A against its union cover before the move.)
- **Any NONZERO unique is a `likely`-style claim and MUST be confirmed by a Sonnet that READS the unique messages.** A keyword/mention count in the container (e.g. "BodyFrameId appears 2174 times") does NOT prove the specific exchange is preserved. The Sonnet reads each unique message and either confirms it is trivial or genuinely preserved elsewhere (archive) or flags it as unique work (keep). The zero-unique archive is the **recall** gate; the Sonnet read is the **precision** gate. Both are required: maximum cleanup, zero loss.

Never archive a canonical even when it is highly contained in one of its own forks (forks
replay most content; the canonical is the primary by recency + distinct-content).

(`ARCHIVE()`, `SONNET_CONFIRM()`, `judge()`, `nominate_debris()`, `report_unsatisfiable_phantoms()`,
`HALT()` / `confirm_no_live_or_HALT()` above are pseudocode placeholders - wire them to your move /
confirm / debris / stop-and-ask steps. `judge()` only ADDS to `kept_unique_forks` or
`tree_archive_candidates` (it never moves a file - the deferred C6 loop does, after the re-close); the
orchestrator runs `judge()` inline since it feeds `KEPT`, whereas `SONNET_CONFIRM()` is the recall-pass
read, batched to the Step-5 agents. `KEPT`, `live`, `kept_unique_forks`, `tree_archive_candidates` etc.
are real variables defined in the pass.)

**Deterministic debris nomination, guarded on `loadbearing`, never `locked`.** You decide whether a file
is debris from the file's own content: an empty resume or fork stub with zero messages, a command-only
shell (`/clear`, `/effort max`, `/resume`, `/model`), a bare `cd`, a one-shot that produced nothing. That
classification needs nothing from the tree or the closure. What it does need is the load-bearing check,
because debris must not be archived if some kept file reconstructs scrollback from it, and a file is
load-bearing only after `loadbearing` is computed (right after the C4 re-close finalizes `KEPT`). So the
scan runs at that point, in the main Step-2 block above: right after `loadbearing` is built, you nominate
the debris and `KEPT.discard` it there, before C5 demotion, before the recall pass, and before the marker
loop. Discarding it there is what keeps those passes honest. Each one measures containment against `KEPT`,
and a debris file left in `KEPT` could be the only container it credits, so the file it "covers" would be
archived and the debris moved out from under it. That is the loss just described - a debris file credited
as the sole container, then archived alongside the file it covered - which the content theorems in `proofs/`
rule out (`content_safe_post_debris`, `c5_demote_no_loss`). Present the
debris hits to the user in the same Step-6 gate as the fork archives, so the user approves one list and
never finds debris mid-titling; "one list up front" is about what the user sees in the gate, not about
when the scan runs. The `is_boilerplate` helper the loop uses (defined with the loop in the main block)
flags throwaway openers conservatively: an empty message, a `<...>` command tag, a `[request interrupted`,
or a bare greeting.

**The guard is `loadbearing`, NOT `locked` - this is a real trap.** When nearly every session is a
standalone single-file tree (no real fork families), each is its own canonical, so the C4 re-close
balloons `locked` to ~ALL files (`locked = locked_closure(KEPT)` with `KEPT ⊇ canonicals ⊇` every
single-file-tree member). `if k in locked` then skips EVERY debris candidate and nominates nothing,
silently. (Real run: 9 empty stubs + 11 command-only shells were all suppressed this way and only
surfaced late, mid-titling.) Guard on the genuinely **load-bearing** set instead - cross-file lpu
targets plus phantom sources - and the `if k in loadbearing: continue` guard excludes a file the moment it is load-bearing (a
0-message stub can still own a `compact_boundary` uuid, so do not infer non-load-bearing from a zero
message count; the guard tests membership directly). Archiving a non-load-bearing file (the debris path is
a SECOND non-load-bearing demotion alongside C5) is proved in `proofs/` not to orphan any kept session
(`FixProto.no_orphan_from_closed_debris`), and not to strand another kept file's only copy of a message:
the recall and C5 passes measure
containment over the post-debris `KEPT` (`content_safe_post_debris`, `c5_demote_no_loss`), and removing
debris only grows other files' residue (`residue_grows_on_shrink`), so the marker tree is unaffected.
`debris ⊆ canonicals` (`Family.singleton_canonicalPick`) keeps debris out of the marker range
(`marker_range_excludes_debris`), and a singleton debris file changes no other file's load-bearing status
(`loadbearing_stable`). Whether the debris file's *own* content is worthless stays the operator's read,
not a theorem.

**A trivial one-shot question is debris too - but route it to the user gate, not an auto-nominate.** A
session whose only substantive turn is a single unanswered question (e.g. "Can I resume chat with this
agent directly?"), especially when a larger kept session covers the same ground, is throwaway - but its
opener is plain prose, so `is_boilerplate` misses it. Don't auto-archive on a 1-turn heuristic (a
one-message session can hold a key); instead flag any `<=1`-substantive-turn one-shot to the **per-item
user gate** (Step 6) with its text, defaulting to keep. (Real run: a 1-message "can I resume this agent"
question was kept by default until the user called it out.)

Keep it conservative (the user still confirms debris before it moves), but do not leave debris
detection entirely to LLM judgment.

**Size is necessary, never sufficient - small does not imply unimportant.** A two-message
session can hold a private key, a derivation, or the one answer that mattered. So nomination
requires a throwaway *opening* (`/`-command, bare `cd`, boilerplate, empty) on top of the small
count; `nmsg <= DEBRIS_MAX` alone never nominates. And debris is still a **move to the
recoverable archive, not a deletion** - nothing here removes a small session, it relocates it
where the user can restore it. The only actor that truly deletes small sessions is the retention
sweep, which the protections below guard against size-agnostically.

## Step 3 - detection rules

- **Fork family** = files in the same lpu tree. Files sharing the same phantom lpu in their first compact_boundary are forks of one conversation tree.
- **False family** = files that share only an identical first message (skill preambles like a `/`-prefixed command invocation, or identical compaction headers). The script unions on **lpu values, never on first-message content**, so false families do not merge by construction. Keep a content common-prefix-length (cpl) calculation only as a **diagnostic** to sanity-check tree membership; do not drive the partition off it.
- **Content-fork members (lpu-union UNDER-groups - do not stop at the lpu tree).** lpu grouping only catches forks that share an lpu value. A fork made by copying messages (fresh uuids, no shared lpu), or a long conversation that continued under a new topic/issue, will NOT union by lpu and masquerades as a separate session/theme. Detect these with a **prose-only** content-overlap pass over the `fingerprints_prose` set from Step 2 (user/assistant TEXT only, no `tool_use`/`tool_result`): if a file shares more than ~60% of its `fingerprints_prose` messages with a larger family member, it is a fork of that family. **Use `fingerprints_prose`, never raw `fingerprints`, for this** - raw content-overlap is confounded by shared tool-results (two sessions that edited the same files share those fingerprints). Real run: one session was 82% *prose*-shared with another theme's canonical (same family, but mis-themed as separate), while a tooling-heavy session showed 100% *raw* overlap with that same canonical yet 1% prose (all shared file-edits) and was a different conversation. The lpu tree is necessary but not sufficient for family membership.
- **Canonical** = most *distinct* content, recency as tiebreak, chosen among non-debris files (a content floor, so a 3-message newest fork-test never wins over a 2000-message sibling). This ranks on raw-distinct (`len(set(fingerprints[k]))`), which counts tool-churn as content; within a true fork family that still picks the most complete member. If you ever see a canonical chosen wrongly because one member is tool-heavy rather than conversation-heavy, switch the rank key to prose-distinct (`len(set(fingerprints_prose[k]))`); not worth doing pre-emptively.
- **Load-bearing (never move)** = the `locked` closure: cross-file lpu targets plus phantom-backfill sources, transitively, seeded from canonicals and live sessions (Step 0), then re-closed over the full kept set so a kept fork's own backfill source is never archived.
- **Archive candidate** = a non-locked, non-canonical fork whose unique content, *after someone reads it*, is self-evidently worthless. Compute unique content by set-difference against the kept files, not by prefix-divergence ("diverged at msg 2" and "4 unique messages" routinely disagree; set-difference is the one that matters for data loss). Then **judge the messages, never the count.** A single message can hold a private key, a derivation, or irreplaceable data. `CEILING` (default 50) only auto-*keeps* forks at or above it. Below it is a judgment zone: read the unique messages and archive only the self-evidently worthless (empty / tool-only turns, exact replays already in the canonical, fork-test artifacts, trivial one-shot Q&A). Anything substantive is flagged for human judgment and kept by default.

**Durable-artifact override.** This is the one case where a *high*-unique fork (>= CEILING)
is still archivable: if the user points at a named durable artifact (a consolidated plan, a
committed doc, a poc) that already captures the fork's content. It runs the opposite
direction from the judgment zone, never a reason to archive a below-CEILING fork on count.

## Step 4 - themer agent (1 Sonnet, top-level session only)

Feed it the session_map digest (no raw JSONLs). It returns themes (per gitBranch + topic)
mapping every file to exactly one theme, plus a `debris` flag for throwaway standalones
(2-to-9-msg fork-tests, aborted sessions, typo-commands). A theme may span several trees;
each tree sits wholly in one theme. Validate the mapping covers every file with no overlaps.

## Step 5 - per-theme agents (Sonnet, batched, top-level session only)

One per theme. **Read-only on JSONLs; they never move files.** Each writes
`~/claude-archive/<theme>/README.md` documenting: the canonical (full uuid + path + why,
including the distinct-vs-raw nuance), each archived fork (uuid, what the branch explored,
its unique messages quoted verbatim, the divergence point), and the load-bearing files kept
in place and why. Each emits `<theme>.json` listing the files its theme proposes to archive.
Pass each agent the verbatim unique-message texts (`ftext`) so it never opens a multi-MB file.

**Division of labour with the Step-2 script.** The script already SETTLED the deterministic archives
(exact 0-unique containment in the recall pass) and the orchestrator already ran the per-tree `judge()`
reads inline (they fed `KEPT`). What is left for these agents is the recall pass's `SONNET_CONFIRM`
zone - the nonzero-but-below-`CEILING` unique cases: read the `missing` messages verbatim (from `ftext`;
a message flagged truncated is auto-substantive, so keep it) and confirm each is trivial/preserved
(archive) or genuinely unique (keep). The agents do NOT re-derive the deterministic 0-unique archives;
they resolve the judgment-zone cases and document both the settled and the judged.

Each README MUST include an **`## Ingestion`** section: what the theme's canonical should fold
in, measured as **unique-vs-canonical** (set-difference against the canonical's OWN
fingerprints, not the whole kept set), split by provenance: (a) **from archived forks**, and
(b) **from retained forks**. The retained-fork half is the one people forget: a kept
*divergent* fork can hold content the canonical lacks, because the canonical only renders its
lpu-*ancestors* cross-file, not divergent siblings. Quote those uniques verbatim from `ftext`.
Load-bearing ancestors are the canonical's own history rendered cross-file, so they are
explicitly "nothing to ingest".

## Step 6 - finalize the moves (orchestrator only)

**Re-read the live registry first (Step 0 is a snapshot).** Steps 4-5 and the user gate can take minutes
to hours, during which a session may have gone live. Re-read `~/.claude/sessions/*.json` immediately
before any `mv` and drop any now-live session from the move list. The registry is the sole liveness
authority - consult it again at mutation time; never substitute an mtime guess.

0.  **Backup precondition (checked gate, not just a principle).** Before the *first* mutation in the whole run - this move, or Step 7's titling/equalisation, whichever you reach first - confirm a current out-of-tree backup exists OR the `SessionStart` backup hook has fired this session. Do not proceed past this line otherwise. This is the gap that bites in practice: equalising or moving before any backup exists means the first slip has no recovery point.
1.  Aggregate the keep/archive list. **Show the user the full list (forks + every debris file, with unique-msg counts) and get explicit go/no-go before moving anything.**
2.  Create `~/claude-archive/<theme>/` dirs (agents already wrote the READMEs there).
3.  `mv` each confirmed file into its theme dir, preserving the uuid filename. Write `~/claude-archive/manifest.json` as a list of `{archived, original, theme, canonical, reason}` records, e.g.:

    ```json
    [{"archived":"~/claude-archive/2110-rework/9b5c....jsonl","original":"~/.claude/projects/<slug>/9b5c....jsonl","theme":"2110-rework","canonical":"a1b2....jsonl","reason":"0 unique vs the kept set"}]
    ```

    so one command restores everything (move each record's `archived` back to its `original`):

    ```bash
    python3 -c 'import json,shutil,os; [shutil.move(os.path.expanduser(r["archived"]), os.path.expanduser(r["original"])) for r in json.load(open(os.path.expanduser("~/claude-archive/manifest.json")))]'
    ```
4.  **Post-move verification (hard gate):** re-scan the project dir; for every kept file - canonical OR retained fork, since a kept fork can need a phantom too - confirm (a) all its cross-file lpu targets still resolve in the project dir, and (b) every phantom it needs still has a kept backfill source. **Recompute `needs()` / `sources()` VERBATIM (Step 2's definitions) over the POST-move file set** - the pre-move maps are stale, and a re-derived approximation (e.g. `par is not None` without the `nb>0` half) gives false orphan flags. Zero orphans on both. Confirm `total - moved` files remain and no live/registry session was touched.

**Operational / low-value but distinct sessions** (skill re-runs, toy experiments, one-off
config tasks, stray Q&A) are neither redundant nor debris: their content is unique, just
low-value. Do NOT archive them on a vibe. Present each one INDIVIDUALLY to the user (a per-item
keep/archive choice, each with a real 1-2 sentence description of what it actually contains)
and default to KEEP. Only deterministic debris and Sonnet-confirmed-redundant content archive
without a per-item user decision.

## Step 7 - title the retained sessions (dormant-only)

Give the picker a themed shape by appending a `custom-title` record to each retained session:

- **Format: `<family> <sub-label>: <1 to 2 sentences, gist first>`.** Use an **umbrella family** label (e.g. `backend`, `claude-patches`) with a **sub-label** for the sub-area: an **issue number** where the store has issue tracking (`1234`, `1290`), otherwise a short **topic tag** (`auth`, `search`, `parser`). Assign the sub-label by **topic, not branch**: many sessions all run on one long-lived branch, so the branch field is a poor theme signal. (The markers below - `[main]` / `[fork]` / `[scroll-dep]` - are project-agnostic and apply either way.) The sentences say what the session actually is or did; the picker truncates the *tail*, which is the reason to front-load the gist - NOT a width to fit (see "no character limit" below). NOT a bare slug, NOT a terse invented phrase (a real run rejected `fork-empty` and `shell/setup ops` as meaningless), NOT 3+ sentences, and NEVER the raw first message (a tool/command echo or `<ide_opened_file>` tag). Date-only differentiators (`(Apr 3)`) are uninformative.
- **Marker taxonomy - every retained session is exactly one of four, decided by *containment* and *unique content*, never by issue number, branch, or a one-per-family assumption.** (Archive is **not** a fifth marker: it is the prior retain-vs-move-out decision, exclusive of all four - only retained files are marked.) Put the marker at the very front so it survives picker truncation.

    - **`[main]`** - a **near-disjoint, substantial** body of work (a head): low `fingerprints_prose` overlap with the other heads. **Lone or with offshoots both qualify** - a lone substantial standalone is a `[main]` with zero forks, and having no children never demotes it. (Real miss: an 1887 head 99% disjoint from the 2110 head was left unmarked because `[main]` was assigned one-per-family; a 2268 frame-naming head, equally disjoint, was missed the same way. Both are `[main]`.)
    - **`[fork] <main>`** - a kept offshoot **mostly contained** in a `[main]` (high overlap) but holding *modest unique content* worth keeping. Name the main it belongs to and what is unique to it. (e.g. a rotations session 82% contained in the 2110 head: `[fork]` of it, unique = the rotations carry-through.)
    - **`[scroll-dep] <main>`** - a `locked` file mostly contained in a `[main]` with **~0 unique** (a pure scrollback bridge). It is kept (not archived) *because it is load-bearing*; that retain-vs-archive call is settled by load-bearing status, never containment %, before any marker is assigned (see the gate in READ THIS FIRST). Name the canonical it backs, and **name the residue read verbatim, never a bare count**: "8 unique" hides that the 8 are policy-refusals; "unique: refusals + asides" tells the reader to skip it. If the residue is genuinely substantive it is not a scroll-dep, it is a `[fork]`.
    - **(no marker)** - a **minor standalone**: a small one-off below the substance floor, in no family. The title alone carries it.

    The four are decided by **three discriminators - two measured, one judged**. **Containment** (near-disjoint vs mostly-inside-a-head) is measured by `fingerprints_prose` overlap; **amount of unique content** (substantial / modest / ~zero) by the unique-fingerprint count against `CEILING` / 0. The **substance floor** (a real body of work vs a minor one-off) is **not** reducible to a constant - **the LLM judges it, and in genuinely uncertain cases the user decides** (several calls in a real run went to the user). The boundaries: `[main]`-vs-`[fork]` is containment (measured); `[fork]`-vs-`[scroll-dep]` is amount-of-unique-content (measured); `[main]`-vs-none is the substance floor (judged). For the floor the numbers give only **eyeball cues, never the verdict**: a session with far fewer of its-own (found-nowhere-else) messages than `CEILING`, or far smaller than the sessions already marked `[main]` (the "order of the other heads" - same rough magnitude), is **possibly** a minor one-off - a flag to look closer, never a presumption. **Small is not the same as unimportant**; raw count alone neither promotes a one-off to `[main]` nor dismisses a small-but-substantial session. `[main]` is reserved for a primary line of work, and that call is judgment, not arithmetic. (Reducing the cue to a hard constant is exactly the proxy the rationale section warns against - so this rule is deliberately a judgment with assists, not a formula.)
- **Read the session before titling it.** A title written from the first message alone is how you get "shell/setup ops" jargon; the real topic is usually mid-conversation.
- **Sonnets may do the READING; the orchestrator keeps the SYNTHESIS global.** For many sessions, the verbatim read is delegable to Sonnet agents (like the Step-5 README agents) - each reports what its session is *for* and quotes representative mid-conversation turns. But the **family labels and the marker taxonomy (`[main]`/`[fork]`/`[scroll-dep]`/none) are a GLOBAL decision**: they depend on cross-session containment and which head a fork belongs to, so independent agents assigning final titles lose consistency (no agent sees the others' family labels). Have agents return per-session substance, then assign family + sub-label + marker yourself in ONE pass holding the whole family map. (For a small store the orchestrator's own per-session digest of user-message throughlines is usually read enough; reach for agents when the sessions are large or many.)
- **Name the substance, never the opening.** Family, sub-label, and sentence describe what the session is *for* - the work it actually did, which lives mid-conversation. Never title from what was on screen when it opened: a seed/context prompt, a skill preamble, the first command, or the first artifact it happened to decode. Real misses: a card-RE fork titled `card-header` after the FAT32 dump on its first screen, and a patch-reapply titled `cli-subprocess` after a mid-session tangent. The first message is the least representative line in the session (same root as `shell/setup ops`); the acceptance test above is the gate.
- **No character limit; "truncates" is not a budget.** Write 1 to 2 complete, plain sentences. "The picker truncates the tail" is the reason to put the gist first so it survives truncation; it is NOT a width to fit, and never a licence to abbreviate, drop words, or compress. A readable sentence the picker cuts off beats a complete one nobody can parse. (Real failure: a run invented a ~96-char cap out of the word "truncates", and once it had a number to optimise, readability lost - it produced cryptic fragments the author themselves could not read.)
- **Acceptance test (pass/fail, not an adjective).** Read the title as someone who never saw the session. If understanding it needs context only the person who ran it holds - raw uuids, coined abbreviations, symbol shorthand - it fails; rewrite. The bar is literally: a colleague who did not do the work, including future-you, understands it cold. If the author of the work cannot parse it, it is wrong.
- **Banned compression tells** (this is what "terse invented phrase" looks like in practice): semicolon-chains standing in for clauses, `/` and `=` as word-savers, dropped articles and verbs, coined compound abbreviations. Write it the way you would say it out loud to a colleague.

    - BAD: `cache: retry/backoff refactor; unique = jitter calc + TTL bug fix (both in client.go)`
    - GOOD: `cache: The widest thread on the retry-and-backoff refactor, covering the connection-pool changes and the exponential-backoff rewrite. Unique to this fork: the jitter-calculation discussion and the TTL-expiry bug fix, both now in client.go.`
- **Established terms only; never coin jargon for a title.** A technical term is allowed only if genuinely established: either globally standard (`FAT32`, `stdin`, `DRM`, `JSONL`) or a term used many times in the actual work (`Patch K`, `Patch E`, named in `patches.md`). The bar is real prior usage on the order of hundreds, never how compact or term-of-art a coinage sounds. NEVER mint an ad-hoc label, abbreviation, or hyphenated compound for the title: `flip-back`, `card-header`, `context-doc`, `cli-subprocess`, `bak-triplet`, `math-probe`, `prebuilt-guard` were all invented at title-time and mean nothing to anyone who was not there. When no established term fits, describe it in plain words; do not manufacture a tag. Plain multi-word English (treatment data, backup files, session rename) is description, not jargon, and is always fine.

    - BAD (opening-anchored + coined): `device card-header: An early Context-Document branch decoding the card's FAT32 BPB fields.`
    - GOOD (substance + established/plain terms, states what stayed open): `device treatment-data: A fork of the card RE that decodes the LBA1 fields, models the daily-activation history, and probes how the card CID ties in, with where the remaining-usage count lives left unresolved.`

Format (exactly what the extension itself writes; append-only, last-wins, reversible):

```json
{"type":"custom-title","customTitle":"<title>","sessionId":"<full-uuid>"}
```

Ensure the file ends with a newline before appending. Two musts:

- **Mtime-neutral by default.** Appending bumps mtime to now, which both collapses the recency-sorted picker (every titled session jumps to "just now") and can re-arm the retention sweep. So capture the file's mtime *before* the append and restore that exact value after: titling then leaves the file exactly as it was, just with a title, and never changes its deletion exposure.

    ```python
    orig = os.path.getmtime(path)
    # ... append the custom-title line ...
    os.utime(path, (orig, orig))   # the file's OWN prior mtime, never "now", never a computed date
    ```

- **Chronological equalisation is opt-in, never the default.** Setting mtime to the session's *true last-activity* timestamp (so the picker is perfectly chronological even when a file's current mtime was bumped by an earlier tool) is a separate pass that requires **explicit user authorisation AND the sweep disabled**. It is exactly the step that deleted 11 sessions in a real run: a computed old mtime trips the sweep, including via the setting-sources bypass. For dormant files left untouched, the pre-append mtime already approximates last-activity order, so the mtime-neutral default usually needs no equalisation at all.
- **Dormant only.** A *live* session (Step 0 registry) flips its title back via the extension's `sessionStates` cache (Patch F), so rename those in-app (`/title` or the sidebar pencil), never by file edit.

Reload the IDE window to see updated titles. `claude --resume ... --name` also sets a title
but resumes the session (adds turns, costs tokens, can trigger compaction), so prefer the
append for dormant files.

## Safety invariants

- **Size is never grounds for deletion or for withholding protection. Small does not imply unimportant.** A one-message session can hold a key, a proof, or the single answer that mattered. The retention sweep deletes small transcripts as readily as large ones, so every guard here is size-agnostic: the backup hook copies every `*.jsonl` however small, `cleanupPeriodDays` and the archive's out-of-sweep location cover all files equally, and archival decisions read content (the CEILING judgment zone), never trust message count. Debris nomination needs a throwaway opening on top of small size and a user confirmation, and is a move, not a delete.
- **Never move or file-edit:** any file in the `locked` closure (cross-file ancestors and phantom-backfill sources, re-closed over the full kept set), canonical files, any fork with >= CEILING unique msgs (unless a durable artifact captures it), any below-CEILING fork whose unique content was judged substantive, or any session in the `~/.claude/sessions` live registry (Step 0, re-read at mutation time; the registry alone decides liveness, never mtime).
- **Move, never delete.** `~/claude-archive` is outside the project dir; the extension stops listing moved files but they are fully recoverable via the manifest. Because the retention sweep walks only `~/.claude/projects/`, a moved file is **sweep-proof regardless of mtime** - so archiving an old session is itself the safest handling of it, not just a decluttering step.
- **Titles are append-only, dormant-only, and mtime-neutral.** Never file-edit a live session's title (it flips back via Patch F); rename live sessions in-app. After appending a `custom-title`, restore the file's *own prior* mtime (captured before the append) - the picker is recency-sorted, and bumping mtime to now destroys the chronological list. Writing a *computed* last-activity mtime (equalisation) is opt-in: user authorisation + sweep disabled, never the default.
- **Agents are read-only on session JSONLs;** only the orchestrator moves files, only after the user gate and the post-move orphan + phantom re-scan.
- Working artifacts and the manifest live under `~/claude-archive/`, never in the project's JSONL dir (the extension globs `*.jsonl`, so a `.json`/`.md` there is ignored, but keep it clean anyway).
- **Back up the whole project dir to a separate device before any destructive step.** A move is only as recoverable as the archived copies + manifest, and the retention sweep plus mtime fragility can still delete *kept* files out from under you - including via the setting-sources bypass (STOP section), which ignores `cleanupPeriodDays` entirely and which this skill's own subagents can trigger mid-run. In a real run 11 kept sessions were deleted mid-cleanup and were recoverable only because an out-of-tree backup on a separate disk happened to exist. Do not rely on the manifest, or on `cleanupPeriodDays`, alone; the `SessionStart` backup hook (STOP section) is the durable guard.

## Knobs

- `CEILING` (default 50): a fork with >= this many unique-vs-kept messages is auto-kept in place. Below it is the *judgment zone*: read the unique messages and archive only the self-evidently worthless; never auto-archive on count (a one-message fork can be priceless). This knob acts only in the keep direction. Over-merge protection is union-on-lpu; false-family detection is union-on-lpu-not-content; neither is this knob's job.
- `DEBRIS_MAX` (default 11): single-file trees this small with throwaway openings are debris candidates (still user-confirmed before moving).

## Design rationale (non-obvious decisions)

These are the choices a reader is most likely to second-guess; the body specifies the mechanics.

- **Phantom protection retains one backfill *source* per needed phantom, not every phantom referencer.** Locking every file that references a needed phantom lpu also locks byte-identical duplicate forks (rooted at the phantom boundary, no in-file pre-content) and guts the cleanup. `locked_closure` keeps **one** origin-bearing source per needed phantom instead - the richest when it must choose fresh, but an already-locked source (e.g. one pulled in by a cross-file edge) satisfies the requirement without adding another, so the kept source is "some source", not necessarily the richest.

- **The keep-lock closure is re-run over the FULL kept set, not just the seed.** A fork kept for its own unique content can itself need a phantom whose only backfill source is content-redundant; the seed closure (canonicals + live) never walks that fork's needs, so a one-pass closure would archive the source and orphan the fork. Re-closing over `KEPT` (`locked = locked_closure(KEPT); KEPT |= locked`) folds every kept file's needs back in, so no kept file's needed phantom is ever left without a kept source. This no-orphan invariant is mechanically proved in `proofs/` (a constructive, axiom-free Lean 4 development; see `proofs/README.md` for exactly what is and isn't covered).

- **The registry is the sole liveness signal; there is no mtime fallback.** `~/.claude/sessions/*.json` lists one entry per running process, so a running session is always registered; the documented near-miss was an agent *not reading* the registry, never the registry omitting a session. recent-mtime added only noise (inspecting an old chat bumps its mtime), so it is gone. The Step-0 read plus a Step-6 mutation-time re-read make the registry authoritative end to end, narrowing any snapshot-staleness to the instant of the move.

- **Content-fork detection is prose-only, not raw fingerprint overlap.** Raw overlap is confounded by shared tool-results: two sessions that edited the same files share those fingerprints. A real case showed 100% raw but 1% prose overlap between unrelated conversations, and 82% prose between a mis-themed fork and its true family. The lpu tree alone under-groups (copied-content forks share no lpu).

- **Count never auto-archives; `CEILING` only auto-*keeps*.** A single message can hold a key or a proof, so a below-`CEILING` fork goes through a verbatim read, never an automatic archive. The only count-based archive is exact zero-unique containment.

- **Steps 4-5 run from the top-level session only.** A sub-agent cannot spawn its own sub-agents, so the themer / per-theme parallelism exists only at the top level.

- **Titling is mtime-neutral by default; chronological equalisation is opt-in.** Restoring the file's own pre-append mtime keeps the recency-sorted picker intact without ever changing a file's deletion exposure. Writing a *computed* true-last-activity mtime would order the picker perfectly but is exactly what deletes old sessions under the live sweep, so it requires user authorisation and the sweep disabled.

- **Every qualitative rule ships with a concrete acceptance test, or it gets proxied around.** Under any pressure, a qualitative instruction ("readable", "complete", "looks done") invites substituting an invented quantitative proxy - a character count, "looks finished", an assumed live-session id - and then optimising the proxy instead of the goal. It is what produced the cryptic titles (a self-invented 96-char cap), and it is the same shape as eyeballing size instead of measuring unique content. The countermeasure is to give each qualitative rule a test it can *fail*: titling has "a colleague who did not do the work understands it cold"; keep/archive has the unique-vs-kept number plus a verbatim read. A rule with nothing to fail against is a rule that will be proxied.

- **Recovery is out of scope.** If sessions are already lost, that is the companion `recover-deleted-sessions-ext4` skill.
