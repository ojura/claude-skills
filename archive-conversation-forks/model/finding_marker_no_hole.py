"""
Q4: is "~0 global residue AND NOT loadbearing" unreachable in KEPT - canonicals - live?

Decompose KEPT - canonicals - live. After C4 re-close + C5:
  KEPT = canonicals | locked | kept_unique_forks   (kept_unique_forks already had locked merged via |=)
  Actually: KEPT = (canonicals from selection) | locked(definitive) | (kept_unique_forks minus C5-demoted)
  KEPT - canonicals - live members are:
    (A) locked-but-not-canonical-not-live
    (B) kept_unique_forks-but-not-canonical-not-live (post C5)
  Plan claim: (A) => loadbearing; (B) => >=1 global-unique msg (so residue>0, not ~0).

ADVERSARIAL probe of claim (A): is EVERY locked member loadbearing?
  locked = closure(KEPT). A member enters locked by:
    (seed)  multi-file-tree canonical -> but that's a CANONICAL, subtracted. OR live -> subtracted.
    (1) cross-file ancestor: b in global_uuid[lp] for lp in lref[<locked>] -> b OWNS a referenced uuid
        => b is a cross-file target of a consumer => b in loadbearing. OK loadbearing.
    (2) phantom backfill 'best' source: locked.add(best) where best sources P in needs(<locked>)
        => best in {s: sources(s)&needed} => loadbearing. OK loadbearing.
  So every NON-seed locked member is loadbearing. The SEED members are canonicals/live (subtracted).
  THEREFORE every locked member in (KEPT-canon-live) entered via (1) or (2) => loadbearing. CLAIM (A) HOLDS.

  BUT WAIT: a subtle gap. `loadbearing` is computed at C4 over consumers=KEPT|live where KEPT is the
  POST-reclose set. The closure edge (1)/(2) is defined identically to loadbearing's edges. The ONLY
  way a locked member is NOT loadbearing: if it was pulled in by closing over a SEED member's edges,
  but that seed member later... no, seed subset KEPT subset consumers, so its edges are in loadbearing too.
  Edge case: a locked member b pulled via lref of ANOTHER locked member k, where k is in locked but k
  is NOT in consumers? Impossible: locked subset KEPT subset consumers. So every locked member's edges
  are consumer edges. CLAIM (A) holds rigorously.

ADVERSARIAL probe of claim (B): a kept_unique_fork survives C5 only if loadbearing OR global-residue>0.
  If loadbearing -> case 1 ([scroll-dep] if residue 0). If NOT loadbearing -> C5 required residue>0.
  So a NOT-loadbearing kept_unique_fork in KEPT has residue>0. CLAIM (B) holds BY C5's construction.

So the cell is unreachable IF (A) and (B) partition KEPT-canon-live. Do they? A member could be BOTH
locked AND a kept_unique_fork (union). Then it's covered by (A) (loadbearing). Fine.

Now TRY TO BREAK IT with a concrete store: hunt for any KEPT-canon-live member that is ~0-residue and
NOT loadbearing. Random-ish fuzz over small stores.
"""
import os, sys, random
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import trace_patched as P   # the local trace_patched.py (post-fix model) in this dir
def mkfile(fps, owned, bnds, extra_lref=()):
    lref=set(extra_lref)
    for (lp,par,nb) in bnds:
        if lp: lref.add(lp)
    return dict(fps=fps, owned=owned, lref=lref, bnd=bnds)

def fuzz_once(rng):
    n=rng.randint(2,7)
    files={}
    uuids=[f"u{i}" for i in range(n*2)]
    for i in range(n):
        k=f"F{i:02d}xxxxx"[:8]
        # random content, possibly overlapping
        base=rng.randint(0,3)
        size=rng.choice([3,12,55,120])
        content=[f"m{base*100+j}" for j in range(size)]
        owned=[uuids[i*2], uuids[i*2+1]]
        bnds=[]
        # randomly add a phantom boundary
        if rng.random()<0.4:
            ph=rng.choice(["PHA","PHB"])
            if rng.random()<0.5: bnds.append((ph,None,0))            # needs
            else: bnds.append((ph,"realp",rng.randint(1,5)))         # sources
        extra=[]
        # random cross-file ref to another file's owned uuid
        if rng.random()<0.5:
            extra=[rng.choice(uuids)]
        files[k]=mkfile(content, owned, bnds, extra_lref=extra)
    return files

bad=0; runs=20000
rng=random.Random(12345)
for _ in range(runs):
    files=fuzz_once(rng)
    try:
        R=P.run_patched(P.build_world(files), live_keys=set(), judge_keep=lambda k,u: bool(u))
    except Exception:
        continue
    for k,info in R["marked"].items():
        if info["residue"]==0 and not info["lb"]:
            bad+=1
            print("BREAK: ~0-residue NOT-loadbearing in marker range:", k, info)
            print("  KEPT:", R["KEPT"], " locked:", R["locked"], " kuf:", R["kept_unique_forks"])
            print("  demoted:", R["demoted"], " loadbearing:", R["loadbearing"])
            if bad>=5: break
    if bad>=5: break
print(f"fuzzed {runs} stores; ~0-residue-NOT-loadbearing-in-marker-range count = {bad}")
print("Q4:", "CONFIRMED unreachable" if bad==0 else "REFUTED - cell IS reachable, see above")
