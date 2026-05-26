/-
  No-orphan invariant for the archive-conversation-forks Step-2 set algebra (SKILL.md).

  Models the committed keep-locked closure + C4 re-close + C5 demotion + C6 deferred archive,
  and proves that archiving never orphans a kept session's phantom-backfill origin.

  Pure Lean 4 core (no mathlib): sets are predicates `File → Prop`; the keep-locked
  closure is an INDUCTIVE predicate, so "closed under the closure rules" is definitional
  and the least-fixpoint/induction principle is free from the recursor.

  MAIN RESULT (no orphan): for every store and every operator `judge` policy, every needed phantom of a
  finally-kept file that has ANY source in the store retains a source in the finally-kept set.
-/

namespace Orphan

/-- A "set of files" is just a predicate (mathlib-free). -/
abbrev FSet (File : Type) := File → Prop

/--
  The keep-locked closure of a seed set `S`, as a least fixpoint via an inductive predicate.

  Parameters abstract the two structural edge types and the source-choice:
  * `cross k b`     : `k` references (via lpu) a uuid that `b` owns  (cross-file ancestor edge).
  * `needs f P`     : file `f` has a root phantom boundary for phantom `P` (relies on a sibling).
  * `sources s P`   : file `s` can backfill phantom `P` (has pre-content before that boundary).
  * `pick P`        : the source the closure REALISES for phantom `P`. NOT necessarily the literal
                      `max(srcs, key=len(set(fps[s])))` richest source: the committed `locked_closure`
                      adds `best=richest` only via `if not (set(srcs)&locked)`, i.e. only when no source
                      of `P` is already locked (e.g. via a cross-file edge). So the realised kept source
                      can be a DIFFERENT source than the richest. `pick P` abstracts the witness the closure instantiates:
                      the source `locked_closure` leaves in `locked` for `P` at fixpoint. Its existence
                      (a source of `P` is in `locked` whenever `P` is a needed phantom of a locked file
                      with >=1 source) is the closure's fixpoint guarantee, verified by fuzz over the
                      committed code; `hpick` below is exactly its defining contract.

  The three constructors are exactly `locked_closure`:
  (seed)  every seed member is in;
  (cross) if `k` is in and `cross k b`, then `b` is in;
  (phan)  if `f` is in, `f needs P`, and `P` has a source, then the realised source `pick P` is in.
          (This is sound precisely because, at fixpoint, the committed code keeps SOME source of `P` in
          `locked`; we name that realised source `pick P`. The model is faithful to the existential the
          code guarantees, NOT to the over-specific claim "the richest source is locked".)
-/
inductive Closure {File Phantom : Type}
    (cross : File → File → Prop) (needs sources : File → Phantom → Prop)
    (pick : Phantom → File) (S : FSet File) : File → Prop where
  | seed  {x : File}   : S x → Closure cross needs sources pick S x
  | cross {k b : File} : Closure cross needs sources pick S k → cross k b →
                          Closure cross needs sources pick S b
  | phan  {f : File} {P : Phantom} :
      Closure cross needs sources pick S f → needs f P → (∃ s, sources s P) →
      Closure cross needs sources pick S (pick P)

variable {File Phantom : Type}
variable (cross : File → File → Prop) (needs sources : File → Phantom → Prop)
variable (pick : Phantom → File)

/--
  STEP 1 - closure property. The closure of `S` is CLOSED under needs→source, given the
  choice-function contract `hpick` ("the realised source `pick P` IS a source of `P` when one exists").
  `hpick` is the abstract form of the committed code's fixpoint guarantee: whenever `P` is a needed
  phantom (of a locked file) with at least one source in the store, `locked_closure` leaves some source
  of `P` in `locked`; `pick P` names that realised source. The proof quantifies over EVERY `pick`
  satisfying `hpick` (the proof never uses "richest"), so it covers the committed code's realised choice
  regardless of which source the `if not (set(srcs)&locked)` branch happens to keep.
  This is the only place the closure's `phan` constructor is used; everything downstream is set bookkeeping.
-/
theorem closure_closed_under_needs
    (S : FSet File)
    (hpick : ∀ P, (∃ s, sources s P) → sources (pick P) P)
    {f : File} {P : Phantom}
    (hf : Closure cross needs sources pick S f)
    (hneed : needs f P)
    (hsrc : ∃ s, sources s P) :
    ∃ s, Closure cross needs sources pick S s ∧ sources s P :=
  ⟨pick P, Closure.phan hf hneed hsrc, hpick P hsrc⟩

