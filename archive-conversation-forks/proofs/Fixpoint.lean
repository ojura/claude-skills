/-
  Internalise the bridge facts (mathlib-free, core Lean): `hpick`, `source_lb`, `cross_lb`,
  `demoted_guard`, `live_not_demoted`, `C5_survivor` are DERIVED here from manifestly-code-true
  inputs (the `IsClosed` fixpoint condition and the actual set definitions of `loadbearing` /
  `demoted`), NOT assumed.

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

/--
  `loadbearing` modelled as its ACTUAL committed definition: the UNION of the two halves the code
  builds (SKILL.md `loadbearing = {cross-file targets of consumers} ∪ {s | sources(s) & needed}`):
  * cross-target half: some consumer `a` has `cross a b` (`a` references a uuid `b` owns), OR
  * phantom-source half: `b` sources some needed phantom.
  Both `cross_lb` and `phan_lb` (previously hypotheses) now fall out of this definition.
-/
def loadbearing (cross : File → File → Prop) (consumers : FSet File) (b : File) : Prop :=
  (∃ a, consumers a ∧ cross a b) ∨ (∃ P, sources b P ∧ needed needs consumers P)

/-- `source_lb` / `phan_lb` DERIVED: a source of a phantom a consumer needs is loadbearing. -/
theorem source_lb_from_def (cross : File → File → Prop) (consumers : FSet File) {s : File} {P : Phantom}
    (hs : sources s P) (hg : ∃ g, consumers g ∧ needs g P) :
    loadbearing needs sources cross consumers s :=
  Or.inr ⟨P, hs, hg⟩

/-- `cross_lb` DERIVED (was a hypothesis): a cross-file target of a consumer is loadbearing. -/
theorem cross_lb_from_def (cross : File → File → Prop) (consumers : FSet File) {a b : File}
    (ha : consumers a) (hcross : cross a b) :
    loadbearing needs sources cross consumers b :=
  Or.inl ⟨a, ha, hcross⟩

/--
  `demoted` = the ACTUAL C5 guard. C5 demotes `k` iff it is a kept-unique fork (`kuf`), is NOT
  load-bearing, and has zero residue (`SKILL.md`: `for k in kept_unique_forks: if k in loadbearing:
  continue; if not residue: discard`). Modelling all three conjuncts makes the marker side-conditions
  derivable: `live_not_demoted` from `kuf`-membership, `C5_survivor` by unpacking the negation.
-/
def demoted (cross : File → File → Prop) (kuf consumers residue : FSet File) (k : File) : Prop :=
  kuf k ∧ ¬ loadbearing needs sources cross consumers k ∧ ¬ residue k

/-- `demoted_guard` DERIVED (was a hypothesis): demoted ⇒ not loadbearing, definitionally. -/
theorem demoted_guard_from_def (cross : File → File → Prop) (kuf consumers residue : FSet File)
    {k : File} (hd : demoted needs sources cross kuf consumers residue k) :
    ¬ loadbearing needs sources cross consumers k := hd.2.1

/-- `live_not_demoted` DERIVED (was a hypothesis): C5 demotes only kept-unique forks, and `live` is
   disjoint from `kuf` (live comes from the registry, kuf from the per-tree fork judgment), so a live
   session is never demoted. -/
theorem live_not_demoted_from_def (cross : File → File → Prop) (kuf consumers residue live : FSet File)
    (hdisj : ∀ x, live x → ¬ kuf x) {x : File} (hlive : live x) :
    ¬ demoted needs sources cross kuf consumers residue x :=
  fun hd => (hdisj x hlive) hd.1

/-- `C5_survivor` DERIVED (was a hypothesis): a kept-unique fork that survives C5 (`¬ demoted`) is
   load-bearing OR has nonzero residue - C5's postcondition, by unpacking `¬(kuf ∧ ¬lb ∧ ¬resid)`
   under the known `kuf`. Constructive: `loadbearing` and `residue` are `Decidable` (both are finite
   set checks in the committed code), so the case-split is decidable, not classical. -/
