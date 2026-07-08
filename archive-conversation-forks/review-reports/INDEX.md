# Archive Conversation Forks Adversarial Review Reports

Generated 2026-06-19.

Review standard: correctness first. Severity ranks risk; it does not filter scope. Language, title, README, and presentation findings stay in scope when they affect operator judgment, auditability, recovery, proof comprehension, or implementation correctness. Required corrections describe the best-known correctness repair. Risk notes do not substitute for repair.

Primary action file to hand to Claude:

- `codex-consolidated-report.md` - integrated report, with findings merged across all agents and local verification.

Source notes:

- `agent-operational-safety.md` - operational hazards in `SKILL.md`, especially live-session handling, backups, restore, parsing, mutation gates, and recovery prose.
- `agent-python-model.md` - Python model and fuzz harness review.
- `agent-lean-proof.md` - Lean proof and proof-README review.
- `agent-consistency.md` - consistency review across skill prose, Python model, Lean proofs, and READMEs.
- `agent-language.md` - reader-burden, terminology, title, warning, and guarantee-language review.

Build note: the Lean proof build succeeded in Docker with:

```bash
docker run --rm --entrypoint /bin/sh \
  -v /home/juraj/claude-skills/archive-conversation-forks/proofs:/work \
  -w /work leanprovercommunity/lean:latest \
  -lc '/home/lean/.elan/bin/lake build'
```

The README command using `leanprovercommunity/lean:4.10.0` did not work because that tag was not found. The local `latest` image contained Lean 4.10.0 but needed the shell entrypoint.
