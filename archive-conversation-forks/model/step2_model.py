#!/usr/bin/env python3
"""
Independent executable model of SKILL.md Step 2 set algebra.

Goal: adversarially search for counterexamples to the four claimed safety properties:
 (i)   archiving never drops the LAST backfill source of a kept session's NEEDED phantom
 (ii)  a content-redundant file archived by the recall pass loses no unique message
 (iii) a live session is never archived or retitled
 (iv)  the marker partition is exhaustive and mutually exclusive

We model files abstractly with the exact fields the SKILL.md pass uses, faithfully transcribe
the pipeline (seed -> locked_closure -> per-tree judge -> KEPT -> re-close -> loadbearing ->
C5 demotion -> C6 deferred archive -> recall pass -> marker loop), then check the invariants
on random stores. judge() (the operator) and the recall SONNET_CONFIRM are modelled as ADVERSARIAL
oracles: they can return any decision, so we test "for all judge policies" like the Lean claims.

Abstraction: a "message" is an int (its raw fingerprint). A file has:
  - fingerprints:   set of raw fingerprints (drives redundancy/containment)
  - fingerprints_prose: set of prose fingerprints (drives marker overlap)
  - lref:  set of lpu values it references (boundary or in-file)
  - owned: set of uuids it owns
  - boundaries: list of (lpu, parentUuid_isNone, n_before)  -- to derive needs()/sources()
  - is_live: bool (from registry)
We DON'T parse JSONL; we generate the post-parse fields directly (the parse is the Python
boundary the Lean explicitly leaves to fuzz; we're auditing the SET ALGEBRA above it).
"""
import random, itertools, sys
from collections import defaultdict

CEILING = 50
DEBRIS_MAX = 11

class Store:
    def __init__(self):
        self.files = []          # list of keys
        self.fingerprints = {}
        self.fingerprints_prose = {}
        self.lref = {}
        self.owned = {}
        self.bnd = {}            # key -> list of (lpu, par_is_none(bool), nb)
        self.live = set()
        self.global_uuid = defaultdict(set)

    def finalize(self):
        for k in self.files:
            for u in self.owned[k]:
                self.global_uuid[u].add(k)
        self.all_lpus = {lp for k in self.files for (lp,_,_) in self.bnd[k] if lp is not None}
        self.phantom = {lp for lp in self.all_lpus if lp not in self.global_uuid}

    # VERBATIM from SKILL.md:
    # def sources(k): return {lp for (lp,par,nb) in bnd[k] if lp in phantom and (par is not None or nb>0)}
    # def needs(k):   return {lp for (lp,par,nb) in bnd[k] if lp in phantom and par is None and nb==0}
    def sources(self, k):
        return {lp for (lp, par_is_none, nb) in self.bnd[k]
                if lp in self.phantom and ((not par_is_none) or nb > 0)}
    def needs(self, k):
        return {lp for (lp, par_is_none, nb) in self.bnd[k]
                if lp in self.phantom and par_is_none and nb == 0}