/-
  The patched pipeline produces a chain of kept sets:
    KEPT0           -- canonicals ∪ preliminary-locked ∪ kept_unique_forks  (pre re-close)
    KEPT_final  x   := Closure cross needs sources pick KEPT0 x              -- C4 `locked=closure(KEPT); KEPT|=locked`
    KEPT_C5     x   := KEPT_final x ∧ ¬ demoted x                            -- C5 drops non-loadbearing 0-residue forks
    KEPT_done   x   := KEPT_C5 x                                            -- C6 archives only files OUTSIDE KEPT_C5

  We carry `loadbearing` and `demoted` as predicates with the two facts the patched code guarantees:
    (G1) demoted_guard : demoted k → ¬ loadbearing k        -- C5's `if k in loadbearing: continue`
    (G2) source_loadbearing : a file that sources a NEEDED phantom is loadbearing
                              (loadbearing |= {s | sources s & needed}); here a phantom is
                              "needed" because some KEPT_C5 (hence consumer) file needs it.
-/

/-- KEPT after C4 re-close = the closure over the pre-reclose kept set `KEPT0`. -/
def KEPT_final (KEPT0 : FSet File) : FSet File :=
  fun x => Closure cross needs sources pick KEPT0 x

/-- KEPT after C5 demotion. -/
def KEPT_C5 (KEPT0 : FSet File) (demoted : FSet File) : FSet File :=
  fun x => KEPT_final cross needs sources pick KEPT0 x ∧ ¬ demoted x

/--
  STEP 2 - the re-close only GROWS the kept set: every pre-reclose member is finally kept.
  (Used implicitly; recorded for completeness.)
-/
theorem kept0_subset_final (KEPT0 : FSet File) {x : File} (hx : KEPT0 x) :
    KEPT_final cross needs sources pick KEPT0 x :=
  Closure.seed hx

/--
  STEP 2' - KEPT_final is closed under needs→source (specialize step 1 to `S := KEPT0`).
  For any finally-kept `f` needing `P` with a source somewhere, `pick P` is finally kept and sources `P`.
-/
theorem final_closed_under_needs
    (KEPT0 : FSet File)
    (hpick : ∀ P, (∃ s, sources s P) → sources (pick P) P)
    {f : File} {P : Phantom}
    (hf : KEPT_final cross needs sources pick KEPT0 f)
    (hneed : needs f P)
    (hsrc : ∃ s, sources s P) :
    KEPT_final cross needs sources pick KEPT0 (pick P) ∧ sources (pick P) P :=
  ⟨Closure.phan hf hneed hsrc, hpick P hsrc⟩

/--
  STEP 3 - C5 safety (Q3): a file that sources a phantom NEEDED by a surviving kept file is
  never demoted by C5. This is the load-bearing guard made explicit: such a source is
  load-bearing (`source_lb`), and C5 demotes only non-load-bearing files (`demoted_guard`),
  so the source survives. The main result reuses this step.
-/
theorem source_survives_C5
    (KEPT0 : FSet File) (loadbearing demoted : FSet File)
    (demoted_guard : ∀ k, demoted k → ¬ loadbearing k)
    (source_lb : ∀ (s : File) (P : Phantom),
        sources s P →
        (∃ g, KEPT_C5 cross needs sources pick KEPT0 demoted g ∧ needs g P) →
        loadbearing s)
    {s : File} {P : Phantom}
    (hs : sources s P)
    (hneeded : ∃ g, KEPT_C5 cross needs sources pick KEPT0 demoted g ∧ needs g P) :
    ¬ demoted s :=
  fun hd => (demoted_guard s hd) (source_lb s P hs hneeded)

