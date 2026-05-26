"""
PATCHED model implementing fix-plan C2-C7 set algebra, to verify against the probes.
Mirrors trace.py but applies:
  C3: seed = multi-file-tree canonicals + live   (NO recent-mtime)
  C4: locked=locked_closure(seed); per-tree judge -> kept_unique_forks + tree_archive_candidates;
      KEPT = canonicals|locked|kept_unique_forks; then locked=locked_closure(KEPT); KEPT|=locked
  C5: demote kept_unique_forks with exactly-0 global residue AND not loadbearing
  C6: deferred archive of tree_archive_candidates - KEPT, with the per-P safety assert
  C7: marker loop with the scroll-dep-regardless-of-ov branch

We expose all intermediate sets and a 'judge' policy hook so probes can drive the JUDGMENT ZONE.
"""
from collections import defaultdict

def build_world(files):
    fps        = {k: v["fps"] for k,v in files.items()}
    fps_prose  = {k: v.get("fps_prose", v["fps"]) for k,v in files.items()}
    owned      = {k: set(v["owned"]) for k,v in files.items()}
    lref       = {k: set(v["lref"]) for k,v in files.items()}
    bnd        = {k: list(v["bnd"]) for k,v in files.items()}
    nmsg       = {k: len(v["fps"]) for k,v in files.items()}
    lts        = {k: v.get("lts","") for k,v in files.items()}
    firstmsg   = {k: v.get("firstmsg","") for k,v in files.items()}
    global_uuid = defaultdict(set)
    for k in fps:
        for u in owned[k]:
            global_uuid[u].add(k)
    return dict(fps=fps, fps_prose=fps_prose, owned=owned, lref=lref, bnd=bnd,
                nmsg=nmsg, lts=lts, firstmsg=firstmsg, global_uuid=global_uuid)