def run_pipeline(S, judge_oracle, recall_oracle, marker_substantive_oracle, buggy_debris_last=False):
    """Faithful transcription of SKILL.md Step 2. Returns a dict of results + records of every
    archive/mark decision, plus enough state to check invariants.

    `buggy_debris_last`: if True, run nominate_debris in the OLD (buggy) position - debris is
    discarded from KEPT only AFTER C5/recall measured residue/containment against a KEPT that still
    held it. This is the ordering the consensus fix replaces; check_no_lost_message FAILS on it.
    If False (default, the FIX), debris is discarded in the one safe window - after loadbearing/needed
    are frozen, before C5/recall/markers - so it masks no one's residue or containment."""
    fingerprints = S.fingerprints; fingerprints_prose = S.fingerprints_prose; lref = S.lref; global_uuid = S.global_uuid
    bnd = S.bnd; live = S.live; phantom = S.phantom
    files = S.files
    # nmsg = DISTINCT-message count, matching SKILL.md:313 `is_debris(k): len(set(fingerprints[k])) <= DEBRIS_MAX`.
    # (Here fingerprints[k] is already a set, so set() is a no-op; in the real Python F is a LIST built by
    #  F.append, where nmsg=len(F) is RAW while is_debris uses len(set(F)) DISTINCT - they diverge there.
    #  We model the DISTINCT form the debris guard actually uses.)
    nmsg = {k: len(set(fingerprints[k])) for k in files}
    lts  = {k: S.lts[k] for k in files}

    def sources(k): return S.sources(k)
    def needs(k): return S.needs(k)

    # union-find
    parent = {k: k for k in fingerprints}
    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]; x = parent[x]
        return x
    bylpu = defaultdict(list)
    for k in fingerprints:
        for lp in lref[k]: bylpu[lp].append(k)
    for ks in bylpu.values():
        for k in ks[1:]: parent[find(k)] = find(ks[0])
    dep = {(a,b) for a in fingerprints for lp in lref[a] for b in global_uuid.get(lp,()) if b != a}
    for a,b in dep: parent[find(a)] = find(b)
    trees = defaultdict(list)
    for k in fingerprints: trees[find(k)].append(k)

    def is_debris(k): return nmsg[k] <= DEBRIS_MAX
    def canonical(ks):
        cand = [k for k in ks if not is_debris(k)] or ks
        # key=(distinct, lts, uuid); model uuid order by key string
        return max(cand, key=lambda k: (len(set(fingerprints[k])), lts[k], k))

    unsatisfiable = {}
    def locked_closure(seed):
        locked = set(seed); changed = True
        while changed:
            changed = False
            for k in list(locked):
                for lp in lref[k]:
                    for b in global_uuid.get(lp, ()):
                        if b not in locked: locked.add(b); changed = True
                for P in needs(k):
                    srcs = [s for s in fingerprints if P in sources(s)]
                    if not srcs:
                        unsatisfiable.setdefault(k, set()).add(P); continue
                    if not (set(srcs) & locked):
                        best = max(srcs, key=lambda s: len(set(fingerprints[s])))
                        locked.add(best); changed = True
        return locked

    seed = {canonical(ks) for ks in trees.values() if len(ks) > 1}
    seed |= {k for k in fingerprints if k in live}
    locked = locked_closure(seed)
    canonicals = {canonical(ks) for ks in trees.values()}

    # per-tree judge
    kept_unique_forks = set(); tree_archive_candidates = set()
    judge_calls = []
    for ks in trees.values():
        if len(ks) == 1: continue
        canon = canonical(ks); keep = set(ks) & locked | {canon}
        kept_fp = set().union(*(set(fingerprints[k]) for k in keep)) if keep else set()
        for k in [x for x in ks if x not in keep]:
            uniq = set(fingerprints[k]) - kept_fp
            if len(uniq) >= CEILING:
                kept_unique_forks.add(k)
            else:
                judge_calls.append((k, frozenset(uniq)))
                if judge_oracle(k, uniq):     # True => keep
                    kept_unique_forks.add(k)
                else:
                    tree_archive_candidates.add(k)

    KEPT = canonicals | set(locked) | kept_unique_forks
    unsatisfiable.clear()
    locked = locked_closure(KEPT); KEPT |= locked

    consumers = KEPT | live
    loadbearing = {b for a in consumers for lp in lref[a] for b in global_uuid.get(lp,()) if b != a}
    needed = set().union(*(needs(a) for a in consumers)) if consumers else set()
    loadbearing |= {s for s in fingerprints if sources(s) & needed}

    # ---- Step-2 nominate_debris (SKILL.md:552-560) ----
    # The one safe window. Debris MUST be discarded from KEPT exactly here: AFTER loadbearing/needed are
    # frozen (lines above), BEFORE C5/recall/markers measure residue & containment (lines below).
    #   * Why not earlier (before loadbearing): a command-only-shell debris file can still carry
    #     lref/needs edges; while it is a CONSUMER its edges may make ANOTHER file load-bearing.
    #     Dropping it before loadbearing is computed would strip a real source's protection and
    #     wrongly archive it - a NEW orphan bug. So debris stays a consumer when loadbearing freezes.
    #   * Why not later (after recall, the buggy_debris_last path): debris left in KEPT MASKS other
    #     files' residue/containment - C5 sees a fork's only message duplicated in a debris shell and
    #     0-demotes it; recall sees a candidate's message covered by a debris shell and 0-archives it;
    #     then debris is moved too -> the message survives only in moved files. That is the
    #     loss class check_no_lost_message catches (debris masking a recall candidate, or a C5 demotion).
    # The guard is `loadbearing`, NEVER `locked` (SKILL.md:555,563): when most sessions are standalone
    # singletons the C4 re-close balloons `locked` to ~all files, so `if k in locked` would suppress
    # every nomination silently. A 0-message stub / command-only shell is never load-bearing.
    # Model note: the store has no opener TEXT, so the throwaway-opener gate (is_boilerplate(fu)) is
    # not modellable here; we nominate ANY non-loadbearing, non-live, is_debris SINGLETON. That is the
    # CONSERVATIVE direction for the fuzz (it nominates AT LEAST as much debris as the real guard, so
    # it stresses the loss invariant harder, never weaker).
    def nominate_debris_step():
        deb = set()
        for ks in trees.values():
            if len(ks) != 1: continue
            k = ks[0]
            if k in loadbearing or k in live: continue   # loadbearing, NOT locked
            if nmsg[k] == 0 or is_debris(k):             # 0-msg stub, or distinct-count floor
                deb.add(k)
        return deb

    debris = set()
    if not buggy_debris_last:
        debris = nominate_debris_step()
        for k in debris:
            KEPT.discard(k); kept_unique_forks.discard(k); locked.discard(k)

    # C5 demotion
    for k in sorted(kept_unique_forks):
        if k in loadbearing: continue
        if not (set(fingerprints[k]) - set().union(*(set(fingerprints[j]) for j in KEPT if j != k))):
            KEPT.discard(k); kept_unique_forks.discard(k); locked.discard(k)

    # C6 deferred archives
    archived = []
    c6_assert_fail = None
    for k in sorted(tree_archive_candidates - KEPT):
        for P in (sources(k) & needed):
            if not any(P in sources(s) for s in KEPT if s != k):
                c6_assert_fail = (k, P)    # the assert would fire
        archived.append(('tree', k))

    # recall pass
    keptset = {k: set(fingerprints[k]) for k in KEPT}
    kept_union = set().union(*keptset.values()) if keptset else set()
    recall_archived = []
    for A in [a for a in fingerprints if a not in KEPT and a not in live and a not in debris]:
        missing = set(fingerprints[A]) - kept_union
        best = min(KEPT, key=lambda b: len(set(fingerprints[A]) - keptset[b]), default=None)
        if not missing:
            recall_archived.append(('recall0', A))
        elif len(missing) < CEILING:
            if not recall_oracle(A, best, missing):   # False => archive
                recall_archived.append(('recallS', A))
        # else KEEP

    if buggy_debris_last:
        # OLD ordering: nominate debris only NOW, after C5/recall already ran with debris in KEPT.
        debris = nominate_debris_step()
        for k in debris:
            KEPT.discard(k); kept_unique_forks.discard(k); locked.discard(k)

    # debris is MOVED to the archive (a recoverable theme), same as C6/recall archives.
    all_archived = set(k for _, k in archived) | set(k for _, k in recall_archived) | debris

    # marker loop
    markers = {}
    for k in sorted(KEPT - canonicals - live):
        head = max(canonicals, key=lambda h: len(set(fingerprints_prose[k]) & set(fingerprints_prose[h])), default=None)
        ov = (len(set(fingerprints_prose[k]) & set(fingerprints_prose[head])) / max(1, len(set(fingerprints_prose[k])))) if head else 0.0
        residue = set(fingerprints[k]) - set().union(*(set(fingerprints[j]) for j in KEPT if j != k))
        lb = k in loadbearing
        zero_resid = (len(residue) == 0)
        if lb and zero_resid:
            markers[k] = 'scroll-dep'
        else:
            HIGH = ov >= 0.6   # "mostly-contained" threshold (SKILL uses ~60% for family; marker says high/low)
            subst = marker_substantive_oracle(k, residue)
            if not HIGH and subst: markers[k] = 'main'
            elif not HIGH and not subst: markers[k] = 'none'
            elif HIGH and subst: markers[k] = 'fork'
            else: markers[k] = 'none'   # HIGH and not subst

    return dict(trees=trees, canonicals=canonicals, KEPT=KEPT, locked=locked,
                loadbearing=loadbearing, needed=needed, kept_unique_forks=kept_unique_forks,
                consumers=consumers, archived=archived, recall_archived=recall_archived,
                all_archived=all_archived, debris=debris, markers=markers, c6_assert_fail=c6_assert_fail,
                sources=sources, needs=needs, kept_union=kept_union, live=live,
                marker_range=set(KEPT - canonicals - live), unsatisfiable=dict(unsatisfiable))


