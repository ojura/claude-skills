import Fixpoint

/-
  Closes the last gap WITHOUT mathlib: `Fixpoint.IsClosed locked` is no longer only an assumption.
  Over a FINITE universe (a `List File`) a closed `locked ⊇ seed` PROVABLY EXISTS - the committed
  `while changed:` loop terminates because each changing iteration adds a universe element to
  `locked`, a monotone fixpoint over a finite set.

  Core Lean 4 only (no mathlib). Core 4.10's `List` lacks `Sublist` and `countP`, so the decreasing
  measure and its strict-drop lemma are hand-rolled here from `List.filter` / `List.length` by plain
  structural induction. Two small lemmas (`gap_le_of_imp`, `gap_lt`) plus well-founded recursion on
  the gap; nothing deep.
-/

namespace TermList

variable {File : Type} [DecidableEq File]

/-- gap U L = number of universe elements not yet in `L` (the loop's decreasing measure). -/
def gap (U L : List File) : Nat := (U.filter (fun u => decide (u ∉ L))).length

/-- Monotonicity of filtered length under a stronger keep-predicate, by induction (no `Sublist`). -/
theorem length_filter_le_of_imp (p q : File → Bool) (himp : ∀ a, p a = true → q a = true) :
    ∀ (l : List File), (l.filter p).length ≤ (l.filter q).length := by
  intro l
  induction l with
  | nil => simp
  | cons a as ih =>
      simp only [List.filter_cons]
      by_cases hp : p a = true
      · -- a kept by p ⇒ kept by q (himp). Both prepend a.
        rw [if_pos hp, if_pos (himp a hp)]
        simp only [List.length_cons]; omega
      · -- a dropped by p; q may or may not keep it - either way RHS ≥ LHS.
        rw [if_neg hp]
        by_cases hq : q a = true
        · rw [if_pos hq]; simp only [List.length_cons]; omega
        · rw [if_neg hq]; exact ih

/-- A `decide (· ∉ x::L)` keep implies a `decide (· ∉ L)` keep (the stronger predicate). -/
theorem notin_cons_imp (x : File) :
    ∀ u, decide (u ∉ x :: L) = true → decide (u ∉ L) = true := by
  intro u h
  simp only [decide_eq_true_eq] at *
  intro hu; exact h (List.mem_cons_of_mem _ hu)

/-- KEY STEP: adding `x ∈ U`, `x ∉ L` to `L` strictly drops the gap.
   Each membership condition is settled to a concrete `decide ... = true/false`, then rewritten into
   the `if` from `List.filter_cons`; `omega` closes the length arithmetic against the monotonicity
   bound. Deliberately surgical (no broad simp) so normalisation stays aligned for `omega`. -/
theorem gap_lt (U L : List File) {x : File} (hxU : x ∈ U) (hxL : x ∉ L) :
    gap U (x :: L) < gap U L := by
  unfold gap
  induction U with
  | nil => exact absurd hxU (List.not_mem_nil x)
  | cons a as ih =>
      simp only [List.filter_cons]
      by_cases hax : a = x
      · -- a = x: dropped by (∉ x::L) (a ∈ x::L), kept by (∉ L) (a ∉ L). Strict drop here.
        subst hax
        have h1 : decide (a ∉ L) = true := decide_eq_true hxL
        have h2 : decide (a ∉ a :: L) = false :=
          decide_eq_false (not_not_intro (List.mem_cons_self a L))
        rw [h1, h2, if_pos rfl, if_neg (by simp)]
        have hmono := length_filter_le_of_imp
          (fun u => decide (u ∉ a :: L)) (fun u => decide (u ∉ L)) (notin_cons_imp a) as
        simp only [List.length_cons]; omega
      · -- a ≠ x: x ∈ as; recurse. a is kept/dropped identically by both predicates.
        have hxas : x ∈ as := by
          cases List.mem_cons.mp hxU with
          | inl h => exact absurd h (fun he => hax he.symm)
          | inr h => exact h
        by_cases haL : a ∈ L
        · -- a ∈ L ⇒ dropped by both.
          have h1 : decide (a ∉ L) = false := decide_eq_false (not_not_intro haL)
          have h2 : decide (a ∉ x :: L) = false :=
            decide_eq_false (not_not_intro (List.mem_cons_of_mem x haL))
          rw [h1, h2, if_neg (by simp), if_neg (by simp)]; exact ih hxas
        · -- a ∉ L and a ≠ x ⇒ a ∉ x::L; kept by both.
          have h1 : decide (a ∉ L) = true := decide_eq_true haL
          have h2 : decide (a ∉ x :: L) = true := decide_eq_true (by
            simp only [List.mem_cons, not_or]; exact ⟨hax, haL⟩)
          rw [h1, h2, if_pos rfl, if_pos rfl]; simp only [List.length_cons]
          have := ih hxas; omega

/--
  EXISTENCE of a closed superset, by well-founded recursion on `gap U L`. `expand` abstracts the
  loop body: given a not-yet-closed `L`, it yields a forced new element `x ∈ U`, `x ∉ L`; when none
  exists, `L` is closed (`IsClosed` over the predicate `(· ∈ L)`). Iterating drops `gap` each step,
  so a closed superset of any `seed ⊆ U` is reached. This proves `IsClosed` is ACHIEVED, not assumed.

  `IsClosedList U L` mirrors `FixProto.IsClosed` for the membership predicate `fun y => y ∈ L`,
  restricted to the universe `U`.
-/
def memPred (L : List File) : File → Prop := fun y => y ∈ L

theorem closed_superset_exists {Phantom : Type}
    (cross : File → File → Prop) (needs sources : File → Phantom → Prop) (U : List File)
    (expand : ∀ L : List File,
        (FixProto.IsClosed cross needs sources (memPred L)) ⊕' (Σ' x : File, x ∈ U ∧ x ∉ L)) :
    ∀ (L : List File), ∃ M : List File,
        (∀ y, y ∈ L → y ∈ M) ∧ FixProto.IsClosed cross needs sources (memPred M) := by
  intro L0
  -- strong recursion on gap U L
  suffices H : ∀ n, ∀ L : List File, gap U L = n →
      ∃ M, (∀ y, y ∈ L → y ∈ M) ∧ FixProto.IsClosed cross needs sources (memPred M) by
    exact H (gap U L0) L0 rfl
  intro n
  induction n using Nat.strongInductionOn with
  | _ n ih =>
    intro L hLn
    cases expand L with
    | inl hclosed => exact ⟨L, fun _ h => h, hclosed⟩
    | inr hwit =>
        obtain ⟨x, hxU, hxL⟩ := hwit
        have hdrop : gap U (x :: L) < n := by rw [← hLn]; exact gap_lt U L hxU hxL
        obtain ⟨M, hM1, hM2⟩ := ih (gap U (x :: L)) hdrop (x :: L) rfl
        exact ⟨M, fun y hy => hM1 y (List.mem_cons_of_mem x hy), hM2⟩

end TermList
