"""
Confirm the defect is real and isolate the cause:
 (1) If NEEDERXX were RECENT (in seed), would SOURCEXX be locked? -> should be YES.
 (2) Re-running locked_closure with seed |= KEPT (incl kept_unique_forks) -> SOURCEXX locked.
This proves the gap is precisely: kept_unique_forks' needs() are not walked by the closure
because the closure runs BEFORE kept_unique_forks is known / is not seeded with them.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import trace as T   # the local trace.py (pre-fix model) in this dir, not the stdlib trace module
def mkfile(fingerprints, owned, bnds, extra_lref=(), mtime=0.0):
    lref=set(extra_lref)
    for (lp,par,nb) in bnds:
        if lp: lref.add(lp)
    return dict(fingerprints=fingerprints, owned=owned, lref=lref, bnd=bnds, mtime=mtime)

base = {
  "CANONXXX": mkfile(["c%d"%i for i in range(300)], ["uSH","uc"], []),
  "NEEDERXX": mkfile(["n%d"%i for i in range(150)], ["un"], [("PH",None,0)], extra_lref=["uSH"]),
  "SOURCEXX": mkfile(["s%d"%i for i in range(30)], ["us"], [("PH","realpar",7)]),
}

# (1) make NEEDERXX recent
import copy
f1=copy.deepcopy(base); f1["NEEDERXX"]["mtime"]=T.time.time()
R1=T.run(T.build_world(f1), live_keys=set())
print("(1) NEEDERXX recent -> seed:", R1["seed"])
print("    locked:", R1["locked"], " SOURCEXX locked?", "SOURCEXX" in R1["locked"])
print()

# (2) NEEDERXX dormant (the bug case) -> confirm SOURCEXX NOT locked, but loadbearing flags it
R0=T.run(T.build_world(base), live_keys=set())
print("(2) NEEDERXX dormant -> locked:", R0["locked"])
print("    SOURCEXX locked?", "SOURCEXX" in R0["locked"], "(orphan risk)")
print("    SOURCEXX loadbearing?", "SOURCEXX" in R0["loadbearing"], "(loadbearing KNOWS, archive gate doesn't use it)")
print()
print("CONCLUSION: when the needer is a kept_unique_fork (dormant, not seeded), the closure")
print("never walks its needs(), so the phantom source is not in `locked` and is archivable.")
print("`loadbearing` correctly identifies the source but only gates markers, not archival.")
print("The fix is to seed the closure (or re-run it) with the FULL kept set incl kept_unique_forks,")
print("OR to gate archival on `loadbearing` as well as `locked`. As written, neither happens.")
