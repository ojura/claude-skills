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

    wired in `~/.claude/settings.json` as `{"hooks":{"SessionStart":[{"hooks":[{"type":"command","command":"~/.claude/hooks/backup-sessions.sh"}]}]}}`. Because it never overwrites with a smaller file, no rewind, compaction, or mtime edit can shrink the backup below the largest state ever captured - which, for an append-only file, contains all real content. (A content-hash-per-state variant - one backup file per distinct hash, never overwrite - is maximally defensive if you distrust the append-only guarantee, but it costs O(growth) storage: a full copy at every session start the file grew. Under the verified append-only behaviour it preserves nothing high-water doesn't, so it is not the default.)

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

There are **two** classes of load-bearing file, and you must protect both:

1.  **Cross-file lpu targets.** File B is load-bearing if some other file references (via lpu) a uuid that B owns. Easy to compute from `global_uuid`.
2.  **Phantom-lpu backfill sources.** When a kept session has a `compact_boundary` whose lpu is phantom and which sits at the chain root (no in-file pre-content), Patch K reconstructs that session's origin from a **sibling that shares the same phantom lpu AND has real messages before its own first boundary with that phantom lpu**. That sibling is load-bearing even though no uuid links to it. Miss this and you silently truncate the kept session's deep scrollback.

   **But do not over-correct.** Locking *every* file that merely references a phantom lpu is wrong: a byte-identical duplicate fork whose phantom boundary sits at the chain head (`parentUuid:null`, zero in-file messages before it) holds no *in-file* pre-content to backfill from, so it is not a load-bearing **source** and is safe to archive. Lock a phantom-sibling only if it is a viable source (has pre-content before its first boundary with that phantom lpu), and you only need to retain **at least one** source per phantom a kept file needs.

**HARD RULE: a `compact_boundary` is never an origin.** It is always a stitch with a real predecessor - in-file, cross-file (Patch J), or phantom-shared (Patch K). A boundary at `parentUuid:null` with nothing before it does NOT mean the session is rootless / standalone / originless; it means its origin is **entirely external** and must be reconstructed - which is the case that most needs lineage work, not the one to dismiss. Concluding "this file carries no origin / is a standalone" from a leading boundary is a hard error: in a real run it produced a wrong family map (forks of one big conversation got mis-read as separate standalones, and a fork's `[main]` was nearly dropped). In code, `nb==0` at a boundary means "origin is external," never "no origin." Safety to archive must come from **content-redundancy** (0 unique vs the kept set), never from a file "looking rootless."

**HARD RULE: keep, `[scroll-dep]`, and archive are ONE decision, made by measurement + read - identical rigor for all three.** Every file gets the same two things: the unique-vs-kept set-difference (a number) and a verbatim read of that unique residue. The skill's archive path already does this; the trap is exempting the *locked* set because it is load-bearing. **Load-bearing protects a file from being *moved*, never from being *measured*.** Three corollaries:

- **Measure the locked set too; never infer content from size.** Run the unique-vs-kept set-difference on every load-bearing file. Load-bearing AND ~zero-unique ⇒ `[scroll-dep]`, established by the number, not by length. (Real miss: a 2142-message locked file was eyeballed as "must be content"; measured, it held 8 unique messages, all policy-refusals and banter.)
- **Redundant structural-role files are archive candidates, not auto-keeps.** When several files can serve one structural role (e.g. two phantom-backfill sources for the same phantom), keep the richest and run the rest through the archive judgment on their read residue. (Real miss: a redundant source survived only by over-keeping; its 45 "unique" were operational noise plus two findings already present in a kept file.)
- **A wrong seed silently skips measurement.** A file wrongly seeded as live (Step 0) or wrongly locked never gets measured - which is how a session 99% contained in the *real* live session stays in the picker. So if a "live" or load-bearing file *measures* ~zero-unique against another, suspect the seed/lock, re-check Step 0, and treat it as the redundant copy it is. "It's big" and "it's load-bearing" never substitute for the number or the read.

The fork/compaction data model (Patches A, D, F, H, J, K) is documented in `github.com/ojura/claude-patches` (`docs/patches.md`); read it first if you have it. If not, the rules above are the operative summary. **If sessions have already been lost/deleted, this skill does not recover them - see the companion `recover-deleted-sessions-ext4` skill.**

## Step 0 - read the live-session registry (authoritative, do this FIRST)

Before touching anything, read `~/.claude/sessions/*.json`: one JSON per **running** Claude
Code process, `{pid, sessionId, cwd, status, updatedAt}`. Each `sessionId` is a live
conversation; its `<uuid>.jsonl` is **never-touch** (no move, no file-edit, no retitle by
file edit). This registry is the source of truth for what is running right now.

**Do NOT infer live state from file mtime, "modified today," or the task-output tmp path.**
They mislead: `--fork-session` mints a new session id, so the live file can differ from what
recency or the tmp dir suggests (real case: heuristics pointed at `a24119fe` while the
registry showed the live session was `464771a1`). The `RECENT_HOURS` mtime check in Step 2 is
only a fallback for sessions with no registry entry. (Optionally `os.kill(pid, 0)` to drop a stale `<pid>.json` from a crashed process; treating a stale entry as live merely over-protects, which is benign.)

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

This pass decides what is safe to move, so it must be exact. Gotchas confirmed in practice:

- `jq` is sometimes a broken snap. Use `python3`.
- Files start with large `file-history-snapshot` records (most of the bytes). Skip them: `if '"file-history-snapshot"' in line[:80]: continue`.
- `timestamp` is sometimes an epoch-ms **integer**, sometimes an ISO string. Normalise before comparing.
- Fork messages get **fresh UUIDs**, so grouping by a shared root uuid fails. Group by lpu, not by uuid.
- A long file is not the most complete one: fork-debugging sessions replay content, so compare **distinct** fingerprints, not raw message counts.
- `/rewind` and ctrl-z do **not** shrink the on-disk JSONL; they orphan a dead branch and keep appending (the rendered conversation shrinks, the file only grows). So a fingerprint pass over the raw file includes orphaned dead-branch messages: distinct-count reflects *all content the file ever held*, not just the live `parentUuid` chain. This is usually harmless (those messages are still real content, and containment/dedup handle them), but be aware a heavily-rewound session's distinct-count is inflated by dead branches when ranking canonicals.
- The judgment zone and the per-theme docs both need verbatim text, but the design forbids reopening multi-MB files downstream. So capture a capped **fingerprint -> text** map here, in the one pass.
- **Compute TWO fingerprint sets, both load-bearing.** A **raw** set (`fps`, hashes `type` + full `content` via `json.dumps`, so it includes `tool_use` / `tool_result`) drives byte-redundancy and containment, i.e. archival **safety** - using prose here would over-archive (same discussion done with different tool work looks "contained"). A **prose** set (`fps_prose`, user/assistant **text only**, no tool blocks) drives **family / theme grouping** - using raw here under-discriminates (two sessions that edited the same files look like one family). They are only half-distinguishable after the fact, so build both in this pass.

Per file extract: msg count, distinct-msg count, first plain user message, gitBranch,
first/last timestamp (normalised), set of owned uuids, lpu references, the `compact_boundary`
records as `(lpu, parentUuid, n_msgs_before_this_boundary)`, the **raw** and **prose** ordered
fingerprint lists, and the capped fingerprint->text map. Then build the lpu DAG and partition:

```python
import json, glob, os, hashlib, datetime, time
from collections import defaultdict
def f8(p): return os.path.basename(p)[:8]
def norm(ts):
    if ts is None: return ""
    if isinstance(ts,(int,float)):
        try: return datetime.datetime.utcfromtimestamp(ts/1000).isoformat()
        except: return str(ts)
    return str(ts)

fps={}; fps_prose={}; ftext={}; owned={}; lref={}; bnd={}; nmsg={}; lts={}; mtime={}; global_uuid=defaultdict(set)
for p in sorted(glob.glob("*.jsonl")):
    k=f8(p); F=[]; Fp=[]; us=set(); L=set(); B=[]; last=""; n=0
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
            if h not in ftext:   # capped raw-fingerprint -> verbatim text, for downstream quoting
                ftext[h]=(o["type"], disp.replace("\n"," ")[:600])
            t=norm(o.get("timestamp"));  last=max(last,t) if t else last
        if o.get("type")=="system" and o.get("subtype")=="compact_boundary":
            B.append((o.get("logicalParentUuid"), o.get("parentUuid"), n))  # n = msgs before boundary
    fps[k]=F; fps_prose[k]=Fp; owned[k]=us; lref[k]=L; bnd[k]=B; nmsg[k]=len(F); lts[k]=last
    mtime[k]=os.path.getmtime(p)

# phantom lpus = referenced but owned by no file
all_lpus={lp for k in bnd for (lp,_,_) in bnd[k] if lp}
phantom={lp for lp in all_lpus if lp not in global_uuid}
def sources(k):   # phantom lpus this file can BACKFILL (has pre-content before that phantom boundary)
    return {lp for (lp,par,nb) in bnd[k] if lp in phantom and (par is not None or nb>0)}
def needs(k):     # phantom lpus this file relies on a sibling for (boundary at root, no pre-content)
    return {lp for (lp,par,nb) in bnd[k] if lp in phantom and par is None and nb==0}

# union-find trees: link files sharing an lpu value, or a cross-file dep edge
parent={k:k for k in fps}
def find(x):
    while parent[x]!=x: parent[x]=parent[parent[x]]; x=parent[x]
    return x
bylpu=defaultdict(list)
for k in fps:
    for lp in lref[k]: bylpu[lp].append(k)
for ks in bylpu.values():
    for k in ks[1:]: parent[find(k)]=find(ks[0])
dep={(a,b) for a in fps for lp in lref[a] for b in global_uuid.get(lp,()) if b!=a}
for a,b in dep: parent[find(a)]=find(b)
trees=defaultdict(list)
for k in fps: trees[find(k)].append(k)

# canonical: most DISTINCT content, recency as tiebreak, among non-debris (content floor)
DEBRIS_MAX=11
def is_debris(k): return nmsg[k] <= DEBRIS_MAX
def canonical(ks):
    cand=[k for k in ks if not is_debris(k)] or ks
    return max(cand, key=lambda k:(len(set(fps[k])), lts[k]))

# keep-locked closure: seed + transitive load-bearing over BOTH edge types
RECENT_HOURS=12
# AUTHORITATIVE never-touch: live sessionIds from ~/.claude/sessions/*.json (Step 0)
live=set()
for sp in glob.glob(os.path.expanduser("~/.claude/sessions/*.json")):
    try: live.add(json.load(open(sp)).get("sessionId","")[:8])
    except: pass
seed={canonical(ks) for ks in trees.values() if len(ks)>1}
seed|={k for k in fps if k in live}                                    # live (authoritative)
seed|={k for k in fps if (time.time()-mtime[k]) < RECENT_HOURS*3600}   # fallback: recently active
def locked_closure(seed):
    locked=set(seed); changed=True
    while changed:
        changed=False
        for k in list(locked):
            for lp in lref[k]:                          # (1) cross-file ancestors
                for b in global_uuid.get(lp,()):
                    if b not in locked: locked.add(b); changed=True
            for P in needs(k):                          # (2) phantom backfill: keep >=1 source
                srcs=[s for s in fps if P in sources(s)]
                if srcs and not (set(srcs)&locked):
                    best=max(srcs, key=lambda s:len(set(fps[s])))  # richest origin
                    locked.add(best); changed=True
    return locked
locked=locked_closure(seed)

# MEASURE the locked set the SAME way as archive candidates - load-bearing protects a file from
# being MOVED, never from being MEASURED. Each locked file's unique-vs-rest-of-kept decides its
# title and flags redundant structural-role dups, by the number + a verbatim read, never by size.
kept_all=set(locked) | {canonical(ks) for ks in trees.values()}
for k in sorted(locked):
    rest=set().union(*(set(fps[j]) for j in kept_all if j!=k))
    uniq=set(fps[k])-rest
    # uniq==0        -> [scroll-dep]; title names the residue ("none"), not a count
    # 0<uniq<CEILING -> READ uniq verbatim (ftext). If trivial: [scroll-dep] titled by what the
    #                   residue IS; and if another kept file fills k's structural role, k is a
    #                   REDUNDANT source -> archive candidate, NOT an auto-keep.
    # uniq>=CEILING  -> genuinely-unique kept fork: its own label, not [scroll-dep].

# archive candidate = non-locked, non-canonical fork whose UNIQUE content is trivial
# (< CEILING msgs) AFTER reading it, OR captured in a user-named durable artifact. Else keep.
CEILING=50         # >= this many unique msgs => AUTO-KEEP. Below => JUDGE content, never auto-archive.
for ks in trees.values():
    if len(ks)==1: continue
    canon=canonical(ks); keep=set(ks)&locked | {canon}
    kept_fp=set().union(*(set(fps[k]) for k in keep)) if keep else set()
    for k in [x for x in ks if x not in keep]:
        uniq=set(fps[k])-kept_fp           # content found NOWHERE in the kept set
        # >= CEILING -> keep in place (substantial unique work, don't even ask)
        # <  CEILING -> JUDGMENT ZONE: read uniq VERBATIM (via ftext) and judge its value;
        #               archive ONLY if self-evidently worthless (empty/tool-only turns, exact
        #               replays already in the canonical, fork-test artifacts, trivial one-shot
        #               Q&A). Any substantive content (a decision, derivation, key, datum,
        #               non-trivial answer, code) -> FLAG for human judgment, default KEEP.
        # Count is NEVER the sole archive trigger; CEILING only auto-keeps (a 1-message fork can
        # hold a private key or a proof). Record uniq verbatim (ftext) for the doc + the judgment.
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

After the per-tree partition, run a global containment pass. KEPT = canonicals + locked
(load-bearing) + kept-unique forks.

```python
keptset={k:set(fps[k]) for k in KEPT}
canon_all={canonical(ks) for ks in trees.values()}
for A in [a for a in fps if a not in KEPT and a not in canon_all and a not in live]:
    # fewest unique messages against ANY single equal-or-larger kept container
    cands=[b for b in KEPT if len(set(fps[b]))>=len(set(fps[A]))]
    best=min(cands, key=lambda b: len(set(fps[A])-keptset[b]), default=None)
    missing = (set(fps[A]) - keptset[best]) if best else set(fps[A])
    if not missing:                       # 0 unique: PROVEN redundant, every msg verbatim in best
        ARCHIVE(A, f"100% contained in {best}")
    elif len(missing) < CEILING:          # JUDGMENT ZONE
        SONNET_CONFIRM(A, best, missing)  # a Sonnet READS missing; archive iff trivial/preserved
    # else: substantial unique -> KEEP
```

Two rules learned the hard way:

- **`0 unique` is exact set-containment, not a heuristic.** Every (type, content) message of A is byte-identical to one in `best`, so archiving A loses nothing. Safe without a Sonnet.
- **Any NONZERO unique is a `likely`-style claim and MUST be confirmed by a Sonnet that READS the unique messages.** A keyword/mention count in the container (e.g. "BodyFrameId appears 2174 times") does NOT prove the specific exchange is preserved. The Sonnet reads each unique message and either confirms it is trivial or genuinely preserved elsewhere (archive) or flags it as unique work (keep). The zero-unique archive is the **recall** gate; the Sonnet read is the **precision** gate. Both are required: maximum cleanup, zero loss.

Never archive a canonical even when it is highly contained in one of its own forks (forks
replay most content; the canonical is the primary by recency + distinct-content).

(`KEPT`, `ARCHIVE()`, `SONNET_CONFIRM()` above are pseudocode placeholders; wire them to your
kept-set and your move/confirm steps.)

**Deterministic debris nomination.** The per-tree partition skips single-file trees
(`if len(ks)==1: continue`), so a standalone throwaway (an empty `/clear`, a `/resume`, a bare
`cd`, a one-shot that produced nothing) is never nominated by the script and falls entirely to
the themer's soft flag plus the user gate - the weakest link, and "a clean list" usually
hinges on debris removal. Nominate it deterministically too:

```python
for ks in trees.values():
    if len(ks)!=1: continue
    k=ks[0]
    if k in locked or k in live: continue
    fu=first_plain_user_msg(k)   # first non-tool, non-meta user message
    if nmsg[k] <= DEBRIS_MAX and (not fu or fu.lstrip().startswith(("/","cd ")) or is_boilerplate(fu)):
        nominate_debris(k)       # route to a debris/ theme; still shown in the user gate before moving
```

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
- **Content-fork members (lpu-union UNDER-groups - do not stop at the lpu tree).** lpu grouping only catches forks that share an lpu value. A fork made by copying messages (fresh uuids, no shared lpu), or a long conversation that continued under a new topic/issue, will NOT union by lpu and masquerades as a separate session/theme. Detect these with a **prose-only** content-overlap pass over the `fps_prose` set from Step 2 (user/assistant TEXT only, no `tool_use`/`tool_result`): if a file shares more than ~60% of its `fps_prose` messages with a larger family member, it is a fork of that family. **Use `fps_prose`, never raw `fps`, for this** - raw content-overlap is confounded by shared tool-results (two sessions that edited the same files share those fingerprints). Real run: one session was 82% *prose*-shared with another theme's canonical (same family, but mis-themed as separate), while a tooling-heavy session showed 100% *raw* overlap with that same canonical yet 1% prose (all shared file-edits) and was a different conversation. The lpu tree is necessary but not sufficient for family membership.
- **Canonical** = most *distinct* content, recency as tiebreak, chosen among non-debris files (a content floor, so a 3-message newest fork-test never wins over a 2000-message sibling). This ranks on raw-distinct (`len(set(fps[k]))`), which counts tool-churn as content; within a true fork family that still picks the most complete member. If you ever see a canonical chosen wrongly because one member is tool-heavy rather than conversation-heavy, switch the rank key to prose-distinct (`len(set(fps_prose[k]))`); not worth doing pre-emptively.
- **Load-bearing (never move)** = the `locked` closure: cross-file lpu targets plus phantom-backfill sources, transitively, seeded from canonicals, live sessions (Step 0), and recently-active sessions.
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

Each README MUST include an **`## Ingestion`** section: what the theme's canonical should fold
in, measured as **unique-vs-canonical** (set-difference against the canonical's OWN
fingerprints, not the whole kept set), split by provenance: (a) **from archived forks**, and
(b) **from retained forks**. The retained-fork half is the one people forget: a kept
*divergent* fork can hold content the canonical lacks, because the canonical only renders its
lpu-*ancestors* cross-file, not divergent siblings. Quote those uniques verbatim from `ftext`.
Load-bearing ancestors are the canonical's own history rendered cross-file, so they are
explicitly "nothing to ingest".

## Step 6 - finalize the moves (orchestrator only)

0.  **Backup precondition (checked gate, not just a principle).** Before the *first* mutation in the whole run - this move, or Step 7's titling/equalisation, whichever you reach first - confirm a current out-of-tree backup exists OR the `SessionStart` backup hook has fired this session. Do not proceed past this line otherwise. This is the gap that bites in practice: equalising or moving before any backup exists means the first slip has no recovery point.
1.  Aggregate the keep/archive list. **Show the user the full list (forks + every debris file, with unique-msg counts) and get explicit go/no-go before moving anything.**
2.  Create `~/claude-archive/<theme>/` dirs (agents already wrote the READMEs there).
3.  `mv` each confirmed file into its theme dir, preserving the uuid filename. Write `~/claude-archive/manifest.json`: archived-file -> original-path -> theme -> canonical -> reason (enough to restore with one command).
4.  **Post-move verification (hard gate):** re-scan the project dir; for every kept canonical confirm (a) all its cross-file lpu targets still resolve in the project dir, and (b) every phantom it needs still has a kept backfill source. Zero orphans on both. Confirm `total - moved` files remain and no live/registry session was touched.

**Operational / low-value but distinct sessions** (skill re-runs, toy experiments, one-off
config tasks, stray Q&A) are neither redundant nor debris: their content is unique, just
low-value. Do NOT archive them on a vibe. Present each one INDIVIDUALLY to the user (a per-item
keep/archive choice, each with a real 1-2 sentence description of what it actually contains)
and default to KEEP. Only deterministic debris and Sonnet-confirmed-redundant content archive
without a per-item user decision.

## Step 7 - title the retained sessions (dormant-only)

Give the picker a themed shape by appending a `custom-title` record to each retained session:

- **Format: `<family> <sub-label>: <1 to 2 sentences, gist first>`.** Use an **umbrella family** label (e.g. `backend`, `claude-patches`) with a **sub-label** for the sub-area: an **issue number** where the store has issue tracking (`1234`, `1290`), otherwise a short **topic tag** (`auth`, `search`, `parser`). Assign the sub-label by **topic, not branch**: many sessions all run on one long-lived branch, so the branch field is a poor theme signal. (The `[main]` / `[scroll-dep]` markers below are project-agnostic and apply either way.) The sentences say what the session actually is or did; the picker truncates the *tail*, which is the reason to front-load the gist - NOT a width to fit (see "no character limit" below). NOT a bare slug, NOT a terse invented phrase (a real run rejected `fork-empty` and `shell/setup ops` as meaningless), NOT 3+ sentences, and NEVER the raw first message (a tool/command echo or `<ide_opened_file>` tag). Date-only differentiators (`(Apr 3)`) are uninformative.
- Canonical of a fork-family theme -> prefix with **`[main]`** at the very front, then the label + what the whole thread is/did. (`[main]` pairs with `[scroll-dep]` so the family's primary and its kept scrollback segments are both flagged even when the picker truncates.) **A distinct sub-lineage head also gets `[main]`** - it is not one-per-family: e.g. within a `claude-patches` family, both the empty-fork-diagnosis canonical and the patch-development head are `[main]` of their respective sub-lineages. A retained fork with only modest unique content -> label + what is unique to it (no marker).
- A load-bearing segment kept ONLY for scrollback (a `locked` file measured ~zero-unique against the kept set) -> prefix the WHOLE title with **`[scroll-dep]`** at the very front, before the label, so it is flagged even when the picker truncates; then name which canonical it backs. **Name the unique residue, read verbatim - never a bare count.** This is the titling-side of "judge the messages, never the count": "8 unique" hides that the 8 are policy-refusals and falsely implies something worth mining, whereas "unique: refusals + asides" tells the reader to skip it. A `[scroll-dep]` entry's whole value is naming the irreplaceable slice, so the read precedes the title; if the residue is genuinely substantive, it is not a scroll-dep, it is a kept fork with its own label.
- **Read the session before titling it.** A title written from the first message alone is how you get "shell/setup ops" jargon; the real topic is usually mid-conversation.
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
- **Never move or file-edit:** any file in the `locked` closure (cross-file ancestors and phantom-backfill sources), canonical files, any fork with >= CEILING unique msgs (unless a durable artifact captures it), any below-CEILING fork whose unique content was judged substantive, or any session in the `~/.claude/sessions` live registry (Step 0; RECENT_HOURS mtime is only a fallback).
- **Move, never delete.** `~/claude-archive` is outside the project dir; the extension stops listing moved files but they are fully recoverable via the manifest. Because the retention sweep walks only `~/.claude/projects/`, a moved file is **sweep-proof regardless of mtime** - so archiving an old session is itself the safest handling of it, not just a decluttering step.
- **Titles are append-only, dormant-only, and mtime-neutral.** Never file-edit a live session's title (it flips back via Patch F); rename live sessions in-app. After appending a `custom-title`, restore the file's *own prior* mtime (captured before the append) - the picker is recency-sorted, and bumping mtime to now destroys the chronological list. Writing a *computed* last-activity mtime (equalisation) is opt-in: user authorisation + sweep disabled, never the default.
- **Agents are read-only on session JSONLs;** only the orchestrator moves files, only after the user gate and the post-move orphan + phantom re-scan.
- Working artifacts and the manifest live under `~/claude-archive/`, never in the project's JSONL dir (the extension globs `*.jsonl`, so a `.json`/`.md` there is ignored, but keep it clean anyway).
- **Back up the whole project dir to a separate device before any destructive step.** A move is only as recoverable as the archived copies + manifest, and the retention sweep plus mtime fragility can still delete *kept* files out from under you - including via the setting-sources bypass (STOP section), which ignores `cleanupPeriodDays` entirely and which this skill's own subagents can trigger mid-run. In a real run 11 kept sessions were deleted mid-cleanup and were recoverable only because an out-of-tree backup (`~/claude_archive`, a separate disk) happened to exist. Do not rely on the manifest, or on `cleanupPeriodDays`, alone; the `SessionStart` backup hook (STOP section) is the durable guard.

## Knobs

- `CEILING` (default 50): a fork with >= this many unique-vs-kept messages is auto-kept in place. Below it is the *judgment zone*: read the unique messages and archive only the self-evidently worthless; never auto-archive on count (a one-message fork can be priceless). This knob acts only in the keep direction. Over-merge protection is union-on-lpu; false-family detection is union-on-lpu-not-content; neither is this knob's job.
- `RECENT_HOURS` (default 12): fallback only. The `~/.claude/sessions` registry (Step 0) is authoritative for live sessions; this mtime window just catches sessions with no registry entry.
- `DEBRIS_MAX` (default 11): single-file trees this small with throwaway openings are debris candidates (still user-confirmed before moving).

## Design rationale (non-obvious decisions)

These are the choices a reader is most likely to second-guess; the body specifies the mechanics.

- **Phantom protection retains one backfill *source* per needed phantom, not every phantom referencer.** Locking every file that references a needed phantom lpu also locks byte-identical duplicate forks (rooted at the phantom boundary, no in-file pre-content) and guts the cleanup. `locked_closure` keeps the richest origin-bearing source instead.

- **Content-fork detection is prose-only, not raw fingerprint overlap.** Raw overlap is confounded by shared tool-results: two sessions that edited the same files share those fingerprints. A real case showed 100% raw but 1% prose overlap between unrelated conversations, and 82% prose between a mis-themed fork and its true family. The lpu tree alone under-groups (copied-content forks share no lpu).

- **Count never auto-archives; `CEILING` only auto-*keeps*.** A single message can hold a key or a proof, so a below-`CEILING` fork goes through a verbatim read, never an automatic archive. The only count-based archive is exact zero-unique containment.

- **Steps 4-5 run from the top-level session only.** A sub-agent cannot spawn its own sub-agents, so the themer / per-theme parallelism exists only at the top level.

- **Titling is mtime-neutral by default; chronological equalisation is opt-in.** Restoring the file's own pre-append mtime keeps the recency-sorted picker intact without ever changing a file's deletion exposure. Writing a *computed* true-last-activity mtime would order the picker perfectly but is exactly what deletes old sessions under the live sweep, so it requires user authorisation and the sweep disabled.

- **Every qualitative rule ships with a concrete acceptance test, or it gets proxied around.** Under any pressure, a qualitative instruction ("readable", "complete", "looks done") invites substituting an invented quantitative proxy - a character count, "looks finished", an assumed live-session id - and then optimising the proxy instead of the goal. It is what produced the cryptic titles (a self-invented 96-char cap), and it is the same shape as eyeballing size instead of measuring unique content. The countermeasure is to give each qualitative rule a test it can *fail*: titling has "a colleague who did not do the work understands it cold"; keep/archive has the unique-vs-kept number plus a verbatim read. A rule with nothing to fail against is a rule that will be proxied.

- **Recovery is out of scope.** If sessions are already lost, that is the companion `recover-deleted-sessions-ext4` skill.