def check_invariants(S, R):
    """Return list of (property, message) violations."""
    v = []
    fingerprints = S.fingerprints
    KEPT = R['KEPT']; sources = R['sources']; needs = R['needs']
    all_archived = R['all_archived']; live = R['live']

    # ---- (iii) live never archived/retitled ----
    for k in live:
        if k in all_archived:
            v.append(('iii', f'live file {k} was archived'))
        if k in R['markers']:
            v.append(('iii', f'live file {k} got a marker {R["markers"][k]}'))
        if k not in KEPT:
            v.append(('iii', f'live file {k} not in KEPT'))

    # ---- (i) no needed phantom of a kept file loses its last kept source ----
    # For every KEPT file f and every phantom P it needs, if P has ANY source in the store,
    # then SOME source of P must remain in KEPT (i.e. not archived). This is the real-world
    # safety: the post-move project dir must still source every needed phantom.
    survivors = KEPT  # files that remain in the project dir = KEPT (archived = moved out)
    for f in KEPT:
        for P in needs(f):
            store_srcs = [s for s in fingerprints if P in sources(s)]
            if not store_srcs:
                continue   # unsatisfiable: origin truly gone, not our bug (should be surfaced)
            kept_srcs = [s for s in store_srcs if s in survivors]
            if not kept_srcs:
                v.append(('i', f'kept file {f} needs phantom {P}, sources exist {store_srcs} but NONE kept'))

    # Also: the C6 assert must never fire silently (it would crash, but we capture it).
    if R['c6_assert_fail']:
        v.append(('i', f'C6 assert would fire: {R["c6_assert_fail"]}'))

    # ---- (ii) recall-archived file loses no unique message ----
    for tag, A in R['recall_archived']:
        if tag == 'recall0':
            # every message of A must live in some KEPT file (excluding A itself, since A is moved out)
            for m in fingerprints[A]:
                if not any(m in fingerprints[b] for b in KEPT if b != A):
                    v.append(('ii', f'recall0-archived {A} loses message {m} (not in any kept file != A)'))
        # recallS is operator-confirmed; not a determinism bug unless oracle lied about preservation.
        # We separately model an HONEST recall oracle below to test the determinism boundary.

    # ALSO test tree-archived (C6) for content loss against the post-move kept set.
    for tag, A in R['archived']:
        for m in fingerprints[A]:
            if not any(m in fingerprints[b] for b in KEPT if b != A):
                # NOTE: C6 archives are judged "worthless" by the operator, NOT required 0-unique.
                # So this is NOT necessarily a bug; record as 'c6-content' for separate analysis.
                v.append(('c6-content', f'C6-archived {A} has message {m} found in no kept file'))
                break

    # ---- (iv) marker partition exhaustive & mutually exclusive over its range ----
    rng = R['marker_range']
    for k in rng:
        if k not in R['markers']:
            v.append(('iv', f'file {k} in marker range but got NO marker (exhaustiveness hole)'))
    for k in R['markers']:
        if k not in rng:
            v.append(('iv', f'file {k} got a marker but is outside marker range (canonical/live?)'))
    # mutual exclusivity is automatic (dict = one marker each); the real claim is no-hole, checked above.
    # The deeper claim: the "~0 residue AND not loadbearing" cell is unreachable in the range.
    for k in rng:
        residue = set(fingerprints[k]) - set().union(*(set(fingerprints[j]) for j in KEPT if j != k))
        lb = k in R['loadbearing']
        if len(residue) == 0 and not lb:
            v.append(('iv-hole', f'file {k}: ~0 residue AND not loadbearing reached marker loop (the "deleted branch") -> got {R["markers"].get(k)}'))

    return v


