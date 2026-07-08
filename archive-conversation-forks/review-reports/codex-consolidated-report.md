# Codex Consolidated Report

Scope: adversarial review of `archive-conversation-forks`, including `SKILL.md`, README files, Python model/fuzz files, Lean proofs, and user-facing prose.

Review standard: correctness first. Severity ranks risk; it does not filter scope. A prose, title, README, or presentation defect remains in scope when it can affect operator judgment, recovery, auditability, proof comprehension, or implementation correctness. Required corrections describe the best-known correctness repair. Risk notes do not substitute for repair.

Verification: Python probes were run locally, and the Lean proofs built successfully in Docker with Lean 4.10.0. The proof build printed the expected axiom audit and two unused-variable warnings in `Check.lean`. No `sorryAx`, `Classical.choice`, or `Lean.ofReduceBool` appeared in the printed audit, but not every named theorem is covered by that printed audit.

## Findings

1. **Critical: the backup gate comes after work that can trigger deletion.**

   `SKILL.md` warns that subagents can trigger a cleanup sweep that bypasses `cleanupPeriodDays`, but the checked backup gate appears after the Step 4/5 agent work.

   **Required correction:** rebuild the procedure around a mandatory preflight phase. No subagents, session-starting actions, mtime work, or moves may happen until an immutable, run-scoped, out-of-tree snapshot has been created and verified by hashes, counts, metadata coverage, and sample restore.

2. **Critical: live-session protection is incomplete.**

   The live registry code only accepts `status == "running"`, while current registry shapes can use active statuses such as `idle` and `busy` and include `procStart`. Missing or unreadable session directories, parse errors, unknown schemas, and PID/`procStart` mismatches can also collapse the live set incorrectly. The final live re-read drops newly live files from the move list without recomputing dependencies that those live files now need.

   **Required correction:** replace ad hoc status filtering with a versioned registry reader that treats known active statuses, including `idle` and `busy`, plus any live PID with matching `procStart` as live. Fixtures must cover known registry shapes. Unknown schema, missing directory, unreadable directory, parse errors, or PID/`procStart` mismatch must fail closed. Require project quiescence during mutation where possible; always recompute the full plan under the final live set and re-issue approval if anything changed.

3. **Critical: malformed JSONL and non-text content can disappear from archive evidence.**

   The parser silently skips malformed lines, and the human/agent preview path stores text blocks only. Unique tool results, attachments, malformed boundaries, or structured content can be omitted from the evidence used to approve an archive.

   **Required correction:** build a lossless archive-review evidence bundle from full raw messages. Parse-dirty files must be kept or repaired before archive; capped previews may be UI only, never the evidence source. Every unique raw record, including tool outputs, attachment metadata, block types, long content, and truncation status, must be available in the audit output.

4. **Critical: archive and restore are not transactional.**

   The procedure moves files and then writes a global manifest. Restore can overwrite newer destinations. The doc also names a metadata hazard, but backup/restore instructions mainly handle JSONL files. The deletion sweep can cover `.jsonl` and `.cast`, so the backup/restore surface is not fully defined.

   **Required correction:** replace direct moves with a transactional copy-to-archive flow: write-ahead manifest, copy, hash-verify, fsync, then remove originals only after verification. Restore must be no-clobber, live-aware, transaction logged, and cover the full swept/session-state surface, including `.cast` when in scope, through backed-up metadata, rebuilt metadata, or a verified Claude-native import/reindex path.

5. **Critical: Python guard failures can look like passing runs.**

   The model records C6 guard failure and still archives. The recall path lacks the SKILL's orphan guard before archive. Fuzz harnesses can catch crashes, skip trials, or print expected observations as "violations" while still exiting successfully.

   **Required correction:** replace ad hoc archive lists with a typed archive state machine. Every transition to `moved` must run the same orphan, content, live, and debris guards; invalid transitions must be unrepresentable or fatal. Test runs must fail closed on guard failure, crash, skipped trial, or core invariant failure, with replayable reproducers.

6. **High: `load-bearing` is used for incompatible concepts.**

   The prose says load-bearing files never move. The code uses `loadbearing` for cross-file targets and all viable phantom sources, while redundant extra sources can still be archived if another kept source remains. Fingerprint sets are also described as load-bearing.

   **Required correction:** redesign the state model across procedure, Python, Lean, and docs with explicit sets such as `must_keep`, `can_source_phantom`, `chosen_source`, `extra_source`, and `archive_candidate`. Encode those names in typed data structures, proof/code names, and operator-facing marker rules. Use the invariant "every kept needer retains at least one kept source."

7. **High: exact containment is not exact as written.**

   The fingerprint uses 12 hex chars of MD5, while prose treats `0 unique` as byte-identical containment. Lean reasons over abstract fingerprints, not truncated hashes.

   **Required correction:** replace truncated-hash identity with a canonical preservation record that contains every field needed for rendering, replay, restore, and audit. Hashes may be used only as indexes, with collision verification against the canonical record before any archive decision. Fields intentionally excluded from content identity, such as UUID or timestamp if excluded, must be named explicitly.

