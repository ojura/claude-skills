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
  - fps:   set of raw fingerprints (drives redundancy/containment)
  - fps_prose: set of prose fingerprints (drives marker overlap)
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
        self.fps = {}
        self.fps_prose = {}
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


def run_pipeline(S, judge_oracle, recall_oracle, marker_substantive_oracle):
    """Faithful transcription of SKILL.md Step 2. Returns a dict of results + records of every
    archive/mark decision, plus enough state to check invariants."""
    fps = S.fps; fps_prose = S.fps_prose; lref = S.lref; global_uuid = S.global_uuid
    bnd = S.bnd; live = S.live; phantom = S.phantom
    files = S.files
    nmsg = {k: len(fps[k]) for k in files}     # model: nmsg = number of fps (close enough; see note)
    lts  = {k: S.lts[k] for k in files}

    def sources(k): return S.sources(k)
    def needs(k): return S.needs(k)

    # union-find
    parent = {k: k for k in fps}
    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]; x = parent[x]
        return x
    bylpu = defaultdict(list)
    for k in fps:
        for lp in lref[k]: bylpu[lp].append(k)
    for ks in bylpu.values():
        for k in ks[1:]: parent[find(k)] = find(ks[0])
    dep = {(a,b) for a in fps for lp in lref[a] for b in global_uuid.get(lp,()) if b != a}
    for a,b in dep: parent[find(a)] = find(b)
    trees = defaultdict(list)
    for k in fps: trees[find(k)].append(k)

    def is_debris(k): return nmsg[k] <= DEBRIS_MAX
    def canonical(ks):
        cand = [k for k in ks if not is_debris(k)] or ks
        # key=(distinct, lts, uuid); model uuid order by key string
        return max(cand, key=lambda k: (len(set(fps[k])), lts[k], k))

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
                    srcs = [s for s in fps if P in sources(s)]
                    if not srcs:
                        unsatisfiable.setdefault(k, set()).add(P); continue
                    if not (set(srcs) & locked):
                        best = max(srcs, key=lambda s: len(set(fps[s])))
                        locked.add(best); changed = True
        return locked

    seed = {canonical(ks) for ks in trees.values() if len(ks) > 1}
    seed |= {k for k in fps if k in live}
    locked = locked_closure(seed)
    canonicals = {canonical(ks) for ks in trees.values()}

    # per-tree judge
    kept_unique_forks = set(); tree_archive_candidates = set()
    judge_calls = []
    for ks in trees.values():
        if len(ks) == 1: continue
        canon = canonical(ks); keep = set(ks) & locked | {canon}
        kept_fp = set().union(*(set(fps[k]) for k in keep)) if keep else set()
        for k in [x for x in ks if x not in keep]:
            uniq = set(fps[k]) - kept_fp
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
    loadbearing |= {s for s in fps if sources(s) & needed}

    # C5 demotion
    for k in sorted(kept_unique_forks):
        if k in loadbearing: continue
        if not (set(fps[k]) - set().union(*(set(fps[j]) for j in KEPT if j != k))):
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
    keptset = {k: set(fps[k]) for k in KEPT}
    kept_union = set().union(*keptset.values()) if keptset else set()
    recall_archived = []
    for A in [a for a in fps if a not in KEPT and a not in live]:
        missing = set(fps[A]) - kept_union
        best = min(KEPT, key=lambda b: len(set(fps[A]) - keptset[b]), default=None)
        if not missing:
            recall_archived.append(('recall0', A))
        elif len(missing) < CEILING:
            if not recall_oracle(A, best, missing):   # False => archive
                recall_archived.append(('recallS', A))
        # else KEEP

    all_archived = set(k for _, k in archived) | set(k for _, k in recall_archived)

    # marker loop
    markers = {}
    for k in sorted(KEPT - canonicals - live):
        head = max(canonicals, key=lambda h: len(set(fps_prose[k]) & set(fps_prose[h])), default=None)
        ov = (len(set(fps_prose[k]) & set(fps_prose[head])) / max(1, len(set(fps_prose[k])))) if head else 0.0
        residue = set(fps[k]) - set().union(*(set(fps[j]) for j in KEPT if j != k))
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
                all_archived=all_archived, markers=markers, c6_assert_fail=c6_assert_fail,
                sources=sources, needs=needs, kept_union=kept_union, live=live,
                marker_range=set(KEPT - canonicals - live), unsatisfiable=dict(unsatisfiable))


