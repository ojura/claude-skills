import Fixpoint

/-
  BOUNDARY LAYER: the par/nb reconstruction of `needs` / `sources`, machine-checked.

  In `Orphan.lean` the predicates `needs` / `sources : File → Phantom → Prop` are OPAQUE
  (`variable`s): the structural theorems hold for ANY needs/sources. This file PROMOTES them to
  their committed definitions - the set-builder the skill actually runs over each file's
  `compact_boundary` records (SKILL.md Step 2):

      def sources(k): return {lp for (lp,par,nb) in bnd[k] if lp in phantom and (par is not None or nb>0)}
      def needs(k):   return {lp for (lp,par,nb) in bnd[k] if lp in phantom and par is None and nb==0}

  This is the SAME move `Fixpoint.lean` makes for `loadbearing` / `demoted`: replace an opaque
  predicate with its actual definition, one layer deeper. The payoff is the par/nb CLASSIFICATION,
  which is exactly where the code has been miswritten before - the `par is not None`-only form that
  dropped the `nb>0` half, silently turning a backfill SOURCE into a NEEDER (the orphan-causing
  direction). Here that classification is proved a TOTAL, MUTUALLY-EXCLUSIVE partition per boundary
  record (`rec_iff` / `rec_excl` / `rec_total`, and the 2-bit cube `bit_partition` closed by `decide`,
  paralleling `Markers.classify_exhaustive`), and the historical bug is exhibited as a machine-checked
  divergence (`lazy_flips_source_to_need`).

  SCOPE (honest). This proves the LOGIC of needs/sources GIVEN the boundary records `(lpu, par, nb)`.
  It does NOT prove the JSONL -> records parse (`o.get("logicalParentUuid")`, `parentUuid`, the
  msgs-before count) - that byte-level extraction is the model-to-code boundary, fuzz-checked only.
  So the gap on needs/sources shrinks from "all of the par/nb layer" to "only the triple extraction".

  FAITHFULNESS of the `par` model. The committed predicate's ONLY use of `par` is `par is not None`,
  a Boolean observation; so modelling `par` as a `Bool` (`parPresent`) is EXACT, not a simplification.
  The `phantom` set (`lp in phantom`) stays an input predicate here - it is a separate set-difference
  (`all_lpus - global_uuid`), not the par/nb logic, and is itself promotable the same way.
-/

namespace Boundary

/--
  A `compact_boundary` record, post-parse. Faithful to the Python triple `(lp, par, nb)`:
  * `lpu`        : the `logicalParentUuid` on this boundary.
  * `parPresent` : whether `parentUuid` is non-null (`par is not None`) - the ONLY thing the committed
                   predicate reads off `par`, so a `Bool` captures it exactly.
  * `nb`         : number of in-file messages before this boundary (`n` in the code).
-/
structure Bdy (Phantom : Type) where
  lpu : Phantom
  parPresent : Bool
  nb : Nat

variable {Phantom : Type}

/-- A record is a backfill SOURCE candidate iff it has pre-content: a non-null parent OR ≥1 message
    before it (`par is not None or nb>0`). -/
def SourceRec (b : Bdy Phantom) : Prop := b.parPresent = true ∨ 0 < b.nb

/-- A record is a NEED iff it sits at the chain root with no pre-content (`par is None and nb==0`). -/
def NeedRec (b : Bdy Phantom) : Prop := b.parPresent = false ∧ b.nb = 0

instance (b : Bdy Phantom) : Decidable (SourceRec b) := by unfold SourceRec; infer_instance
instance (b : Bdy Phantom) : Decidable (NeedRec b) := by unfold NeedRec; infer_instance

/--
  PER-RECORD PARTITION (the crux). `NeedRec` is the EXACT negation of `SourceRec`: every boundary
  record is a source XOR a need. This is the identity the `par is not None`-only bug violated.
  Constructive (no `Classical`): `SourceRec` is decidable and the `nb` case-split is `Nat.eq_zero_or_pos`.
-/
theorem rec_excl (b : Bdy Phantom) : ¬ (SourceRec b ∧ NeedRec b) := by
  rintro ⟨hsrc, hpar, hnb⟩
  rcases hsrc with hp | hpos
  · exact Bool.noConfusion (hp ▸ hpar)          -- parPresent = true vs = false
  · exact Nat.lt_irrefl 0 (hnb ▸ hpos)          -- 0 < nb vs nb = 0

theorem rec_total (b : Bdy Phantom) : SourceRec b ∨ NeedRec b := by
  rcases Nat.eq_zero_or_pos b.nb with hz | hpos
  · cases hp : b.parPresent with
    | true  => exact Or.inl (Or.inl hp)
    | false => exact Or.inr ⟨hp, hz⟩
  · exact Or.inl (Or.inr hpos)

theorem rec_iff (b : Bdy Phantom) : NeedRec b ↔ ¬ SourceRec b :=
  ⟨fun hn hs => rec_excl b ⟨hs, hn⟩, fun hns => (rec_total b).resolve_left hns⟩

/-
  The 2-bit mechanical core, mirroring `Markers.classify`/`classify_exhaustive`. `srcBit`/`needBit`
  are the Boolean transcription of the two record conditions on the only two bits the predicate reads:
  `p` = (par is not None) = `parPresent`, `q` = (nb > 0). `bit_partition` is the same partition as
  `rec_iff`, stated over the Bool cube and closed by `decide` (axiom-free).
-/
def srcBit (p q : Bool) : Bool := p || q
def needBit (p q : Bool) : Bool := (!p) && (!q)

theorem bit_partition (p q : Bool) : needBit p q = !(srcBit p q) := by
  cases p <;> cases q <;> rfl