8. **High: the durable-artifact override is an unverified archive path.**

   Step 2 auto-keeps high-unique forks, and the Python model matches that. The skill later allows archiving them if a durable artifact captures the content, but this path is absent from the model and proof archive-path list.

   **Required correction:** make artifact preservation a first-class archive path with a durable preservation witness: exact message IDs or raw records covered by the artifact, artifact path/commit, human approval, restore/audit manifest, and explicit model/proof status. High-unique forks must not move until this witness exists.

9. **High: agent output can become archive authority.**

   The procedure lets theme agents flag debris and emit proposed archive JSON that Step 6 aggregates.

   **Required correction:** agents may emit evidence summaries only, not archive lists. A deterministic planner owns candidate sets, and the orchestrator validates every final decision against planner output and attached evidence.

10. **High: store selection can target the wrong project.**

    Store selection by count and recent timestamps conflicts with the procedure's own warning that timestamps are noisy.

    **Required correction:** implement store selection as a deterministic resolver over transcript `cwd`, session IDs, registry entries, and slug candidates. Stop if more than one store matches or if the resolver cannot prove the target store.

11. **High: the Lean proof is not end-to-end for the implemented pipeline.**

    The README calls the main theorem unconditional, but the theorem still assumes closure and `locked <= consumers`. Debris safety, C5 content preservation, and bounded-to-unbounded closure are not all derived from the concrete pipeline.

    **Required correction:** prove one pipeline-level theorem over the actual final picker set: closure loop output, `locked <= consumers`, C5, debris nomination, final archive set, and the bridge from bounded closure to `FixProto.IsClosed` must all be defined and connected.

12. **High: Lean recall preservation is not connected to the implementation precondition.**

    The Lean `recall_no_loss` definitions do not exclude the archived file `A` from the preserving witnesses, while the implementation recall loop only considers files with `A not in KEPT`. This is a proof-contract gap rather than direct evidence that the implemented loop self-witnesses.

    **Required correction:** define archive semantics and prove preservation against post-move survivors, then connect that theorem to the implementation precondition that recall candidates are not in `KEPT`. Self-witnessing should be unrepresentable in the proof contract.

13. **High: targeted probes and model tests miss claimed paths.**

    `targeted.py` does not build the C4 trap it claims because `FORK` becomes canonical. `finding_f1_fix_confirmed.py` imports the pre-fix trace. Main fuzz does not hit `CEILING=50` threshold branches.

    **Required correction:** build phase-aware regression fixtures that assert graph shape, pre-reclose failure, post-reclose protection, final no-orphan behavior, canonical choice, kept-fork status, and threshold branch coverage. Missing setup facts or coverage must fail the test run.

14. **High: debris is routed inconsistently.**

    The Python model removes debris from `KEPT`, then recall can process the same file because it is non-kept and non-live. The real debris classifier also depends on opener text that the model omits.

    **Required correction:** use the typed archive state machine so each moved file has exactly one final route and debris cannot re-enter recall, C6, or marker phases. Model the opener text, debris classifier, and user confirmation gate as explicit inputs/oracles, then assert that no file moves through debris unless the gate approves it.

15. **High: JSONL parser coverage is overstated.**

    The model README says post-parse fields are generated directly and JSONL is not parsed. The proof README says JSONL-to-record extraction remains fuzz-checked.

    **Required correction:** extract the JSONL parser into a shared implementation with fixtures and fuzz/property tests for `owned`, `lref`, `bnd`, timestamps, `sources`, and `needs`; make the model consume parser output.

16. **High: unsatisfiable phantom handling is under-specified.**

    The procedure records `unsatisfiable` phantoms, but post-move verification demands zero orphans. Existing missing origins make that gate impossible or invite ad hoc weakening.

    **Required correction:** make unsatisfiable phantoms a hard stop pending recovery, or make the verifier record a pre-move baseline and prove that the operation introduces no new orphans. The chosen rule must be explicit before any move plan can be approved.

17. **Medium-high: README and proof language hide the gates.**

    Phrases such as "fully recoverable," "provably redundant," "live sessions are never touched," "unconditional," and "fully-wired" appear before the reader sees the assumptions and unchecked boundaries.

    **Required correction:** rewrite the README as an operator contract and the proof README as a proof contract. Each guarantee must state preconditions, non-guarantees, recovery path, theorem name, exact hypotheses, conclusion, supplied-by-code facts, trust boundary, and audit status.

18. **Medium-high: recall prose names the wrong preservation object.**

    The code uses kept-union containment, but the prose says every message is byte-identical to one in `best`, where `best` is only the closest single kept file for reporting.

    **Required correction:** remove `best` from the safety path. The recall report must store a kept-union proof artifact that lists the kept file or files covering each archived raw message identity.

