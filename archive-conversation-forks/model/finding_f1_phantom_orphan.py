"""
EDGE: the phantom-NEEDER is a dormant, non-canonical fork. It joins its tree via a
shared NON-phantom-ish lpu with the canonical, but the canonical does NOT reference PH.
Does the needer enter `locked` so its needs(PH) gets walked and a source locked?

closure walks needs(k) ONLY for k already in locked. The needer enters locked only if:
  (a) it is the tree canonical (no, by construction), or
  (b) it is recent/live (no, dormant), or
  (c) it is a cross-file lpu TARGET of some locked file (i.e. some locked file's lref
      contains a uuid the needer owns), or
  (d) it is itself a phantom-source that some locked needer pulls in.

If none of (a-d), the needer is in KEPT (it could be kept_unique_forks if it has >=CEILING
unique, or archived if not) but its needs(PH) is NEVER walked => the PH source is never locked
=> the source can be archived => needer's deep origin orphaned IF needer is kept.

Build it: shared lpu SH (a REAL uuid owned by canonical) ties needer into the tree.
Needer ALSO has boundary (PH,None,0). Source has boundary (PH,real,5). Source does NOT
share SH, and does NOT co-tree with needer EXCEPT via PH... but PH is in BOTH their lref,
so bylpu[PH] unions them anyway. Argh -- PH-in-lref forces union again.

The ONLY way needer+source land in different trees is if the SOURCE does not carry PH in
its lref. But sources(s) REQUIRES PH in bnd[s], which forces PH into lref[s]. So source
ALWAYS co-trees with needer. CONCLUSION: needer and its sources are ALWAYS in one tree,
so the tree has >1 file => its canonical is seeded. But is the CANONICAL guaranteed to
trigger the needs walk? No -- the canonical might not need PH. We need SOME locked file in
the tree to be the needer OR to transitively pull the needer in.

Test: tree = {CANON (big, no PH), NEEDER (dormant, needs PH, NOT canonical, NOT cross-ref'd),
SOURCE (sources PH)}. All co-tree via PH-in-lref (needer & source) + SH (canon & needer).
seed={CANON}. Does closure reach NEEDER and walk needs(PH)?
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import trace as T   # the local trace.py (pre-fix model) in this dir, not the stdlib trace module
def mkfile(fingerprints, owned, bnds, extra_lref=(), mtime=0.0):
    lref=set(extra_lref)
    for (lp,par,nb) in bnds:
        if lp: lref.add(lp)
    return dict(fingerprints=fingerprints, owned=owned, lref=lref, bnd=bnds, mtime=mtime)

files = {
  # CANON: big, owns SH-target uuid 'uSH'. references nothing phantom. Tree canonical.
  "CANONXXX": mkfile(["c%d"%i for i in range(300)], ["uSH","uc"], []),
  # NEEDER: dormant fork. needs PH. shares SH with canon (references uSH cross-file => dep edge to CANON).
  #         NOT cross-referenced BY anyone. NOT recent. NOT canonical (300>150).
  "NEEDERXX": mkfile(["n%d"%i for i in range(150)], ["un"], [("PH",None,0)], extra_lref=["uSH"]),
  # SOURCE: sources PH. small, dormant. co-trees via PH-in-lref with NEEDER.
  "SOURCEXX": mkfile(["s%d"%i for i in range(30)], ["us"], [("PH","realpar",7)]),
}
W=T.build_world(files)
R=T.run(W, live_keys=set())
print("phantom:", R["phantom"])
print("trees:", R["trees"])
print("canonicals:", R["canonicals"])
print("seed:", R["seed"])
print("locked:", R["locked"])
print("NEEDERXX locked?", "NEEDERXX" in R["locked"])
print("SOURCEXX locked?", "SOURCEXX" in R["locked"], " <- the phantom source")
print("loadbearing:", R["loadbearing"])
print()
# Walk the closure by hand:
print("Closure trace:")
print(" seed = {CANONXXX} (tree canonical)")
print(" CANONXXX.lref =", W["lref"]["CANONXXX"], "-> no cross-file targets it doesn't own; needs(CANON)=",R["needs"]("CANONXXX"))
print(" Does NEEDERXX ever get locked? It is a cross-file ANCESTOR of nobody locked.")
print("   But CANONXXX does NOT reference uSH... wait, NEEDERXX references uSH, CANONXXX OWNS uSH.")
print("   The dep edge is needer->canon (needer refs uSH which canon owns). That UNIONS them")
print("   (tree) but does NOT put NEEDER into locked: closure adds CROSS-FILE TARGETS of locked")
print("   files (b in global_uuid[lp] for lp in lref[locked]). CANON's lref has no uSH.")
print("   So closure never adds NEEDER. needs(NEEDER) never walked. SOURCE never locked.")
trees=R["trees"]; fingerprints=W["fingerprints"]; locked=R["locked"]; canonical=R["canonical"]; CEILING=50
for root,ks in trees.items():
    if len(ks)==1: continue
    canon=canonical(ks); keep=set(ks)&locked | {canon}
    kept_fp=set().union(*(set(fingerprints[k]) for k in keep)) if keep else set()
    print(f"tree {root}: canon={canon} keep={keep}")
    for k in [x for x in ks if x not in keep]:
        uniq=set(fingerprints[k])-kept_fp
        print(f"   fork {k}: uniq={len(uniq)} -> {'AUTO-KEEP(kept_unique_fork)' if len(uniq)>=CEILING else 'JUDGMENT'}")
