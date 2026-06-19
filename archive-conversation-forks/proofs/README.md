# Set-algebra safety invariants, machine-checked in Lean 4

This directory contains a Lean 4 formalisation and proof of the safety invariants of the
Step 2 set algebra in `../SKILL.md` (the keep-locked closure, the C4 re-close, the C5
demotion, the C6 deferred archive, and the recall pass). The main result is the **no-orphan**
invariant; three further invariants (recall no-loss, live-survives, marker no-hole) are
proved alongside it.

## What is proved

**`FixProto.no_orphan_from_closed` (the structural safety lemma) and `TermList.no_orphan_from_closedB` (its oracle-free, end-to-end form).**

> For every store and every operator `judge` policy, every needed phantom `P` of a
> finally-kept file `f`, if `P` has **any** source anywhere in the store, retains a
> source that is **also** in the finally-kept set.

In plain terms: the cleanup never archives the last phantom-backfill source of a session it
keeps, so a kept session's deep origin can never be orphaned by the move. ("Phantom",
"source", "needs", and the keep-locked closure are defined in `../SKILL.md`.)

The safety guarantee is a CHAIN of discharged lemmas, not one monolithic theorem. `Orphan.no_orphan`
is the **conditional core**: it takes the three bridge facts (`hpick` / `source_lb` / `demoted_guard`)
as hypotheses. `FixProto.no_orphan_from_closed` discharges all three from a single closedness
hypothesis plus the set definitions of `loadbearing` / `demoted` - but it remains *conditional on that
closedness*: it takes `IsClosed locked` (the loop reached its fixpoint). The honest question is what
discharges the closedness, and at which strength.

`TermList.no_orphan_from_closedB` is the form a reader should land on, because it is achievable
**oracle-free and end-to-end**. It consumes the BOUNDED closedness `ClosedB` - the finite fixpoint the
committed loop actually computes (quantifiers over the present files `U` and phantoms `Ps`) - plus two
completeness facts that hold of the store (every needed phantom is in `Ps`, every source is in `U`).
The safety argument needs closedness only through the phantom rule (`phan_closed`); it never uses
`cross_closed`. And `TermList.closed_superset_exists_constructed` produces exactly this `ClosedB` with
NO `expand` oracle. So the chain `closed_superset_exists_constructed` (bounded closure, oracle-free) →
`no_orphan_from_closedB` (no orphan) assumes nothing the code does not compute. The real-edge witness
`D1WitnessReal` (Check.lean) shows this is non-vacuous on a store with a genuine cross edge AND a genuine
phantom, not the trivial edgeless case.

The unbounded `FixProto.IsClosed` and `closed_superset_exists` remain as the abstract generic form
(`IsClosed → no-orphan`, with achievability *relative to* the `expand` oracle), useful for reasoning;
but the oracle-free, end-to-end safety goes through the bounded `ClosedB`. `Orphan.no_orphan` is the
reusable lemma all of these are built from.

