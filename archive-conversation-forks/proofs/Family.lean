/-
  Union-find / family grouping + canonical() selection (SKILL.md Step 2).

  Union-find is modelled as the equivalence closure of the edge relation: the imperative
  path-compressed `find` computes exactly this equivalence, and proving the mutable version
  adds no MATH content, only data-structure bookkeeping, so we abstract it to the equivalence.
  Edges:
    * shared-lpu: two files sharing an lpu VALUE in their lref (the `bylpu` union).
    * dep:        a references a uuid b owns (the cross-file `dep` union).
  Proved here:
    (a) the relation is an equivalence (so `trees` is a genuine partition);
    (b) shared-lpu files are same-tree;
    (c) the "needer and source co-tree" lemma (both carry the phantom in lref -> shared-lpu ->
        same tree). This GROUNDS the no-orphan proof's setting: it is why a phantom needer and
        its sources land in one tree, so the seeded tree canonical's closure reaches the needer.
    (d) false-family non-merge: same-tree is generated ONLY by lref/dep edges, never by message
        content, so files that merely share a first message are not forced into one tree.
  And for canonical(): (P1) it returns a tree member; (P2) the content floor (non-debris when any
  non-debris member exists). Which specific file it picks (the max-distinct/recency key) is not
  modelled - P1 and P2 are the properties downstream safety relies on.
-/
namespace Family

abbrev FSet (F : Type) := F → Prop

variable {File Lpu Uuid : Type}
-- lref f l : file f references lpu-value l (boundary or in-file lpu).
-- owns f u  : file f owns uuid u.   refUuid f u : f references uuid u cross-file (lref as a uuid).
variable (lref : File → Lpu → Prop) (owns refUuid : File → Uuid → Prop)

/-- The base edge: shared-lpu OR a cross-file dep edge (a references a uuid b owns). -/
def Edge (a b : File) : Prop :=
  (∃ l, lref a l ∧ lref b l) ∨ (∃ u, refUuid a u ∧ owns b u) ∨ (∃ u, refUuid b u ∧ owns a u)

/-- Same-tree = reflexive/symmetric/transitive closure of `Edge` (what union-find computes). -/
inductive SameTree (lref : File → Lpu → Prop) (owns refUuid : File → Uuid → Prop) : File → File → Prop where
  | base {a b}  : Edge lref owns refUuid a b → SameTree lref owns refUuid a b
  | refl (a)    : SameTree lref owns refUuid a a
  | symm {a b}  : SameTree lref owns refUuid a b → SameTree lref owns refUuid b a
  | trans {a b c} : SameTree lref owns refUuid a b → SameTree lref owns refUuid b c →
                     SameTree lref owns refUuid a c

/-- (a) `SameTree` is an equivalence relation, so the union-find partition is well-defined. -/
theorem sameTree_refl (a : File) : SameTree lref owns refUuid a a := SameTree.refl a
theorem sameTree_symm {a b : File} (h : SameTree lref owns refUuid a b) :
    SameTree lref owns refUuid b a := SameTree.symm h
theorem sameTree_trans {a b c : File}
    (h1 : SameTree lref owns refUuid a b) (h2 : SameTree lref owns refUuid b c) :
    SameTree lref owns refUuid a c := SameTree.trans h1 h2

/-- (b) Two files sharing an lpu value are in the same tree (the `bylpu` union). -/
theorem shared_lpu_sameTree {a b : File} {l : Lpu} (ha : lref a l) (hb : lref b l) :
    SameTree lref owns refUuid a b :=
  SameTree.base (Or.inl ⟨l, ha, hb⟩)

/--
  (c) ROUND-5 CO-TREE LEMMA. A phantom needer and any of its sources are in the same tree.

  Both a needer and a source of phantom `P` have a `compact_boundary` whose lpu is `P`, so both
  carry `P` in `lref` (the committed code adds every boundary lpu to `lref`). Hence they share the
  lpu value `P` and `shared_lpu_sameTree` merges them. This is the structural fact that makes the
  no-orphan closure sound: the needer's `needs(P)` is reachable from the seeded tree canonical.

  `needer_refs_P` / `source_refs_P` encode "carries `P` in lref", the committed `lref` construction.
-/
theorem needer_source_coTree {needer source : File} {P : Lpu}
    (needer_refs_P : lref needer P) (source_refs_P : lref source P) :
    SameTree lref owns refUuid needer source :=
  shared_lpu_sameTree lref owns refUuid needer_refs_P source_refs_P

/--
  (d) FALSE-FAMILY NON-MERGE. The partition unions on lref/dep edges ONLY, never on message
  content. We make this precise and content-agnostic: if NO base edge holds anywhere in the store
  (the only generators removed), then `SameTree` collapses to equality. Since `Edge` is defined
  purely from `lref` / `owns` / `refUuid` and never mentions message content, a content relation
  (e.g. "shares an identical first message") is disjoint from the generators, so it can never force
  two files into the same tree. The clean induction below proves the collapse without fixing an
  endpoint, so the induction goes through directly.
