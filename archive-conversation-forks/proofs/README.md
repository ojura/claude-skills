# No-orphan invariant, machine-checked in Lean 4

This directory contains a Lean 4 formalisation and proof of the **no-orphan invariant** for
the Step 2 set algebra in `../SKILL.md` (the keep-locked closure, the C4 re-close, the C5
demotion, and the C6 deferred archive).

## What is proved

The main theorem is `Orphan.no_orphan`:

> For every store and every operator `judge` policy, every needed phantom `P` of a
> finally-kept file `f`, if `P` has **any** source anywhere in the store, retains a
> source that is **also** in the finally-kept set.

In plain terms: the cleanup never archives the last phantom-backfill source of a session it
keeps, so a kept session's deep origin can never be orphaned by the move. ("Phantom",
"source", "needs", and the keep-locked closure are defined in `../SKILL.md`.)

Properties of the proof:

- **Constructive, zero axioms.** `#print axioms no_orphan` reports *does not depend on any
  axioms* (no `sorry`, no `propext`, no `Classical.choice`). The supporting lemmas
  (`closure_closed_under_needs`, `final_closed_under_needs`, `source_survives_C5`,
  `kept0_subset_final`) are all axiom-free too.
- **All judge policies, all stores.** Files, fingerprints, `needs`, `sources`, and the
  cross-file edge are opaque; the operator's per-fork keep/archive judgment enters only as
  the abstract seed set, so the single theorem covers every possible judge outcome and every
  store shape at once. This is strictly stronger than the property-based fuzz the skill was
  also checked against, which only samples judge outcomes.
- **Non-vacuous.** `Check.lean` instantiates the theorem on a concrete store where a kept
  file genuinely needs a sourced phantom (so the conclusion is a real existence claim, not
  trivially true of nothing), and shows the retained witness is the actual source.
  `concrete_no_orphan` compiles axiom-free.

## How the closure is modelled

The keep-locked closure is an **inductive predicate** (`Orphan.Closure`) with three
constructors mirroring the committed `locked_closure`: the seed, the cross-file ancestor
edge, and the phantom-backfill-source edge. Because the closure is inductive, "closed under
the closure rules" is true by definition and the least-fixpoint induction principle comes
free from the recursor. This is why the proof needs **no mathlib** (no `Finset`, no
finite-lattice monotone-closure machinery): plain `File -> Prop` predicates suffice.

The C4 re-close (`locked = locked_closure(KEPT); KEPT |= locked`) is `KEPT_final`, the
closure over the pre-re-close kept set. C5 is `KEPT_C5 = KEPT_final` minus the demoted
forks. C6 archives only files outside `KEPT_C5`, so it cannot remove a kept member; the
theorem about `KEPT_C5` is therefore the theorem about the final picker set.

## How to build

Stock Lean 4.10.0, no mathlib, no network fetch. With `elan`/`lake` on a v4.10.0 toolchain:

```
lake build
```

The build prints the `#print axioms` lines from `Check.lean`; each must read *does not
depend on any axioms*. To rebuild from a clean state, `rm -rf .lake/build` first.

A `leanprovercommunity/lean` Docker image pinned at Lean 4.10.0 works as-is; run `lake
build` from this directory inside the container.

## Honest scope

This proof certifies the **set algebra**: given three facts the code establishes, archiving
never orphans a kept session. It is **not** a proof of the Python end to end. Specifically:

- The three hypotheses (`hpick`, `demoted_guard`, `source_lb`) are transcribed from the
  Step 2 code; the proof certifies the logic *given* them. That the Python actually
  implements them (the JSONL parse, the union-find partition, `canonical()`, the
  fixpoint loop) is checked by the property-based fuzz, not by Lean.
- `pick P` abstracts the source the closure **realises** for a phantom `P` at fixpoint, not
  the literal richest source. The committed `locked_closure` adds its richest candidate only
  when no source of `P` is already locked (`if not (set(srcs) & locked)`), so a different,
  already-locked source can be the one kept. The proof depends only on the existential the
  code guarantees ("some source of a needed phantom stays locked"), via the `hpick` contract,
  and is generic over which source that is. It does not assume the richest source is locked.
- Marker exhaustiveness (the `[main]` / `[fork]` / `[scroll-dep]` / none taxonomy being
  exhaustive and exclusive) is **not** formalised here. It needs the union-find
  "needer and source co-tree" lemma, which would require modelling the tree partition;
  it was checked by fuzz only.

## Files

- `Orphan.lean` - definitions and the five theorems (the closure, the re-close, C5 safety, the main result).
- `Check.lean` - the axiom audit (`#print axioms`) and the concrete non-vacuity model.
- `lakefile.toml`, `lean-toolchain` - build configuration (Lean 4.10.0, no dependencies).