**`Orphan.recall_no_loss` (the recall pass's content-level safety).** The recall pass archives
a candidate `A` when its message set is fully contained in the union of the kept files
(`missing = fingerprints[A] - kept_union` is empty). This theorem proves that decision loses nothing:
if the test passes, every message of `A` still lives in some kept file. Where `no_orphan` is
the *structural* safety of archiving (never drop a needed source), `recall_no_loss` is the
*content* safety (never drop a message).

**Scope of the content-safety guarantee.** A move can lose content in one specific way: archiving a file
that holds the only copy of some message a kept file still needs. The proof separates that from a different
question it deliberately leaves to a person. For each archived file ask two things. First, is this file's
own content worthless? That is the operator's verbatim read (for debris) or the Sonnet's read (for a judged
fork); it is not a math predicate, so it stays out of Lean. Second, does archiving this file strand another
file's only copy of a message? That one is a math predicate, and it is proved.

Four paths archive a file: the 0-unique recall path, the C6 per-tree archive of judged-worthless forks, the
nonzero-recall `SONNET_CONFIRM` path, and the Step-2 debris nomination. `recall_no_loss` covers the
0-unique recall path: when its `missing = ∅` test passes, every message of the archived file still lives in
a kept file. `content_safe_post_debris` and `c5_demote_no_loss` extend this to the debris and C5 paths: the
recall and C5 archives measure containment against the kept set *after* debris is removed, so the file they
credit as the container is itself never a debris file that is also leaving. The load-bearing ordering is
that debris is discarded before the recall and C5 passes compute containment, so no path keeps a container
a later path then moves. The C6 judged-worthless path and the nonzero `SONNET_CONFIRM` path still have no
message-loss theorem, by design: whether their unique residue is worthless is the read, not Lean.
`no_orphan` covers all four paths *structurally* (none ever orphans a kept session's phantom source); the
message-loss guarantee now holds for the recall, debris, and C5 paths, and only the worthlessness judgment
rests on the read.

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

**`Orphan.classify` + `*_iff` (the marker assignment is total and mutually exclusive).** Complementing
the no-hole (exhaustiveness) result, `classify` models the documented decision tree as a total
function of the measured inputs, and `classify_total` / `scrollDep_iff` / `main_iff` / `fork_iff` /
`none_iff` / `classify_exhaustive` prove every input yields exactly one marker, with the four
branch-guards pairwise disjoint and jointly exhaustive (by `decide` over the Bool input cube). The
one genuinely-judgment input - whether the residue is "substantive" - enters as an opaque Bool, so
this formalises everything mechanical about the tree and isolates the single operator call.

Two honest notes on `classify`. First, the no-hole guarantee (no retained file falls through the
deleted `~0-residue AND NOT load-bearing` cell) is carried by **`marker_no_hole`**, not by
`classify`'s totality - a total function trivially returns something for every input; what matters is
that the *real* range can never hit the removed branch, which is the load-bearing argument in
`marker_no_hole`. Second, `classify` folds two distinct operator judgments into the single
`substantive` Bool (the `[main]`-vs-none substance floor and the `[fork]`-vs-none triviality call),
and it is slightly MORE decisive than the loop comments: for the high-ov + non-substantive case the
prose leaves open, `classify` commits to `none`. So `classify` documents one faithful resolution of
the tree, not the only admissible one; `marker_no_hole` is the load-bearing guarantee.

**`FixProto.no_orphan_from_closed_debris` + `Family.singleton_canonicalPick` / `Orphan.marker_range_excludes_debris` (the DEBRIS demotion, brought inside the proof).** The committed pipeline removes files
from the picker on TWO non-load-bearing paths, not one: C5 demotion AND the Step-2 `nominate_debris`
(guarded by `if k in loadbearing: continue`, so `debris k → ¬loadbearing k`). `no_orphan` was always
generic over the removal set - it constrains `demoted` ONLY through `demoted_guard : removed → ¬loadbearing`
- so the second removal composes: `no_orphan_from_closed_debris` proves no-orphan survives `locked` minus
BOTH demotions, because the fixpoint-realised source is load-bearing and NEITHER path removes a
load-bearing file. The marker side needs nothing extra structurally: `Family.singleton_canonicalPick`
proves a singleton tree's canonical is its sole member (whether or not it is `is_debris`, since the
floored `cand` falls back to `ks`), hence `debris ⊆ canonicals` (`debris_nominated_canonical`), and
`marker_range_excludes_debris` then shows the marker loop's `¬canonicals` range never contains a debris
file - so `marker_no_hole` carries verbatim. This closes the gap the SKILL.md guard change
(`locked`→`loadbearing` in debris nomination) would otherwise have left: the new archive path is
machine-checked, not assumed safe. Structurally, `no_orphan_from_closed_debris` shows debris removal never
orphans a kept session's phantom source. For content, `content_safe_post_debris` and `c5_demote_no_loss`
show the recall and C5 passes credit only a kept file that survives debris removal, so archiving a debris
file never strands another file's only copy of a message; this rests on discarding debris before those
passes compute containment, and on `residue_grows_on_shrink` (removing debris only grows other files'
residue, so the marker tree is untouched). A singleton debris file changes no other file's load-bearing
status (`loadbearing_stable`). The new theorems are fully constructive (no axioms); the `canonicalPick`
and the concrete debris witnesses list only `propext`, like the other `canonical_*` lemmas. What still
rests on the operator's read is one thing: whether the debris file's own content is worthless. That a file
holds nothing worth keeping is a person's judgment; that archiving it loses no other file's message is now
proved.

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

**`Boundary.*` (the par/nb reconstruction of `needs` / `sources`, promoted from opaque to defined).**
In `Orphan.lean` / `Fixpoint.lean` the relations `needs` / `sources : File → Phantom → Prop` are
opaque `variable`s, so the structural theorems hold for *any* needs/sources. `Boundary.lean` does the
same move `Fixpoint.lean` does for `loadbearing` / `demoted`, one layer deeper: it DEFINES them as the
committed set-builder over each file's `compact_boundary` records `(lpu, par, nb)` (`sourcesOf` /
`needsOf`, SKILL.md `sources(k)` / `needs(k)` verbatim). The payoff is the par/nb CLASSIFICATION - the
single place this code has been miswritten before (the `par is not None`-only form that dropped the
`nb>0` half, turning a backfill SOURCE into a NEEDER, the orphan-causing direction). Proved: the
per-record source/need test is a TOTAL, MUTUALLY-EXCLUSIVE partition (`rec_excl` / `rec_total` /
`rec_iff`: a need is the EXACT negation of a source), with the mechanical 2-bit core closed by `decide`
(`bit_partition`, the same technique as `classify_exhaustive`); and the historical bug is exhibited as
a machine-checked DIVERGENCE (`lazy_flips_source_to_need`: on a null-parent-but-real-pre-content record
the committed test says SOURCE while the lazy par-only test says NEED). `no_orphan_from_closed_bnd`
restates the no-orphan over the DEFINED relations - instantiation, since the algebra was
always generic over needs/sources - so the guarantee lands on the relations the skill actually
computes, not on opaque stand-ins. All `Boundary` results are **fully axiom-free**. SCOPE: this proves
the LOGIC of needs/sources GIVEN the `(lpu, par, nb)` records; the JSONL→records parse stays
fuzz-checked (see Honest scope), so the gap on needs/sources shrinks from "all of the par/nb layer" to
"only the triple extraction".

