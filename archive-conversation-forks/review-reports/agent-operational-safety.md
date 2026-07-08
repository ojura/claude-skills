# Agent Report: Operational Safety

Scope: operational hazards in `archive-conversation-forks/SKILL.md` and the surrounding README claims. Wording and presentation findings stay in scope when they threaten operator judgment, recovery, implementation correctness, or future auditability. Required corrections name the best-known correctness repair.

## Findings

1. **Critical: live-session registry handling is too narrow.**

   [SKILL.md:332](/home/juraj/claude-skills/archive-conversation-forks/SKILL.md:332) only accepts `running`; current registry entries can use active statuses such as `idle` and `busy` while still open. Missing or unreadable session directories, parse errors, unknown schema, and PID/`procStart` mismatches can also collapse the live set incorrectly.

   **Required correction:** replace ad hoc status filtering with a versioned registry reader that treats known active statuses, including `idle` and `busy`, plus any live PID with matching `procStart` as live. Fixtures must cover known registry shapes. Unknown schema, missing directory, unreadable directory, parse errors, or PID/`procStart` mismatch must fail closed.

2. **Critical: the backup gate is too late.**

   The doc warns subagents can trigger deletion at [SKILL.md:47](/home/juraj/claude-skills/archive-conversation-forks/SKILL.md:47), but subagents start at [SKILL.md:645](/home/juraj/claude-skills/archive-conversation-forks/SKILL.md:645) and backup is checked at [SKILL.md:685](/home/juraj/claude-skills/archive-conversation-forks/SKILL.md:685).

   **Required correction:** make an immutable, verified, run-scoped snapshot a precondition for starting the procedure at all, including before subagents. Verify hashes, file counts, restoreability, and metadata coverage.

3. **Critical: final live re-read does not protect new dependencies.**

   [SKILL.md:680](/home/juraj/claude-skills/archive-conversation-forks/SKILL.md:680) drops newly live files from the move list, but does not recompute files they now depend on.

   **Required correction:** require the target project to be quiescent during mutation where possible. Always recompute the full plan under the final live set, then re-issue the complete approval packet if anything changed.

4. **Critical: parse errors can hide content or lineage.**

   [SKILL.md:245](/home/juraj/claude-skills/archive-conversation-forks/SKILL.md:245) silently skips malformed JSONL lines.

   **Required correction:** parse-dirty transcripts are unsafe to archive unless repaired or manually reviewed from raw bytes.

5. **Critical: operator approval can be based on incomplete evidence.**

   Raw hashes include tool and structured content at [SKILL.md:255](/home/juraj/claude-skills/archive-conversation-forks/SKILL.md:255), but review previews keep only text blocks at [SKILL.md:260](/home/juraj/claude-skills/archive-conversation-forks/SKILL.md:260).

   **Required correction:** build an evidence bundle from full raw messages, with exact raw content available for every unique fingerprint. Capped previews may be UI only, never the evidence source.

6. **Critical: archive and restore are not transactional.**

   Moves and manifest writing at [SKILL.md:688](/home/juraj/claude-skills/archive-conversation-forks/SKILL.md:688), plus the restore one-liner at [SKILL.md:697](/home/juraj/claude-skills/archive-conversation-forks/SKILL.md:697), can leave incomplete state or overwrite newer files.

   **Required correction:** replace direct moves with a transactional copy-to-archive flow: write-ahead manifest, copy, hash-verify, fsync, then remove the original only after verification. Restore must be no-clobber, live-aware, and transaction logged.

7. **High: agents can become an archive authority.**

   Agents emit debris/archive JSON at [SKILL.md:647](/home/juraj/claude-skills/archive-conversation-forks/SKILL.md:647) and [SKILL.md:658](/home/juraj/claude-skills/archive-conversation-forks/SKILL.md:658), then Step 6 aggregates.

   **Required correction:** agents may emit evidence summaries only, not archive lists. A deterministic planner owns candidate sets, and the orchestrator validates every final decision against planner output and attached evidence.

8. **High: restore ignores the metadata hazard the doc names.**

   [SKILL.md:57](/home/juraj/claude-skills/archive-conversation-forks/SKILL.md:57) says missing session-index metadata can cause re-deletion, and the cleanup warning includes `.jsonl` and `.cast` files, but backup and restore mainly handle JSONLs.

   **Required correction:** define and verify the full swept/session-state surface needed for restore, including `.cast` when in scope. Back up that state with the transcripts, or provide a verified Claude-native import/reindex path that rebuilds it.

9. **High: store selection can target the wrong project.**

   [SKILL.md:183](/home/juraj/claude-skills/archive-conversation-forks/SKILL.md:183) recommends file count and recent timestamps, while the procedure elsewhere says timestamps are noisy.

   **Required correction:** identify the store from transcript `cwd`, session IDs, registry entries, and slug candidates; stop if ambiguous.

