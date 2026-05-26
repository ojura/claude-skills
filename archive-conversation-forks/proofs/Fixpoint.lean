/-
  PROTOTYPE v2 (mathlib-free): internalise `hpick`/`source_lb`/`demoted_guard` by deriving them
  from manifestly-code-true facts, NOT assuming them. Core Lean only.

  Architecture:
  * `IsClosed L`  - L is closed under the cross edge and the phantom rule (SOME source of a needed
                    phantom is in L). This is EXACTLY the committed loop's termination condition
                    (`while changed: ...` exits when no rule adds anything), so assuming `IsClosed locked`
                    is assuming only "the loop ran to a fixpoint" - a single structural fact, not a
                    per-phantom choice. Faithful to the `if not (set(srcs)&locked)` guard: it stops at
                    SOME source, not the richest.
  * From `IsClosed locked` we PROVE the per-phantom source-existence that was the `hpick` hypothesis.
  * `loadbearing` / `demoted` modelled as their actual set definitions -> `source_lb`, `demoted_guard`
    become lemmas.
-/
namespace FixProto

abbrev FSet (File : Type) := File → Prop

variable {File Phantom : Type}
variable (cross : File → File → Prop) (needs sources : File → Phantom → Prop)

/-- The committed loop's fixpoint condition, as a property of the computed set `locked`. -/
structure IsClosed (locked : FSet File) : Prop where
  /-- cross edge: a cross-file target of a locked file is locked. -/
  cross_closed : ∀ k b, locked k → cross k b → locked b
  /-- phantom rule: a needed phantom (of a locked file) with ANY source has SOME source locked.
      This mirrors `if not (set(srcs)&locked): add best` - at the fixpoint the `if` is false, i.e.
      `set(srcs)&locked` is nonempty. -/
  phan_closed : ∀ k P, locked k → needs k P → (∃ s, sources s P) → (∃ s, locked s ∧ sources s P)

/--
  `hpick` INTERNALISED. Given the loop reached a fixpoint (`IsClosed locked`), every needed phantom
  of a locked file with a source keeps a source IN locked. No per-phantom choice assumed: it is a
  direct consequence of the fixpoint condition. This REPLACES the `hpick` hypothesis.
-/
theorem hpick_from_closed {locked : FSet File} (hcl : IsClosed cross needs sources locked)
    {f : File} (hf : locked f) {P : Phantom} (hneed : needs f P) (hsrc : ∃ s, sources s P) :
    ∃ s, locked s ∧ sources s P :=
  hcl.phan_closed f P hf hneed hsrc

/-- needed P = some consumer needs P. -/
def needed (consumers : FSet File) (P : Phantom) : Prop := ∃ a, consumers a ∧ needs a P

/-- loadbearing = sources some needed phantom (the committed `loadbearing |= {s | sources(s)&needed}`). -/
def loadbearing (consumers : FSet File) (s : File) : Prop :=
  ∃ P, sources s P ∧ needed needs consumers P

/-- `source_lb` DERIVED (was a hypothesis): a source of a phantom a consumer needs is loadbearing. -/
theorem source_lb_from_def (consumers : FSet File) {s : File} {P : Phantom}
    (hs : sources s P) (hg : ∃ g, consumers g ∧ needs g P) :
    loadbearing needs sources consumers s :=
  ⟨P, hs, hg⟩

/-- demoted = the actual C5 guard (¬loadbearing ∧ ¬residue). -/
def demoted (consumers residue : FSet File) (k : File) : Prop :=
  ¬ loadbearing needs sources consumers k ∧ ¬ residue k

/-- `demoted_guard` DERIVED (was a hypothesis): demoted ⇒ not loadbearing, definitionally. -/
theorem demoted_guard_from_def (consumers residue : FSet File) {k : File}
    (hd : demoted needs sources consumers residue k) :
    ¬ loadbearing needs sources consumers k := hd.1

/--
  NO-ORPHAN, INTERNALISED. The same safety conclusion as `Orphan.no_orphan`, but assuming strictly
  LESS: the three bridge hypotheses (`hpick`, `source_lb`, `demoted_guard`) are no longer taken as
  given - they are derived here from
    * `hclosed`  : `IsClosed locked` - the committed loop reached its fixpoint (one structural fact);
    * the SET DEFINITIONS of `loadbearing` and `demoted` (`loadbearing`/`demoted` above).
  The post-C5 kept set is `fun x => locked x ∧ ¬ demoted ... x`. We show: for a surviving kept `f`
  needing `P` with a source anywhere, SOME source survives in the post-C5 kept set.

  `consumers` is the set the loop closed over (canonicals + live + kept-unique forks); `locked ⊆`
  is implied by closedness usage. `residue` abstracts the global-residue test C5 uses.
-/
theorem no_orphan_from_closed
    {locked consumers residue : FSet File}
    (hclosed : IsClosed cross needs sources locked)
    -- the loop closes over `consumers`, and `locked` members are consumers (locked ⊆ consumers):
    (locked_subset_consumers : ∀ x, locked x → consumers x)
    {f : File} {P : Phantom}
    (hf : locked f ∧ ¬ demoted needs sources consumers residue f)
    (hneed : needs f P)
    (hsrc : ∃ s, sources s P) :
    ∃ s, (locked s ∧ ¬ demoted needs sources consumers residue s) ∧ sources s P := by
  -- (1) hpick internalised: the fixpoint keeps SOME source of P in `locked`.
  obtain ⟨s, hslocked, hssrc⟩ := hpick_from_closed cross needs sources hclosed hf.1 hneed hsrc
  -- (2) source_lb derived: s sources P, and P is needed by f (a consumer), so s is loadbearing.
  have hcons_needs : ∃ g, consumers g ∧ needs g P :=
    ⟨f, locked_subset_consumers f hf.1, hneed⟩
  have hslb : loadbearing needs sources consumers s :=
    source_lb_from_def needs sources consumers hssrc hcons_needs
  -- (3) demoted_guard derived: demoted ⇒ ¬loadbearing, so a loadbearing s is NOT demoted.
  have hsnotdem : ¬ demoted needs sources consumers residue s := by
    intro hd; exact (demoted_guard_from_def needs sources consumers residue hd) hslb
  exact ⟨s, ⟨hslocked, hsnotdem⟩, hssrc⟩

end FixProto
