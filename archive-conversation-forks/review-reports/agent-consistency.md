# Agent Report: Cross-Artifact Consistency

Scope: mismatches across `SKILL.md`, `README.md`, `model/`, and `proofs/`. Wording issues stay in scope when they create inconsistent contracts across artifacts or make a proof/model boundary look stronger than it is. Required corrections name the best-known correctness repair.

## Findings

1. **High: exact containment is not exact as written.**

   The raw fingerprint is `md5(...).hexdigest()[:12]`, [SKILL.md:253](/home/juraj/claude-skills/archive-conversation-forks/SKILL.md:253), but the recall rule says a `0 unique` archive proves byte-identical message containment, [SKILL.md:560](/home/juraj/claude-skills/archive-conversation-forks/SKILL.md:560). Lean proves containment over an abstract `fingerprints` relation, not over truncated hashes.

   **Required correction:** replace truncated-hash identity with canonical preservation records containing every field needed for rendering, replay, restore, and audit. Hashes may be used only as indexes, with collision verification against the canonical record before any archive decision. Fields intentionally excluded from content identity, such as UUID or timestamp if excluded, must be named explicitly.

2. **High: the human-read safety gate is fed previews, not verbatim raw unique records.**

   `ftext` stores text-only display content and caps it at 2000 chars, [SKILL.md:260](/home/juraj/claude-skills/archive-conversation-forks/SKILL.md:260) and [SKILL.md:264](/home/juraj/claude-skills/archive-conversation-forks/SKILL.md:264). The procedure later says archive candidates record unique texts verbatim, from `ftext`, [SKILL.md:512](/home/juraj/claude-skills/archive-conversation-forks/SKILL.md:512), and relies on reads for C6 and Sonnet paths. Tool blocks and non-text content can disappear from the read surface.

   **Required correction:** make the archive-review artifact lossless: every unique raw record, including tool blocks and long content, must be available to the operator/Sonnet and recorded in audit output. Omitted or truncated records are hard stop conditions.

3. **High: load-bearing is used for incompatible concepts.**

   The prose says a load-bearing file can never be archived, [SKILL.md:103](/home/juraj/claude-skills/archive-conversation-forks/SKILL.md:103). The code defines `loadbearing` as all cross-file targets plus all viable phantom sources, [SKILL.md:421](/home/juraj/claude-skills/archive-conversation-forks/SKILL.md:421), then says redundant extra sources stay archivable, [SKILL.md:424](/home/juraj/claude-skills/archive-conversation-forks/SKILL.md:424). Step 3 equates load-bearing with `locked`, [SKILL.md:637](/home/juraj/claude-skills/archive-conversation-forks/SKILL.md:637).

   **Required correction:** redesign the state model across procedure, Python, Lean, and docs with explicit sets such as `required_to_keep`, `viable_source`, `selected_source`, and `archive_candidate`. Prove and test against those names. The shared invariant should be: every kept needer retains at least one kept source.

4. **High: the durable-artifact override is outside the verified pipeline.**

   Step 2 auto-keeps high-unique forks, [SKILL.md:390](/home/juraj/claude-skills/archive-conversation-forks/SKILL.md:390), and the Python model does the same, [model/step2_model.py:138](/home/juraj/claude-skills/archive-conversation-forks/model/step2_model.py:138). The skill later allows archiving such a fork if a durable artifact captures it, [SKILL.md:640](/home/juraj/claude-skills/archive-conversation-forks/SKILL.md:640). The proof README's archive-path list omits that path, [proofs/README.md:44](/home/juraj/claude-skills/archive-conversation-forks/proofs/README.md:44).

   **Required correction:** make artifact preservation a first-class archive path with a durable preservation witness: exact message IDs or raw records covered by the artifact, artifact path/commit, human approval, and restore/audit manifest. Model it as an explicit path with stated non-Lean semantic assumptions.

5. **High: JSONL parsing is described as fuzz-checked, but the model says it is not parsed.**

   The model README says it generates post-parse fields directly and does not parse JSONL, [model/README.md:29](/home/juraj/claude-skills/archive-conversation-forks/model/README.md:29). The proof README says the JSONL-to-record extraction remains fuzz-checked, [proofs/README.md:173](/home/juraj/claude-skills/archive-conversation-forks/proofs/README.md:173).

   **Required correction:** extract the JSONL parser into a shared implementation with fixtures and fuzz/property tests for `owned`, `lref`, `bnd`, timestamps, `sources`, and `needs`; make the model consume parser output.

6. **High: recall docs conflict on union containment versus single-file containment.**

   The recall code archives when `missing = set(A) - kept_union` is empty, [SKILL.md:542](/home/juraj/claude-skills/archive-conversation-forks/SKILL.md:542). `best` is only a reporting aid, [SKILL.md:543](/home/juraj/claude-skills/archive-conversation-forks/SKILL.md:543). The prose then says every message is in `best`, [SKILL.md:560](/home/juraj/claude-skills/archive-conversation-forks/SKILL.md:560).

   **Required correction:** make union containment produce an audit witness: for each archived raw message identity, record the kept file or files that preserve it. `best` must not be part of the safety predicate.

