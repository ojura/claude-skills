#!/usr/bin/env python3
"""
Targeted adversarial search aimed squarely at the structural traps SKILL.md cites:
 - the C4 re-close orphan fix: a kept-unique fork NEEDS a phantom whose SOLE source is content-redundant.
   Without the re-close, that source is archived and the fork orphaned. We construct exactly that.
 - multiple sources for one needed phantom (the "redundant extra source stays archivable" claim).
 - cross-file lpu chains seeded only from a kept-unique fork (not a canonical/live).
 - the C5 demotion interacting with locked-set membership.
We also model the judge oracle ADVERSARIALLY but bias it to archive sources, to stress no-orphan.
"""
import os, random, sys
from collections import defaultdict
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from step2_model import Store, run_pipeline, check_invariants, snapshot

def build_reclose_trap(rng):
    """Hand-build the canonical 're-close orphan' scenario and randomize around it.
    Files:
      C   : a canonical of its own tree (big), in tree T1.
      FORK: a kept-unique fork in tree T1 that NEEDS phantom P (root boundary, nb=0).
      SRC : the ONLY source of P (has pre-content before its P-boundary). SRC is content-redundant
            (its messages all duplicated in C) so the recall pass WOULD archive it absent protection.
    The trap: FORK shares an lpu with C (same tree), but its NEED on P points at SRC which is in a
    DIFFERENT tree (SRC shares P in lref with FORK -> co-tree merges them actually). The re-close must
    lock SRC because FORK (kept) needs P.
    """
    S = Store()
    S.files = ['C', 'FORK', 'SRC', 'NOISE']
    S.lts = {'C': 0.9, 'FORK': 0.5, 'SRC': 0.3, 'NOISE': 0.1}
    # C: big distinct content; canonical
    cmsgs = set(range(0, 60))
    S.fps['C'] = set(cmsgs)
    # FORK: mostly contained in C but with >=CEILING tree-local unique to force kept_unique_fork.
    fork_unique = set(range(100, 100 + 60))   # 60 unique -> >= CEILING=50, auto-kept
    S.fps['FORK'] = set(range(0, 10)) | fork_unique
    # SRC: content fully duplicated in C (so recall sees 0 unique) -> would be archived w/o protection
    S.fps['SRC'] = set(range(0, 20))          # subset of C's messages
    S.fps['NOISE'] = set(range(200, 205))
    for k in S.files:
        S.fps_prose[k] = set(list(S.fps[k])[: max(1, len(S.fps[k])//2)])
    # owned uuids: C owns some; FORK shares an lpu with C via lref to force same tree.
    S.owned = {'C': {'cu1','cu2'}, 'FORK': set(), 'SRC': set(), 'NOISE': set()}
    # lref: C and FORK share lpu 'Lshared' -> same tree. FORK and SRC share phantom 'P' (boundary).
    S.lref = {'C': {'Lshared'}, 'FORK': {'Lshared'}, 'SRC': set(), 'NOISE': set()}
    # boundaries: FORK needs P (root boundary, par_is_none=True, nb=0). SRC sources P (nb>0).
    S.bnd = {'C': [], 'NOISE': [],
             'FORK': [('P', True, 0)],          # needs P
             'SRC':  [('P', False, 2)]}          # sources P (par not None)
    # the code adds boundary lpus to lref:
    S.lref['FORK'].add('P'); S.lref['SRC'].add('P')
    # randomize: maybe add a SECOND source SRC2 (then SRC archivable), maybe make C live, etc.
    if rng.random() < 0.5:
        S.files.append('SRC2'); S.lts['SRC2'] = 0.2
        S.fps['SRC2'] = set(range(0, 25)); S.fps_prose['SRC2'] = set(range(0,5))
        S.owned['SRC2'] = set(); S.lref['SRC2'] = {'P'}
        S.bnd['SRC2'] = [('P', False, 3)]      # also sources P, richer
    if rng.random() < 0.3:
        S.live.add('C')
    if rng.random() < 0.3:
        S.live.add('FORK')
    S.finalize()
    return S

def adv_archive_sources(rng):
    # judge: bias toward ARCHIVE (return False = archive) to stress no-orphan on sources
    def judge(k, uniq): return rng.random() < 0.2   # mostly archive
    def recall(A, best, missing): return rng.random() < 0.2  # mostly archive
    def subst(k, residue): return rng.random() < 0.5
    return judge, recall, subst

def main():
    seed = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    N = int(sys.argv[2]) if len(sys.argv) > 2 else 50000
    rng = random.Random(seed)
    from collections import defaultdict
    counts = defaultdict(int); examples = {}; crashes = 0
    for t in range(N):
        S = build_reclose_trap(rng)
        j, rc, sb = adv_archive_sources(rng)
        try:
            R = run_pipeline(S, j, rc, sb)
        except AssertionError as e:
            counts['c6-assert-raised'] += 1
            if 'c6-assert-raised' not in examples: examples['c6-assert-raised'] = (t, repr(e), snapshot(S))
            continue
        except Exception as e:
            crashes += 1
            if 'crash' not in examples: examples['crash'] = (t, repr(e), snapshot(S))
            continue
        for prop, msg in check_invariants(S, R):
            counts[prop] += 1
            if prop not in examples: examples[prop] = (t, msg, snapshot(S))
    print(f"=== TARGETED re-close trap: {N} trials, seed {seed} ===  crashes={crashes}")
    for prop in sorted(counts):
        print(f"  [{prop}]: {counts[prop]}   first: {examples[prop][1]}")
    if not any(p in counts for p in ('i','ii','iii','iv','iv-hole')):
        print("  core (i)(ii)(iii)(iv)(iv-hole): NO violations.")
    # Show the no-orphan-relevant detail for one trap instance
    rng2 = random.Random(seed+1)
    S = build_reclose_trap(rng2)
    R = run_pipeline(S, lambda k,u: False, lambda a,b,m: False, lambda k,r: True)  # archive everything possible
    print("\n--- one trap instance, judge=ARCHIVE-ALL ---")
    print(snapshot(S))
    print(f"  KEPT={sorted(R['KEPT'])}")
    print(f"  archived(tree/C6)={R['archived']}  recall_archived={R['recall_archived']}")
    print(f"  SRC in KEPT? {'SRC' in R['KEPT']}   (must be True: FORK needs P, SRC sources P)")
    print(f"  FORK needs={sorted(R['needs']('FORK'))}  SRC sources={sorted(R['sources']('SRC'))}")
    print(f"  markers={R['markers']}")

if __name__ == '__main__':
    main()
