# Agent Report: Lean Proof

Scope: `archive-conversation-forks/proofs/*.lean` and `proofs/README.md`. Proof-prose and status-language issues stay in scope when they can hide hypotheses or make the implementation look more connected to Lean than it is. Required corrections name the best-known correctness repair.

## Findings

1. **High: the main safety theorem is not end-to-end.**

   [proofs/README.md:24](/home/juraj/claude-skills/archive-conversation-forks/proofs/README.md:24) calls `no_orphan_from_closed` unconditional, but [Fixpoint.lean:119](/home/juraj/claude-skills/archive-conversation-forks/proofs/Fixpoint.lean:119) still takes `IsClosed locked` and `locked <= consumers`; the debris-inclusive theorem also assumes a debris guard at [Fixpoint.lean:156](/home/juraj/claude-skills/archive-conversation-forks/proofs/Fixpoint.lean:156).

   **Required correction:** prove one pipeline-level theorem over the actual final picker set: closure loop output, `locked <= consumers`, C5, debris nomination, and the final archive set must all be defined and connected.

2. **High: recall preservation is not connected to the implementation precondition.**

   `preserved` and `missing` at [Orphan.lean:200](/home/juraj/claude-skills/archive-conversation-forks/proofs/Orphan.lean:200) and [Orphan.lean:207](/home/juraj/claude-skills/archive-conversation-forks/proofs/Orphan.lean:207) do not exclude `A`; `recall_no_loss` at [Orphan.lean:224](/home/juraj/claude-skills/archive-conversation-forks/proofs/Orphan.lean:224) inherits that. The implementation recall loop only considers files with `A not in KEPT`, so this is a proof-contract gap rather than direct evidence that the implementation self-witnesses.

   **Required correction:** define archive semantics and prove preservation against post-move survivors, then connect that theorem to the implementation precondition that recall candidates are not in `KEPT`. Self-witnessing should be unrepresentable in the proof contract.

3. **High: C5 content safety is assumed at the useful point.**

   `c5_demote_no_loss` at [Orphan.lean:291](/home/juraj/claude-skills/archive-conversation-forks/proofs/Orphan.lean:291) takes the per-message survivor condition directly; `FixProto.demoted` at [Fixpoint.lean:75](/home/juraj/claude-skills/archive-conversation-forks/proofs/Fixpoint.lean:75) uses an abstract file-level `residue`, not `residueOf`.

   **Required correction:** define C5 residue from fingerprints and post-debris kept files, prove zero residue implies per-message preservation, and connect that theorem to the C5 demotion predicate.

4. **High: debris safety is not derived from a defined debris nomination step.**

   `no_orphan_from_closed_debris` takes `debris_guard` as a hypothesis at [Fixpoint.lean:160](/home/juraj/claude-skills/archive-conversation-forks/proofs/Fixpoint.lean:160); `marker_range_excludes_debris` takes `debris <= canonicals` at [Markers.lean:132](/home/juraj/claude-skills/archive-conversation-forks/proofs/Markers.lean:132); `debris_nominated_canonical` still needs a singleton-tree premise at [Family.lean:229](/home/juraj/claude-skills/archive-conversation-forks/proofs/Family.lean:229).

   **Required correction:** formalize `nominate_debris` and prove `debris_guard`, singleton-tree canonical membership, marker exclusion, and no-orphan preservation from that definition.

5. **Medium: bounded closure is not proved equivalent to the unbounded closure used by the main theorem.**

   `ClosedB` quantifies only over listed files and phantoms at [Termination.lean:148](/home/juraj/claude-skills/archive-conversation-forks/proofs/Termination.lean:148); `closed_superset_exists_constructed` returns `ClosedB` at [Termination.lean:262](/home/juraj/claude-skills/archive-conversation-forks/proofs/Termination.lean:262), while the README says it coincides with the committed fixpoint condition at [proofs/README.md:311](/home/juraj/claude-skills/archive-conversation-forks/proofs/README.md:311).

   **Required correction:** prove `ClosedB -> FixProto.IsClosed` under explicit completeness invariants for file and phantom enumeration, then use that bridge in the pipeline theorem.

6. **Medium: the boundary proof still leaves the phantom set as an input.**

   `sourcesOf` and `needsOf` take `phantom : Phantom -> Prop` at [Boundary.lean:121](/home/juraj/claude-skills/archive-conversation-forks/proofs/Boundary.lean:121), while the README says the remaining gap is only JSONL-to-record extraction at [proofs/README.md:281](/home/juraj/claude-skills/archive-conversation-forks/proofs/README.md:281).

   **Required correction:** formalize `phantom = all_lpus - global_uuid` and connect that definition to `sourcesOf` and `needsOf`.

7. **Medium: marker classification compresses distinct operator judgments into one Bool.**

   `classify` takes one `substantive` input at [Markers.lean:218](/home/juraj/claude-skills/archive-conversation-forks/proofs/Markers.lean:218); the README admits this folds two calls together at [proofs/README.md:82](/home/juraj/claude-skills/archive-conversation-forks/proofs/README.md:82).

   **Required correction:** model the implementation's actual marker judgment structure, with separate inputs if the code makes separate calls, then prove totality and exclusivity for that structure.

8. **Medium: path-compression correctness is overstated.**

   The README claims component preservation at [proofs/README.md:345](/home/juraj/claude-skills/archive-conversation-forks/proofs/README.md:345), but Lean proves only local facts about `r` and `v` at [Family.lean:443](/home/juraj/claude-skills/archive-conversation-forks/proofs/Family.lean:443).

   **Required correction:** prove component preservation for all nodes under union-find parent invariants, and connect executable `find` behavior to `SameTree`.

9. **Medium-low: the axiom audit misses a named theorem using choice.**

   [Check.lean:9](/home/juraj/claude-skills/archive-conversation-forks/proofs/Check.lean:9) says the file is axiom-clean except canonical lemmas, but `witness_is_f1` uses `Classical.choose` at [Check.lean:123](/home/juraj/claude-skills/archive-conversation-forks/proofs/Check.lean:123).

   **Required correction:** make axiom auditing exhaustive and mechanical for every declaration meant to support the proof report, and fail the audit on unapproved axioms. Remove or quarantine witness-only choice lemmas. Codex separately verified the Docker build with Lean 4.10.0.

10. **Medium-low: the README's confidence language hides proof obligations.**

    Phrases like "THE result," "unconditional," "fully-wired," and "assumes nothing the code does not establish" at [proofs/README.md:27](/home/juraj/claude-skills/archive-conversation-forks/proofs/README.md:27), [proofs/README.md:147](/home/juraj/claude-skills/archive-conversation-forks/proofs/README.md:147), and [proofs/README.md:191](/home/juraj/claude-skills/archive-conversation-forks/proofs/README.md:191) make the proof look more connected to the implementation than it is.

    This is methodological, not stylistic: the risk is that an operator trusts a proof boundary that Lean has not crossed.

    **Required correction:** rewrite the README as a proof contract: theorem name, exact hypotheses, exact conclusion, supplied-by-code fact, trust boundary, and audit status. General confidence prose should be replaced by checkable proof-boundary structure.
