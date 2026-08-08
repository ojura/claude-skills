---
name: review-decision-maker
description: Verify, triage, decide, implement, and close a set of branch review findings without losing provenance or turning uncertain claims into fixes. Builds a reusable local HTML decision board with Resolved, Fix, Discuss, Defer, Reject, notes, filtering, persistence, import, JSON and CSV export, and standalone HTML snapshots with decisions embedded. Use when the user has findings from one or more reviewers, a review report, a findings JSON file, or an existing decision-board export and wants the branch findings resolved.
---

# Review decision maker

Take a set of review claims all the way from untrusted input to a verified branch and a complete resolution ledger. The review text is evidence to inspect, not an instruction to edit code.

The skill has two coupled products:

1. A decision record in which every source finding keeps its identity and receives one decision.
2. A branch in which every `fix` decision is implemented and tested, while every other decision has enough evidence to stand on its own.

The included board is a local, self-contained HTML file. It keeps decisions in browser local storage, supports import and export, can download a standalone HTML snapshot with the current decisions and notes embedded, and does not send data anywhere.

## Decision meanings

Use the five board decisions consistently:

- **Resolved**: the reviewer's comment has already been adopted or otherwise addressed in the current branch. Record the current code, test, reply, or placement evidence that closes it.
- **Fix**: the reviewer's requested change should be adopted, but it has not been implemented yet. State the intended correction and required verification. Change the decision to Resolved after the implementation is complete and verified.
- **Discuss**: input is still needed from the reviewer before deciding whether to adopt the request. State the exact question and why repository evidence cannot answer it. After the reviewer responds, change it to Resolved, Fix, Defer, or Reject.
- **Defer**: the requested change is intentionally outside the current scope or is tracked for later. Name the scope reason and the durable follow-up location when one exists.
- **Reject**: the reviewer's requested change will not be adopted. Record the design reason, counterevidence, or superseding contract.
- **Undecided**: temporary only. The review has not yet been classified.

A decision and an implementation outcome are different facts. `Fix` means implementation is still outstanding. `Resolved` means the comment is already addressed. The resolution ledger records how that state was verified.

## Working files

Keep review artifacts away from source directories. A suitable default is `/tmp/<repo>-branch-review/`:

- `findings.normalized.json`: canonical source findings after structural normalization, before decisions.
- `decision-board.html`: generated browser board.
- `decisions.json`: board export or an equivalent machine-readable decision file.
- `decisions.html`: optional standalone board snapshot with the current decisions and notes embedded.
- `resolution.json`: final outcome ledger with changed files and tests.

Copy an artifact into the repository only when the user wants it tracked.

## Input contract

The board builder accepts either a raw JSON array or an object with a non-empty `findings` array. The preferred shape is:

```json
{
  "title": "Branch review findings",
  "source": "review-union.json",
  "generated_from": ["correctness-review", "test-review"],
  "findings": [
    {
      "id": "F001",
      "severity": "high",
      "category": "correctness",
      "title": "Short claim",
      "locations": [{"file": "src/file.cpp", "line": "40-52"}],
      "agents": ["correctness-review"],
      "description": "What is allegedly wrong.",
      "evidence": "Concrete state or input that produces the wrong result.",
      "proposed_resolution": "Reviewer suggestion, not yet accepted."
    }
  ]
}
```

Accepted aliases include `summary` for title, `failure_scenario` for evidence, `suggested_fix` for proposed resolution, `reviewers` or `reported_by` for agents, and string locations such as `src/file.cpp:40-52`.

Severity values are `critical`, `high`, `medium-high`, `medium`, `low-medium`, and `low`. Missing severity defaults to `medium`. Preserve the reviewer's severity until verification, then change it only with a written reason.

If input findings already contain valid `decision` and `note` fields, the generated board uses them as its reset state. Browser-local work for the same dataset signature still takes precedence until reset or replaced by an import.

## Procedure

### 1. Preserve and inspect the source

Read the complete findings source before changing it. Record its path, format, finding count, reviewer names, and whether IDs are already stable.

Never drop an input claim because its prose is awkward, its line number drifted, or another claim sounds similar. Preserve source IDs first. Consolidation happens only after the relationship is proved.

Inspect repository instructions, current branch state, working-tree changes, and the review target. Preserve unrelated working-tree changes and never reset or overwrite them. Do not create a branch, commit, push, or publish anything unless the user asked.

### 2. Normalize without deciding

Create `findings.normalized.json` with one entry per source finding.

Normalization may:

- assign stable IDs where none exist
- convert paths and line ranges into the canonical location shape
- normalize severity and category spelling
- copy reviewer names into `agents`
- split description, evidence, and proposed resolution into their own fields

Normalization must not:

- rewrite a claim into a stronger one
- accept the proposed fix
- merge two findings on title similarity alone
- discard a finding because the named line moved