**`TermList.*` (closedness is ACHIEVED oracle-free at the bounded level `ClosedB`).** There are two
results, at two strengths, and the distinction matters. `closed_superset_exists` yields the UNBOUNDED
`FixProto.IsClosed`, but it takes the loop-body oracle `expand` as a hypothesis - so it RELOCATES the
termination burden rather than discharging it oracle-free. `closed_superset_exists_constructed`
CONSTRUCTS that oracle (for a finite store with decidable relations, `expand` is a decidable finite
search - `crossViol` / `phanViol` / `phanSrc` - returning a forced new element or certifying closure via
`closed_of_none`) and runs the whole loop with NO oracle parameter; what it achieves is BOUNDED
closedness `ClosedB` (quantifiers over the present files `U` / phantoms `Ps`), the exact finite fixpoint
the committed loop computes. This is the committed loop's termination: each changing iteration adds a
universe element, a monotone fixpoint over a finite set, with decreasing measure `gap` (not-yet-included
universe elements) and strict-drop lemma `gap_lt`, hand-rolled in core Lean (no mathlib) and closed by
`Nat.strongInductionOn`.

The safety conclusion consumes this bounded `ClosedB` DIRECTLY, via `no_orphan_from_closedB` (which uses
closedness only through `phan_closed`, never `cross_closed`), so the oracle-free chain is end-to-end with
no step that inflates `ClosedB` to the unbounded `IsClosed`. We do NOT rest on a bare "`ClosedB`
coincides with the unbounded fixpoint" assertion: the safety theorem needs only the bounded form, and
`D1WitnessReal` exercises the whole chain on a store with a real cross edge and a real phantom.

Properties of the proofs:

- **Axiom honesty.** Most theorems are **fully constructive**; the few that aren't use only
  trusted-core axioms. `no_orphan` and its structural lemmas (`closure_closed_under_needs`,
  `final_closed_under_needs`, `source_survives_C5`, `kept0_subset_final`, `nonseed_loadbearing`,
  `live_subset_keptC5`), the recall and marker theorems (`recall_no_loss`, `marker_no_hole`), the
  union-find lemmas (`needer_source_coTree`, `noEdges_sameTree_eq`), all four `FixProto` results, and
  every `Boundary` result (`rec_excl` / `rec_total` / `rec_iff`, `bit_partition`,
  `lazy_flips_source_to_need`, `no_orphan_from_closed_bnd`)
  report *does not depend on any axioms*. `recall_no_loss` stays constructive by case-splitting on
  the kept-union membership as a `Decidable` instance (the faithful counterpart of the code's set
  `in`, not `Classical.em`); `marker_no_hole` by a direct `cases` on the closure derivation. The
  `canonical_*` lemmas list `propext` (from `List.filter` reasoning); the `TermList.*` termination
  lemmas list `[propext, Quot.sound]` (from `List` / `decide` / `Quot`). Both are the innocuous
  trusted core. None lists `Classical.choice`, `sorryAx`, or `Lean.ofReduceBool`: no `sorry`, no
  `native_decide`, no mathlib.
