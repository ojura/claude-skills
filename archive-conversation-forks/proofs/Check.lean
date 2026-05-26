import Orphan
import Markers
import Family
open Orphan

/- VERIFY-OUTCOME 1: no hidden axioms / sorry. The structural lemmas are fully constructive
   ("does not depend on any axioms"). The recall/marker theorems use `Classical.em`, so they
   list the trusted core (`Classical.choice`, `propext`, `Quot.sound`); that is NOT a `sorry`.
   The crucial check is that NONE lists `sorryAx`. -/
#print axioms no_orphan
#print axioms closure_closed_under_needs
#print axioms final_closed_under_needs
#print axioms source_survives_C5
#print axioms kept0_subset_final
#print axioms recall_no_loss
#print axioms live_subset_keptC5
#print axioms marker_no_hole
#print axioms nonseed_loadbearing
#print axioms Family.sameTree_refl
#print axioms Family.sameTree_symm
#print axioms Family.sameTree_trans
#print axioms Family.shared_lpu_sameTree
#print axioms Family.needer_source_coTree
#print axioms Family.noEdges_sameTree_eq
#print axioms Family.content_not_a_generator
#print axioms Family.canonical_mem
#print axioms Family.canonical_nondebris

/- VERIFY-OUTCOME 2: the theorem is NON-VACUOUS. Build a concrete store where:
   - the hypotheses (hpick, demoted_guard, source_lb) all hold,
   - there is a finally-kept file `f` that genuinely NEEDS a phantom with a source,
   so the conclusion is a real (not trivially-empty) existence claim. We then INSTANTIATE
   `no_orphan` and read off the witness, confirming it is the expected source. -/

-- Two files, one phantom. f0 needs P0; f1 sources P0. pick P0 = f1.
inductive Fil | f0 | f1
deriving DecidableEq
inductive Pha | p0

open Fil Pha

def cross : Fil → Fil → Prop := fun _ _ => False           -- no cross-file edges
def needs : Fil → Pha → Prop := fun f _ => f = f0          -- only f0 needs the phantom
def sources : Fil → Pha → Prop := fun f _ => f = f1        -- only f1 can source it
def pick : Pha → Fil := fun _ => f1                        -- closure picks f1
def KEPT0 : FSet Fil := fun f => f = f0                    -- f0 is pre-reclose kept (e.g. a kept-unique fork)
def loadbearing : FSet Fil := fun f => f = f1              -- f1 is load-bearing (it sources a needed phantom)
def demoted : FSet Fil := fun _ => False                   -- C5 demotes nothing here

-- hpick: pick returns a real source.
theorem hpick_ok : ∀ P, (∃ s, sources s P) → sources (pick P) P := by
  intro _ _; rfl

-- demoted_guard: nothing demoted, vacuously fine.
theorem demoted_guard_ok : ∀ k, demoted k → ¬ loadbearing k := by
  intro _ h; exact absurd h (by intro hh; cases hh)

-- source_lb: the only source is f1, and f1 = loadbearing. Holds without even needing the needer hyp.
theorem source_lb_ok :
    ∀ (s : Fil) (P : Pha), sources s P →
      (∃ g, KEPT_C5 cross needs sources pick KEPT0 demoted g ∧ needs g P) → loadbearing s := by
  intro s _ hs _
  -- hs : sources s P  i.e. s = f1; goal loadbearing s i.e. s = f1
  exact hs

-- f0 is in KEPT_C5: it is in KEPT0 (seed of the closure) and not demoted.
theorem f0_kept : KEPT_C5 cross needs sources pick KEPT0 demoted f0 := by
  refine ⟨Closure.seed ?_, ?_⟩
  · rfl                              -- KEPT0 f0
  · intro h; cases h                -- ¬ demoted f0

theorem f0_needs : needs f0 p0 := rfl
theorem src_exists : ∃ s, sources s p0 := ⟨f1, rfl⟩

-- Instantiate the main result on this concrete, non-vacuous store.
theorem concrete_no_orphan :
    ∃ s, KEPT_C5 cross needs sources pick KEPT0 demoted s ∧ sources s p0 :=
  no_orphan cross needs sources pick KEPT0 loadbearing demoted
    hpick_ok demoted_guard_ok source_lb_ok f0_kept f0_needs src_exists

-- And the witness is exactly f1 (the source), retained in KEPT_C5. Confirms non-triviality:
-- the existential is satisfied by a real, kept source, not by emptiness.
theorem witness_is_f1 : (Classical.choose concrete_no_orphan) = f1 := by
  -- the only file satisfying `sources · p0` is f1, so the chosen witness must be f1
  have h := Classical.choose_spec concrete_no_orphan
  -- h.2 : sources (choose) p0  i.e.  choose = f1
  exact h.2

#print axioms concrete_no_orphan

/- VERIFY-OUTCOME 3: the RECALL no-loss theorem is non-vacuous. A concrete store with one message
   carried by both the archive candidate `f0` and a kept file `f1`: the recall test passes
   (`missing` empty) and `recall_no_loss` yields real content preservation. -/
def msgFps : Fil → Pha → Prop := fun _ _ => True          -- both files carry the (single) message `p0`
def keptB  : FSet Fil := fun f => f = f1                   -- f1 is kept

