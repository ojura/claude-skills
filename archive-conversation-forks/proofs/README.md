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

Properties of the proofs:

- **Axiom honesty.** `no_orphan` and its structural lemmas (`closure_closed_under_needs`,
  `final_closed_under_needs`, `source_survives_C5`, `kept0_subset_final`,
  `nonseed_loadbearing`, `live_subset_keptC5`) are **fully constructive**: `#print axioms`
  reports *does not depend on any axioms*. `recall_no_loss` and `marker_no_hole` use a
  classical case-split, so they list the standard trusted core
  (`propext`, `Classical.choice`, `Quot.sound`). None lists `sorryAx`: there are no `sorry`s.
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
depend on any axioms* or a list drawn only from `propext` / `Classical.choice` / `Quot.sound`
(the trusted core), and never `sorryAx`. To rebuild from a clean state, `rm -rf .lake` first.

A `leanprovercommunity/lean` Docker image pinned at Lean 4.10.0 works as-is; run `lake
build` from this directory inside the container.

## Honest scope

These proofs certify the **set algebra**: given facts the code establishes, archiving never
orphans a kept session, never drops a message of a 0-unique candidate, never moves a live
session, and the marker tree has no hole. They are **not** a proof of the Python end to end:

- The hypotheses (`hpick`, `demoted_guard`, `source_lb`, the `cross_lb` / `phan_lb` /
  `C5_survivor_residue` / `live_not_demoted` facts) are transcribed from the Step 2 code; the
  proofs certify the logic *given* them. That the Python actually implements them (the JSONL
  parse, the union-find partition, `canonical()`, the fixpoint loop) is checked by the
  property-based fuzz, not by Lean.
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
- The union-find tree partition, the family-grouping rules, and `canonical()` selection are
  **not** formalised here; they are checked by fuzz. (`marker_no_hole` sidesteps union-find by
  working from the closure inversion plus the enriched seed, so it needs no tree model.)

## Files

- `Orphan.lean` - the closure, the re-close, C5 safety, `no_orphan`, and the recall no-loss theorem.
- `Markers.lean` - the closure-inversion lemma, `live_subset_keptC5`, and `marker_no_hole`.
- `Check.lean` - the axiom audit (`#print axioms`) and concrete non-vacuity models for all theorems.
- `lakefile.toml`, `lean-toolchain` - build configuration (Lean 4.10.0, no dependencies).