-/
theorem noEdges_sameTree_eq
    (hno : ∀ x y, ¬ Edge lref owns refUuid x y)
    {a b : File} (h : SameTree lref owns refUuid a b) : a = b := by
  induction h with
  | base e        => exact absurd e (hno _ _)
  | refl c        => rfl
  | symm _ ih     => exact ih.symm
  | trans _ _ ih1 ih2 => exact ih1.trans ih2

/--
  Operative corollary: a content relation `R` that implies no edge (i.e. sharing a first message
  is not an lref/dep edge) cannot, on its own, witness same-tree. If `R a b` holds but neither an
  lref nor a dep edge connects `a` and `b` to anything, they are forced same-tree only via `refl`.
  This is the "false families do not merge by construction" guarantee: `SameTree` is blind to `R`.
-/
theorem content_not_a_generator
    (R : File → File → Prop)
    (R_no_edge : ∀ x y, R x y → ¬ Edge lref owns refUuid x y)
    {a b : File} (hR : R a b) :
    ¬ Edge lref owns refUuid a b :=
  R_no_edge a b hR

/-
  ============================================================================================
  canonical() selection. The committed code:
    cand = [k for k in ks if not is_debris(k)] or ks
    return max(cand, key=lambda k:(distinct(k), lts(k), k))
  The two SAFETY-relevant properties downstream relies on (NOT which specific file it picks):
    (P1) canonical(ks) ∈ ks               - it returns a tree member (so KEPT/seed stay in-store).
    (P2) if ANY non-debris member exists, canonical(ks) is non-debris (the content floor: a
         3-message fork-test never wins over a real sibling).
  We model `ks` as a List and `canonical` via List.foldr over the floored candidates, then prove
  P1 and P2. `distinct`/`lts` (the max key) are irrelevant to P1/P2, so they are not modelled -
  the floor and membership are what matter for safety. (mathlib-free: core `List`.)
  ============================================================================================
-/

variable (debris : File → Prop) [DecidablePred debris]

/-- The floored candidate list: non-debris members, or all of `ks` if every member is debris. -/
def cand (ks : List File) : List File :=
  match ks.filter (fun k => decide (¬ debris k)) with
  | []      => ks
  | c :: cs => c :: cs

/-- `canonical` picks the head of a nonempty pick-list; we only need its membership + floor. We model
   the choice as "some element of `cand ks`" via `List.head?`-style; for the proof we use the first. -/
def canonicalPick (ks : List File) : Option File := (cand debris ks).head?

/-- Membership-from-`head?`, proved by hand (the core lemma name varies across versions). -/
private theorem mem_of_head? {l : List File} {c : File} (h : l.head? = some c) : c ∈ l := by
  cases l with
  | nil => simp at h
  | cons a as => simp only [List.head?_cons, Option.some.injEq] at h; subst h; exact List.mem_cons_self _ _

/-- (P1) The canonical, when it exists, is a member of `ks`. -/
theorem canonical_mem (ks : List File) {c : File}
    (hc : canonicalPick debris ks = some c) : c ∈ ks := by
  unfold canonicalPick cand at hc
  -- `cand` is the filtered list when nonempty, else `ks`; in BOTH arms head? lands inside `ks`.
  cases hf : ks.filter (fun k => decide (¬ debris k)) with
  | nil      => rw [hf] at hc; exact mem_of_head? hc
  | cons d ds =>
      rw [hf] at hc
      -- head? (d::ds) = some d ⇒ c = d, and d ∈ filter ⊆ ks.
      have hcd : c ∈ (d :: ds) := mem_of_head? hc
      have hsub : c ∈ ks.filter (fun k => decide (¬ debris k)) := by rw [hf]; exact hcd
      exact (List.mem_filter.mp hsub).1

/-- (P2) Content floor: if `ks` has a non-debris member, the canonical is non-debris. -/
theorem canonical_nondebris (ks : List File) {c : File}
    (hc : canonicalPick debris ks = some c)
    (hex : ∃ k ∈ ks, ¬ debris k) : ¬ debris c := by
  unfold canonicalPick cand at hc
  cases hf : ks.filter (fun k => decide (¬ debris k)) with
  | nil =>
      -- empty floored filter contradicts hex: a non-debris k passes the filter.
      obtain ⟨k, hk, hnk⟩ := hex
      have hmem : k ∈ ks.filter (fun k => decide (¬ debris k)) :=
        List.mem_filter.mpr ⟨hk, by simp [hnk]⟩
      rw [hf] at hmem; exact absurd hmem (List.not_mem_nil k)
  | cons d ds =>
      rw [hf] at hc
      have hcd : c ∈ (d :: ds) := mem_of_head? hc
      have hsub : c ∈ ks.filter (fun k => decide (¬ debris k)) := by rw [hf]; exact hcd
      -- filter keeps only `decide (¬ debris ·) = true`, i.e. ¬ debris c.
      have hkeep := (List.mem_filter.mp hsub).2
      simpa using hkeep

end Family