19. **Medium-high: mtime and title-write safety are under-specified.**

    The skill says mtime-neutral titling is safe even with the sweep active, while other sections say disable or mitigate the sweep first. Step 7 appends `custom-title` JSONL records to retained dormant sessions, but the safety invariants say never move or file-edit locked/canonical/kept files.

    **Required correction:** split the invariant into "never move" for retained/locked/canonical files and "never file-edit live sessions." Title appends are allowed only through a Step-7 dormant-session path that writes JSON with `json.dumps`, appends atomically, re-parses the appended line, and restores mtime only after validation. Default titling may be described as mtime-neutral, never safe. Timestamp equalisation needs its own preflight: verified backup, sweep mitigation, version caveat, and explicit user approval.

20. **Medium: source/need prose is narrower than the predicate.**

    The prose describes a phantom source as having real messages before its boundary. The actual predicate is per-boundary: `parentUuid` present or `nb > 0`; a file can both need and source the same phantom through different records.

    **Required correction:** make the `sources`/`needs` boundary classifier a single shared spec with executable tests and Lean examples for all `parPresent`/`nb` cases, then generate or mirror prose, Python comments, and proof docs from that spec.

21. **Medium: settings and backup-hook instructions are not merge-safe enough.**

    Whole-object JSON snippets can cause users to overwrite existing settings. The backup-hook paragraph also compresses too many assumptions into one block.

    **Required correction:** provide a settings patcher or validated merge command with backup, rollback, and post-write verification. Move backup setup into a standalone verified-backup procedure with install, run, verify, restore-test, version-check, and content-hash backup paths.

22. **Medium: post-move verification is too buried for the final safety gate.**

    "Zero orphans on both" appears after a long move procedure and is easy to skim.

    **Required correction:** provide an executable verifier that rebuilds the post-move map and fails closed. If verification fails, restore before doing anything else.

23. **Medium: proof and model scope needs one coverage table.**

    Caveats for C6, nonzero recall, debris, parser extraction, marker judgment, and canonical selection are scattered across prose.

    **Required correction:** build a maintained coverage matrix tied to actual theorem names, model checks, runtime checks, trusted-oracle paths, and archive paths. It should be generated or checked in CI so stale scope claims fail review.

24. **Medium: pseudocode is too large to be the safety mechanism.**

    The skill says the Python is not turnkey, yet the procedure depends on humans wiring stubs correctly.

    **Required correction:** ship a tested planner with fixtures, dry-run output, schema checks, and property tests. Treat prose as documentation for the tool, not the executable safety mechanism.

25. **Medium-low: title, warning, and noun-stack issues affect auditability.**

    Dense family sub-label titles, repeated all-caps warnings, stale internal line references, and compressed phrases like "phantom-lpu backfill sources" make it harder to see which gate is active and why a retained fork matters.

    **Required correction:** make titles and prose derived from structured audit data where possible. Use a shared glossary and first-use expansions across `SKILL.md`, README, model README, and proof README; reserve strong labels for real stop gates; remove stale line references; and turn dense classifications into decision tables with acceptance tests.

26. **Low: stale model/proof wording still matters.**

    The model says live sessions are never retitled, but it does not model title mutation. The canonical tie-break comment says lexically first while the code picks the largest key. The proof README has conflicting statements about whether exact key selection is modeled.

    **Required correction:** model mutation phases explicitly, including title writes, and define canonical selection once as executable spec/code tested against Python and aligned with Lean's `canonicalByKey`. Comments and READMEs should be generated from or checked against those contracts.

27. **Low: Docker build docs are stale.**

    `leanprovercommunity/lean:4.10.0` was not found. The local `leanprovercommunity/lean:latest` image had Lean 4.10.0 and Lake 5.0.0, but direct entrypoint execution failed. Running through `/bin/sh` worked.

    **Required correction:** add a verified proof-build target that uses the known-good Docker invocation and fails if the documented command diverges from the working build path.

## Verification

Commands run:

```bash
python3 archive-conversation-forks/model/step2_model.py 12345 10000
python3 archive-conversation-forks/model/targeted.py 5 10000
python3 archive-conversation-forks/model/finding_marker_no_hole.py
python3 archive-conversation-forks/model/finding_f1_phantom_orphan.py
python3 archive-conversation-forks/model/finding_f1_fix_confirmed.py
python3 archive-conversation-forks/model/finding_f2_halt_swallow.py
docker run --rm --entrypoint /bin/sh \
  -v /home/juraj/claude-skills/archive-conversation-forks/proofs:/work \
  -w /work leanprovercommunity/lean:latest \
  -lc '/home/lean/.elan/bin/lake build'
```

Result:

- Python fixed-order fuzz had no core invariant violations in the sample run; it reported expected C6-content observations.
- Targeted fuzz had no core violations, but the trap construction itself is flawed as noted.
- Lean proof build completed successfully in Docker with Lean 4.10.0.
- Local session registry confirmed live entries can be `status:"idle"`.