/-- The lazy `par is not None`-only source test (the round-5 bug: drops the `nb>0` half). -/
def SourceLazy (b : Bdy Phantom) : Prop := b.parPresent = true
/-- Its complement (the lazy `par is None`-only need test, dropping the `nb==0` half). -/
def NeedLazy (b : Bdy Phantom) : Prop := b.parPresent = false

/--
  THE ROUND-5 BUG, machine-checked. A boundary with a NULL parent but real pre-content
  (`parPresent = false, nb = 1`) IS a backfill SOURCE under the committed test (`SourceRec`, via the
  `nb>0` half). The lazy `par is not None`-only test (`SourceLazy`) drops it - and its complement
  (`NeedLazy`) FILES IT AS A NEED. That is the exact orphan-causing flip: a genuine source the kept
  session backfills from gets re-read as a needer, so it can be archived and the kept session's deep
  origin is orphaned. Previously a fuzz observation; here a checked proposition.
-/
theorem lazy_flips_source_to_need (Q : Phantom) :
    SourceRec (⟨Q, false, 1⟩ : Bdy Phantom)        -- committed test: a SOURCE (nb > 0)
    ∧ ¬ SourceLazy (⟨Q, false, 1⟩ : Bdy Phantom)   -- lazy par-only test: NOT a source
    ∧ NeedLazy (⟨Q, false, 1⟩ : Bdy Phantom) :=    -- lazy complement: a NEED  ← the orphan flip
  ⟨Or.inr Nat.one_pos, fun h => Bool.noConfusion h, rfl⟩

/-
  RELATION LEVEL: the committed set-builder, as `File → Phantom → Prop` relations over a per-file
  boundary list `bnd`. These are the values that, in `Orphan.lean` / `Fixpoint.lean`, were the opaque
  `needs` / `sources`.
-/
variable {File : Type}

/-- `sources(k)` VERBATIM: phantom lpus `k` can backfill (some record of `k` for that phantom is a
    `SourceRec`). -/
def sourcesOf (phantom : Phantom → Prop) (bnd : File → List (Bdy Phantom)) (k : File) (Q : Phantom) : Prop :=
  ∃ b ∈ bnd k, b.lpu = Q ∧ phantom Q ∧ SourceRec b

/-- `needs(k)` VERBATIM: phantom lpus `k` relies on a sibling for (some record of `k` for that phantom
    is a `NeedRec`). -/
def needsOf (phantom : Phantom → Prop) (bnd : File → List (Bdy Phantom)) (k : File) (Q : Phantom) : Prop :=
  ∃ b ∈ bnd k, b.lpu = Q ∧ phantom Q ∧ NeedRec b

/-- The relations are DECIDABLE (computable), as in the code (`set`-builder over a finite `bnd[k]`),
    given decidable phantom-membership and lpu-equality. -/
instance [DecidableEq Phantom] (phantom : Phantom → Prop) [DecidablePred phantom]
    (bnd : File → List (Bdy Phantom)) (k : File) (Q : Phantom) :
    Decidable (sourcesOf phantom bnd k Q) := by unfold sourcesOf; infer_instance
instance [DecidableEq Phantom] (phantom : Phantom → Prop) [DecidablePred phantom]
    (bnd : File → List (Bdy Phantom)) (k : File) (Q : Phantom) :
    Decidable (needsOf phantom bnd k Q) := by unfold needsOf; infer_instance

-- Honest scope of the partition at the relation level: by `rec_excl`, no SINGLE boundary record
-- witnesses both `needsOf` and `sourcesOf` for a phantom. At the FILE level a file MAY both need and
-- source the same phantom, but only via DISTINCT records (two boundaries for the same lpu) - so
-- `needsOf k Q ∧ sourcesOf k Q` is consistent (it just means `k` self-backfills), never a contradiction.

/--
  CAPSTONE: the unconditional no-orphan, over the COMMITTED par/nb relations. `needsOf` / `sourcesOf`
  replace the opaque `needs` / `sources`; since `FixProto.no_orphan_from_closed` is ∀-quantified over
  those relations, it holds verbatim for the boundary set-builder (this is instantiation, not a new
  proof obligation - the structural algebra was always generic over needs/sources). The point: the
  no-orphan guarantee - a kept session's needed phantom always retains a kept source - now applies to
  the relations the skill actually COMPUTES from `(lpu, par, nb)` records, not to opaque stand-ins.
  Mirrors the `Markers.*_wired` capstones (theorems restated over the defined predicates).
-/
theorem no_orphan_from_closed_bnd
    (phantom : Phantom → Prop) (bnd : File → List (Bdy Phantom))
    (cross : File → File → Prop)
    {locked kuf consumers residue : File → Prop}
    (hclosed : FixProto.IsClosed cross (needsOf phantom bnd) (sourcesOf phantom bnd) locked)
    (locked_subset_consumers : ∀ x, locked x → consumers x)
    {f : File} {P : Phantom}
    (hf : locked f ∧
        ¬ FixProto.demoted (needsOf phantom bnd) (sourcesOf phantom bnd) cross kuf consumers residue f)
    (hneed : needsOf phantom bnd f P)
    (hsrc : ∃ s, sourcesOf phantom bnd s P) :
    ∃ s, (locked s ∧
        ¬ FixProto.demoted (needsOf phantom bnd) (sourcesOf phantom bnd) cross kuf consumers residue s)
        ∧ sourcesOf phantom bnd s P :=
  FixProto.no_orphan_from_closed cross (needsOf phantom bnd) (sourcesOf phantom bnd)
    hclosed locked_subset_consumers hf hneed hsrc

end Boundary
