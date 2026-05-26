# Set-algebra safety invariants, machine-checked in Lean 4

This directory contains a Lean 4 formalisation and proof of the safety invariants of the
Step 2 set algebra in `../SKILL.md` (the keep-locked closure, the C4 re-close, the C5
demotion, the C6 deferred archive, and the recall pass). The main result is the **no-orphan**
invariant; three further invariants (recall no-loss, live-survives, marker no-hole) are
proved alongside it.

## What is proved

**`Orphan.no_orphan` (the main result).**

> For every store and every operator `judge` policy, every needed phantom `P` of a
> finally-kept file `f`, if `P` has **any** source anywhere in the store, retains a
> source that is **also** in the finally-kept set.

In plain terms: the cleanup never archives the last phantom-backfill source of a session it
keeps, so a kept session's deep origin can never be orphaned by the move. ("Phantom",
"source", "needs", and the keep-locked closure are defined in `../SKILL.md`.)

**`Orphan.recall_no_loss` (the recall pass's content-level safety).** The recall pass archives
a candidate `A` when its message set is fully contained in the union of the kept files
(`missing = fps[A] - kept_union` is empty). This theorem proves that decision loses nothing:
if the test passes, every message of `A` still lives in some kept file. Where `no_orphan` is
the *structural* safety of archiving (never drop a needed source), `recall_no_loss` is the
*content* safety (never drop a message). Together they cover both archive paths.

**`Orphan.live_subset_keptC5` (live sessions are never moved).** Every live session (from the
`~/.claude/sessions` registry) is in the final kept set: the code seeds live into the closure
and C5 only ever demotes kept-unique forks, so a live session survives both. This is the
"never touch a running session" guarantee at the set-algebra level.

**`Orphan.marker_no_hole` (the marker tree is exhaustive over its range).** Every retained file
in the marker loop's range (`KEPT_C5` minus canonicals minus live) is either load-bearing or
has nonzero residue, so the `~0-residue AND NOT load-bearing` branch deleted from the marker
decision tree is unreachable. The proof is by inversion on the closure derivation
(`nonseed_loadbearing`: a non-seed closure member entered via the cross-file or phantom edge,
both of which are load-bearing) plus C5's demotion contract (a surviving non-load-bearing
kept-unique fork must have had nonzero residue). This was previously checked by fuzz only.

**`Family.*` (union-find grouping and `canonical()` selection).** The union-find partition is
modelled as the equivalence closure of the edge relation (shared-lpu or cross-file `dep`); the
imperative path-compressed `find` computes exactly this equivalence, so abstracting to it loses
no math content. Proved: it is an equivalence (so `trees` is a genuine partition); files sharing
an lpu value are same-tree; the **co-tree lemma** (a phantom needer and its sources both carry
the phantom in `lref`, so they group together) - which is the structural fact that grounds the
no-orphan setting, since it is *why* a needer's tree canonical can reach it; and false-family
non-merge (`SameTree` is generated only by lref/dep edges, never by message content, so files
sharing only a first message are not forced together). For `canonical()`: it returns a tree
member (`canonical_mem`) and respects the content floor (`canonical_nondebris`: non-debris
whenever a non-debris member exists). Which specific file it picks (the max-distinct / recency
key) is deliberately not modelled - membership and the floor are the safety-relevant properties.

**`FixProto.*` (the bridge facts derived, not assumed).** The theorems in `Orphan` / `Markers` are
stated over bridge hypotheses (`hpick`, `source_lb`, `cross_lb`, `phan_lb`, `demoted_guard`,
`live_not_demoted`, `C5_survivor`) that connect the abstract model to the code. `Fixpoint.lean`
discharges all of them from strictly weaker inputs, so none is a bald assumption:

- The six `loadbearing` / `demoted` facts become **lemmas** once those two predicates are modelled as
  their actual committed definitions: `loadbearing` as the UNION of the cross-file-target half and
  the phantom-source half (SKILL.md `loadbearing |= {targets} ∪ {s | sources(s) & needed}`), and
  `demoted` as the three-conjunct C5 guard `kuf ∧ ¬loadbearing ∧ ¬residue` (`for k in kuf: if k in
  loadbearing: continue; if not residue: discard`). `source_lb_from_def` / `cross_lb_from_def`
  (loadbearing from either half), `demoted_guard_from_def` (demoted ⇒ ¬loadbearing),
  `live_not_demoted_from_def` (live disjoint from `kuf` ⇒ not demoted), and `C5_survivor_from_def`
  (a surviving `kuf` is loadbearing or has residue, by a decidable case-split - constructive) all
  fall out of those definitions.
- `hpick` is derived (`hpick_from_closed`) from a single structural fact, `IsClosed locked`: the
  committed `while changed:` loop reached its fixpoint. `IsClosed` says exactly what loop
  termination says - no cross edge and no phantom rule can add anything more - so at the fixpoint
  every needed phantom of a locked file with a source keeps SOME source in `locked` (faithful to the
  `if not (set(srcs) & locked)` guard, which stops at some source, not the richest). This replaces a
  per-phantom existence claim that looked like it needed choice with one obviously-code-true fact.

`no_orphan_from_closed` reassembles the main result from these: same conclusion as `no_orphan`, but
assuming only `IsClosed locked` plus the set definitions, not the bridge hypotheses. All seven
`FixProto` results are fully axiom-free.

The marker theorems are wired the same way: `marker_no_hole_wired` and `live_subset_keptC5_wired`
(in `Markers.lean`) restate `marker_no_hole` / `live_subset_keptC5` over the FixProto-DEFINED
`loadbearing` and `demoted`, discharging the `cross_lb` / `phan_lb` / `live_not_demoted` /
`C5_survivor` side-conditions internally via the `*_from_def` lemmas. Their only inputs are the seed
decomposition, `closure ⊆ consumers`, `hpick`, and `live`/`kuf` disjointness - each a code fact -
so nothing about loadbearing/demoted is assumed at the marker layer either. Both are axiom-free.

**`TermList.*` (`IsClosed` is achievable, not assumed).** `closed_superset_exists` proves that over a
finite universe a closed `locked ⊇ seed` exists, so `IsClosed` - the one fact `hpick` rested on - is
discharged rather than assumed. This is the committed loop's termination: each changing iteration
adds a universe element, a monotone fixpoint over a finite set. The decreasing measure (`gap`, the
count of not-yet-included universe elements) and its strict-drop lemma (`gap_lt`) are hand-rolled in
core Lean (no mathlib) and closed by `Nat.strongInductionOn`. The only residue is the `expand`
loop-body oracle, which is constructible for any concrete decidable store (demonstrated by
`concrete_termination`).

Properties of the proofs:

- **Axiom honesty.** Most theorems are **fully constructive**; the few that aren't use only
  trusted-core axioms. `no_orphan` and its structural lemmas (`closure_closed_under_needs`,
  `final_closed_under_needs`, `source_survives_C5`, `kept0_subset_final`, `nonseed_loadbearing`,
  `live_subset_keptC5`), the recall and marker theorems (`recall_no_loss`, `marker_no_hole`), the
  union-find lemmas (`needer_source_coTree`, `noEdges_sameTree_eq`), and all four `FixProto` results
  report *does not depend on any axioms*. `recall_no_loss` stays constructive by case-splitting on
  the kept-union membership as a `Decidable` instance (the faithful counterpart of the code's set
  `in`, not `Classical.em`); `marker_no_hole` by a direct `cases` on the closure derivation. The
  `canonical_*` lemmas list `propext` (from `List.filter` reasoning); the `TermList.*` termination
  lemmas list `[propext, Quot.sound]` (from `List` / `decide` / `Quot`). Both are the innocuous
  trusted core. None lists `Classical.choice`, `sorryAx`, or `Lean.ofReduceBool`: no `sorry`, no
  `native_decide`, no mathlib.