def check_no_lost_message(S, R):
    """Oracle for the loss where debris left in KEPT masks another file's content. Survivors are computed over the FINAL set
    SURVIVORS = KEPT - all_archived (archived files are MOVED out, INCLUDING debris, which
    `nominate_debris` archives but does NOT itself remove from KEPT - C5 does). The masking bug is:
    a debris shell, still in KEPT when C5/recall measure containment, makes a fork's residue or a
    recall candidate's missing-set LOOK empty, so the file is demoted/0-archived; then debris is also
    moved, so the message survives in NO surviving file.

    We assert ONLY the DETERMINISTIC, safe-BY-CONSTRUCTION paths, which carry NO operator judgment and
    therefore must NEVER lose content:
      * recall0  - archived because `missing == empty` (every message claimed covered). A recall0 file
                   that loses a message is a TRUE bug (the C5->recall0 chain lands here too: a
                   C5-demoted fork re-enters the recall pass and is recall0-archived).
    We do NOT flag (they are not bugs):
      * recallS / C6-tree - operator/Sonnet judged the residue worthless (SKILL.md: C6 archives are
        NOT required 0-unique). Losing content there is an intended, judged decision, already covered
        by `check_invariants` as 'c6-content'.
      * a debris file's OWN content - the real `nominate_debris` only fires on a throwaway OPENER
        (empty / command-only / boilerplate), so debris content is by definition disposable. The model
        has no opener TEXT, so it over-nominates any small singleton; flagging debris-own-content would
        be a MODEL artifact, not a code bug. (This exclusion is the one place the model deliberately under-checks;
        the masking of OTHER files' content - the actual bug - is fully asserted.)

    KEY: survivors use the FINAL all_archived, NOT the recall-time `kept_union` snapshot. The snapshot
    still holds the about-to-be-moved debris shell, so the loss is invisible to the old per-path
    checks and visible only here. This FAILS on buggy_debris_last and PASSES on the one-safe-window fix."""
    fingerprints = S.fingerprints
    KEPT = R['KEPT']; all_archived = R['all_archived']
    survivors = KEPT - all_archived
    surv_union = set().union(*(set(fingerprints[k]) for k in survivors)) if survivors else set()
    debris = R['debris']
    deb_union = set().union(*(set(fingerprints[k]) for k in debris)) if debris else set()
    v = []
    for tag, A in R['recall_archived']:
        if tag != 'recall0':
            continue   # recallS is operator-judged; not a determinism bug
        lost = set(fingerprints[A]) - surv_union
        if lost:
            masked = sorted(lost & deb_union)   # the lost messages a debris shell was holding
            v.append(('lost-message',
                      f'recall0-archived {A} loses message(s) {sorted(lost)} - survive in NO kept '
                      f'non-archived file; masked-by-debris={masked} (debris={sorted(debris)}, '
                      f'survivors={sorted(survivors)})'))
    return v


