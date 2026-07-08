# Agent Report: Python Model And Fuzz Harness

Scope: `archive-conversation-forks/model/*.py` and `model/README.md`. Test-output and prose defects stay in scope when they can hide a wrong test, teach operators to ignore failures, overstate executable coverage, or weaken proof comprehension. Required corrections name the best-known correctness repair.

## Findings

1. **Critical: archive guards must stop the run, not become observations.**

   [step2_model.py:197](/home/juraj/claude-skills/archive-conversation-forks/model/step2_model.py:197) records a C6 guard failure and still archives the file. [trace_patched.py:125](/home/juraj/claude-skills/archive-conversation-forks/model/trace_patched.py:125) records the same failure shape. The recall path also lacks the SKILL's orphan guard before archiving. The executable harness is not a faithful archive-state test while invalid transitions are recorded as observations and archive reasons are not exclusive.

   **Required correction:** replace ad hoc archive lists with a single typed archive state machine. Every transition to `moved` must run the same orphan, content, live, and debris guards; invalid transitions must be unrepresentable or fatal.

2. **Critical: failing fuzz runs can still look successful.**

   [step2_model.py:440](/home/juraj/claude-skills/archive-conversation-forks/model/step2_model.py:440) catches crashes and [step2_model.py:447](/home/juraj/claude-skills/archive-conversation-forks/model/step2_model.py:447) only counts violations. [finding_marker_no_hole.py:77](/home/juraj/claude-skills/archive-conversation-forks/model/finding_marker_no_hole.py:77) skips exceptions.

   **Required correction:** convert probes into real tests with fail-closed exit behavior, replayable seeds, crash accounting, skipped-case accounting, and explicit expected-failure modes for pre-fix models. Remove blanket `except: continue`.

3. **High: the targeted C4 probe tests the wrong shape.**

   In [targeted.py:30](/home/juraj/claude-skills/archive-conversation-forks/model/targeted.py:30), `FORK` has more distinct messages than `C`, so the canonical chooser makes `FORK` canonical. The probe does not test a kept non-canonical fork whose phantom source is protected only by the re-close.

   **Required correction:** build a phase-aware regression fixture that asserts graph shape, pre-reclose failure, post-reclose protection, and final no-orphan behavior. The probe must fail if any setup assumption is false.

4. **High: debris re-enters later archive logic.**

   [step2_model.py:185](/home/juraj/claude-skills/archive-conversation-forks/model/step2_model.py:185) removes debris from `KEPT`, then [step2_model.py:210](/home/juraj/claude-skills/archive-conversation-forks/model/step2_model.py:210) lets recall process every non-kept non-live file. The SKILL excludes debris from recall.

   **Required correction:** use the typed archive state machine so once a file is routed to debris, no later phase can inspect it as a recall, C6, or marker candidate. Add global exclusivity checks for archive reasons.

5. **High: adversarial-oracle wording overstates what the executable does.**

   [model/README.md:22](/home/juraj/claude-skills/archive-conversation-forks/model/README.md:22) says the fuzz tests arbitrary judge policies, while [step2_model.py:413](/home/juraj/claude-skills/archive-conversation-forks/model/step2_model.py:413) samples random booleans.

   **Required correction:** split the harness into quantified modes: exhaustive oracle enumeration for bounded stores, randomized search for larger stores, and explicit trusted-read preconditions for judged paths. Outputs must state which mode supports each claim.

6. **Medium: threshold paths are not covered by the main fuzz.**

   [step2_model.py:434](/home/juraj/claude-skills/archive-conversation-forks/model/step2_model.py:434) caps messages at 8, so `CEILING=50` paths are unreachable.

   **Required correction:** add coverage-directed generators with required branch coverage for `CEILING` boundaries, high-missing recall, auto-kept forks, zero/global residue transitions, and debris interactions. Missing branch coverage must fail the test run.

7. **Medium: the fix-confirmed probe does not run the fixed model.**

   [finding_f1_fix_confirmed.py:10](/home/juraj/claude-skills/archive-conversation-forks/model/finding_f1_fix_confirmed.py:10) imports pre-fix `trace`, despite [model/README.md:59](/home/juraj/claude-skills/archive-conversation-forks/model/README.md:59) saying it confirms the re-close fix.

   **Required correction:** add a pre/post regression fixture with assertions: old model exposes the orphan risk, patched model keeps the source, and both runs report the expected phase-specific facts.

8. **Medium: debris own-content safety is outside the model but easy to miss.**

   [step2_model.py:171](/home/juraj/claude-skills/archive-conversation-forks/model/step2_model.py:171) says opener text is not modeled, and [step2_model.py:344](/home/juraj/claude-skills/archive-conversation-forks/model/step2_model.py:344) excludes debris's own content from loss checks.

   **Required correction:** model the opener text, debris classifier, and user confirmation gate as explicit inputs/oracles, then assert that no file moves through debris unless that gate approves it.

9. **Low: output calls expected observations violations.**

   [step2_model.py:455](/home/juraj/claude-skills/archive-conversation-forks/model/step2_model.py:455) prints `[c6-content] violations`, while [model/README.md:90](/home/juraj/claude-skills/archive-conversation-forks/model/README.md:90) says this is expected and not a bug.

   **Required correction:** replace free-text result printing with typed result classes: hard failure, expected pre-fix failure, judged-path observation, coverage result, skipped case, and crashed case. CI and humans should not infer severity from label strings.

10. **Low: probe prose contradicts itself in ways that can hide wrong tests.**

    [targeted.py:23](/home/juraj/claude-skills/archive-conversation-forks/model/targeted.py:23) says `SRC` is in a different tree, then says the `P` lref co-tree merges it. That confusion is directly related to the bad probe shape.

    **Required correction:** make prose claims executable: every structural claim in a probe header should have a matching assertion in setup. Generated probe summaries should come from checked fixture facts.

11. **Low: README confidence language outruns executable guarantees.**

    [model/README.md:18](/home/juraj/claude-skills/archive-conversation-forks/model/README.md:18) says the models are faithful re-implementations, and [model/README.md:26](/home/juraj/claude-skills/archive-conversation-forks/model/README.md:26) says they establish the properties across a large sample. That is too strong while guards are modeled as observations, debris is double-routed, and probes miss claimed branches.

    **Required correction:** tie README claims to a maintained verification matrix: proof-covered, fuzz-covered, exhaustively enumerated, trusted-oracle, and out-of-scope. The matrix should be generated from test metadata and backed by tests.