def run_patched(W, live_keys, DEBRIS_MAX=11, CEILING=50, judge_keep=None):
    """judge_keep(k,uniq) -> True to keep (kept_unique_fork), False to ARCHIVE-candidate.
    Default: archive (return False) i.e. the operator judged it worthless."""
    if judge_keep is None:
        judge_keep = lambda k,uniq: False
    fps=W["fps"]; fps_prose=W["fps_prose"]; owned=W["owned"]; lref=W["lref"]
    bnd=W["bnd"]; nmsg=W["nmsg"]; lts=W["lts"]; global_uuid=W["global_uuid"]

    all_lpus={lp for k in bnd for (lp,_,_) in bnd[k] if lp}
    phantom={lp for lp in all_lpus if lp not in global_uuid}
    def sources(k):
        return {lp for (lp,par,nb) in bnd[k] if lp in phantom and (par is not None or nb>0)}
    def needs(k):
        return {lp for (lp,par,nb) in bnd[k] if lp in phantom and par is None and nb==0}

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

    def is_debris(k): return nmsg[k] <= DEBRIS_MAX
    def canonical(ks):
        cand=[k for k in ks if not is_debris(k)] or ks
        return max(cand, key=lambda k:(len(set(fps[k])), lts[k]))

    live=set(live_keys)

    # C3 seed: NO recent-mtime
    seed={canonical(ks) for ks in trees.values() if len(ks)>1}
    seed|=live

    unsatisfiable={}
    def locked_closure(seedset):
        locked=set(seedset); changed=True
        while changed:
            changed=False
            for k in list(locked):
                for lp in lref[k]:
                    for b in global_uuid.get(lp,()):
                        if b not in locked: locked.add(b); changed=True
                for P in needs(k):
                    srcs=[s for s in fps if P in sources(s)]
                    if not srcs:
                        unsatisfiable.setdefault(k,set()).add(P); continue
                    if not (set(srcs)&locked):
                        best=max(srcs, key=lambda s:len(set(fps[s])))
                        locked.add(best); changed=True
        return locked

    # C4 preliminary closure (canonicals+live)
    locked=locked_closure(seed)

    kept_unique_forks=set(); tree_archive_candidates=set()
    for ks in trees.values():
        if len(ks)==1: continue
        canon=canonical(ks); keep=set(ks)&locked | {canon}
        kept_fp=set().union(*(set(fps[k]) for k in keep)) if keep else set()
        for k in [x for x in ks if x not in keep]:
            uniq=set(fps[k])-kept_fp
            if len(uniq)>=CEILING: kept_unique_forks.add(k)
            else:
                if judge_keep(k,uniq): kept_unique_forks.add(k)
                else: tree_archive_candidates.add(k)

    KEPT = {canonical(ks) for ks in trees.values()} | set(locked) | kept_unique_forks
    # C4 re-close over FULL kept set
    unsatisfiable={}  # recompute on definitive closure (plan says compute on definitive closure)
    locked = locked_closure(KEPT); KEPT |= locked

    canonicals={canonical(ks) for ks in trees.values()}
    consumers = KEPT | live
    loadbearing  = {b for a in consumers for lp in lref[a] for b in global_uuid.get(lp,()) if b!=a}
    needed       = set().union(*(needs(a) for a in consumers)) if consumers else set()
    loadbearing |= {s for s in fps if sources(s) & needed}

    # C5 demotion: drop exactly-0-global-residue, non-loadbearing kept_unique_forks
    demoted=set()
    for k in sorted(kept_unique_forks):
        if k in loadbearing: continue
        resid = set(fps[k]) - set().union(*(set(fps[j]) for j in KEPT if j!=k)) if any(j!=k for j in KEPT) else set(fps[k])
        if not resid:
            KEPT.discard(k); kept_unique_forks.discard(k); demoted.add(k)
    # NOTE: plan recomputes loadbearing? It does NOT re-derive loadbearing/consumers after C5.
    # We keep loadbearing as-is (computed pre-demotion) to mirror the plan EXACTLY.

    # C6 deferred archive with assert
    c6_archived=set(); c6_assert_fires=[]
    for k in sorted(tree_archive_candidates - KEPT):
        for P in (sources(k) & needed):
            ok = any(P in sources(s) for s in KEPT if s!=k)
            if not ok: c6_assert_fires.append((k,P))
        c6_archived.add(k)

    # C8 recall pass over fps - KEPT - live (canon_all subset KEPT so drop it)
    keptset={k:set(fps[k]) for k in KEPT}
    kept_union=set().union(*keptset.values()) if keptset else set()
    recall_archived=set(); recall_judgment=set()
    for A in [a for a in fps if a not in KEPT and a not in live]:
        missing=set(fps[A])-kept_union
        if not missing: recall_archived.add(A)
        elif len(missing)<CEILING: recall_judgment.add(A)
        # else keep

    # C7 marker loop
    marked={}
    for k in sorted(KEPT - canonicals - live):
        head=max(canonicals, key=lambda h: len(set(fps_prose[k]) & set(fps_prose[h])), default=None)
        ov=(len(set(fps_prose[k]) & set(fps_prose[head]))/max(1,len(set(fps_prose[k])))) if head else 0.0
        residue=set(fps[k]) - set().union(*(set(fps[j]) for j in KEPT if j!=k)) if any(j!=k for j in KEPT) else set(fps[k])
        lb = k in loadbearing
        # plan C7 decision tree:
        if lb and len(residue)==0:
            label="[scroll-dep]"
        else:
            if ov < 0.5:   # "low ov" threshold is a judgment in the plan; use 0.5 as a probe stand-in
                label="[main]" if len(residue)>0 else "none(low-ov,~0,not-LB)"  # the supposedly-unreachable cell
            else:
                label="[fork]" if len(residue)>0 else "??high-ov ~0 not-LB"
        marked[k]=dict(head=head, ov=ov, residue=len(residue), lb=lb, label=label)

    return dict(trees=dict(trees), phantom=phantom, seed=seed, locked=locked,
                kept_unique_forks=kept_unique_forks, KEPT=KEPT, canonicals=canonicals,
                live=live, loadbearing=loadbearing, needed=needed, consumers=consumers,
                tree_archive_candidates=tree_archive_candidates, demoted=demoted,
                c6_archived=c6_archived, c6_assert_fires=c6_assert_fires,
                recall_archived=recall_archived, recall_judgment=recall_judgment,
                marked=marked, unsatisfiable=unsatisfiable,
                sources=sources, needs=needs, canonical=canonical)

if __name__=="__main__":
    print("patched module ok")
