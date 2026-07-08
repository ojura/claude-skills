# Agent Report: Language And Reader Burden

Scope: prose, headings, titles, README confidence language, and terminology. Language issues are correctness issues when they can make an operator skip a gate, misunderstand a proof, or make a wrong implementation look right. Required corrections name the best-known correctness repair.

## Findings

1. **Critical: the backup gate comes after work that can trigger the deletion bug.**

   [SKILL.md:47](/home/juraj/claude-skills/archive-conversation-forks/SKILL.md:47), [SKILL.md:54](/home/juraj/claude-skills/archive-conversation-forks/SKILL.md:54), [SKILL.md:645](/home/juraj/claude-skills/archive-conversation-forks/SKILL.md:645), and [SKILL.md:685](/home/juraj/claude-skills/archive-conversation-forks/SKILL.md:685). The prose says subagents can bypass `cleanupPeriodDays`, then delays the checked backup precondition until Step 6, after Step 4/5 agents.

   **Required correction:** rebuild the procedure around a mandatory preflight phase. No subagents, session-starting actions, mtime work, or moves may happen until an out-of-tree backup has been created and verified by count, path, and sample restore. A hook run qualifies only when its copied output is verified.

2. **Critical: load-bearing is used for incompatible states.**

   [SKILL.md:103](/home/juraj/claude-skills/archive-conversation-forks/SKILL.md:103), [SKILL.md:205](/home/juraj/claude-skills/archive-conversation-forks/SKILL.md:205), [SKILL.md:417](/home/juraj/claude-skills/archive-conversation-forks/SKILL.md:417), [SKILL.md:424](/home/juraj/claude-skills/archive-conversation-forks/SKILL.md:424), and [SKILL.md:637](/home/juraj/claude-skills/archive-conversation-forks/SKILL.md:637). The doc says load-bearing files never move, then says redundant extra sources can move, and even calls fingerprint sets load-bearing.

   **Required correction:** redesign terminology and set names together across prose, pseudocode, proof docs, and READMEs: `must_keep`, `can_source_phantom`, `chosen_source`, `extra_source`, `archive_candidate`. Do not keep `load-bearing` as a catch-all term.

3. **High: the recall pass names the wrong safety witness.**

   [SKILL.md:526](/home/juraj/claude-skills/archive-conversation-forks/SKILL.md:526), [SKILL.md:543](/home/juraj/claude-skills/archive-conversation-forks/SKILL.md:543), and [SKILL.md:560](/home/juraj/claude-skills/archive-conversation-forks/SKILL.md:560). The code proves containment in the union of kept files, but the prose says every message is in `best`.

   **Required correction:** remove `best` from the safety path. The recall report should store a kept-union proof artifact that lists the kept file or files covering each archived message. `best` may appear only as an optional human hint after the safety proof.

4. **High: mtime and title-write safety are described too loosely.**

   [SKILL.md:33](/home/juraj/claude-skills/archive-conversation-forks/SKILL.md:33), [SKILL.md:44](/home/juraj/claude-skills/archive-conversation-forks/SKILL.md:44), [SKILL.md:735](/home/juraj/claude-skills/archive-conversation-forks/SKILL.md:735), [SKILL.md:743](/home/juraj/claude-skills/archive-conversation-forks/SKILL.md:743), and [SKILL.md:762](/home/juraj/claude-skills/archive-conversation-forks/SKILL.md:762). "Mtime-neutral titling is safe" can be read as permission to proceed under an active sweep. The raw `custom-title` template can also create malformed JSONL if a title with quotes or newlines is interpolated directly.

   **Required correction:** split mtime handling into a separate gated procedure. Default titling may be described as mtime-neutral, never safe. Title writes must use `json.dumps`, append atomically, re-parse the appended line, and restore mtime only after validation. Equalisation needs its own preflight: verified backup, sweep mitigation, version caveat, and user approval.

5. **High: mutation-time live checks are weaker than the initial hard gate.**

   [SKILL.md:159](/home/juraj/claude-skills/archive-conversation-forks/SKILL.md:159) and [SKILL.md:680](/home/juraj/claude-skills/archive-conversation-forks/SKILL.md:680). Step 6 says to re-read and drop now-live sessions, but omits the "unreadable registry or unresolved running session means stop" rule.

   **Required correction:** replace prose duplication with a single reusable live-registry gate used before analysis and immediately before mutation. Its contract should be fixed: unreadable registry, unresolved running session, or partial result means stop.

6. **High: README guarantee language hides the gates.**

   [README.md:5](/home/juraj/claude-skills/archive-conversation-forks/README.md:5), [README.md:18](/home/juraj/claude-skills/archive-conversation-forks/README.md:18), [README.md:28](/home/juraj/claude-skills/archive-conversation-forks/README.md:28), and [README.md:32](/home/juraj/claude-skills/archive-conversation-forks/README.md:32). "Fully recoverable," "provably redundant," and "live sessions are never touched" are only true after the backup, registry, and read gates.

   **Required correction:** rewrite the README as an operator contract: preconditions, guarantees only after preconditions, non-guarantees, and recovery path. Broad claims such as "fully recoverable" should be scoped or removed.