- **All judge policies, all stores.** Files, fingerprints, `needs`, `sources`, and the
  cross-file edge are opaque in the structural theorems; the operator's per-fork keep/archive judgment
  enters only as the abstract seed set, so each theorem covers every possible judge outcome and every
  store shape at once. (`Boundary.lean` *additionally* pins `needs` / `sources` to their committed
  boundary set-builder and feeds that defined instance back through the same theorems - extra coverage,
  not a loss of generality.) This is strictly stronger than the property-based fuzz the skill was also
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
depend on any axioms* or a subset of `[propext, Quot.sound]` (the trusted core; `Quot.sound` enters
only via the `List` / `decide` machinery in the termination and search lemmas), and never
`Classical.choice`, `sorryAx`, or `Lean.ofReduceBool`. To rebuild clean, `rm -rf .lake` first.

A `leanprovercommunity/lean` Docker image pinned at Lean 4.10.0 works as-is; run `lake
build` from this directory inside the container.

## Honest scope

These proofs cover the **structural algorithm** of Step 2 - archiving never orphans a kept session
(`no_orphan`, all paths), never moves a live session, the marker tree is exhaustive with a total +
mutually-exclusive assignment, the union-find grouping is an equivalence (co-tree and false-family),
`canonical()` returns a member / respects the floor / picks the max key, the keep-locked loop
terminates, and even the `find` path-compression optimisation preserves components. The bridge facts
connecting the abstract model to the code are *derived*, not assumed, and the one loop-body oracle is
*constructed*. Two things are deliberately NOT machine-checked, for different reasons. (a) **Whether a
judged residue is worthless**: `recall_no_loss`, `content_safe_post_debris`, and `c5_demote_no_loss` prove
no archived file strands another file's only message on the 0-unique recall, debris, and C5 paths; only
the C6 judged-worthless and nonzero-`SONNET_CONFIRM` paths lack a message-loss theorem, because whether
their unique residue is worthless is the operator / Sonnet read (see the content-safety scope note above),
not a math predicate. The debris path is additionally exercised by the property-based fuzz
(`check_no_lost_message` in `step2_model.py`, which fails on the old debris-last ordering and passes on the
window-of-one fix). (b) The
**model-to-Python boundary** (genuinely irreducible for a model-level proof): whether the Python
computes what the abstract relations (`cross`, the fingerprints, the key scalars) and the
`needs`/`sources` boundary records say - the JSONL parse, the actual mutable union-find, the real
`canonical()` key computation, the loop body - checked by the property-based fuzz. Concretely, what
stays out of model:

A specific, operator-facing instance of (b), now NARROWED by `Boundary.lean`. `needs` / `sources`
USED to be fully opaque relations (`variable (needs sources : File -> Phantom -> Prop)`), so the
par/nb logic that *computes* them was entirely below the proof. `Boundary.lean` now DEFINES them as
the committed set-builder - `sourcesOf` / `needsOf` over the `(lpu, par, nb)` records, `sources(k) =
{lp for (lp,par,nb) in bnd[k] if lp in phantom and (par is not None or nb>0)}` verbatim - and
machine-checks the classification: the per-record source/need partition (`rec_iff`: a need is the
EXACT negation of a source) and the `par is not None`-only bug as a checked divergence
(`lazy_flips_source_to_need`). So the par/nb LOGIC, precisely where the code has been miswritten
before (the lazy form that dropped the `nb>0` half), is **no longer fuzz-only - it is proved**. What
remains fuzz-checked is strictly the JSONL→records EXTRACTION: that `bnd[k]` really holds this file's
`(logicalParentUuid, parentUuid-is-present, msgs-before)` triples. The practical consequence is
unchanged and still load-bearing: **the proof certifies the committed set-builder, not a re-derived
approximation.** The theorems are about `sourcesOf` / `needsOf` AS WRITTEN; an operator who eyeballs or
hand-rolls the orphan check instead of running the committed definition steps outside what is proved,
and `lazy_flips_source_to_need` is exactly the proof that the shortcut diverges (a source read as a
need). Run the committed predicate, never a re-derived one; do not let this proof's existence tempt
the shortcut. (Mirrors the `sources()`/`needs()` DISCIPLINE note in `../SKILL.md`.)