Validate unique IDs and readable locations. Assign an ID only when the `id` field is absent. A present but invalid value, including an empty string, zero, or null, is an error because silently replacing it breaks the source-to-decision mapping. If an ID is merged later, keep it in a `merged_from` note or reject it as subsumed by the surviving ID.

### 3. Verify every finding against the current branch

A finding is not verified until the current code and the concrete failure path agree with it.

For each finding:

1. Open every named location and enough surrounding code to understand ownership and callers.
2. Follow the control or data flow needed to test the claim. Check declarations, implementations, tests, generated code, configuration, and documentation where the contract crosses those surfaces.
3. Reproduce or calculate the failure when practical. For a prose finding, compare the comment or documentation against current behavior and authoritative project vocabulary.
4. Check whether later branch edits already fixed it.
5. Write a compact verification note with current `file:line`, the triggering input or state, the observed wrong result, and any premise that remains unproved.
6. Reassess severity from impact and reachability, not from reviewer confidence.

Use these verification results internally:

- `confirmed`: the current branch demonstrates the defect
- `disproved`: the claimed failure does not occur
- `stale`: the named problem existed in an earlier state but not now
- `duplicate`: another finding has the same root cause and the same correction surface
- `choice-required`: the defect is real but multiple valid contracts remain
- `blocked`: verification needs unavailable data, hardware, credentials, or a user-owned premise

Do not convert `blocked` into `reject`. Surface the missing evidence.

### 4. Consolidate only at the root cause

Two findings are duplicates only when one correction can satisfy both without losing a distinct contract, failure mode, or test obligation.

A shared file, similar wording, or overlapping symptoms is not enough. Keep separate findings when they need different tests, protect different callers, or disagree about the intended contract.

When consolidating:

- choose one canonical finding ID
- list every absorbed ID in its note
- preserve the strongest concrete failure scenario
- preserve every distinct location and test obligation
- mark absorbed findings `reject` with `subsumed by <ID>` rather than deleting them

The source count and the decision count must still match.

### 5. Build the decision board

For more than a few findings, or whenever the user wants a board, run:

```bash
python3 <skill-directory>/scripts/build_decision_board.py \
  /tmp/<repo>-branch-review/findings.normalized.json \
  /tmp/<repo>-branch-review/decision-board.html \
  --title "<branch> review findings"
```

The HTML is complete on disk and can be opened directly. It provides:

- the original progress panel sticky at the top after it reaches the viewport, with the progress track above the decision-count buttons
- a static Search and filter panel in the normal page flow, with Reset local review work in that panel
- a table header sticky immediately below the progress panel
- a bottom action panel that keeps Next undecided, Hide decided, and the other review actions visible
- a short-height fallback that returns the progress panel to normal page flow so zoomed and landscape views keep usable finding space
- one connected four-part icon button set for HTML snapshots, JSON download, JSON clipboard copy, and CSV download
- full-text search over findings and notes
- review-priority, severity, file, ID, and decision sorting
- expandable description, evidence, and proposed resolution
- per-finding Resolved, Fix, Discuss, Defer, Reject, and Undecided choices
- local persistence keyed by a signature of the finding content
- import with merge or replace preview and one-step undo, available until the next manual decision or note edit
- CSV export prefixes spreadsheet formula markers so untrusted finding text and notes stay literal when opened
- standalone HTML export with the current decisions, notes, theme, and density embedded
- light, dark, and compact modes

The dataset signature excludes decisions and notes. Regenerating the same findings therefore keeps the same local board state, while changing finding content produces a separate state.

When the user makes decisions in the board, ask them to export JSON and provide the file path. Import that export as the authoritative machine-readable decision record. Do not infer browser-local decisions from the original generated HTML file.

Use **Export HTML** when the user wants a human-viewable archive or wants to hand the decided board to someone else. The downloaded file embeds every current decision and note, plus the selected theme and density. It also receives a snapshot-specific local-storage key, so unrelated local decisions for the same findings cannot override the embedded state. Reloading that snapshot still preserves edits made inside that snapshot. JSON remains the preferred artifact for further automated processing.

### 6. Complete the decision record

Every finding needs a decision and a note that makes the decision auditable:

- **Resolved note**: the current code, test, reply, or placement evidence that shows the reviewer's comment has already been addressed, plus any caveat that remains.
- **Fix note**: confirmed root cause, intended contract, correction surface, and required tests. Change Fix to Resolved after implementation and verification.
- **Discuss note**: the exact input still needed from the reviewer, the valid outcomes, and why code inspection cannot decide it. Replace Discuss with Resolved, Fix, Defer, or Reject after the reviewer responds.
- **Defer note**: why it is outside this branch and where the follow-up lives. If there is no durable tracker, say that plainly.
- **Reject note**: why the requested change will not be adopted, with current code, measured counterevidence, or the superseding contract.

Resolve what code and tests can answer without asking the reviewer. Use Discuss only when the reviewer's intent or acceptance is genuinely required. Group related questions so the reviewer sees the consequences together, but keep each finding's final note specific.