def random_store(rng, nfiles, nmsgs, nlpus, nuuids):
    S = Store()
    S.files = [f'F{i}' for i in range(nfiles)]
    S.lts = {}
    msg_pool = list(range(nmsgs))
    prose_pool = list(range(nmsgs))   # prose fingerprints drawn from a parallel pool
    uuid_pool = [f'u{i}' for i in range(nuuids)]
    lpu_pool = [f'L{i}' for i in range(nlpus)]
    for i, k in enumerate(S.files):
        # message set
        msz = rng.randint(0, nmsgs)
        S.fingerprints[k] = set(rng.sample(msg_pool, msz)) if msz else set()
        psz = rng.randint(0, min(len(S.fingerprints[k]) if S.fingerprints[k] else 0, len(prose_pool)) or 0)
        S.fingerprints_prose[k] = set(rng.sample(sorted(S.fingerprints[k]), psz)) if (S.fingerprints[k] and psz) else set()
        # owned uuids
        osz = rng.randint(0, min(3, nuuids))
        S.owned[k] = set(rng.sample(uuid_pool, osz)) if osz else set()
        # lref: references to lpu values AND possibly uuids (cross-file). Mix lpu-pool + uuid-pool.
        ref_universe = lpu_pool + uuid_pool
        rsz = rng.randint(0, min(3, len(ref_universe)))
        S.lref[k] = set(rng.sample(ref_universe, rsz)) if rsz else set()
        # boundaries: each may reference an lpu, with par_is_none and nb
        nb_count = rng.randint(0, 2)
        bs = []
        for _ in range(nb_count):
            lp = rng.choice(lpu_pool + uuid_pool + [None])
            par_is_none = rng.random() < 0.5
            nb = rng.randint(0, 3)
            # IMPORTANT: lref must include boundary lpus (the code adds every boundary lpu to lref)
            if lp is not None:
                S.lref[k].add(lp)
            bs.append((lp, par_is_none, nb))
        S.bnd[k] = bs
        S.lts[k] = rng.random()
        if rng.random() < 0.15:
            S.live.add(k)
    S.finalize()
    return S