- After `Fixpoint.lean` and `Termination.lean`, the bridge facts are all *derived*, not assumed.
  `source_lb`, `cross_lb`, `phan_lb`, `demoted_guard`, `live_not_demoted`, and `C5_survivor` all
  follow from the set definitions of `loadbearing` (now the actual UNION of the cross-target and
  phantom-source halves) and `demoted` (the actual three-conjunct C5 guard `kuf ∧ ¬loadbearing ∧
  ¬residue`); `hpick` follows from closedness; and closedness itself is **achieved oracle-free at the
  BOUNDED level**: `closed_superset_exists_constructed` yields `ClosedB` (the finite fixpoint the loop
  actually computes) with the loop-body oracle *constructed*, not assumed (see below). The safety
  conclusion consumes that bounded `ClosedB` directly via `no_orphan_from_closedB`; the unbounded
  `IsClosed` (`closed_superset_exists`) is reached only *relative to* the `expand` oracle and is not
  needed for safety. So the only thing not formalised is whether the Python *implements* the abstract
  relations (the JSONL parse, the actual union-find, `canonical()`, the loop body) - checked by the fuzz.
- The termination proof (`Termination.lean`) is **mathlib-free, in core Lean**. Core Lean 4.10's
  `List` lacks `Sublist` and `countP`, so the decreasing-measure lemma (`gap_lt`: adding a forced
  element strictly shrinks the count of not-yet-included universe elements) and its monotonicity
  helper are hand-rolled by plain structural induction, then fed to `Nat.strongInductionOn`. Mathlib
  (a `Finset.card` one-liner) was evaluated and declined: it is not wired as a build dependency, so
  importing it would break the stock-image `lake build`, and the core proof is a handful of lemmas.
- The loop-body oracle (`expand`) is **constructed, not assumed.** For a finite store with DECIDABLE
  relations, `expand` is built as a decidable finite search (`crossViol` / `phanViol` / `phanSrc`)
  that, given a not-yet-closed set, returns a forced new element or certifies bounded closedness
  (`closed_of_none`, the refutation direction). `closed_superset_exists_constructed` then runs the
  whole loop with that constructed oracle - no oracle parameter - yielding a bounded-closed superset.
  Closedness here is BOUNDED (`ClosedB`: quantifiers over the finite universe and phantom list, source
  existential bounded to the universe), the faithful model of a loop that never scans infinitely; over
  a finite store it coincides with the committed loop's fixpoint condition. `Check.lean`'s
  `concrete_termination_constructed` runs it on a concrete store with no oracle passed in.
- `pick P` abstracts the source the closure **realises** for a phantom `P` at fixpoint, not
  the literal richest source. The committed `locked_closure` adds its richest candidate only
  when no source of `P` is already locked (`if not (set(srcs) & locked)`), so a different,
  already-locked source can be the one kept. The proofs depend only on the existential the
  code guarantees ("some source of a needed phantom stays locked"), via the `hpick` contract,
  and are generic over which source that is. They do not assume the richest source is locked.
- Marker classification is formalised at TWO levels. `marker_no_hole` proves the partition has no
  unreachable-into hole (no file falls through); and `classify` + `classify_total` /
  `scrollDep_iff` / `main_iff` / `fork_iff` / `none_iff` / `classify_exhaustive` prove the documented
  decision tree is a TOTAL function assigning exactly one marker, with the four branch-guards
  pairwise disjoint and jointly exhaustive (by `decide` over the input cube). What stays out of model
  is ONE genuinely-judgment input: whether the residue read is "substantive" - not a math predicate,
  so it enters `classify` as an opaque Bool. Given the bucket classification, the assignment is fully
  pinned down; only the substantive/not call is the operator's.
