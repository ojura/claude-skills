"""
Faithful re-implementation of the SKILL.md Step 2 set construction, to probe
the structural CLAIMS against what the code actually computes.

We model files abstractly: each file k has
  fps[k]      : list of raw fingerprints
  fps_prose[k]: list of prose fingerprints
  owned[k]    : set of uuids
  lref[k]     : set of lpu values referenced
  bnd[k]      : list of (lpu, parentUuid, n_before)
  nmsg[k], lts[k], mtime[k], firstmsg[k]
We then run the EXACT set algebra from the skill and assert the prose claims.
"""
from collections import defaultdict
import time

def build_world(files):
    fps        = {k: v["fps"] for k,v in files.items()}
    fps_prose  = {k: v.get("fps_prose", v["fps"]) for k,v in files.items()}
    owned      = {k: set(v["owned"]) for k,v in files.items()}
    lref       = {k: set(v["lref"]) for k,v in files.items()}
    bnd        = {k: list(v["bnd"]) for k,v in files.items()}
    nmsg       = {k: len(v["fps"]) for k,v in files.items()}
    lts        = {k: v.get("lts","") for k,v in files.items()}
    mtime      = {k: v.get("mtime", 0.0) for k,v in files.items()}
    firstmsg   = {k: v.get("firstmsg","") for k,v in files.items()}
    global_uuid = defaultdict(set)
    for k in fps:
        for u in owned[k]:
            global_uuid[u].add(k)
    return dict(fps=fps, fps_prose=fps_prose, owned=owned, lref=lref, bnd=bnd,
                nmsg=nmsg, lts=lts, mtime=mtime, firstmsg=firstmsg,
                global_uuid=global_uuid)

def run(W, live_keys, now=None, RECENT_HOURS=12, DEBRIS_MAX=11, CEILING=50):
    fps=W["fps"]; fps_prose=W["fps_prose"]; owned=W["owned"]; lref=W["lref"]
    bnd=W["bnd"]; nmsg=W["nmsg"]; lts=W["lts"]; mtime=W["mtime"]
    global_uuid=W["global_uuid"]
    if now is None: now=time.time()

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

    seed={canonical(ks) for ks in trees.values() if len(ks)>1}
    seed|={k for k in fps if k in live}
    seed|={k for k in fps if (now-mtime[k]) < RECENT_HOURS*3600}

    unsatisfiable={}
    def locked_closure(seed):
        locked=set(seed); changed=True
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
    locked=locked_closure(seed)

    CEIL=CEILING
    kept_unique_forks=set()
    judged=[]  # files that hit the JUDGMENT ZONE (placeholder judge())
    for ks in trees.values():
        if len(ks)==1: continue
        canon=canonical(ks); keep=set(ks)&locked | {canon}
        kept_fp=set().union(*(set(fps[k]) for k in keep)) if keep else set()
        for k in [x for x in ks if x not in keep]:
            uniq=set(fps[k])-kept_fp
            if len(uniq)>=CEIL:
                kept_unique_forks.add(k)
            else:
                judged.append(k)   # judge() -> ARCHIVE or kept_unique_forks.add

    KEPT = {canonical(ks) for ks in trees.values()} | set(locked) | kept_unique_forks

    canonicals={canonical(ks) for ks in trees.values()}
    consumers = KEPT | live
    loadbearing  = {b for a in consumers for lp in lref[a] for b in global_uuid.get(lp,()) if b!=a}
    needed       = set().union(*(needs(a) for a in consumers)) if consumers else set()
    loadbearing |= {s for s in fps if sources(s) & needed}

    marked={}
    for k in sorted(KEPT - canonicals - live):
        head=max(canonicals, key=lambda h: len(set(fps_prose[k]) & set(fps_prose[h])), default=None)
        ov=(len(set(fps_prose[k]) & set(fps_prose[head]))/max(1,len(set(fps_prose[k])))) if head else 0.0
        residue=set(fps[k]) - set().union(*(set(fps[j]) for j in KEPT if j!=k)) if any(j!=k for j in KEPT) else set(fps[k])
        marked[k]=dict(head=head, ov=ov, residue=residue)

    return dict(trees=dict(trees), phantom=phantom, seed=seed, locked=locked,
                kept_unique_forks=kept_unique_forks, KEPT=KEPT, canonicals=canonicals,
                live=live, loadbearing=loadbearing, needed=needed, consumers=consumers,
                marked=marked, judged=judged, unsatisfiable=unsatisfiable,
                sources=sources, needs=needs, canonical=canonical,
                marker_range=set(KEPT - canonicals - live))

if __name__=="__main__":
    print("module ok")
