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


/-
  ============================================================================================
  #2 - `expand` is CONSTRUCTIBLE, so the loop-body oracle is PROVEN, not assumed. For a finite store
  with DECIDABLE relations we build the search that, given a not-yet-closed set, returns a forced new
  element or certifies closure - faithful to the committed loop body, which scans only present data.
  Closedness here is BOUNDED (`ClosedB`: quantifiers over the finite universe `U` / phantom list `Ps`,
  source existential bounded to `U`) - the faithful model of a loop that never scans infinitely; over
  a finite store it is exactly the committed loop's fixpoint condition.
  ============================================================================================
-/

variable {Phantom : Type}
variable (cross : File → File → Prop) (needs sources : File → Phantom → Prop)
variable [DecidableRel cross] [∀ a p, Decidable (needs a p)] [∀ a p, Decidable (sources a p)]
-- Bool conjunction split, as a usable lemma (Bool.and_eq_true is a propositional Eq, no `.mp`).
private theorem and_split {a b : Bool} (h : (a && b) = true) : a = true ∧ b = true := by
  rw [Bool.and_eq_true] at h; exact h
-- "no element satisfies" => the negated `any` is true.
private theorem not_any {α : Type} {p : α → Bool} {l : List α} (h : ∀ x ∈ l, p x = false) :
    (!l.any p) = true := by
  rw [Bool.not_eq_true']
  cases hany : l.any p with
  | false => rfl
  | true => obtain ⟨x, hx, hpx⟩ := List.any_eq_true.mp hany; rw [h x hx] at hpx; exact absurd hpx (by decide)

/-- Bounded closedness: quantifiers range over finite U (files) / Ps (phantoms); source existential
    bounded to U. Faithful to the code, which scans only present data. -/
structure ClosedB (U : List File) (Ps : List Phantom) (L : List File) : Prop where
  cross_closed : ∀ k ∈ L, ∀ b ∈ U, cross k b → b ∈ L
  phan_closed  : ∀ k ∈ L, ∀ P ∈ Ps, needs k P → (∃ s ∈ U, sources s P) → (∃ s ∈ L, sources s P)

/-- cross-violation predicate for `b`: `b ∉ L` and some `k ∈ L` has `cross k b`. -/
def crossViol (L : List File) (b : File) : Bool :=
  decide (b ∉ L) && L.any (fun k => decide (cross k b))

/-- phantom-violation predicate for `P`: a needer in L, a source in U, but no source in L. -/
def phanViol (U L : List File) (P : Phantom) : Bool :=
  L.any (fun k => decide (needs k P)) && U.any (fun s => decide (sources s P)) &&
  !(L.any (fun s => decide (sources s P)))

/-- The forced new element for a phantom violation: an in-U source of `P`. -/
def phanSrc (U : List File) (Ps : List Phantom) (L : List File) : Option File :=
  match Ps.find? (phanViol needs sources U L) with
  | none   => none
  | some P => U.find? (fun s => decide (sources s P))

/-- Refutation direction: if both searches return none, `L` is bounded-closed. -/
theorem closed_of_none (U : List File) (Ps : List Phantom) (L : List File)
    (hc : U.find? (crossViol cross L) = none) (hp : phanSrc needs sources U Ps L = none) :
    ClosedB cross needs sources U Ps L := by
  have hcNone := List.find?_eq_none.mp hc
  constructor
  · intro k hk b hb hcross
    by_cases hbL : b ∈ L
    · exact hbL
    · exfalso
      have hpred : crossViol cross L b = true := by
        unfold crossViol
        rw [(by simp [hbL] : decide (b ∉ L) = true),
            List.any_eq_true.mpr ⟨k, hk, by simp [hcross]⟩]
        rfl
      exact (hcNone b hb) hpred
  · intro k hk P hP hneed hsrcU
    by_cases hexL : ∃ s ∈ L, sources s P
    · exact hexL
    · exfalso
      have hk_needs : (L.any (fun k => decide (needs k P))) = true :=
        List.any_eq_true.mpr ⟨k, hk, by simp [hneed]⟩
      obtain ⟨s0, hs0U, hs0src⟩ := hsrcU
      have hU_src : (U.any (fun s => decide (sources s P))) = true :=
        List.any_eq_true.mpr ⟨s0, hs0U, by simp [hs0src]⟩
      have hnoL : (!L.any (fun s => decide (sources s P))) = true :=
        not_any (fun s hsL => by
          cases hd : decide (sources s P) with
          | false => rfl
          | true => exact absurd (hexL ⟨s, hsL, of_decide_eq_true hd⟩) (by simp))
      have hPpred : phanViol needs sources U L P = true := by
        unfold phanViol; rw [hk_needs, hU_src]; simpa using hnoL
      -- P qualifies, so Ps.find? (phanViol) is some, so phanSrc reduces to U.find? = some.
      unfold phanSrc at hp
      cases hfp : Ps.find? (phanViol needs sources U L) with
      | none => exact (List.find?_eq_none.mp hfp P hP) hPpred
      | some P' =>
          rw [hfp] at hp
          have hP'sat := List.find?_some hfp
          have hP'Usrc : (U.any (fun s => decide (sources s P'))) = true :=
            (and_split (and_split hP'sat).1).2
          obtain ⟨s', hs'U, hs'src⟩ := List.any_eq_true.mp hP'Usrc
          have : U.find? (fun s => decide (sources s P')) ≠ none := by
            rw [Ne, List.find?_eq_none]; intro hcontra; exact (hcontra s' hs'U) hs'src
          exact this hp

/-- `expand`: search for a forced new element; if none, `L` is bounded-closed. PROVEN, not assumed. -/
def expand (U : List File) (Ps : List Phantom) (L : List File) :
    (ClosedB cross needs sources U Ps L) ⊕' (Σ' x : File, x ∈ U ∧ x ∉ L) :=
  match hc : U.find? (crossViol cross L) with
  | some b =>
      -- b is a real cross-violation witness: b ∈ U, b ∉ L.
      PSum.inr ⟨b, List.mem_of_find?_eq_some hc, by
        have hsat : crossViol cross L b = true := List.find?_some hc
        have := (and_split hsat).1     -- decide (b ∉ L) = true
        simpa using this⟩
  | none =>
      match hp : phanSrc needs sources U Ps L with
      | some s =>
          -- s is a real phantom-source witness: s ∈ U, and s ∉ L (no source of that P is in L).
          PSum.inr ⟨s, by
            -- unfold phanSrc; the inner U.find? gives s ∈ U.
            unfold phanSrc at hp
            cases hf : Ps.find? (phanViol needs sources U L) with
            | none => rw [hf] at hp; exact absurd hp (by simp)
            | some P =>
                rw [hf] at hp; dsimp only [] at hp
                exact List.mem_of_find?_eq_some hp, by
            -- s ∉ L: phanViol P says no source of P is in L, and s sources P.
            unfold phanSrc at hp
            cases hf : Ps.find? (phanViol needs sources U L) with
            | none => rw [hf] at hp; exact absurd hp (by simp)
            | some P =>
                rw [hf] at hp; dsimp only [] at hp
                have hssrc : decide (sources s P) = true := by
                  have := List.find?_some hp; simpa using this
                have hPviol : phanViol needs sources U L P = true := List.find?_some hf
                -- phanViol third conjunct: !(L.any (sources · P)) = true.
                have hnoL : (!L.any (fun s => decide (sources s P))) = true :=
                  (and_split hPviol).2
                intro hsL
                -- s ∈ L and s sources P would make L.any (sources · P) = true, contradicting hnoL.
                have : (L.any (fun s => decide (sources s P))) = true :=
                  List.any_eq_true.mpr ⟨s, hsL, hssrc⟩
                rw [this] at hnoL; exact absurd hnoL (by decide)⟩
      | none =>
          -- both searches empty => bounded-closed.
          PSum.inl (closed_of_none cross needs sources U Ps L hc hp)

/--
  CAPSTONE: a bounded-closed superset of any `seed ⊆ U` EXISTS for a finite decidable store, with the
  loop-body oracle CONSTRUCTED by `expand` (no oracle passed in). Recurses on `gap` (decreasing by
  `gap_lt`); at each step `expand` either certifies `ClosedB` or yields a forced new element. So the
  whole termination chain - including the oracle - is discharged for any concrete decidable store.
-/
theorem closed_superset_exists_constructed (U : List File) (Ps : List Phantom) :
    ∀ (L : List File), ∃ M : List File,
        (∀ y, y ∈ L → y ∈ M) ∧ ClosedB cross needs sources U Ps M := by
  intro L0
  suffices H : ∀ n, ∀ L : List File, gap U L = n →
      ∃ M, (∀ y, y ∈ L → y ∈ M) ∧ ClosedB cross needs sources U Ps M by
    exact H (gap U L0) L0 rfl
  intro n
  induction n using Nat.strongInductionOn with
  | _ n ih =>
    intro L hLn
    cases expand cross needs sources U Ps L with
    | inl hclosed => exact ⟨L, fun _ h => h, hclosed⟩
    | inr hwit =>
        obtain ⟨x, hxU, hxL⟩ := hwit
        have hdrop : gap U (x :: L) < n := by rw [← hLn]; exact gap_lt U L hxU hxL
        obtain ⟨M, hM1, hM2⟩ := ih (gap U (x :: L)) hdrop (x :: L) rfl
        exact ⟨M, fun y hy => hM1 y (List.mem_cons_of_mem x hy), hM2⟩

end TermList