def adversarial_oracles(rng):
    # judge: random keep/archive
    def judge(k, uniq): return rng.random() < 0.5
    # recall SONNET: random keep/archive
    def recall(A, best, missing): return rng.random() < 0.5
    # marker substantive: random
    def subst(k, residue): return rng.random() < 0.5
    return judge, recall, subst


def main():
    seed = int(sys.argv[1]) if len(sys.argv) > 1 else 12345
    N = int(sys.argv[2]) if len(sys.argv) > 2 else 200000
    # argv[3] in {"buggy","fixed"} selects the debris-ordering under test (default fixed).
    buggy = (len(sys.argv) > 3 and sys.argv[3] == "buggy")
    rng = random.Random(seed)
    counts = defaultdict(int)
    examples = {}
    crashes = 0
    for trial in range(N):
        # small stores stress the algebra; phantom/cross edges need overlap to fire
        nfiles = rng.randint(1, 6)
        nmsgs = rng.randint(0, 8)
        nlpus = rng.randint(0, 4)
        nuuids = rng.randint(0, 4)
        S = random_store(rng, nfiles, nmsgs, nlpus, nuuids)
        j, rc, sb = adversarial_oracles(rng)
        try:
            R = run_pipeline(S, j, rc, sb, buggy_debris_last=buggy)
        except Exception as e:
            crashes += 1
            if 'crash' not in examples:
                examples['crash'] = (seed, trial, repr(e))
            continue
        viols = check_invariants(S, R) + check_no_lost_message(S, R)
        for prop, msg in viols:
            counts[prop] += 1
            if prop not in examples:
                examples[prop] = (trial, msg, snapshot(S))
    print(f"=== {N} trials, seed {seed}, debris-ordering={'BUGGY(last)' if buggy else 'FIXED(safe-window)'} ===")
    print(f"crashes (incl C6 assert as captured, not raised): {crashes}")
    for prop in sorted(counts):
        print(f"  [{prop}] violations: {counts[prop]}")
        tr, msg, snap = examples[prop]
        print(f"      first @trial {tr}: {msg}")
    if not counts:
        print("  NO violations of (i),(ii),(iii),(iv) found.")
    # Print any c6-content / iv-hole examples in detail (these are the interesting analysis cases)
    for prop in ('lost-message', 'c6-content', 'iv-hole', 'i', 'ii', 'iii', 'iv'):
        if prop in examples and prop in counts:
            print(f"\n--- detail [{prop}] ---")
            print(examples[prop][2])


def snapshot(S):
    lines = []
    for k in S.files:
        lines.append(f"  {k}: fingerprints={sorted(S.fingerprints[k])} prose={sorted(S.fingerprints_prose[k])} owned={sorted(S.owned[k])} "
                     f"lref={sorted(S.lref[k])} bnd={S.bnd[k]} live={k in S.live} "
                     f"needs={sorted(S.needs(k))} sources={sorted(S.sources(k))}")
    lines.append(f"  phantom={sorted(S.phantom)}")
    return "\n".join(lines)


if __name__ == '__main__':
    main()