10. **High: durable-artifact override lacks a proof gate.**

    [SKILL.md:640](/home/juraj/claude-skills/archive-conversation-forks/SKILL.md:640) allows archiving high-unique forks if an artifact captures them.

    **Required correction:** do not archive high-unique forks until their unique content is ingested into a verified durable artifact or retained elsewhere with a traceable evidence map from exact raw messages to preserved content.

11. **High: recovery claims overstate safety.**

    [README.md:5](/home/juraj/claude-skills/archive-conversation-forks/README.md:5) says "fully recoverable"; [SKILL.md:16](/home/juraj/claude-skills/archive-conversation-forks/SKILL.md:16) says "everything stays recoverable." The procedure itself lists failure modes where this is false.

    **Required correction:** make recoverability a checked invariant of the procedure: verified backup, transaction log, manifest, metadata coverage, and restore dry-run. README language should reflect enforced state, not promise it.

12. **Medium: cleanup instructions contradict their own gate strength.**

    The heading says disable the sweep first at [SKILL.md:18](/home/juraj/claude-skills/archive-conversation-forks/SKILL.md:18), but [SKILL.md:44](/home/juraj/claude-skills/archive-conversation-forks/SKILL.md:44) says default titling is safe even with the sweep active.

    **Required correction:** separate mandatory preflight gates from optional mitigations, so the operator knows exactly what blocks the run.

13. **Medium: settings snippets are not merge-safe.**

    Whole-object JSON examples at [SKILL.md:35](/home/juraj/claude-skills/archive-conversation-forks/SKILL.md:35) and [SKILL.md:80](/home/juraj/claude-skills/archive-conversation-forks/SKILL.md:80) can cause a user to overwrite existing settings.

    **Required correction:** provide a settings patcher or validated merge command with backup, rollback, and post-write verification. Avoid hand-edited whole-object snippets.

14. **Medium: exact containment rests on truncated hashes.**

    [SKILL.md:256](/home/juraj/claude-skills/archive-conversation-forks/SKILL.md:256) truncates MD5 to 12 hex chars; [SKILL.md:560](/home/juraj/claude-skills/archive-conversation-forks/SKILL.md:560) calls 0-unique containment exact.

    **Required correction:** final zero-unique archive decisions must compare canonical preservation records containing every field needed for rendering, replay, restore, and audit. Hashes may index, not prove containment. Fields intentionally excluded from content identity must be named explicitly.

15. **Medium: overloaded terms make the procedure easy to misimplement.**

    `locked`, `loadbearing`, "phantom source," "archive candidate," C5/C6, and "recall" are used across [SKILL.md:417](/home/juraj/claude-skills/archive-conversation-forks/SKILL.md:417), [SKILL.md:475](/home/juraj/claude-skills/archive-conversation-forks/SKILL.md:475), and [SKILL.md:516](/home/juraj/claude-skills/archive-conversation-forks/SKILL.md:516). Some prose says phantom archive candidates "must not be" sources at [SKILL.md:512](/home/juraj/claude-skills/archive-conversation-forks/SKILL.md:512), while redundant sources can be archived.

    **Required correction:** encode the glossary into typed data structures and proof/code names: protected source, redundant source, consumer, candidate, archived. Prose must mirror those names exactly.

16. **Medium: pseudocode is too large to audit as a safety mechanism.**

    [SKILL.md:191](/home/juraj/claude-skills/archive-conversation-forks/SKILL.md:191) says the Python is not turnkey, yet the procedure relies on humans wiring stubs correctly.

    **Required correction:** ship a tested planner with fixtures, dry-run output, schema checks, and property tests. Treat prose as documentation for the tool, not the executable safety mechanism.

17. **Low: title guidance can still reduce auditability.**

    The title section bans terse phrases well, but [SKILL.md:712](/home/juraj/claude-skills/archive-conversation-forks/SKILL.md:712) encourages dense family sub-label titles that may still hide archive rationale from future readers.

    **Required correction:** generate titles from the audited manifest schema: role, parent/canonical, retained/archive reason, and residue summary. Titles should be derived audit artifacts, not free-form summaries.

18. **Low: README is too confident for a hazardous operator procedure.**

    [README.md:28](/home/juraj/claude-skills/archive-conversation-forks/README.md:28) compresses safety into "Move, never delete" and manifest recovery. That invites a reader to trust the archive path more than the backup and verification gates.

    **Required correction:** make the README a gate checklist and threat-model entry point, not a capability pitch. It should name mandatory preconditions before describing archive benefits.