theorem C5_survivor_from_def (cross : File → File → Prop) (kuf consumers residue : FSet File)
    {x : File} [Decidable (loadbearing needs sources cross consumers x)] [Decidable (residue x)]
    (hk : kuf x) (hnd : ¬ demoted needs sources cross kuf consumers residue x) :
    loadbearing needs sources cross consumers x ∨ residue x := by
  -- hnd : ¬(kuf x ∧ ¬lb ∧ ¬resid). With kuf x, this forces ¬(¬lb ∧ ¬resid) = lb ∨ resid.
  cases (inferInstance : Decidable (loadbearing needs sources cross consumers x)) with
  | isTrue hlb => exact Or.inl hlb
  | isFalse hlb =>
      cases (inferInstance : Decidable (residue x)) with
      | isTrue hr  => exact Or.inr hr
      | isFalse hr => exact absurd ⟨hk, hlb, hr⟩ hnd

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
    {locked kuf consumers residue : FSet File}
    (hclosed : IsClosed cross needs sources locked)
    -- the loop closes over `consumers`, and `locked` members are consumers (locked ⊆ consumers):
    (locked_subset_consumers : ∀ x, locked x → consumers x)
    {f : File} {P : Phantom}
    (hf : locked f ∧ ¬ demoted needs sources cross kuf consumers residue f)
    (hneed : needs f P)
    (hsrc : ∃ s, sources s P) :
    ∃ s, (locked s ∧ ¬ demoted needs sources cross kuf consumers residue s) ∧ sources s P := by
  -- (1) hpick internalised: the fixpoint keeps SOME source of P in `locked`.
  obtain ⟨s, hslocked, hssrc⟩ := hpick_from_closed cross needs sources hclosed hf.1 hneed hsrc
  -- (2) source_lb derived: s sources P, and P is needed by f (a consumer), so s is loadbearing.
  have hcons_needs : ∃ g, consumers g ∧ needs g P :=
    ⟨f, locked_subset_consumers f hf.1, hneed⟩
  have hslb : loadbearing needs sources cross consumers s :=
    source_lb_from_def needs sources cross consumers hssrc hcons_needs
  -- (3) demoted_guard derived: demoted ⇒ ¬loadbearing, so a loadbearing s is NOT demoted.
  have hsnotdem : ¬ demoted needs sources cross kuf consumers residue s := by
    intro hd; exact (demoted_guard_from_def needs sources cross kuf consumers residue hd) hslb
  exact ⟨s, ⟨hslocked, hsnotdem⟩, hssrc⟩

/--
  DEBRIS-INCLUSIVE no-orphan. The committed pipeline removes files from the picker on TWO
  non-loadbearing paths: C5 demotion (`demoted` above) AND the Step-2 debris nomination
  (`nominate_debris`, guarded by `if k in loadbearing: continue`, so `debris k → ¬ loadbearing k`).
  The final picker is `locked` minus BOTH. No-orphan survives the second removal for the SAME reason
  it survives C5: the source the fixpoint realises is load-bearing, and BOTH removal paths take only
  NON-load-bearing files, so the realised source lies in neither. This covers the debris path - the one
  the SKILL.md guard change `locked`→`loadbearing` introduced. `no_orphan` was already generic over `demoted` (it constrains it only
  through `demoted_guard`); this theorem makes the second non-loadbearing removal explicit and checked.

  `debris_guard` is precisely the `if k in loadbearing: continue` test. We do NOT assume "stubs own no
  uuid": a 0-message stub can own a `compact_boundary` uuid, so `nmsg==0` does not by itself rule out
  load-bearing. The guard decides it directly by testing membership in `loadbearing`, and the hypothesis
  states exactly what that runtime test enforces: a nominated debris file is not load-bearing.