- **All judge policies, all stores.** Files, fingerprints, `needs`, `sources`, and the
  cross-file edge are opaque; the operator's per-fork keep/archive judgment enters only as
  the abstract seed set, so each theorem covers every possible judge outcome and every store
  shape at once. This is strictly stronger than the property-based fuzz the skill was also
  checked against, which only samples judge outcomes.
- **Non-vacuous.** `Check.lean` instantiates each theorem on a concrete store where its
  precondition genuinely holds (a kept file that needs a sourced phantom; a 0-unique recall
  candidate whose message is in a kept file; a surviving kept-unique fork with residue), so
  every conclusion is a real claim, not vacuously true of nothing.

## How the closure is modelled

The keep-locked closure is an **inductive predicate** (`Orphan.Closure`) with three
constructors mirroring the committed `locked_closure`: the seed, the cross-file ancestor
edge, and the phantom-backfill-source edge. Because the closure is inductive, "closed under
the closure rules" is true by definition, the least-fixpoint induction principle comes free
from the recursor, and `cases` on a membership derivation gives the inversion lemma directly.
This is why the proofs need **no mathlib** (no `Finset`, no finite-lattice monotone-closure
machinery): plain `File -> Prop` predicates suffice.

The C4 re-close (`locked = locked_closure(KEPT); KEPT |= locked`) is `KEPT_final`, the
closure over the pre-re-close kept set. C5 is `KEPT_C5 = KEPT_final` minus the demoted
forks. C6 archives only files outside `KEPT_C5`, so it cannot remove a kept member; the
theorems about `KEPT_C5` are therefore theorems about the final picker set. `Markers.lean`
adds an enriched seed (`seed0 = canonicals union live union kept-unique-forks`) so the
inversion can place a non-canonical, non-live member into the kept-unique-fork bucket and
invoke C5's contract; the abstract `no_orphan` in `Orphan.lean` is left generic over the seed.

## How to build

Stock Lean 4.10.0, no mathlib, no network fetch. With `elan`/`lake` on a v4.10.0 toolchain:

```
lake build
```

The build prints the `#print axioms` lines from `Check.lean`; each must read either *does not
depend on any axioms* or `[propext]` (the only axiom any theorem here uses), and never
`Classical.choice`, `sorryAx`, or `Lean.ofReduceBool`. To rebuild clean, `rm -rf .lake` first.

A `leanprovercommunity/lean` Docker image pinned at Lean 4.10.0 works as-is; run `lake
build` from this directory inside the container.

## Honest scope

These proofs certify the **set algebra**: given facts the code establishes, archiving never
orphans a kept session, never drops a message of a 0-unique candidate, never moves a live
session, and the marker tree has no hole. They are **not** a proof of the Python end to end:

- After `Fixpoint.lean` and `Termination.lean`, the bridge facts are all *derived*, not assumed.
  `source_lb`, `cross_lb`, `phan_lb`, `demoted_guard`, `live_not_demoted`, and `C5_survivor` all
  follow from the set definitions of `loadbearing` (now the actual UNION of the cross-target and
  phantom-source halves) and `demoted` (the actual three-conjunct C5 guard `kuf ∧ ¬loadbearing ∧
  ¬residue`); `hpick` follows from `IsClosed locked`; and `IsClosed` itself is no longer assumed - it
  is *proven achievable* (`closed_superset_exists`: a closed `locked ⊇ seed` exists over a finite
  universe). The only residue left inside the algebra is the `expand` step below. That the Python
  *implements* the abstract relations (the JSONL parse, the actual union-find, `canonical()`, the
  loop body) is checked by the property-based fuzz, not by Lean.