/--
  MAIN THEOREM (no orphan), over the FINAL kept set after C4 re-close + C5 demotion + C6 archive.

  Hypotheses (each a fact the committed code establishes; see SKILL.md Step 2):
  * `hpick`            : the realised source `pick P` is an actual source of `P` when one exists. This is
                         the committed `locked_closure` fixpoint guarantee ("some source of a needed
                         phantom stays in `locked`"), NOT the over-specific "richest source is locked"
                         (the `if not (set(srcs)&locked)` branch can keep a different source). The proof
                         is generic over any `pick` meeting this contract.
  * `demoted_guard`    : C5 demotes only non-loadbearing files (`if k in loadbearing: continue`).
  * `source_lb`        : any file that sources phantom `P`, when `P` is needed by a kept-and-surviving
                         file, is load-bearing (`loadbearing |= {s | sources s & needed}`, with
                         `needed = ⋃ needs(consumers)` and `consumers = KEPT | live ⊇ KEPT_C5`).

  Conclusion: every needed phantom `P` of a surviving kept file `f`, if `P` has ANY source in the
  store, retains a source `s` that is ALSO in the surviving kept set `KEPT_C5`.

  NOTE on C6: C6 archives only members of `tree_archive_candidates - KEPT_C5`, i.e. files NOT in
  `KEPT_C5`. It therefore cannot remove any member of `KEPT_C5`, so `KEPT_done = KEPT_C5` for the
  purposes of membership, and the theorem about `KEPT_C5` IS the theorem about the final picker set.
-/
theorem no_orphan
    (KEPT0 : FSet File) (loadbearing demoted : FSet File)
    (hpick : ∀ P, (∃ s, sources s P) → sources (pick P) P)
    (demoted_guard : ∀ k, demoted k → ¬ loadbearing k)
    (source_lb : ∀ (s : File) (P : Phantom),
        sources s P →
        (∃ g, KEPT_C5 cross needs sources pick KEPT0 demoted g ∧ needs g P) →
        loadbearing s)
    {f : File} {P : Phantom}
    (hf : KEPT_C5 cross needs sources pick KEPT0 demoted f)
    (hneed : needs f P)
    (hsrc : ∃ s, sources s P) :
    ∃ s, KEPT_C5 cross needs sources pick KEPT0 demoted s ∧ sources s P := by
  -- The closure supplies `pick P`: finally kept and an actual source.
  have hfin : KEPT_final cross needs sources pick KEPT0 f := hf.1
  obtain ⟨hpick_final, hpick_src⟩ :=
    final_closed_under_needs cross needs sources pick KEPT0 hpick hfin hneed hsrc
  -- `P` is needed by `f`, which survives C5; by STEP 3 the source `pick P` survives C5.
  have hneeded : ∃ g, KEPT_C5 cross needs sources pick KEPT0 demoted g ∧ needs g P := ⟨f, hf, hneed⟩
  have hnotdem : ¬ demoted (pick P) :=
    source_survives_C5 cross needs sources pick KEPT0 loadbearing demoted
      demoted_guard source_lb hpick_src hneeded
  exact ⟨pick P, ⟨hpick_final, hnotdem⟩, hpick_src⟩

/-
  ============================================================================================
  RECALL-PASS NO-LOSS (the second archive path's safety property).

  `no_orphan` above guarantees the STRUCTURAL safety of archiving: a kept session's needed
  phantom always keeps a source. The recall pass archives on a DIFFERENT criterion - exact
  content redundancy - so it needs its own safety theorem: archiving a 0-unique candidate
  loses no message. Messages are opaque here (`fps f m` = "file `f` carries message-fingerprint
  `m`"); this is a separate, content-level layer from the structural closure above.
  ============================================================================================
-/

/-- "Every message of `A` lives in some kept file" - `A`'s content is fully preserved by `KEPT`. -/
def preserved {Msg : Type} (fps : File → Msg → Prop) (KEPT : FSet File) (A : File) : Prop :=
  ∀ m, fps A m → ∃ b, KEPT b ∧ fps b m

/--
  The recall pass's `missing` set: messages of `A` not found in any kept file. The committed code
  computes `missing = set(fps[A]) - kept_union` and archives `A` iff `missing` is empty.
-/
def missing {Msg : Type} (fps : File → Msg → Prop) (KEPT : FSet File) (A : File) : Msg → Prop :=
  fun m => fps A m ∧ ¬ ∃ b, KEPT b ∧ fps b m

/--
  RECALL NO-LOSS. If the recall test passes (`missing A` is empty: every message of `A` is in some
  kept file), then archiving `A` preserves all of `A`'s content. The hypothesis `hempty` is exactly
  the committed code's `if not missing` test stated set-theoretically, so this certifies that the
  0-unique archive decision loses nothing - the content-level counterpart of `no_orphan`'s structural
  guarantee. `A`'s own membership is irrelevant: the witness `b` may be any kept file (the code's
  "split-contained across several kept files" case is covered, since `b` is existentially quantified
  per message, not a single container).

  FULLY CONSTRUCTIVE. "m lives in some kept file" is `Decidable` (the committed code computes it as a
  Python `in` against the kept-union set), so the case-split is `Decidable`, NOT classical: this
  theorem depends on NO axioms. The decidability instance is the faithful counterpart of the set
  membership the code actually evaluates.
-/
theorem recall_no_loss {Msg : Type} (fps : File → Msg → Prop) (KEPT : FSet File) (A : File)
    [∀ m, Decidable (∃ b, KEPT b ∧ fps b m)]
    (hempty : ∀ m, ¬ missing fps KEPT A m) :
    preserved fps KEPT A := by
  intro m hm
  -- Decidable (not classical) case-split on the kept-union membership the code computes.
  cases (inferInstance : Decidable (∃ b, KEPT b ∧ fps b m)) with
  | isTrue h  => exact h
  | isFalse h => exact absurd ⟨hm, h⟩ (hempty m)

end Orphan