-/
theorem no_orphan_from_closed_debris
    {locked kuf consumers residue debris : FSet File}
    (hclosed : IsClosed cross needs sources locked)
    (locked_subset_consumers : ∀ x, locked x → consumers x)
    (debris_guard : ∀ k, debris k → ¬ loadbearing needs sources cross consumers k)
    {f : File} {P : Phantom}
    (hf : (locked f ∧ ¬ demoted needs sources cross kuf consumers residue f) ∧ ¬ debris f)
    (hneed : needs f P)
    (hsrc : ∃ s, sources s P) :
    ∃ s, (locked s ∧ ¬ demoted needs sources cross kuf consumers residue s) ∧ ¬ debris s
          ∧ sources s P := by
  -- The C5 result already supplies a source `s` that is locked and not C5-demoted.
  obtain ⟨s, hs_keptC5, hssrc⟩ :=
    no_orphan_from_closed cross needs sources hclosed locked_subset_consumers hf.1 hneed hsrc
  -- That source sources `P`, which `f` (a consumer) needs, so it is load-bearing - hence not debris.
  have hcons_needs : ∃ g, consumers g ∧ needs g P :=
    ⟨f, locked_subset_consumers f hf.1.1, hneed⟩
  have hslb : loadbearing needs sources cross consumers s :=
    source_lb_from_def needs sources cross consumers hssrc hcons_needs
  have hsnotdebris : ¬ debris s := fun hd => (debris_guard s hd) hslb
  exact ⟨s, hs_keptC5, hsnotdebris, hssrc⟩

/-- `consumers` with `d` removed, as an `FSet` (the real predicate `loadbearing` takes). -/
def without (consumers : FSet File) (d : File) : FSet File := fun x => consumers x ∧ x ≠ d

/--
  LOADBEARING STABILITY. For a file `d` that (i) is no OTHER file's cross-target source
  (`∀ b, b ≠ d → ¬ cross d b`) and (ii) shares no needed phantom with any other file
  (`∀ P, needs d P → ∀ s, s ≠ d → ¬ sources s P`), removing `d` from `consumers` leaves `loadbearing`
  unchanged on every `b ≠ d`. These two hypotheses are EXACTLY the two halves of the `len(ks)==1`
  singleton-tree guard, re-expressed in the `cross`/`needs`/`sources` vocabulary `loadbearing` uses; the
  `Family` bridge lemmas (`singleton_d_no_cross` / `singleton_d_no_share`) discharge them from the guard.
  This is what lets the FROZEN `loadbearing` (computed once with `d` still a consumer) equal the
  recompute-without-`d` value for every `b ≠ d`, so discarding a singleton debris file changes no other
  file's load-bearing status. The hypothesis is `∀ b, b ≠ d → ¬ cross d b` (not `∀ b`): a `cross d d`
  self-edge is harmless and the singleton guard does not exclude it. -/
theorem loadbearing_stable
    (cross : File → File → Prop) (consumers : FSet File) {d : File}
    (d_no_cross : ∀ b, b ≠ d → ¬ cross d b)
    (d_no_share : ∀ P, needs d P → ∀ s, s ≠ d → ¬ sources s P)
    {b : File} (hb : b ≠ d) :
    loadbearing needs sources cross consumers b
      ↔ loadbearing needs sources cross (without consumers d) b := by
  constructor
  · rintro (⟨a, hcons, hcr⟩ | ⟨P, hsrc, a, hcons, hneed⟩)
    · refine Or.inl ⟨a, ⟨hcons, ?_⟩, hcr⟩
      rintro rfl; exact (d_no_cross b hb) hcr
    · refine Or.inr ⟨P, hsrc, a, ⟨hcons, ?_⟩, hneed⟩
      rintro rfl; exact (d_no_share P hneed b hb) hsrc
  · rintro (⟨a, ⟨hcons, _⟩, hcr⟩ | ⟨P, hsrc, a, ⟨hcons, _⟩, hneed⟩)
    · exact Or.inl ⟨a, hcons, hcr⟩
    · exact Or.inr ⟨P, hsrc, a, hcons, hneed⟩

end FixProto