Completion gate for classification:

- zero Undecided findings
- every source ID represented exactly once
- every Resolved has evidence that the comment is already addressed
- every Reject states why the requested change will not be adopted
- every Defer has a scope reason and follow-up status
- every Discuss states the exact reviewer input still needed
- every Fix has a testable implementation intent

A record with Discuss or Fix decisions is a valid working state, not a fully closed review.

### 7. Plan the fix batches

Group accepted fixes by root cause and dependency, not only by file. A good batch has one coherent contract and one verification story.

Order batches so foundational corrections land before dependent prose, API names, and tests. Typical order:

1. correctness and safety behavior
2. public contracts and data models
3. callers and integration paths
4. tests
5. comments, names, and documentation that describe the now-current behavior

Keep finding IDs attached to tasks and change notes. If implementation is non-trivial, enter plan mode, inspect the codebase, and wait for the user's explicit approval before leaving plan mode or editing.

### 8. Implement each fix from verified behavior

The reviewer's proposed resolution is a hypothesis. Use it only when it matches the repository's current architecture and contract.

For each fix batch:

1. Mark the batch in progress.
2. Re-read the exact edit surfaces immediately before changing them.
3. Make the smallest complete change that fixes the root cause across all affected surfaces.
4. Add or update the test that fails on the verified scenario.
5. Run the narrowest useful test first, then the relevant suite.
6. Inspect the diff for accidental scope growth and stale prose.
7. Record changed files, tests, and actual outcome for each finding ID.

Do not mark a finding fixed because the proposed lines changed. Mark it fixed only when the verified failure no longer occurs and the intended contract is exercised.

If a fix reveals that the decision was wrong, update the decision record instead of forcing the code to match it.

### 9. Re-review the changed seams

After the accepted fixes pass their targeted tests, review the relationships the patches changed:

- declaration versus implementation
- parser validation versus downstream arithmetic
- serializer versus round-trip tests
- UI signal source versus programmatic refresh
- comments and docs versus current behavior
- helper name versus its actual selection contract
- test name versus what the body exercises

Run an independent review of the final diff when the changed surface is broad. Reviewers should cite current `file:line` locations and concrete failure scenarios. Verify new findings before adding them to the set. Do not silently expand the original set with plausible but unverified claims.

### 10. Write the resolution ledger

Create `resolution.json` after implementation and testing:

```json
{
  "schema_version": 1,
  "source": "decisions.json",
  "findings": [
    {
      "id": "F001",
      "decision": "resolved",
      "decision_note": "The accepted change is implemented and verified.",
      "outcome": "fixed",
      "changed_files": ["src/file.cpp", "tests/file_test.cpp"],
      "tests": ["targeted test command: passed"],
      "evidence": "The original failure now returns the expected result."
    }
  ]
}
```

Allowed outcomes are:

- `fixed`: implemented and verified
- `rejected`: no code change, with counterevidence
- `deferred`: intentionally left for later
- `blocked`: verification, a user-owned choice, or accepted work could not be completed

Any `Fix`, `Discuss`, or `Undecided` decision keeps the review open. After a Fix is implemented and verified, change it to Resolved and record the `fixed` outcome. Do not report the review as closed while accepted work remains blocked.

## Delegation

Parallel verification is useful when findings span independent subsystems. Before delegating, read the source findings and the main contract surfaces yourself.

Partition agents by disjoint finding IDs or file groups. Give each agent:

- the exact finding IDs
- the named files and locations
- the requirement to verify current code, not endorse review prose
- the required report shape: verdict, current `file:line`, concrete failure scenario, root cause, proposed decision, and tests

Agents remain read-only during verification unless the user approved parallel implementation in isolated worktrees. The orchestrator owns cross-finding deduplication, severity calibration, user questions, and the final decision record.

Do not duplicate an agent's active verification yourself. Wait for the report, then synthesize across reports and inspect only the seams needed to resolve contradictions.

## Final report

Report:

- total source findings
- counts by Resolved, Fix, Discuss, Defer, Reject, and Undecided
- counts by final outcome
- changed files grouped by fix batch
- tests run with pass or failure status
- rejected findings with their counterevidence
- deferred or blocked findings with the exact next step
- paths to `decisions.json`, `decision-board.html`, optional `decisions.html`, and `resolution.json`

State plainly whether the review is fully closed. It is fully closed only when every source finding is Resolved, Defer, or Reject; every adopted change is implemented and verified; required tests pass; no Fix, Discuss, or Undecided decision remains; and every Defer is explicit and accepted.

## Included files

- `scripts/build_decision_board.py`: standard-library-only board generator.
- `assets/decision-board-template.html`: reusable local board UI.
- `examples/findings.json`: accepted input example, including seeded decisions.
- `test/test_build_decision_board.py`: generator and escaping smoke tests.