7. **High: title mutation contradicts the stated safety invariant.**

   Step 7 appends title records to retained dormant sessions, [SKILL.md:710](/home/juraj/claude-skills/archive-conversation-forks/SKILL.md:710) and [SKILL.md:741](/home/juraj/claude-skills/archive-conversation-forks/SKILL.md:741), while safety invariants say never move or file-edit locked/canonical/kept files, [SKILL.md:761](/home/juraj/claude-skills/archive-conversation-forks/SKILL.md:761).

   **Required correction:** split the invariant into "never move" for retained/locked/canonical files and "never file-edit live sessions." Title appends are allowed only through the Step-7 dormant, JSON-validated, mtime-neutral path.

8. **High: unsatisfiable phantom handling is under-specified.**

   `unsatisfiable` phantoms are only reported, [SKILL.md:361](/home/juraj/claude-skills/archive-conversation-forks/SKILL.md:361) and [SKILL.md:413](/home/juraj/claude-skills/archive-conversation-forks/SKILL.md:413), while post-move verification demands zero orphans, [SKILL.md:699](/home/juraj/claude-skills/archive-conversation-forks/SKILL.md:699).

   **Required correction:** make unsatisfiable phantoms a hard stop pending recovery, or make the verifier record a pre-move baseline and prove that the operation introduces no new orphans. The selected rule must be explicit before any move plan can be approved.

9. **Medium: the debris model is broader than the real debris classifier, but the README calls the pipeline exact.**

   The model README says the exact Step 2 pipeline is run, [model/README.md:18](/home/juraj/claude-skills/archive-conversation-forks/model/README.md:18). The model itself says it cannot model opener text and nominates any small singleton, [model/step2_model.py:171](/home/juraj/claude-skills/archive-conversation-forks/model/step2_model.py:171). The skill requires a throwaway opener, [SKILL.md:623](/home/juraj/claude-skills/archive-conversation-forks/SKILL.md:623).

   **Required correction:** factor debris classification into shared executable code used by both procedure and model, with explicit inputs for opener text and raw/distinct count behavior.

10. **Medium: Step 4-5 theming is claimed as fuzz-checked without a matching artifact.**

   [proofs/README.md:348](/home/juraj/claude-skills/archive-conversation-forks/proofs/README.md:348) says full family/theme assignment is fuzz-checked. The model scope is Step 2 set algebra, [model/README.md:18](/home/juraj/claude-skills/archive-conversation-forks/model/README.md:18), while Steps 4-5 are Sonnet theming and README generation, [SKILL.md:645](/home/juraj/claude-skills/archive-conversation-forks/SKILL.md:645).

   **Required correction:** define a testable theming contract covering completeness, no overlaps, tree/theme consistency, content-fork handling, archive-list consistency, and README required sections. Add a checker/fuzzer for that contract.

11. **Medium: source/need prose is narrower than the predicate proved in Lean.**

   The prose describes a source as having real messages before its boundary, [SKILL.md:116](/home/juraj/claude-skills/archive-conversation-forks/SKILL.md:116). The actual predicate is `parentUuid present OR nb > 0`, [SKILL.md:277](/home/juraj/claude-skills/archive-conversation-forks/SKILL.md:277), and Lean proves that per-record split, [proofs/Boundary.lean:78](/home/juraj/claude-skills/archive-conversation-forks/proofs/Boundary.lean:78).

   **Required correction:** make the `sources`/`needs` boundary classifier a single shared spec with executable tests and Lean examples for all `parPresent`/`nb` cases, then generate or mirror the prose from that spec.

12. **Medium: proof and model scope is spread across prose instead of one auditable coverage table.**

    The proof README has scattered caveats for C6, nonzero recall, debris, parser extraction, marker judgment, and canonical selection, for example [proofs/README.md:44](/home/juraj/claude-skills/archive-conversation-forks/proofs/README.md:44), [proofs/README.md:251](/home/juraj/claude-skills/archive-conversation-forks/proofs/README.md:251), and [proofs/README.md:273](/home/juraj/claude-skills/archive-conversation-forks/proofs/README.md:273). This makes the assurance story hard to audit and easy to overstate.

    **Required correction:** build a maintained coverage matrix tied to actual theorem names, model checks, runtime checks, and archive paths. It should be generated and checked in CI so stale scope claims fail review.

13. **Low: live retitled is not actually modelled.**

    The model goal says live sessions are never archived or retitled, [model/step2_model.py:7](/home/juraj/claude-skills/archive-conversation-forks/model/step2_model.py:7). The check only tests archive membership, marker assignment, and kept membership, [model/step2_model.py:262](/home/juraj/claude-skills/archive-conversation-forks/model/step2_model.py:262).

    **Required correction:** model mutation phases explicitly: Step 0 live snapshot, Step 6 live re-read, move decisions, Step 7 title writes, and dormant-only guard. Check that live sessions cannot enter move or title mutation sets.

14. **Low: canonical tie-break and proof README wording are stale enough to hurt auditability.**

    The code comment says the final `k` tie-break picks the lexically first filename, [SKILL.md:316](/home/juraj/claude-skills/archive-conversation-forks/SKILL.md:316), but `max(..., k)` picks the largest key, [SKILL.md:318](/home/juraj/claude-skills/archive-conversation-forks/SKILL.md:318). The proof README first says the exact key is not modelled, [proofs/README.md:120](/home/juraj/claude-skills/archive-conversation-forks/proofs/README.md:120), then says max-key selection is formalised, [proofs/README.md:335](/home/juraj/claude-skills/archive-conversation-forks/proofs/README.md:335).

    **Required correction:** define canonical selection once as executable spec/code, test it against Python, and align Lean's `canonicalByKey` statement and README claims with that exact function.