- The termination proof (`Termination.lean`) is **mathlib-free, in core Lean**. Core Lean 4.10's
  `List` lacks `Sublist` and `countP`, so the decreasing-measure lemma (`gap_lt`: adding a forced
  element strictly shrinks the count of not-yet-included universe elements) and its monotonicity
  helper are hand-rolled by plain structural induction, then fed to `Nat.strongInductionOn`. Mathlib
  (a `Finset.card` one-liner) was evaluated and declined: it is not wired as a build dependency, so
  importing it would break the stock-image `lake build`, and the core proof is only two small lemmas.
  The residue here is the `expand` parameter of `closed_superset_exists` - the loop-body oracle that,
  given a not-yet-closed set, returns a forced new element (or certifies closure). For a concrete
  store with decidable relations `expand` is constructible, not assumed; `Check.lean`'s
  `concrete_termination` builds it explicitly and runs the theorem, showing it is realisable.
- `pick P` abstracts the source the closure **realises** for a phantom `P` at fixpoint, not
  the literal richest source. The committed `locked_closure` adds its richest candidate only
  when no source of `P` is already locked (`if not (set(srcs) & locked)`), so a different,
  already-locked source can be the one kept. The proofs depend only on the existential the
  code guarantees ("some source of a needed phantom stays locked"), via the `hpick` contract,
  and are generic over which source that is. They do not assume the richest source is locked.
- `marker_no_hole` proves the marker partition has no unreachable-into hole; it does **not**
  prove the markers are semantically correct (that a `[fork]` is really mostly-contained, etc.).
  The `ov` / `residue` thresholds are operator read-and-judge calls, not arithmetic, so that
  layer is deliberately outside the formalisation.
- The union-find partition and `canonical()` are formalised in `Family.lean` at the
  EQUIVALENCE / property level (it is an equivalence; shared-lpu and needer-source group; content
  does not merge; canonical returns a member and respects the floor). What is NOT formalised: the
  imperative path-compressed `find` itself (it computes the same equivalence, but the mutable data
  structure is bookkeeping with no extra math), the exact `canonical` max-key choice (only
  membership and the floor matter for safety), and the full family/theme assignment of Steps 4-5.
  Those remain fuzz-checked. (`marker_no_hole` itself needs no tree model: it works from the
  closure inversion plus the enriched seed.)

  These are deliberately **structural-only**, and that is safe because grouping and canonical
  choice are not on the data-loss path: a wrong grouping or a wrong canonical pick can only cause a
  file to be KEPT that might have been archivable (over-keep), never cause a unique file to be lost.
  Data loss is gated downstream by `no_orphan` (a needed source is preserved regardless of tree
  shape) and `recall_no_loss` (redundancy is measured against the whole kept union, not per-tree),
  both of which hold for any grouping. So the worst case of a union-find or `canonical()` error is a
  cluttered picker, not a dropped session - which is why leaving them at the structural / fuzz level
  is acceptable.

## Files

- `Orphan.lean` - the closure, the re-close, C5 safety, `no_orphan`, and the recall no-loss theorem.
- `Markers.lean` - the closure-inversion lemma, `live_subset_keptC5`, and `marker_no_hole`.
- `Family.lean` - union-find as an equivalence (the co-tree and false-family lemmas) and `canonical()` membership + floor.
- `Fixpoint.lean` - `IsClosed`, the seven bridge facts derived (`hpick_from_closed`, `source_lb_from_def`, `cross_lb_from_def`, `demoted_guard_from_def`, `live_not_demoted_from_def`, `C5_survivor_from_def`), and `no_orphan_from_closed`.
- `Termination.lean` - `gap_lt` and `closed_superset_exists`: a closed set exists over a finite universe, discharging `IsClosed`. Core Lean, hand-rolled (no mathlib).
- `Check.lean` - the axiom audit (`#print axioms`) and concrete non-vacuity models for all theorems.
- `lakefile.toml`, `lean-toolchain` - build configuration (Lean 4.10.0, no dependencies).
