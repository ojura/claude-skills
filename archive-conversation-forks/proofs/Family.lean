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
  BRIDGE LEMMAS for the loadbearing-stability fact (`FixProto.loadbearing_stable`). A SINGLETON-tree
  file `d` (the committed `len(ks)==1` guard, i.e. `∀ y≠d, ¬ SameTree d y`) satisfies the two non-coupling
  hypotheses `loadbearing_stable` needs, because the very edges that would couple `loadbearing` to `d` - a
  cross-uuid reference, or a needer and a source sharing a phantom lpu - are exactly the edges union-find
  merges on, so a singleton has none. `noEdges_sameTree_eq` is NOT used: this is a LOCAL fact about `d`.
  ============================================================================================
-/

/-- The real guard supplies the STRONGER `¬ SameTree`; that implies the weaker `¬ Edge` the stability
    proof needs, via `SameTree.base`. -/
theorem notSameTree_notEdge {a b : File}
    (h : ¬ SameTree lref owns refUuid a b) : ¬ Edge lref owns refUuid a b :=
  fun he => h (SameTree.base he)

/-- Cross half: a singleton `d` is no OTHER file's cross-target source. A `crossF d b` edge is a dep edge
    (`refUuid d u ∧ owns b u`, the middle `Edge` disjunct), so it would put `d` and `b` in one tree,
    contradicting the singleton guard. -/
theorem singleton_d_no_cross
    {crossF : File → File → Prop} {d : File}
    (cross_is_dep : ∀ a b, crossF a b → ∃ u, refUuid a u ∧ owns b u)
    (hsingleton : ∀ y, y ≠ d → ¬ SameTree lref owns refUuid d y) :
    ∀ b, b ≠ d → ¬ crossF d b := by
  intro b hbd hcr
  obtain ⟨u, hru, hou⟩ := cross_is_dep d b hcr
  exact (hsingleton b hbd) (SameTree.base (Or.inr (Or.inl ⟨u, hru, hou⟩)))

/-- Phantom half: a singleton `d` shares no needed phantom with any other file. If `d` needs `P` and
    `s ≠ d` sources `P`, both carry `P` in `lref`, so `needer_source_coTree` puts them in one tree,
    contradicting the singleton guard. -/
theorem singleton_d_no_share
    {needsF sourcesF : File → Lpu → Prop} {d : File}
    (needs_refs : ∀ f P, needsF f P → lref f P)
    (src_refs   : ∀ f P, sourcesF f P → lref f P)
    (hsingleton : ∀ y, y ≠ d → ¬ SameTree lref owns refUuid d y) :
    ∀ P, needsF d P → ∀ s, s ≠ d → ¬ sourcesF s P := by
  intro P hneed s hsd hsrc
  exact (hsingleton s hsd)
    (needer_source_coTree lref owns refUuid (needs_refs d P hneed) (src_refs s P hsrc))

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

/--
  DEBRIS ⊆ CANONICALS - the structural fact the debris-demotion safety rests on, PROVED not assumed.
  `nominate_debris` runs only inside `for ks in trees.values(): if len(ks)!=1: continue; k=ks[0]`, so a
  nominated file is the SOLE member of its tree. The canonical of a singleton tree is that member,
  whether or not it is `is_debris`: if `is_debris k` the floored `cand` filter is empty and falls back
  to `ks=[k]` (the `[] => ks` arm); if not, `[k]` already passes the floor - either way
  `canonicalPick debris [k] = some k`. So every debris-nominated file is its own tree's canonical, hence
  in `canonicals = {canonical(ks) | ks ∈ trees}`. (The marker carry-over in `Markers.lean` rests on
  exactly this; here it is discharged off `canonicalPick`, not taken as a hypothesis.)
-/
theorem singleton_canonicalPick (k : File) : canonicalPick debris [k] = some k := by
  unfold canonicalPick cand
  by_cases h : debris k <;> simp [List.filter, List.head?, h]