7. **Medium-high: proof confidence comes before proof scope.**

   [proofs/README.md:37](/home/juraj/claude-skills/archive-conversation-forks/proofs/README.md:37), [proofs/README.md:253](/home/juraj/claude-skills/archive-conversation-forks/proofs/README.md:253), [proofs/README.md:350](/home/juraj/claude-skills/archive-conversation-forks/proofs/README.md:350), and [model/README.md:26](/home/juraj/claude-skills/archive-conversation-forks/model/README.md:26). The proof prose uses "proved," "never," and "safe" before the operator sees what is outside the model.

   **Required correction:** split the proof README into "what an operator may rely on" and "proof internals." Every theorem claim should map to the exact procedure step and name unchecked inputs before the claim is made.

8. **Medium-high: the backup hook paragraph is too compressed for a destructive-risk guard.**

   [SKILL.md:64](/home/juraj/claude-skills/archive-conversation-forks/SKILL.md:64) and [SKILL.md:80](/home/juraj/claude-skills/archive-conversation-forks/SKILL.md:80). It mixes append-only assumptions, shrink behavior, hook wiring, storage cost, version caveats, and backup-mode selection in one block.

   **Required correction:** move backup setup into a standalone verified-backup procedure with install, run, verify, restore-test, version-check, and content-hash backup paths. Default to content-hash backups unless append-only behavior has been verified for the current Claude Code version.

9. **Medium: post-move verification is not prominent enough for the final safety gate.**

   [SKILL.md:699](/home/juraj/claude-skills/archive-conversation-forks/SKILL.md:699). "Zero orphans on both" is easy to skim after the move instructions.

   **Required correction:** provide an executable verifier that rebuilds the post-move map and fails closed. If verification fails, restore before doing anything else.

10. **Medium: stale internal line references weaken auditability.**

    [SKILL.md:599](/home/juraj/claude-skills/archive-conversation-forks/SKILL.md:599). "the line-555 guard" no longer points to the visible guard.

    **Required correction:** remove line-number references inside prose and name the exact expression or block instead.

11. **Medium: the title section still creates operator burden that affects later recovery and audit.**

    [SKILL.md:712](/home/juraj/claude-skills/archive-conversation-forks/SKILL.md:712), [SKILL.md:720](/home/juraj/claude-skills/archive-conversation-forks/SKILL.md:720), and [SKILL.md:722](/home/juraj/claude-skills/archive-conversation-forks/SKILL.md:722). The marker/title rules are correct in intent but overloaded. Bad titles make retained forks and scroll dependencies harder to audit later.

    **Required correction:** separate titling from archive safety. Make titling a structured audit layer with a decision table, required fields, examples, and a review pass that checks every title against the session's measured role and residue.

12. **Medium-low: too many warnings compete for attention.**

    [SKILL.md:18](/home/juraj/claude-skills/archive-conversation-forks/SKILL.md:18), [SKILL.md:84](/home/juraj/claude-skills/archive-conversation-forks/SKILL.md:84), [SKILL.md:120](/home/juraj/claude-skills/archive-conversation-forks/SKILL.md:120), [SKILL.md:122](/home/juraj/claude-skills/archive-conversation-forks/SKILL.md:122), and [SKILL.md:130](/home/juraj/claude-skills/archive-conversation-forks/SKILL.md:130). Repeated "HARD RULE," bold, and all-caps words blur which conditions are actual stop gates.

    **Required correction:** reserve "STOP" and "HARD GATE" for actions that halt the run; put the rest in normal procedural language.

13. **Medium-low: proof README has a status-signaling register in places.**

    [proofs/README.md:88](/home/juraj/claude-skills/archive-conversation-forks/proofs/README.md:88), [proofs/README.md:148](/home/juraj/claude-skills/archive-conversation-forks/proofs/README.md:148), and [proofs/README.md:191](/home/juraj/claude-skills/archive-conversation-forks/proofs/README.md:191). "Axiom-free," "machine-checked," "fully constructive," and "trusted core" are useful facts, but stacked together they read like confidence advertising.

    **Required correction:** rewrite proof-facing prose around operational reliance, not proof prestige. Keep axiom and theorem facts in a compact audit appendix.

14. **Medium-low: compressed noun stacks make the hardest parts harder to implement correctly.**

    [SKILL.md:7](/home/juraj/claude-skills/archive-conversation-forks/SKILL.md:7), [SKILL.md:122](/home/juraj/claude-skills/archive-conversation-forks/SKILL.md:122), [proofs/README.md:157](/home/juraj/claude-skills/archive-conversation-forks/proofs/README.md:157), and [proofs/README.md:267](/home/juraj/claude-skills/archive-conversation-forks/proofs/README.md:267). Phrases like "phantom-lpu backfill sources," "unique-vs-kept set-difference," and "model-to-Python boundary" are precise for insiders but hide the action.

    **Required correction:** add a glossary and first-use expansions that turn each compressed term into an action and a check, then use those exact terms consistently across `SKILL.md`, README, model README, and proof README.
