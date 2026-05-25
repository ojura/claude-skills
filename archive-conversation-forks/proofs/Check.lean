import Orphan
open Orphan

/- VERIFY-OUTCOME 1: no hidden axioms / sorry. Should print only the standard trusted core
   (propext, Classical.choice, Quot.sound) or fewer - and crucially NOT `sorryAx`. -/
#print axioms no_orphan
#print axioms closure_closed_under_needs
#print axioms final_closed_under_needs
#print axioms source_survives_C5
#print axioms kept0_subset_final

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