-- Decidable kept-union membership (the faithful counterpart of the code's set `in`): always true
-- here since f1 is kept and carries every message. Provided so recall_no_loss stays axiom-free.
instance recallDec : ∀ m, Decidable (∃ b, keptB b ∧ msgFps b m) :=
  fun _ => isTrue ⟨f1, rfl, trivial⟩

theorem recall_missing_empty : ∀ m, ¬ missing msgFps keptB f0 m := by
  intro _ h
  -- missing = (carries m) ∧ ¬∃ kept b carrying m; but f1 is kept and carries m, contradiction.
  exact h.2 ⟨f1, rfl, trivial⟩

theorem concrete_recall_no_loss : preserved msgFps keptB f0 :=
  recall_no_loss msgFps keptB f0 recall_missing_empty

#print axioms concrete_recall_no_loss

/- VERIFY-OUTCOME 4: MARKER no-hole is non-vacuous. Enriched store where a kept-unique fork `g0`
   is non-canonical, non-live, survives C5, is NOT load-bearing, and HAS nonzero residue - so it
   lands in the `∃ residue` disjunct (a real `[main]`/`[fork]`/none member, not the deleted cell). -/
inductive Fil2 | g0 | c0
deriving DecidableEq

open Fil2

def canon2 : FSet Fil2 := fun f => f = c0                  -- c0 is a canonical
def live2  : FSet Fil2 := fun _ => False                   -- nothing live
def kuf2   : FSet Fil2 := fun f => f = g0                  -- g0 is a kept-unique fork
def demo2  : FSet Fil2 := fun _ => False                   -- nothing demoted
def lb2    : FSet Fil2 := fun _ => False                   -- nothing load-bearing
def cross2 : Fil2 → Fil2 → Prop := fun _ _ => False
def needs2 : Fil2 → Pha → Prop := fun _ _ => False         -- no phantom needs
def src2   : Fil2 → Pha → Prop := fun _ _ => False
def pick2  : Pha → Fil2 := fun _ => c0
def resid2 : Fil2 → Pha → Prop := fun f _ => f = g0        -- g0 has a globally-unique message

-- g0 is in KEPT_C5: in the seed (as a kuf) and not demoted.
theorem g0_keptC5 : KEPT_C5 cross2 needs2 src2 pick2 (seed0 canon2 live2 kuf2) demo2 g0 := by
  refine ⟨Closure.seed ?_, ?_⟩
  · exact Or.inr (Or.inr rfl)         -- g0 ∈ kuf ⊆ seed0
  · intro h; cases h                   -- ¬ demoted

theorem g0_not_canon : ¬ canon2 g0 := by intro h; cases h
theorem g0_not_live  : ¬ live2 g0  := by intro h; cases h

-- C5's postcondition: a surviving kuf is loadbearing OR has residue. Here g0 has residue.
theorem c5_survivor2 : ∀ x, kuf2 x → ¬ demo2 x → lb2 x ∨ ∃ m, resid2 x m := by
  intro x hk _
  -- hk : x = g0, so resid2 x p0 holds; take the right disjunct.
  exact Or.inr ⟨p0, hk⟩

-- no cross/phan edges in this store, so cross_lb/phan_lb hold vacuously.
theorem cross_lb2 : ∀ k b, Closure cross2 needs2 src2 pick2 (seed0 canon2 live2 kuf2) k →
    cross2 k b → lb2 b := by intro _ _ _ h; cases h
theorem phan_lb2 : ∀ f P, Closure cross2 needs2 src2 pick2 (seed0 canon2 live2 kuf2) f →
    needs2 f P → (∃ s, src2 s P) → lb2 (pick2 P) := by intro _ _ _ h; cases h

theorem concrete_marker_no_hole : lb2 g0 ∨ ∃ m, resid2 g0 m :=
  marker_no_hole cross2 needs2 src2 pick2 canon2 live2 kuf2 demo2 lb2 resid2
    cross_lb2 phan_lb2 c5_survivor2 g0_keptC5 g0_not_canon g0_not_live

#print axioms concrete_marker_no_hole

/- VERIFY-OUTCOME 5: the FAMILY co-tree lemma is non-vacuous. Two files both carrying phantom lpu
   `pL` in their lref are forced into the same tree - the real reason a needer and its source group. -/
inductive Fil3 | n0 | s0
deriving DecidableEq
inductive Lpu3 | pL
inductive Uuid3 | u0
open Fil3 Lpu3

def lref3 : Fil3 → Lpu3 → Prop := fun _ _ => True       -- both n0,s0 carry pL (the shared phantom)
def owns3 : Fil3 → Uuid3 → Prop := fun _ _ => False
def ref3  : Fil3 → Uuid3 → Prop := fun _ _ => False

theorem concrete_coTree : Family.SameTree lref3 owns3 ref3 n0 s0 :=
  Family.needer_source_coTree lref3 owns3 ref3 (P := pL) trivial trivial

#print axioms concrete_coTree

/- VERIFY-OUTCOME 6: canonical() membership + content floor are non-vacuous. A 3-element list with
   one non-debris member: the floored pick is in the list AND is non-debris. -/
def debris3 : Fil3 → Prop := fun f => f = s0           -- s0 is debris, n0 is not
instance : DecidablePred debris3 := fun f => by unfold debris3; infer_instance

-- cand picks the non-debris sublist [n0]; head? = some n0. Reduces definitionally (no native_decide,
-- which would add the `Lean.ofReduceBool` trust axiom - we keep the witness axiom-clean).
theorem canon3_eq : Family.canonicalPick debris3 [s0, n0, s0] = some n0 := by
  decide

theorem concrete_canonical_mem : n0 ∈ [s0, n0, s0] :=
  Family.canonical_mem debris3 [s0, n0, s0] canon3_eq

theorem concrete_canonical_nondebris : ¬ debris3 n0 :=
  Family.canonical_nondebris debris3 [s0, n0, s0] canon3_eq ⟨n0, by simp, by decide⟩

#print axioms concrete_canonical_mem
#print axioms concrete_canonical_nondebris
