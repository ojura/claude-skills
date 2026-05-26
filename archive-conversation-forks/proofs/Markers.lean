import Orphan
import Fixpoint

/-
  Two further invariants of the committed Step 2 set algebra, on top of the closure model in
  `Orphan.lean`:

  * `live_subset_keptC5`     - every live session survives to the final kept set (#3). The committed
                               code seeds `live` into the closure (`seed |= {k | k in live}`) and C5
                               only ever demotes kept-unique forks, so a live session is never moved.
  * `marker_no_hole`         - the marker decision tree has NO "~0 residue AND NOT load-bearing" cell
                               (#2): every member of `KEPT_C5 - canonicals - live` that is not
                               load-bearing has nonzero residue, so it cannot fall through the deleted
                               branch. (Round-5 Q4, previously fuzz-only; here machine-checked.)

  This needs the ENRICHED model: the pre-re-close seed `KEPT0` is decomposed into its three real
  sources `canonicals ∪ live ∪ kuf` (kept-unique forks), so the closure-inversion can place a
  non-canonical, non-live, non-load-bearing member into `kuf` and invoke C5's demotion contract.
  The abstract `no_orphan` in `Orphan.lean` is undisturbed - it stays generic over `KEPT0`.
-/

namespace Orphan

variable {File Phantom : Type}
variable (cross : File → File → Prop) (needs sources : File → Phantom → Prop)
variable (pick : Phantom → File)

/--
  The enriched seed: `KEPT0 = canonicals ∪ live ∪ kuf`. Matches the committed
  `KEPT = canonicals | set(locked) | kept_unique_forks` at the point the re-close starts
  (the preliminary `locked` is itself the closure of `canonicals ∪ live`, so it folds back in).
-/
def seed0 (canonicals live kuf : FSet File) : FSet File :=
  fun x => canonicals x ∨ live x ∨ kuf x

/--
  CLOSURE INVERSION (the key step in marker-no-hole). A closure member that is NOT in the seed must
  have entered via the `cross` or `phan` edge, and BOTH land in `loadbearing`. So a non-seed member
  is load-bearing. (Proof: `cases` on the derivation; the `seed` case contradicts non-membership.)

  `cross_lb` / `phan_lb` are the committed `loadbearing` definition restricted to closure members:
  loadbearing collects cross-file targets of consumers and sources of needed phantoms, and the
  closure only ever follows exactly those two edges, with the consumer being a prior closure member
  (`consumers = KEPT | live ⊇ closure`).
-/
theorem nonseed_loadbearing
    (S : FSet File) (loadbearing : FSet File)
    (cross_lb : ∀ k b, Closure cross needs sources pick S k → cross k b → loadbearing b)
    (phan_lb  : ∀ f P, Closure cross needs sources pick S f → needs f P → (∃ s, sources s P) →
                  loadbearing (pick P))
    {x : File} (hx : Closure cross needs sources pick S x) (hns : ¬ S x) :
    loadbearing x := by
  cases hx with
  | seed hs       => exact absurd hs hns
  | cross hk e    => exact cross_lb _ _ hk e
  | phan hf hn he => exact phan_lb _ _ hf hn he

/--
  #3 - LIVE SURVIVES. Every live session is in the final kept set `KEPT_C5`.

  `live_seeded`    : the code seeds live into the closure (`seed |= {k | k in live}`), so a live
                     session is in `KEPT_final` by the `seed` constructor (here: live ⊆ seed0).
  `live_not_demoted` : C5's demotion loop ranges over `kept_unique_forks` only; a live session is
                     not a kept-unique fork, so it is never demoted. (In the code, live and kuf are
                     disjoint: live comes from the registry, kuf from the per-tree fork judgment.)
-/
theorem live_subset_keptC5
    (canonicals live kuf demoted : FSet File)
    (live_not_demoted : ∀ x, live x → ¬ demoted x)
    {x : File} (hlive : live x) :
    KEPT_C5 cross needs sources pick (seed0 canonicals live kuf) demoted x := by
  refine ⟨?_, live_not_demoted x hlive⟩
  -- x ∈ KEPT_final = Closure(seed0): live ⊆ seed0, so `seed` applies.
  exact Closure.seed (Or.inr (Or.inl hlive))

/--
  #2 - MARKER NO-HOLE. For every member `x` of `KEPT_C5 - canonicals - live`, either `x` is
  load-bearing OR `x` has nonzero residue. Equivalently: there is NO `x` in that range that is
  simultaneously `~0 residue` AND `not load-bearing` - the branch deleted from the marker tree is
  unreachable, so the tree is exhaustive over its range.

  `residue x` abstracts the committed `residue = set(fps[k]) - ⋃_{j∈KEPT, j≠k} fps[j]` as the set of
  globally-unique messages of `x`; "nonzero residue" is `∃ m, residue x m`.

  Hypotheses (each a committed-code fact):
  * `cross_lb`, `phan_lb`  : the loadbearing-from-closure facts (see `nonseed_loadbearing`); the
                             cross-file target / phantom source of a closure member is load-bearing.
  * `C5_survivor`          : C5's actual postcondition. C5 demotes a kept-unique fork iff
                             (not load-bearing AND exactly-0 residue), so a SURVIVING kuf is
                             load-bearing OR has nonzero residue. Passing this disjunction directly
                             (rather than "not load-bearing -> residue") keeps the proof constructive:
                             no need to decide `loadbearing x`.

  FULLY CONSTRUCTIVE. The proof is a direct `cases` on the closure derivation (`seed` / `cross` /
  `phan`), so it needs neither `Classical.em` nor any decidability instance: it depends on NO axioms.
  (The `seed` case splits the `canonicals ∨ live ∨ kuf` disjunction the seed already carries; the
  `cross` / `phan` cases hand back load-bearing directly. This inlines `nonseed_loadbearing`.)
-/
theorem marker_no_hole {Msg : Type}
    (canonicals live kuf demoted loadbearing : FSet File)
    (residue : File → Msg → Prop)
    (cross_lb : ∀ k b, Closure cross needs sources pick (seed0 canonicals live kuf) k →
                  cross k b → loadbearing b)
    (phan_lb  : ∀ f P, Closure cross needs sources pick (seed0 canonicals live kuf) f →
                  needs f P → (∃ s, sources s P) → loadbearing (pick P))
    (C5_survivor : ∀ x, kuf x → ¬ demoted x → loadbearing x ∨ ∃ m, residue x m)
    {x : File}
    (hx : KEPT_C5 cross needs sources pick (seed0 canonicals live kuf) demoted x)
    (hncanon : ¬ canonicals x) (hnlive : ¬ live x) :
    loadbearing x ∨ ∃ m, residue x m := by
  obtain ⟨hfin, hnotdem⟩ := hx
  -- Direct inversion on HOW x entered the closure - constructive, no `em`, no decidability.
  cases hfin with
  | seed hs =>
      -- x ∈ seed = canonicals ∨ live ∨ kuf. Not canonical, not live ⇒ kuf ⇒ C5's postcondition.
      rcases hs with hc | hl | hk
      · exact absurd hc hncanon
      · exact absurd hl hnlive
      · exact C5_survivor x hk hnotdem
  | cross hk e    => exact Or.inl (cross_lb _ _ hk e)
  | phan hf hn he => exact Or.inl (phan_lb _ _ hf hn he)

/-
  ============================================================================================
  FULLY-WIRED capstones: the marker / live theorems stated over the FixProto-DEFINED `loadbearing`
  and `demoted` (their actual committed set definitions), with the `cross_lb` / `phan_lb` /
  `live_not_demoted` / `C5_survivor` hypotheses DISCHARGED internally via the `FixProto.*_from_def`
  lemmas. So nothing about loadbearing/demoted is assumed here; the only inputs are the seed
  decomposition, `closure ⊆ consumers`, and disjointness of `live` from `kuf` - each a code fact.
  ============================================================================================
-/

/-- `live_subset_keptC5`, wired: `demoted` is the FixProto definition; `live_not_demoted` is discharged
   from `live`/`kuf` disjointness. -/
theorem live_subset_keptC5_wired
    (canonicals live kuf consumers residue : FSet File)
    (hdisj : ∀ x, live x → ¬ kuf x)
    {x : File} (hlive : live x) :
    KEPT_C5 cross needs sources pick (seed0 canonicals live kuf)
      (FixProto.demoted needs sources cross kuf consumers residue) x :=
  live_subset_keptC5 cross needs sources pick canonicals live kuf
    (FixProto.demoted needs sources cross kuf consumers residue)
    (fun _ hy => FixProto.live_not_demoted_from_def needs sources cross kuf consumers residue live hdisj hy)
    hlive

/-- `marker_no_hole`, wired: `loadbearing` / `demoted` are the FixProto definitions; the three side
   conditions are discharged from the set definitions. `closure_subset_consumers` (closure ⊆ consumers)
   is the one structural input, and `[Decidable ...]` instances reflect the finite checks the code does. -/
theorem marker_no_hole_wired {Msg : Type}
    (canonicals live kuf consumers : FSet File) (residue : File → Msg → Prop)
    (closure_subset_consumers : ∀ y,
        Closure cross needs sources pick (seed0 canonicals live kuf) y → consumers y)
    -- the realised-source contract (`hpick`); itself derivable from `IsClosed` (FixProto.hpick_from_closed).
    (hpick : ∀ P, (∃ s, sources s P) → sources (pick P) P)
    [∀ y, Decidable (FixProto.loadbearing needs sources cross consumers y)]
    [∀ y, Decidable (∃ m, residue y m)]
    {x : File}
    (hx : KEPT_C5 cross needs sources pick (seed0 canonicals live kuf)
            (FixProto.demoted needs sources cross kuf consumers (fun y => ∃ m, residue y m)) x)
    (hncanon : ¬ canonicals x) (hnlive : ¬ live x) :
    FixProto.loadbearing needs sources cross consumers x ∨ ∃ m, residue x m := by
  refine marker_no_hole cross needs sources pick canonicals live kuf
    (FixProto.demoted needs sources cross kuf consumers (fun y => ∃ m, residue y m))
    (FixProto.loadbearing needs sources cross consumers) residue
    ?cross_lb ?phan_lb ?c5 hx hncanon hnlive
  case cross_lb =>
    intro k b hk e
    exact FixProto.cross_lb_from_def needs sources cross consumers (closure_subset_consumers k hk) e
  case phan_lb =>
    intro f P hf hn he
    -- pick P is a source of P (by hpick, since he : ∃ s, sources s P), and P is needed by f (a
    -- consumer), so loadbearing (pick P) via the phantom-source half of the definition.
    exact FixProto.source_lb_from_def needs sources cross consumers
      (s := pick P) (P := P) (hpick P he) ⟨f, closure_subset_consumers f hf, hn⟩
  case c5 =>
    intro y hk hnd
    rcases FixProto.C5_survivor_from_def needs sources cross kuf consumers (fun y => ∃ m, residue y m)
      hk hnd with hlb | hr
    · exact Or.inl hlb
    · exact Or.inr hr

/-
  ============================================================================================
  MARKER CLASSIFICATION (the mechanical core of #5). `marker_no_hole` above proves the partition has
  no unreachable cell (every retained file is loadbearing-or-residue, so none falls through). This
  section proves the COMPLEMENTARY mechanical fact: the documented decision tree assigns EXACTLY ONE
  marker per file, deterministically, with the four branch-guards pairwise disjoint and jointly
  exhaustive. What stays OUT of scope (genuinely a judgment, not math): whether the residue read is
  "substantive" - that enters as an opaque Bool input. We formalise that GIVEN the bucket
  classification, the assignment is well-defined and matches the committed tree.
  ============================================================================================
-/

/-- The four markers a retained file can carry. -/
inductive Marker | main | fork | scrollDep | none
deriving DecidableEq, Repr

/-- The committed marker decision tree (Step 2 marker loop) as a TOTAL function of the measured
    inputs: `lb` (load-bearing, structural), `highOv` (prose overlap high vs low), `substantive`
    (residue read as substantive - the operator judgment, here an opaque Bool), `zeroResid` (residue
    ~0). Mirrors the comments: load-bearing ∧ ~0-residue ⇒ [scroll-dep] regardless of ov; else by
    (ov, substantive): low+substantive ⇒ [main], high+substantive ⇒ [fork], otherwise none. -/
def classify (lb highOv substantive zeroResid : Bool) : Marker :=
  if lb && zeroResid then Marker.scrollDep
  else
    match highOv, substantive with
    | false, true  => Marker.main
    | false, false => Marker.none
    | true,  true  => Marker.fork
    | true,  false => Marker.none

/-- TOTAL: every input yields a marker (anchors "exactly one marker per file"). -/
theorem classify_total (lb highOv substantive zeroResid : Bool) :
    ∃ m, classify lb highOv substantive zeroResid = m := ⟨_, rfl⟩

/-- Each marker is produced under EXACTLY its documented guard (branch correctness + mutual
    exclusivity), by exhausting the 16-case Bool cube. Axiom-free. -/
theorem scrollDep_iff (lb highOv substantive zeroResid : Bool) :
    classify lb highOv substantive zeroResid = Marker.scrollDep ↔ (lb = true ∧ zeroResid = true) := by
  cases lb <;> cases highOv <;> cases substantive <;> cases zeroResid <;> decide

theorem main_iff (lb highOv substantive zeroResid : Bool) :
    classify lb highOv substantive zeroResid = Marker.main ↔
      (¬(lb = true ∧ zeroResid = true) ∧ highOv = false ∧ substantive = true) := by
  cases lb <;> cases highOv <;> cases substantive <;> cases zeroResid <;> decide

theorem fork_iff (lb highOv substantive zeroResid : Bool) :
    classify lb highOv substantive zeroResid = Marker.fork ↔
      (¬(lb = true ∧ zeroResid = true) ∧ highOv = true ∧ substantive = true) := by
  cases lb <;> cases highOv <;> cases substantive <;> cases zeroResid <;> decide

theorem none_iff (lb highOv substantive zeroResid : Bool) :
    classify lb highOv substantive zeroResid = Marker.none ↔
      (¬(lb = true ∧ zeroResid = true) ∧ substantive = false) := by
  cases lb <;> cases highOv <;> cases substantive <;> cases zeroResid <;> decide

/-- EXHAUSTIVE: every input lands in exactly one of the four markers (no gap). -/
theorem classify_exhaustive (lb highOv substantive zeroResid : Bool) :
    classify lb highOv substantive zeroResid = Marker.scrollDep ∨
    classify lb highOv substantive zeroResid = Marker.main ∨
    classify lb highOv substantive zeroResid = Marker.fork ∨
    classify lb highOv substantive zeroResid = Marker.none := by
  cases lb <;> cases highOv <;> cases substantive <;> cases zeroResid <;> decide

end Orphan