def check_invariants(S, R):
    """Return list of (property, message) violations."""
    v = []
    fps = S.fps
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
            store_srcs = [s for s in fps if P in sources(s)]
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
            for m in fps[A]:
                if not any(m in fps[b] for b in KEPT if b != A):
                    v.append(('ii', f'recall0-archived {A} loses message {m} (not in any kept file != A)'))
        # recallS is operator-confirmed; not a determinism bug unless oracle lied about preservation.
        # We separately model an HONEST recall oracle below to test the determinism boundary.

    # ALSO test tree-archived (C6) for content loss against the post-move kept set.
    for tag, A in R['archived']:
        for m in fps[A]:
            if not any(m in fps[b] for b in KEPT if b != A):
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
        residue = set(fps[k]) - set().union(*(set(fps[j]) for j in KEPT if j != k))
        lb = k in R['loadbearing']
        if len(residue) == 0 and not lb:
            v.append(('iv-hole', f'file {k}: ~0 residue AND not loadbearing reached marker loop (the "deleted branch") -> got {R["markers"].get(k)}'))

    return v


def random_store(rng, nfiles, nmsgs, nlpus, nuuids):
    S = Store()
    S.files = [f'F{i}' for i in range(nfiles)]
    S.lts = {}
    msg_pool = list(range(nmsgs))
    prose_pool = list(range(nmsgs))   # prose fps drawn from a parallel pool
    uuid_pool = [f'u{i}' for i in range(nuuids)]
    lpu_pool = [f'L{i}' for i in range(nlpus)]
    for i, k in enumerate(S.files):
        # message set
        msz = rng.randint(0, nmsgs)
        S.fps[k] = set(rng.sample(msg_pool, msz)) if msz else set()
        psz = rng.randint(0, min(len(S.fps[k]) if S.fps[k] else 0, len(prose_pool)) or 0)
        S.fps_prose[k] = set(rng.sample(sorted(S.fps[k]), psz)) if (S.fps[k] and psz) else set()
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
            R = run_pipeline(S, j, rc, sb)
        except Exception as e:
            crashes += 1
            if 'crash' not in examples:
                examples['crash'] = (seed, trial, repr(e))
            continue
        viols = check_invariants(S, R)
        for prop, msg in viols:
            counts[prop] += 1
            if prop not in examples:
                examples[prop] = (trial, msg, snapshot(S))
    print(f"=== {N} trials, seed {seed} ===")
    print(f"crashes (incl C6 assert as captured, not raised): {crashes}")
    for prop in sorted(counts):
        print(f"  [{prop}] violations: {counts[prop]}")
        tr, msg, snap = examples[prop]
        print(f"      first @trial {tr}: {msg}")
    if not counts:
        print("  NO violations of (i),(ii),(iii),(iv) found.")
    # Print any c6-content / iv-hole examples in detail (these are the interesting analysis cases)
    for prop in ('c6-content', 'iv-hole', 'i', 'ii', 'iii', 'iv'):
        if prop in examples and prop in counts:
            print(f"\n--- detail [{prop}] ---")
            print(examples[prop][2])


def snapshot(S):
    lines = []
    for k in S.files:
        lines.append(f"  {k}: fps={sorted(S.fps[k])} prose={sorted(S.fps_prose[k])} owned={sorted(S.owned[k])} "
                     f"lref={sorted(S.lref[k])} bnd={S.bnd[k]} live={k in S.live} "
                     f"needs={sorted(S.needs(k))} sources={sorted(S.sources(k))}")
    lines.append(f"  phantom={sorted(S.phantom)}")
    return "\n".join(lines)


if __name__ == '__main__':
    main()