/--
  On a SINGLETON tree `[k]`, `canonicalPick debris [k] = some k` matches the committed `canonical([k]) =
  k` exactly - which is all `debris_nominated_canonical` needs: a debris-nominated file's tree is the
  singleton `[k]` (the `len(ks)==1` guard), so its tree-canonical is itself, giving `debris ⊆ canonicals`.
  NOTE the scope: `canonicalPick` takes the HEAD of the floored candidates, not the max-key element. On a
  MULTI-file tree the committed `canonical(ks) = max(cand, key=...)` is modelled by
  `Family.Canon.canonicalByKey` (proved to select a maximal key), NOT by `canonicalPick`; head and max
  coincide only on singletons, which is the case this lemma is scoped to. -/
theorem debris_nominated_canonical (treeOf : File → List File) {k : File}
    (hsingleton : treeOf k = [k]) :
    canonicalPick debris (treeOf k) = some k := by
  rw [hsingleton]; exact singleton_canonicalPick debris k


/-
  ============================================================================================
  CANONICAL MAX-KEY SELECTION (#4, canonical part). `canonical_mem` / `canonical_nondebris` above
  prove canonical returns a member and respects the floor. This section proves the actual SELECTION:
  the committed `max(cand, key=(distinct, lts, uuid))` picks an element whose key is MAXIMAL among the
  floored candidates. The key's three components are modelled as the scalars the code compares - all
  `Nat` (distinct-count = set size; `norm()` = a sortable instant; uuid = hex number). Computing those
  scalars from fingerprints / parsing / bytes is the Python boundary; the LEX SELECTION is exact here.
  Core Lean, no mathlib (core `Nat` has the full order theory; core `String` lacks the order lemmas,
  which is why the components are modelled as their compared scalars rather than raw strings).
  ============================================================================================
-/
namespace Canon

abbrev Key := Nat × Nat × Nat

/-- Lexicographic strict-less on the triple. -/
def klt (a b : Key) : Prop :=
  a.1 < b.1 ∨ (a.1 = b.1 ∧ (a.2.1 < b.2.1 ∨ (a.2.1 = b.2.1 ∧ a.2.2 < b.2.2)))

/-- Lexicographic ≤ (the negation-complete companion). -/
def kle (a b : Key) : Prop := klt a b ∨ a = b

instance : DecidablePred (fun p : Key × Key => klt p.1 p.2) := fun _ => inferInstanceAs (Decidable (_ ∨ _))
instance (a b : Key) : Decidable (klt a b) := inferInstanceAs (Decidable (_ ∨ _))

/-- Totality: klt a b, a = b, or klt b a. -/
theorem klt_tri (a b : Key) : klt a b ∨ a = b ∨ klt b a := by
  unfold klt
  obtain ⟨a1, a2, a3⟩ := a; obtain ⟨b1, b2, b3⟩ := b
  rcases Nat.lt_trichotomy a1 b1 with h1 | h1 | h1
  · exact Or.inl (Or.inl h1)
  · subst h1
    rcases Nat.lt_trichotomy a2 b2 with h2 | h2 | h2
    · exact Or.inl (Or.inr ⟨rfl, Or.inl h2⟩)
    · subst h2
      rcases Nat.lt_trichotomy a3 b3 with h3 | h3 | h3
      · exact Or.inl (Or.inr ⟨rfl, Or.inr ⟨rfl, h3⟩⟩)
      · subst h3; exact Or.inr (Or.inl rfl)
      · exact Or.inr (Or.inr (Or.inr ⟨rfl, Or.inr ⟨rfl, h3⟩⟩))
    · exact Or.inr (Or.inr (Or.inr ⟨rfl, Or.inl h2⟩))
  · exact Or.inr (Or.inr (Or.inl h1))

/-- kle is total. -/
theorem kle_total (a b : Key) : kle a b ∨ kle b a := by
  rcases klt_tri a b with h | h | h
  · exact Or.inl (Or.inl h)
  · exact Or.inl (Or.inr h)
  · exact Or.inr (Or.inl h)

/-- klt is transitive. -/
theorem klt_trans {a b c : Key} (hab : klt a b) (hbc : klt b c) : klt a c := by
  unfold klt at *
  obtain ⟨a1,a2,a3⟩ := a; obtain ⟨b1,b2,b3⟩ := b; obtain ⟨c1,c2,c3⟩ := c
  rcases hab with h | ⟨e1, h⟩ <;> rcases hbc with h' | ⟨e1', h'⟩
  · exact Or.inl (Nat.lt_trans h h')
  · subst e1'; exact Or.inl h
  · subst e1; exact Or.inl h'
  · subst e1; subst e1'
    refine Or.inr ⟨rfl, ?_⟩
    rcases h with h2 | ⟨e2, h⟩ <;> rcases h' with h2' | ⟨e2', h'⟩
    · exact Or.inl (Nat.lt_trans h2 h2')
    · subst e2'; exact Or.inl h2
    · subst e2; exact Or.inl h2'
    · subst e2; subst e2'; exact Or.inr ⟨rfl, Nat.lt_trans h h'⟩

/-- kle is transitive (needed for argmax). -/
theorem kle_trans {a b c : Key} (hab : kle a b) (hbc : kle b c) : kle a c := by
  rcases hab with hab | hab
  · rcases hbc with hbc | hbc
    · exact Or.inl (klt_trans hab hbc)
    · subst hbc; exact Or.inl hab
  · subst hab; exact hbc

/-- ¬ klt a b → kle b a (from trichotomy). -/
theorem kle_of_not_klt {a b : Key} (h : ¬ klt a b) : kle b a := by
  rcases klt_tri a b with h' | h' | h'
  · exact absurd h' h
  · exact Or.inr h'.symm
  · exact Or.inl h'

/-- argmax over a key function, by left fold (matches `max(cand, key=...)`). -/
def argmax (key : File → Key) : File → List File → File
  | best, []      => best
  | best, x :: xs => argmax key (if klt (key best) (key x) then x else best) xs

/-- the running best's key only grows (kle seed (argmax)). -/
theorem argmax_ge_seed (key : File → Key) :
    ∀ (l : List File) (best : File), kle (key best) (key (argmax key best l)) := by
  intro l
  induction l with
  | nil => intro best; exact Or.inr rfl
  | cons x xs ih =>
      intro best
      simp only [argmax]
      by_cases h : klt (key best) (key x)
      · simp only [h, if_true]; exact kle_trans (Or.inl h) (ih x)
      · simp only [h, if_false]; exact ih best

/-- argmax's key is ≥ every element's key: canonical picks a MAXIMAL-key element. -/
theorem argmax_ge_mem (key : File → Key) :
    ∀ (l : List File) (best y : File), y ∈ l → kle (key y) (key (argmax key best l)) := by
  intro l
  induction l with
  | nil => intro _ y hy; exact absurd hy (List.not_mem_nil y)
  | cons x xs ih =>
      intro best y hy
      simp only [argmax]
      cases List.mem_cons.mp hy with
      | inl hyx =>
          subst hyx
          by_cases h : klt (key best) (key y)
          · simp only [h, if_true]; exact argmax_ge_seed key xs y
          · simp only [h, if_false]
            exact kle_trans (kle_of_not_klt h) (argmax_ge_seed key xs best)
      | inr hyxs =>
          by_cases h : klt (key best) (key x)
          · simp only [h, if_true]; exact ih x y hyxs
          · simp only [h, if_false]; exact ih best y hyxs

/-- argmax returns either the seed or a list element: it is a member of `best :: l`. Proved via the
    cleaner intermediate "= seed ∨ ∈ l". -/
theorem argmax_eq_or_mem (key : File → Key) :
    ∀ (l : List File) (best : File), argmax key best l = best ∨ argmax key best l ∈ l := by
  intro l
  induction l with
  | nil => intro best; exact Or.inl rfl
  | cons x xs ih =>
      intro best
      simp only [argmax]
      by_cases h : klt (key best) (key x)
      · simp only [h, if_true]
        rcases ih x with he | hm
        · exact Or.inr (by rw [he]; exact List.mem_cons_self x xs)
        · exact Or.inr (List.mem_cons_of_mem x hm)
      · simp only [h, if_false]
        rcases ih best with he | hm
        · exact Or.inl he
        · exact Or.inr (List.mem_cons_of_mem x hm)

theorem argmax_mem (key : File → Key) (l : List File) (best : File) :
    argmax key best l ∈ best :: l := by
  rcases argmax_eq_or_mem key l best with he | hm
  · rw [he]; exact List.mem_cons_self best l
  · exact List.mem_cons_of_mem best hm

variable (debris : File → Prop) [DecidablePred debris]

/-- canonical, modelling the committed `max(cand, key=...)`: arg-max over the floored candidates. -/
def canonicalByKey (key : File → Key) (ks : List File) : Option File :=
  match cand debris ks with
  | []      => Option.none
  | c :: cs => some (argmax key c cs)

/-- THE SELECTION PROPERTY: every floored candidate's key is ≤ the chosen canonical's key, and the
    canonical is one of the floored candidates. So `canonical` genuinely picks a maximal-key element. -/
theorem canonicalByKey_is_max (key : File → Key) (ks : List File) {c : File}
    (hc : canonicalByKey debris key ks = some c) :
    c ∈ cand debris ks ∧ ∀ y ∈ cand debris ks, kle (key y) (key c) := by
  unfold canonicalByKey at hc
  cases hcd : cand debris ks with
  | nil => rw [hcd] at hc; exact absurd hc (by simp)
  | cons d ds =>
      rw [hcd] at hc
      simp only [Option.some.injEq] at hc; subst hc
      refine ⟨?_, ?_⟩
      · -- argmax is a member of (d :: ds)
        have : argmax key d ds ∈ (d :: ds) := by
          have hmem := argmax_mem key ds d
          exact hmem
        exact this
      · intro y hy
        -- every element of (d::ds) has key ≤ argmax's key
        cases List.mem_cons.mp hy with
        | inl hyd => subst hyd; exact argmax_ge_seed key ds y
        | inr hys => exact argmax_ge_mem key ds d y hys

end Canon


/-
  ============================================================================================
  PATH COMPRESSION (the `find` optimisation, "for fun"). The union-find partition is formalised AS the
  `SameTree` equivalence above; the imperative `find` adds path compression (repoint visited nodes
  straight at the root) as a speed optimisation. Here we show compression PRESERVES the computed
  component: repointing a node `v` at its root `r` leaves every root a root and makes `v` find `r`, so
  the optimised `find` computes the same answer as the naive one. Core Lean, no mathlib.
  ============================================================================================
-/
namespace Compress

variable {Node : Type} [DecidableEq Node]

/-- root via fuel-bounded iteration of `parent` (fuel = an upper bound on chain length). -/
def root (parent : Node → Node) : Nat → Node → Node
  | 0,      x => x
  | fuel+1, x => let p := parent x; if p = x then x else root parent fuel p

/-- A node is a root if it is its own parent. -/
def isRoot (parent : Node → Node) (x : Node) : Prop := parent x = x

/-- Compression step: repoint `v` directly at `r` (its root). New parent map. -/
def compress (parent : Node → Node) (v r : Node) : Node → Node :=
  fun x => if x = v then r else parent x

/-- If `r` is a root and we repoint `v` to `r`, then `r` is still a root in the new map (r ≠ v case:
    its parent is unchanged; and we only repoint v). Needs r ≠ v (a root distinct from the compressed
    node), which holds when v is not already the root. -/
theorem compress_preserves_root_self (parent : Node → Node) {v r : Node}
    (hr : isRoot parent r) (hrv : r ≠ v) : isRoot (compress parent v r) r := by
  unfold isRoot compress
  rw [if_neg hrv]; exact hr

/-- KEY: after compressing `v` to its root `r`, computing root of `v` in the new map gives `r`
    (in one step, since v now points straight at r, and r is a root). -/
theorem root_compress_v (parent : Node → Node) {v r : Node}
    (hr : isRoot parent r) (hrv : r ≠ v) :
    root (compress parent v r) 2 v = r := by
  -- parent' v = r (v branch); parent' r = r (r ≠ v, unchanged, and r is a root). So root in ≤2 steps.
  have hpv : compress parent v r v = r := by unfold compress; rw [if_pos rfl]
  have hpr : compress parent v r r = r := by unfold compress; rw [if_neg hrv]; exact hr
  unfold root
  rw [hpv]
  by_cases h : r = v
  · exact absurd h hrv
  · rw [if_neg h]
    -- goal: root (compress..) 1 r = r ; unfold once more, parent' r = r.
    unfold root
    rw [hpr, if_pos rfl]

end Compress

end Family