- The marker proofs model `residue` as a STATIC predicate, and that is faithful for the files the
  marker loop ranges over (C5 survivors) by a residue-monotonicity argument: C5 removes only files
  with EXACTLY 0 residue, which contribute no unique message, so removing them can only GROW every
  other file's residue against the shrinking kept set. Hence a C5 survivor with nonzero residue keeps
  nonzero residue against the final `KEPT` - the static model never overstates a survivor's residue.
  (Mirrors the in-code comment in `SKILL.md`'s C5 block.)
- `canonical()` is formalised at the property level (`canonical_mem` returns a member,
  `canonical_nondebris` respects the content floor) AND the selection level: `Family.Canon` builds the
  key's lexicographic order in core Lean (`klt` / `kle` over `Nat × Nat × Nat`, with totality,
  transitivity proven from core `Nat` lemmas - no mathlib `LinearOrder`), and `canonicalByKey_is_max`
  proves arg-max picks an element whose key is maximal among the floored candidates. The key's three
  components are modelled as the scalars the code compares (distinct-count, normalised-sortable
  timestamp, uuid), all `Nat`; computing those scalars from fingerprints / parsing / bytes is the
  Python boundary (note: core `String` lacks the order lemmas, which is why the components are modelled
  as their compared scalars rather than raw strings). The union-find partition is formalised AS the
  equivalence `SameTree` (`Family.lean`: equivalence laws, shared-lpu and needer-source grouping,
  content-does-not-merge), and the `find` path-compression optimisation is shown to preserve the
  computed component (`Family.Compress`: `compress_preserves_root_self`, `root_compress_v` - repointing
  a node at its root leaves every node's root unchanged), so the optimised `find` returns the same
  answer as the naive one. The full family/theme assignment of Steps 4-5 is fuzz-checked.

  These are deliberately **structural-only**, and that is safe because grouping and canonical
  choice are not on the data-loss path: a wrong grouping or a wrong canonical pick can only cause a
  file to be KEPT that might have been archivable (over-keep), never cause a unique file to be lost.
  Data loss is gated downstream by `no_orphan` (a needed source is preserved regardless of tree
  shape) and `recall_no_loss` (redundancy is measured against the whole kept union, not per-tree),
  both of which hold for any grouping. So the worst case of a union-find or `canonical()` error is a
  cluttered picker, not a dropped session - which is why leaving them at the structural / fuzz level
  is acceptable.

## Files

- `Orphan.lean` - the closure, the re-close, C5 safety, `no_orphan`, the recall no-loss theorem, and the content-safety-under-debris theorems (`content_safe_post_debris` and `c5_demote_no_loss` for the recall and C5 paths over the post-debris kept set, plus `residue_grows_on_shrink` / `nonzero_residue_survives_shrink`).
- `Markers.lean` - the closure-inversion lemma, `live_subset_keptC5`, `marker_no_hole`, `marker_range_excludes_debris` (the marker range omits debris), the wired capstones, and the marker classification (`classify` + total/exclusive/exhaustive lemmas).
- `Family.lean` - union-find as an equivalence (co-tree and false-family lemmas), `canonical()` membership + floor + max-key selection (`Family.Canon`, core lex order), `singleton_canonicalPick` / `debris_nominated_canonical` (`debris ⊆ canonicals`), the loadbearing-stability bridges (`singleton_d_no_cross` / `singleton_d_no_share`, discharging the singleton guard into the stability hypotheses), and `find` path-compression correctness (`Family.Compress`).
- `Fixpoint.lean` - `IsClosed`, the seven bridge facts derived (`hpick_from_closed`, `source_lb_from_def`, `cross_lb_from_def`, `demoted_guard_from_def`, `live_not_demoted_from_def`, `C5_survivor_from_def`), `no_orphan_from_closed`, `no_orphan_from_closed_debris` (no-orphan survives the debris demotion), and `loadbearing_stable` (discarding a singleton debris file changes no other file's load-bearing status).
- `Boundary.lean` - the par/nb layer: `needs` / `sources` DEFINED as the committed set-builder over `(lpu, par, nb)` records (`sourcesOf` / `needsOf`), the per-record source/need partition (`rec_excl` / `rec_total` / `rec_iff`, 2-bit cube `bit_partition`), the `par is not None`-only bug as a checked divergence (`lazy_flips_source_to_need`), and `no_orphan_from_closed_bnd` (the no-orphan over the defined relations). Axiom-free.
- `Termination.lean` - `gap_lt`; `closed_superset_exists` (yields the unbounded `IsClosed` GIVEN the `expand` oracle); the constructed, oracle-free `closed_superset_exists_constructed` (builds `expand` via `closed_of_none`, yields the BOUNDED `ClosedB` - the finite fixpoint the loop computes); and `no_orphan_from_closedB` (the safety conclusion over `ClosedB`, the oracle-free end-to-end form, fed directly by the constructed achiever). Core Lean, hand-rolled (no mathlib).
- `Check.lean` - the axiom audit (`#print axioms`) and concrete non-vacuity models for all theorems.
- `lakefile.toml`, `lean-toolchain` - build configuration (Lean 4.10.0, no dependencies).
