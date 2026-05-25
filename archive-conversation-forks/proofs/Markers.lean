import Orphan

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
  * `cross_lb`, `phan_lb`     : the loadbearing-from-closure facts (see `nonseed_loadbearing`).
  * `C5_survivor_residue`     : a kept-unique fork that survives C5 and is NOT load-bearing has
                                nonzero residue. This is C5's contract read backwards: C5 demotes a
                                kuf iff (not load-bearing AND exactly-0 residue), so a SURVIVING
                                non-load-bearing kuf must have had nonzero residue.
-/
theorem marker_no_hole {Msg : Type}
    (canonicals live kuf demoted loadbearing : FSet File)
    (residue : File → Msg → Prop)
    (cross_lb : ∀ k b, Closure cross needs sources pick (seed0 canonicals live kuf) k →
                  cross k b → loadbearing b)
    (phan_lb  : ∀ f P, Closure cross needs sources pick (seed0 canonicals live kuf) f →
                  needs f P → (∃ s, sources s P) → loadbearing (pick P))
    (C5_survivor_residue : ∀ x, kuf x → ¬ demoted x → ¬ loadbearing x → ∃ m, residue x m)
    {x : File}
    (hx : KEPT_C5 cross needs sources pick (seed0 canonicals live kuf) demoted x)
    (hncanon : ¬ canonicals x) (hnlive : ¬ live x) :
    loadbearing x ∨ ∃ m, residue x m := by
  obtain ⟨hfin, hnotdem⟩ := hx
  -- hfin : Closure(seed0) x.  Classify x by whether it is in the seed.
  by_cases hseed : seed0 canonicals live kuf x
  · -- x is in the seed = canonicals ∨ live ∨ kuf. Not canonical, not live ⇒ kuf.
    rcases hseed with hc | hl | hk
    · exact absurd hc hncanon
    · exact absurd hl hnlive
    · -- x ∈ kuf, survives C5 (¬demoted). If not loadbearing, C5_survivor_residue gives residue.
      rcases Classical.em (loadbearing x) with hlb | hnlb
      · exact Or.inl hlb
      · exact Or.inr (C5_survivor_residue x hk hnotdem hnlb)
  · -- x not in seed ⇒ entered via cross/phan ⇒ load-bearing (inversion).
    exact Or.inl (nonseed_loadbearing cross needs sources pick
      (seed0 canonicals live kuf) loadbearing cross_lb phan_lb hfin hseed)

end Orphan
