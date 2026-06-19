import Orphan
import Markers
import Family
import Fixpoint
import Boundary
import Termination
open Orphan

/- VERIFY-OUTCOME 1: no hidden axioms / sorry. Every theorem here is fully constructive ("does not
   depend on any axioms") EXCEPT `canonical_mem` / `canonical_nondebris` (and their concrete
   witnesses), which list only `propext` (from `List.filter` reasoning). The crucial checks: NONE
   lists `sorryAx`, `Classical.choice`, or `Lean.ofReduceBool`. -/
#print axioms no_orphan
#print axioms closure_closed_under_needs
#print axioms final_closed_under_needs
#print axioms source_survives_C5
#print axioms kept0_subset_final
#print axioms recall_no_loss
#print axioms live_subset_keptC5
#print axioms marker_no_hole
#print axioms nonseed_loadbearing
-- fully-wired marker capstones: stated over the DEFINED loadbearing/demoted, side-conditions discharged.
#print axioms live_subset_keptC5_wired
#print axioms marker_no_hole_wired
-- marker classification (#5 mechanical core): the decision tree is total, exclusive, exhaustive.
-- NOTE: the no-hole guarantee is carried by `marker_no_hole` (the deleted cell is unreachable in the
-- real range), NOT by `classify`'s totality (a function trivially returns something). `classify` also
-- folds two operator judgments (substance floor, triviality) into the one `substantive` Bool and is
-- slightly more decisive than the loop prose (it picks `none` for the high-ov + non-substantive case
-- the comments leave open): it documents one faithful resolution, not the only admissible one.
#print axioms classify_total
#print axioms scrollDep_iff
#print axioms main_iff
#print axioms fork_iff
#print axioms none_iff
#print axioms classify_exhaustive
#print axioms Family.sameTree_refl
#print axioms Family.sameTree_symm
#print axioms Family.sameTree_trans
#print axioms Family.shared_lpu_sameTree
#print axioms Family.needer_source_coTree
#print axioms Family.noEdges_sameTree_eq
#print axioms Family.content_not_a_generator
#print axioms Family.canonical_mem
#print axioms Family.canonical_nondebris
-- canonical max-key SELECTION (#4 canonical part): lex order + argmax picks a maximal-key element.
#print axioms Family.Canon.kle_total
#print axioms Family.Canon.klt_trans
#print axioms Family.Canon.argmax_ge_mem
#print axioms Family.Canon.canonicalByKey_is_max
-- path compression (#4 find part, "for fun"): compression preserves the computed component.
#print axioms Family.Compress.compress_preserves_root_self
#print axioms Family.Compress.root_compress_v
-- Fixpoint internalisation: the bridge facts derived, not assumed.
#print axioms FixProto.hpick_from_closed
#print axioms FixProto.source_lb_from_def
#print axioms FixProto.demoted_guard_from_def
#print axioms FixProto.no_orphan_from_closed
-- Termination: IsClosed is ACHIEVABLE (a closed superset exists over a finite universe), so the
-- last assumption is discharged. Core Lean, no mathlib; these list only [propext, Quot.sound].
#print axioms TermList.gap_lt
#print axioms TermList.length_filter_le_of_imp
#print axioms TermList.closed_superset_exists
-- #2: expand CONSTRUCTED (oracle proven, not assumed) + the oracle-free existence capstone.
#print axioms TermList.closed_of_none
#print axioms TermList.expand
#print axioms TermList.closed_superset_exists_constructed

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
   lands in the `∃ residue` disjunct (a real `[main]`/`[fork]`/none member, not the deleted cell).
   WHY a STATIC `residue` predicate is faithful here: the marker loop ranges over C5 survivors, and C5
   removes only EXACTLY-0-residue files (which contribute no unique message), so removing them only
   GROWS every survivor's residue against the shrinking kept set. A survivor with nonzero residue
   keeps it against the final KEPT, so static `residue` never overstates a survivor's uniqueness. -/
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

/- VERIFY-OUTCOME 4b: the WIRED marker capstone is non-vacuous - stated over the DEFINED
   loadbearing/demoted with the side-conditions discharged internally. Reuse the g0/c0 store. -/
def consum2 : FSet Fil2 := fun _ => True                     -- both files are consumers
-- loadbearing is provably false for every file here (no cross edges at all, and only g0 sources
-- nothing while c0 sources nothing either - src2 is identically False).
instance : ∀ y, Decidable (FixProto.loadbearing needs2 src2 cross2 consum2 y) := fun y =>
  isFalse (by
    unfold FixProto.loadbearing cross2 src2
    rintro (⟨a, _, hc⟩ | ⟨P, hs, _⟩)
    · exact hc
    · exact hs.elim)
-- ∃ m, resid2 y m is decidable for every y (resid2 y p0 = (y = g0)).
instance : ∀ y, Decidable (∃ m, resid2 y m) := fun y =>
  if h : y = g0 then isTrue ⟨p0, h⟩
  else isFalse (by rintro ⟨m, hm⟩; exact h hm)

-- g0 ∈ KEPT_C5 over the DEFINED demoted: in the seed (kuf), and not demoted (it has residue).
theorem g0_keptC5_wired :
    KEPT_C5 cross2 needs2 src2 pick2 (seed0 canon2 live2 kuf2)
      (FixProto.demoted needs2 src2 cross2 kuf2 consum2 (fun y => ∃ m, resid2 y m)) g0 := by
  refine ⟨Closure.seed (Or.inr (Or.inr rfl)), ?_⟩
  intro hd; exact hd.2.2 ⟨p0, rfl⟩            -- ¬residue fails: g0 has residue

theorem concrete_marker_no_hole_wired :
    FixProto.loadbearing needs2 src2 cross2 consum2 g0 ∨ ∃ m, resid2 g0 m :=
  marker_no_hole_wired cross2 needs2 src2 pick2 canon2 live2 kuf2 consum2 resid2
    (fun _ _ => trivial)                       -- closure ⊆ consumers (consumers = ⊤)
    (fun _ he => he.elim (fun s hs => hs.elim))  -- hpick: vacuous (no sources)
    g0_keptC5_wired g0_not_canon g0_not_live

-- live = {c0}, disjoint from kuf2 = {g0}; c0 survives C5.
theorem concrete_live_subset_wired :
    KEPT_C5 cross2 needs2 src2 pick2 (seed0 canon2 (fun f => f = c0) kuf2)
      (FixProto.demoted needs2 src2 cross2 kuf2 consum2 (fun _ => True)) c0 :=
  live_subset_keptC5_wired cross2 needs2 src2 pick2 canon2 (fun f => f = c0) kuf2 consum2 (fun _ => True)
    (by intro x hx; rw [hx]; unfold kuf2; decide)   -- c0 ∉ kuf2 (={g0})
    (x := c0) rfl

#print axioms concrete_marker_no_hole_wired
#print axioms concrete_live_subset_wired

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

/- VERIFY-OUTCOME 6b: canonical MAX-KEY selection is non-vacuous. Key gives n0 more distinct content
   (5) than s0 (1); among the floored candidates [n0,s0] canonicalByKey picks the maximal-key one. -/
def key3 : Fil3 → Family.Canon.Key := fun f => if f = n0 then (5, 0, 0) else (1, 0, 0)
def nodeb3 : Fil3 → Prop := fun _ => False         -- nothing debris here, so cand = the whole list
instance : DecidablePred nodeb3 := fun _ => isFalse (by unfold nodeb3; exact id)

theorem concrete_canonical_max :
    ∃ c, Family.Canon.canonicalByKey nodeb3 key3 [n0, s0] = some c ∧
      (∀ y ∈ Family.cand nodeb3 [n0, s0], Family.Canon.kle (key3 y) (key3 c)) := by
  have hpick : Family.Canon.canonicalByKey nodeb3 key3 [n0, s0]
      = some (Family.Canon.argmax key3 n0 [s0]) := rfl
  exact ⟨_, hpick, (Family.Canon.canonicalByKey_is_max nodeb3 key3 [n0, s0] hpick).2⟩

#print axioms concrete_canonical_max

/- VERIFY-OUTCOME 6c: path compression is non-vacuous. parent: n0 → s0, s0 → s0 (s0 root). Compress
   n0 to s0; root of n0 in the compressed map is s0 (the same component). -/
def par3 : Fil3 → Fil3 := fun f => if f = n0 then s0 else s0   -- n0↦s0, s0↦s0

theorem concrete_compress :
    Family.Compress.root (Family.Compress.compress par3 n0 s0) 2 n0 = s0 :=
  Family.Compress.root_compress_v par3 (r := s0) (v := n0)
    (by unfold Family.Compress.isRoot par3; decide) (by decide)

#print axioms concrete_compress

/- VERIFY-OUTCOME 7: the INTERNALISED no-orphan (no_orphan_from_closed) is non-vacuous. A concrete
   closed `locked` (containing a needer f0 and its source f1), with the loop's fixpoint condition
   `IsClosed` actually proved for this store - so the conclusion (a source survives in the post-C5
   set) is a real claim derived from IsClosed, not from an assumed hpick/source_lb/demoted_guard. -/
def crossN : Fil → Fil → Prop := fun _ _ => False
def needsN : Fil → Pha → Prop := fun f _ => f = f0
def srcN   : Fil → Pha → Prop := fun f _ => f = f1
def lockedN : FSet Fil := fun _ => True            -- both files locked (a valid closed set here)
def kufN   : FSet Fil := fun _ => False            -- no kept-unique forks here
def consumersN : FSet Fil := fun _ => True
def residueN : FSet Fil := fun _ => True           -- nonzero residue everywhere -> nothing demoted

-- IsClosed holds for this store: no cross edges; the only needed phantom (p0, needed by f0) has
-- source f1, which is locked. So phan_closed is witnessed by f1.
theorem lockedN_closed : FixProto.IsClosed crossN needsN srcN lockedN where
  cross_closed := by intro _ _ _ e; cases e
  phan_closed  := by intro _ _ _ _ _; exact ⟨f1, trivial, rfl⟩

theorem concrete_no_orphan_from_closed :
    ∃ s, (lockedN s ∧ ¬ FixProto.demoted needsN srcN crossN kufN consumersN residueN s) ∧ srcN s p0 := by
  refine FixProto.no_orphan_from_closed crossN needsN srcN lockedN_closed
    (fun _ _ => trivial) (f := f0) (P := p0) ⟨trivial, ?_⟩ rfl ⟨f1, rfl⟩
  -- ¬ demoted f0: demoted = kuf ∧ ¬lb ∧ ¬residue, but kufN f0 is False, so the conjunction fails.
  intro hd; exact hd.1

#print axioms concrete_no_orphan_from_closed

/- VERIFY-OUTCOME 9: the four marker side-conditions, previously hypotheses, are now DERIVED from the
   loadbearing/demoted set definitions (FixProto). Axiom-free; each a few lines. -/
#print axioms FixProto.cross_lb_from_def
#print axioms FixProto.live_not_demoted_from_def
#print axioms FixProto.C5_survivor_from_def

-- Non-vacuity: cross_lb on a store with a real cross edge; live_not_demoted with live/kuf disjoint;
-- C5_survivor where a surviving kuf has residue.
theorem concrete_cross_lb :
    FixProto.loadbearing needsN srcN (fun a _ => a = f0) consumersN f1 :=
  FixProto.cross_lb_from_def needsN srcN (fun a _ => a = f0) consumersN (a := f0) trivial rfl

theorem concrete_live_not_demoted :
    ¬ FixProto.demoted needsN srcN crossN kufN consumersN residueN f0 :=
  FixProto.live_not_demoted_from_def needsN srcN crossN kufN consumersN residueN (live := fun _ => True)
    (fun _ _ h => h) trivial

-- Decidable instances for the concrete store: loadbearing f0 is provably false (no cross edge to
-- f0, and f0 sources nothing since srcN f0 P = (f0 = f1) is false); residueN f0 is provably true.
instance : Decidable (FixProto.loadbearing needsN srcN crossN consumersN f0) :=
  isFalse (by
    unfold FixProto.loadbearing crossN srcN
    rintro (⟨a, _, hc⟩ | ⟨P, hs, _⟩)
    · exact hc                       -- crossN a f0 = False
    · exact absurd hs (by decide))   -- srcN f0 P = (f0 = f1), decidably false
instance : Decidable (residueN f0) := isTrue trivial

theorem concrete_C5_survivor :
    FixProto.loadbearing needsN srcN crossN consumersN f0 ∨ residueN f0 :=
  FixProto.C5_survivor_from_def needsN srcN crossN (fun _ => True) consumersN residueN
    (x := f0) trivial (fun hd => hd.2.2 trivial)

#print axioms concrete_cross_lb
#print axioms concrete_live_not_demoted
#print axioms concrete_C5_survivor

/- VERIFY-OUTCOME 8: termination is non-vacuous AND `expand` is constructible (not a hidden
   assumption). Over a 1-element universe with no edges, every set is already closed, so we build the
   `expand` oracle concretely (always reports closed) and obtain a closed superset. This shows the
   termination theorem yields a real closed set and that its `expand` hypothesis is realisable. -/
def crossT : Fil → Fil → Prop := fun _ _ => False
def needsT : Fil → Pha → Prop := fun _ _ => False
def srcT   : Fil → Pha → Prop := fun _ _ => False

-- expand: with no needs and no cross edges, ANY L is closed, so always take the left (closed) branch.
def expandT : ∀ L : List Fil,
    (FixProto.IsClosed crossT needsT srcT (TermList.memPred L)) ⊕' (Σ' x : Fil, x ∈ ([f0] : List Fil) ∧ x ∉ L) :=
  fun L => PSum.inl {
    cross_closed := by intro _ _ _ e; cases e
    phan_closed  := by intro _ _ _ hn; cases hn }

theorem concrete_termination :
    ∃ M : List Fil, (∀ y, y ∈ ([] : List Fil) → y ∈ M)
      ∧ FixProto.IsClosed crossT needsT srcT (TermList.memPred M) :=
  TermList.closed_superset_exists crossT needsT srcT [f0] expandT []

#print axioms concrete_termination

/- VERIFY-OUTCOME 8b: the CONSTRUCTED-oracle version is non-vacuous - no `expand` passed in. The
   store's relations are decidable (all identically False here), so `closed_superset_exists_constructed`
   builds the search itself and yields a bounded-closed superset. Confirms the oracle is realisable. -/
instance : DecidableRel crossT := fun _ _ => isFalse (by unfold crossT; exact id)
instance : ∀ a p, Decidable (needsT a p) := fun _ _ => isFalse (by unfold needsT; exact id)
instance : ∀ a p, Decidable (srcT a p) := fun _ _ => isFalse (by unfold srcT; exact id)

theorem concrete_termination_constructed :
    ∃ M : List Fil, (∀ y, y ∈ ([] : List Fil) → y ∈ M)
      ∧ TermList.ClosedB crossT needsT srcT [f0] ([] : List Pha) M :=
  TermList.closed_superset_exists_constructed crossT needsT srcT [f0] [] []

#print axioms concrete_termination_constructed

/- VERIFY-OUTCOME 10: the par/nb BOUNDARY layer (Boundary.lean). `needs`/`sources` are no longer
   opaque - they are the committed set-builder over `(lpu, par, nb)` records. Audited here:
   (a) the per-record classification is a TOTAL, MUTUALLY-EXCLUSIVE partition (rec_excl/total/iff) with
       the 2-bit cube closed by `decide` (bit_partition), and the `par is not None`-only bug is a
       CHECKED divergence (lazy_flips_source_to_need) - all axiom-free;
   (b) a concrete boundary store feeds the DEFINED relations through the unconditional no-orphan, with
       the surviving source being a record the par-only bug would have DROPPED (parPresent=false, nb>0). -/
#print axioms Boundary.rec_excl
#print axioms Boundary.rec_total
#print axioms Boundary.rec_iff
#print axioms Boundary.bit_partition
#print axioms Boundary.lazy_flips_source_to_need
#print axioms Boundary.no_orphan_from_closed_bnd

-- f0 has a ROOT phantom boundary for p0 (-> needsOf); f1 has a phantom boundary with pre-content via
-- nb>0 (-> sourcesOf) - the SAME record shape (null parent, real pre-content) the lazy par-only test drops.
def bndB : Fil → List (Boundary.Bdy Pha)
  | f0 => [⟨p0, false, 0⟩]     -- root: parPresent=false, nb=0  -> NeedRec
  | f1 => [⟨p0, false, 3⟩]     -- pre-content via nb=3>0        -> SourceRec (null parent: bug-relevant)
def phantomB : Pha → Prop := fun _ => True

theorem f0_needsB : Boundary.needsOf phantomB bndB f0 p0 :=
  ⟨⟨p0, false, 0⟩, List.mem_cons_self _ _, rfl, trivial, rfl, rfl⟩
theorem f1_sourcesB : Boundary.sourcesOf phantomB bndB f1 p0 :=
  ⟨⟨p0, false, 3⟩, List.mem_cons_self _ _, rfl, trivial, Or.inr (by decide)⟩

def crossB : Fil → Fil → Prop := fun _ _ => False
def lockedB : FSet Fil := fun _ => True
def kufB : FSet Fil := fun _ => False
def consumersB : FSet Fil := fun _ => True
def residueB : FSet Fil := fun _ => True

-- IsClosed for the boundary-DEFINED relations: no cross edges; any needed phantom with a source keeps
-- that source locked (everything is locked here, so the given source already works).
theorem lockedB_closed :
    FixProto.IsClosed crossB (Boundary.needsOf phantomB bndB) (Boundary.sourcesOf phantomB bndB) lockedB where
  cross_closed := fun _ _ _ e => e.elim
  phan_closed  := fun _ _ _ _ hsrc => by obtain ⟨s, hs⟩ := hsrc; exact ⟨s, trivial, hs⟩

-- Instantiate the unconditional no-orphan over the par/nb relations. The witness is f1, retained -
-- and f1 sources p0 via a (parPresent=false, nb=3) record, the exact one a `par is not None`-only
-- check would have dropped. So the proof keeps precisely the source the historical bug orphaned.
theorem concrete_no_orphan_from_closed_bnd :
    ∃ s, (lockedB s ∧ ¬ FixProto.demoted (Boundary.needsOf phantomB bndB)
            (Boundary.sourcesOf phantomB bndB) crossB kufB consumersB residueB s)
         ∧ Boundary.sourcesOf phantomB bndB s p0 := by
  refine Boundary.no_orphan_from_closed_bnd phantomB bndB crossB lockedB_closed (fun _ _ => trivial)
    (f := f0) (P := p0) ⟨trivial, ?_⟩ f0_needsB ⟨f1, f1_sourcesB⟩
  intro hd; exact hd.1     -- ¬ demoted f0: demoted needs kufB f0 = False

#print axioms concrete_no_orphan_from_closed_bnd

/- VERIFY-OUTCOME 11: the DEBRIS demotion (Step-2 `nominate_debris`, guarded on `loadbearing`), checked
   inside the proof. Three checks:
   (a) `debris ⊆ canonicals` is REAL: a singleton tree's canonical is its member, so a debris-nominated
       file is its own tree's canonical (off `canonicalPick`, NOT assumed);
   (b) no-orphan SURVIVES debris removal (a second non-load-bearing demotion) - a genuinely non-vacuous
       3-file store where a debris file `d` is removed yet the kept needer `a`'s source `b` survives;
   (c) the marker range EXCLUDES debris, composing (a) with the carry-over lemma. -/
#print axioms Family.singleton_canonicalPick
#print axioms Family.debris_nominated_canonical
#print axioms FixProto.no_orphan_from_closed_debris
#print axioms marker_range_excludes_debris

-- (a) debris ⊆ canonicals, concretely. `debrisC` marks f0 as is_debris; its tree is the singleton
-- [f0]; so its tree-canonical is f0 itself (whether or not f0 is is_debris - the `cand` floor falls back).
-- `treeOfC` is partition-faithful (each file is its own singleton tree: `f ∈ treeOfC f`).
def debrisC : Fil → Prop := fun f => f = f0
instance : DecidablePred debrisC := fun f => (inferInstance : Decidable (f = f0))
def treeOfC : Fil → List Fil := fun f => [f]

theorem concrete_singleton_canonical : Family.canonicalPick debrisC [f0] = some f0 :=
  Family.singleton_canonicalPick debrisC f0

theorem concrete_debris_canonical : Family.canonicalPick debrisC (treeOfC f0) = some f0 :=
  Family.debris_nominated_canonical debrisC treeOfC (k := f0) rfl

-- canonicals modelled as the committed "{canonical(ks)}": x is canonical iff its own tree's canonical.
def canonicalsC : FSet Fil := fun x => Family.canonicalPick debrisC (treeOfC x) = some x
theorem debrisC_sub_canon : ∀ x, debrisC x → canonicalsC x := by
  intro x hx; unfold canonicalsC; subst hx; exact concrete_debris_canonical

#print axioms concrete_singleton_canonical
#print axioms concrete_debris_canonical

-- (b) no-orphan survives debris removal. 3 files: `dNeed` needs p0, `dSrc` sources p0 (load-bearing),
-- `dJunk` is a non-load-bearing debris file removed from the picker. The surviving source is `dSrc`,
-- provably ¬debris. (Fresh store `FilD` to avoid the `Fil3`/`debris3` already used above.)
inductive FilD | dNeed | dSrc | dJunk
deriving DecidableEq
open FilD

def crossD : FilD → FilD → Prop := fun _ _ => False
def needsD : FilD → Pha → Prop := fun f _ => f = dNeed
def srcD   : FilD → Pha → Prop := fun f _ => f = dSrc
def lockedD : FSet FilD := fun _ => True
def kufD : FSet FilD := fun _ => False
def consumersD : FSet FilD := fun _ => True
def residueD : FSet FilD := fun _ => True
def debrisD : FSet FilD := fun f => f = dJunk

theorem lockedD_closed : FixProto.IsClosed crossD needsD srcD lockedD where
  cross_closed := by intro _ _ _ e; cases e
  phan_closed  := by intro _ _ _ _ _; exact ⟨dSrc, trivial, rfl⟩

-- debris_guard: dJunk is debris and NOT load-bearing (no cross edge to it; it sources nothing).
theorem debrisD_guard : ∀ k, debrisD k → ¬ FixProto.loadbearing needsD srcD crossD consumersD k := by
  intro k hk; subst hk
  unfold FixProto.loadbearing crossD srcD
  rintro (⟨x, _, hc⟩ | ⟨P, hs, _⟩)
  · exact hc
  · exact absurd hs (by decide)

theorem concrete_no_orphan_debris :
    ∃ s, (lockedD s ∧ ¬ FixProto.demoted needsD srcD crossD kufD consumersD residueD s)
          ∧ ¬ debrisD s ∧ srcD s p0 := by
  refine FixProto.no_orphan_from_closed_debris crossD needsD srcD lockedD_closed
    (fun _ _ => trivial) debrisD_guard (f := dNeed) (P := p0) ⟨⟨trivial, ?_⟩, ?_⟩ rfl ⟨dSrc, rfl⟩
  · intro hd; exact hd.1          -- ¬ demoted dNeed (kufD dNeed = False)
  · simp only [debrisD]; decide   -- ¬ debrisD dNeed : (dNeed = dJunk) is false

#print axioms concrete_no_orphan_debris

-- (c) the marker range excludes debris, on a partition-faithful store. Three files: `mDeb` is a singleton
-- debris tree; `mCanon`/`mNon` form a genuine 2-file tree whose canonical (the head of the floored
-- candidates) is `mCanon`, so `mNon` is genuinely NON-canonical. marker_range_excludes_debris then shows
-- the non-canonical `mNon` is non-debris, non-vacuously.
inductive FilM | mDeb | mCanon | mNon
deriving DecidableEq
open FilM

def debrisM : FilM → Prop := fun f => f = mDeb
instance : DecidablePred debrisM := fun f => (inferInstance : Decidable (f = mDeb))
def treeOfM : FilM → List FilM := fun f => match f with
  | mDeb   => [mDeb]                 -- singleton debris tree
  | mCanon => [mCanon, mNon]         -- a genuine 2-file tree; head-of-cand canonical is mCanon
  | mNon   => [mCanon, mNon]         -- mNon is the non-head member -> not its tree's canonical
def canonicalsM : FSet FilM := fun x => Family.canonicalPick debrisM (treeOfM x) = some x

theorem debrisM_sub_canon : ∀ x, debrisM x → canonicalsM x := by
  intro x hx; unfold canonicalsM; subst hx
  exact Family.singleton_canonicalPick debrisM mDeb

theorem mNon_non_canonical : ¬ canonicalsM mNon := by
  unfold canonicalsM treeOfM; decide

theorem concrete_marker_excludes_debris : ¬ debrisM mNon :=
  marker_range_excludes_debris canonicalsM debrisM debrisM_sub_canon (x := mNon) mNon_non_canonical

#print axioms debrisM_sub_canon
#print axioms concrete_marker_excludes_debris

/- ============================================================================================
   CONTENT SAFETY UNDER DEBRIS REMOVAL - the forced witnesses. Each store is namespaced so the constructor
   names don't collide; only the fingerprint relations are
   renamed to the committed `fingerprints` convention. Generic lemmas live in Orphan.lean
   (`content_safe_post_debris`, `c5_demote_no_loss`, `residue_grows_on_shrink`); these are the non-vacuity
   instances, same architecture as `no_orphan_from_closed_debris` / `concrete_no_orphan_debris`.
   ============================================================================================ -/

/- (d) RECALL discrimination: discarding debris CHANGES the recall outcome. {A, d, kK}, one message m0;
    m0 lives only in A and the debris d; kK carries nothing, forcing `missing`-nonempty for a REAL reason.
    Over KEPT-with-debris the recall test wrongly passes (A's only container is debris d); over
    KEPT-minus-debris it fails (A correctly kept). The two conjuncts in tension foreclose vacuity. -/
namespace RecallDebrisWitness
inductive CFil | A | d | kK
deriving DecidableEq
inductive CMsg | m0
deriving DecidableEq
open CFil CMsg

def cfingerprints : CFil → CMsg → Prop := fun f _ => f = A ∨ f = d
def cdebris : FSet CFil := fun f => f = d
def keptWith : FSet CFil := fun f => f = d ∨ f = kK
def keptMinus : FSet CFil := fun f => keptWith f ∧ ¬ cdebris f          -- = {kK}

instance : ∀ m, Decidable (∃ b, keptWith b ∧ cfingerprints b m) := fun _ =>
  isTrue ⟨d, Or.inl rfl, Or.inr rfl⟩
instance : ∀ m, Decidable (∃ b, keptMinus b ∧ cfingerprints b m) := fun _ =>
  isFalse (by
    rintro ⟨b, ⟨hk, hnd⟩, hf⟩
    rcases hk with h | h
    · exact hnd h
    · subst h; rcases hf with h2 | h2 <;> cases h2)

theorem buggy_missing_empty : ∀ m, ¬ missing cfingerprints keptWith A m := by
  intro _ h; exact h.2 ⟨d, Or.inl rfl, Or.inr rfl⟩
theorem buggy_certifies_archive : preserved cfingerprints keptWith A :=
  recall_no_loss cfingerprints keptWith A buggy_missing_empty
theorem buggy_witness_is_debris : ¬ ∃ b, keptWith b ∧ ¬ cdebris b ∧ cfingerprints b m0 := by
  rintro ⟨b, hk, hnd, hf⟩
  rcases hk with h | h
  · subst h; exact hnd rfl
  · subst h; rcases hf with h2 | h2 <;> cases h2
theorem fixed_missing_nonempty : missing cfingerprints keptMinus A m0 := by
  refine ⟨Or.inl rfl, ?_⟩
  rintro ⟨b, ⟨hk, hnd⟩, hf⟩
  rcases hk with h | h
  · exact hnd h
  · subst h; rcases hf with h2 | h2 <;> cases h2
theorem discard_changes_outcome :
    (∀ m, ¬ missing cfingerprints keptWith A m) ∧ (∃ m, missing cfingerprints keptMinus A m) :=
  ⟨buggy_missing_empty, ⟨m0, fixed_missing_nonempty⟩⟩

#print axioms buggy_certifies_archive
#print axioms discard_changes_outcome
end RecallDebrisWitness

/- (e) C5 discrimination (residue-keyed). The demoted file `kFork` is IN the kept set (a kuf). Over
    KEPT-with-debris kFork reads 0-residue (m0 also in debris d) -> would be demoted; over KEPT-minus-debris
    it has residue -> NOT demoted. kFork flips demote->keep purely because d left. -/
namespace C5DebrisWitness
inductive C5Fil | kFork | d | kK
deriving DecidableEq
inductive C5Msg | m0
deriving DecidableEq
open C5Fil C5Msg

def c5fingerprints : C5Fil → C5Msg → Prop := fun f _ => f = kFork ∨ f = d
def c5debris : FSet C5Fil := fun f => f = d
def keptWith : FSet C5Fil := fun f => f = kFork ∨ f = d ∨ f = kK
def keptMinus : FSet C5Fil := fun f => keptWith f ∧ ¬ c5debris f
def demotedOver (KEPT : FSet C5Fil) (k : C5Fil) : Prop :=
  ∀ m, c5fingerprints k m → ∃ j, KEPT j ∧ j ≠ k ∧ c5fingerprints j m

theorem buggy_demotes : demotedOver keptWith kFork := by
  intro m _; exact ⟨d, Or.inr (Or.inl rfl), by decide, Or.inr rfl⟩
theorem buggy_other_is_debris :
    ¬ ∃ j, (keptWith j ∧ ¬ c5debris j) ∧ j ≠ kFork ∧ c5fingerprints j m0 := by
  rintro ⟨j, ⟨hk, hnd⟩, hne, hf⟩
  rcases hk with h | h | h
  · exact hne h
  · subst h; exact hnd rfl
  · subst h; rcases hf with h2 | h2 <;> cases h2
theorem fixed_not_demoted : ¬ demotedOver keptMinus kFork := by
  intro hdem
  obtain ⟨j, ⟨hmem, hnd⟩, hne, hf⟩ := hdem m0 (Or.inl rfl)
  rcases hmem with hh | hh | hh
  · exact hne hh
  · subst hh; exact hnd rfl
  · subst hh; rcases hf with h2 | h2 <;> cases h2
theorem c5_discard_changes_outcome :
    demotedOver keptWith kFork ∧ ¬ demotedOver keptMinus kFork :=
  ⟨buggy_demotes, fixed_not_demoted⟩

#print axioms c5_discard_changes_outcome
end C5DebrisWitness

/- (f) JOINT non-vacuity: ONE store, ONE shared post-debris KEPT, where BOTH paths fire
    AND both positive capstones discharge - so the pipeline composes (three separate stores would not).
    {jA, jB, jFork, jd, jK}, msgs {mA, mB, mF}; the single debris `jd` is the false container for BOTH
    mA (recall) and mF (C5); jB and jK are the genuine non-debris homes that make the positives non-vacuous. -/
namespace JointDebrisWitness
inductive JFil | jA | jB | jFork | jd | jK
deriving DecidableEq
inductive JMsg | mA | mB | mF
deriving DecidableEq
open JFil JMsg

def jfingerprints : JFil → JMsg → Prop := fun f m =>
  match f, m with
  | jA, mA => True | jB, mB => True | jFork, mF => True
  | jd, mA => True | jd, mF => True | jK, mB => True
  | _, _ => False
def jdebris : FSet JFil := fun f => f = jd
def keptWith : FSet JFil := fun f => f = jB ∨ f = jFork ∨ f = jd ∨ f = jK
def keptMinus : FSet JFil := fun f => keptWith f ∧ ¬ jdebris f          -- {jB, jFork, jK}

instance : ∀ f m, Decidable (jfingerprints f m) := fun f m => by
  unfold jfingerprints; cases f <;> cases m <;> infer_instance
instance : DecidablePred jdebris := fun f => by unfold jdebris; infer_instance

instance : ∀ m, Decidable (∃ b, keptMinus b ∧ jfingerprints b m) := fun m =>
  match m with
  | mA => isFalse (by rintro ⟨b, ⟨hk, hnd⟩, hf⟩; rcases hk with h|h|h|h <;>
            (subst h; first | exact hnd rfl | exact (by cases hf)))
  | mB => isTrue ⟨jK, ⟨Or.inr (Or.inr (Or.inr rfl)), by decide⟩, by decide⟩
  | mF => isTrue ⟨jFork, ⟨Or.inr (Or.inl rfl), by decide⟩, by decide⟩

theorem recall_A_missing : missing jfingerprints keptMinus jA mA := by
  refine ⟨trivial, ?_⟩
  rintro ⟨b, ⟨hk, hnd⟩, hf⟩
  rcases hk with h|h|h|h
  · subst h; cases hf
  · subst h; cases hf
  · subst h; exact hnd rfl
  · subst h; cases hf
theorem recall_B_missing_empty : ∀ m, ¬ missing jfingerprints keptMinus jB m := by
  intro m
  rcases m with _ | _ | _
  · intro h; cases h.1
  · intro h; exact h.2 ⟨jK, ⟨Or.inr (Or.inr (Or.inr rfl)), by decide⟩, by decide⟩
  · intro h; cases h.1
theorem recall_B_preserved : preserved jfingerprints keptMinus jB :=
  recall_no_loss jfingerprints keptMinus jB recall_B_missing_empty

def demotedOver (KEPT : FSet JFil) (k : JFil) : Prop :=
  ∀ m, jfingerprints k m → ∃ j, KEPT j ∧ j ≠ k ∧ jfingerprints j m
theorem c5_buggy_demotes : demotedOver keptWith jFork := by
  intro m hm
  match m, hm with
  | mF, _ => exact ⟨jd, Or.inr (Or.inr (Or.inl rfl)), by decide, by decide⟩
theorem c5_fixed_not_demoted : ¬ demotedOver keptMinus jFork := by
  intro hdem
  obtain ⟨j, ⟨hmem, hnd⟩, hne, hf⟩ := hdem mF trivial
  rcases hmem with h|h|h|h
  · subst h; cases hf
  · exact hne h
  · subst h; exact hnd rfl
  · subst h; cases hf
theorem c5_K_demote_no_loss :
    ∀ m, jfingerprints jK m → ∃ j, (keptMinus j ∧ ¬ jdebris j) ∧ jfingerprints j m :=
  c5_demote_no_loss jfingerprints keptMinus jdebris jK
    (by
      intro m hm
      match m, hm with
      | mB, _ => exact ⟨jB, ⟨⟨Or.inl rfl, by decide⟩, by decide⟩, by decide, by decide⟩)

theorem joint_pipeline_non_vacuous :
    missing jfingerprints keptMinus jA mA
  ∧ preserved jfingerprints keptMinus jB
  ∧ (demotedOver keptWith jFork ∧ ¬ demotedOver keptMinus jFork)
  ∧ (∀ m, jfingerprints jK m → ∃ j, (keptMinus j ∧ ¬ jdebris j) ∧ jfingerprints j m) :=
  ⟨recall_A_missing, recall_B_preserved,
   ⟨c5_buggy_demotes, c5_fixed_not_demoted⟩, c5_K_demote_no_loss⟩

#print axioms recall_B_preserved
#print axioms joint_pipeline_non_vacuous
end JointDebrisWitness

/- (g) LOADBEARING STABILITY non-vacuity. `dJunk` is a singleton debris (no cross edge, sources
    nothing); `c0` is loadbearing via a phantom it sources that the consumer `n0` needs. Removing `dJunk`
    from consumers leaves `c0` loadbearing both before and after - a genuine ↔ over the real
    `FixProto.loadbearing`, not a vacuous biconditional between two falses. -/
namespace LoadbearingStableWitness
inductive F3 | lbN | lbSrc | lbDeb
deriving DecidableEq
inductive P1 | lbP
open F3 P1

def crossC  : F3 → F3 → Prop := fun _ _ => False
def needsC  : F3 → P1 → Prop := fun f _ => f = lbN
def sourcesC : F3 → P1 → Prop := fun f _ => f = lbSrc
def consumersC : FSet F3 := fun _ => True

theorem dJunk_no_cross : ∀ b, b ≠ lbDeb → ¬ crossC lbDeb b := by
  intro b _ h; exact h
theorem dJunk_no_share : ∀ P, needsC lbDeb P → ∀ s, s ≠ lbDeb → ¬ sourcesC s P := by
  intro P hneed s _ _
  exact absurd (show lbDeb = lbN from hneed) (by decide)

theorem concrete_loadbearing_stable :
    FixProto.loadbearing needsC sourcesC crossC consumersC lbSrc
      ↔ FixProto.loadbearing needsC sourcesC crossC (FixProto.without consumersC lbDeb) lbSrc :=
  FixProto.loadbearing_stable needsC sourcesC crossC consumersC
    dJunk_no_cross dJunk_no_share (b := lbSrc) (by decide)

theorem concrete_c0_loadbearing : FixProto.loadbearing needsC sourcesC crossC consumersC lbSrc :=
  Or.inr ⟨lbP, rfl, ⟨lbN, trivial, rfl⟩⟩

#print axioms concrete_loadbearing_stable
#print axioms concrete_c0_loadbearing
end LoadbearingStableWitness

-- Generic content-safety + monotonicity + stability lemmas (the theorems the witnesses instantiate):
#print axioms Orphan.content_safe_post_debris
#print axioms Orphan.c5_demote_no_loss
#print axioms Orphan.residue_grows_on_shrink
#print axioms Orphan.nonzero_residue_survives_shrink
#print axioms FixProto.loadbearing_stable
#print axioms Family.singleton_d_no_cross
#print axioms Family.singleton_d_no_share
