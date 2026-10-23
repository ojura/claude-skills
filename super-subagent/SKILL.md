---
name: super-subagent
description: Prepare an addressable Claude Code worker that inherits the calling conversation or another selected worker, then start it with SendMessage. Supports arbitrary model IDs that the Agent tool schema rejects, generates a readable agent name from the description, and links workflow agents into the session's top-level subagents directory for resumption. Use when the user asks for a super subagent, a nested context-inheriting worker, a child of another worker, an unrestricted provider model, or recovery of workflow subagents after a workflow failure.
---

# Super subagent

Prepare a child from the conversation that invokes the helper. The helper writes new agent files and prints the agent ID for `SendMessage`.

For every automatic call, choose a fresh 32-character lowercase hexadecimal invocation ID. Put its literal value in the environment assignment in the same Bash tool call. Choose the value yourself; never ask the user for it or reuse the example below.

```bash
SUPER_SUBAGENT_INVOCATION_ID=7c5e3f4918a2d6b04e9f1a7c3d8b2650 \
  python3 /home/juraj/claude-skills/super-subagent/scripts/super_subagent.py prepare \
  "DESCRIPTION"
```

Then start it:

```text
SendMessage({to: "<agent_id>", message: "<directive>"})
```

The helper never edits the parent or source transcript. New files use mode `0600` and are published with atomic no-replace hard links, so a destination created concurrently is not overwritten. Ordinary errors and handled signals remove files published by the failed invocation. SIGKILL or power loss between the two file publications can still leave one file because the helper has no recovery journal.

## Caller selection

`prepare` selects its source automatically:

- From the main conversation, it writes `fork-context-ref` plus a seed sidechain message. Claude Code loads the main prefix from the session JSONL.
- From an existing worker, it copies the completed local sidechain and assigns new UUIDs and prompt IDs. It retains the root `fork-context-ref` when one exists. General-purpose and specialized workers without that record are also supported.

The helper identifies the caller from the exact active assistant response containing the unresolved Bash invocation. It parses shell command positions rather than matching arbitrary argument text, checks the main transcript, top-level workers, and workflow workers, and requires the invocation ID from the helper's environment assignment to match the recorded command. A recent interrupted call therefore cannot be selected while the current tool call is still being written. Automatic selection also requires a parseable timestamp written no more than ten seconds before this helper process started. It waits up to five seconds by default for Claude Code to publish the current tool call, then requires exactly one caller.

Use an explicit source when automatic detection is unavailable or ambiguous:

```text
--from-main
--from-agent-id AGENT_ID
--from-agent-name NAME
```

Automatic selection stops with an error when the invocation ID is absent or malformed rather than guessing from unresolved calls. A manually entered helper command that omits the environment assignment must use one of the explicit source options. `--from-agent-name` must match exactly one metadata file. `--from-agent-id` is unambiguous.

## Fork another worker

Any caller can prepare a child from another worker's completed context:

```bash
python3 /home/juraj/claude-skills/super-subagent/scripts/super_subagent.py prepare \
  "Continue their investigation" \
  --from-agent-id aSOURCE_AGENT_ID
```

Use `--from-agent-name NAME` instead when the name identifies exactly one worker. The source may be a top-level or workflow worker in the current session. The helper reads the source transcript, writes a remapped copy into the new child's files, and does not modify the source.

Change the discovery wait with:

```text
--caller-discovery-timeout SECONDS
```

The value must be greater than zero and no more than 60 seconds.

## Worker context copying

When the caller is a worker, `prepare`:

1. retains its `fork-context-ref` when one exists;
2. follows the parent links of the exact active helper response;
3. keeps parent alias records and recovers completed parallel tool calls and results;
4. removes the active helper response and earlier interrupted tool responses while retaining later completed turns;
5. rewires parent links around removed records;
6. orders completed blocks of each assistant response by `apiBlockIndex`, places their tool results in call order, and links reordered history as one sequence;
7. copies the completed records into the new transcript;
8. remaps `uuid`, `promptId`, `parentUuid`, local `logicalParentUuid`, `sourceToolAssistantUUID`, and compact-metadata UUID references;
9. leaves the source transcript unchanged.

A general-purpose or specialized worker without `fork-context-ref` receives the copied local history plus a new fork-instruction message. A compacted sidechain is accepted when its local parent and compact-metadata UUID references can all be resolved. Missing ancestors or omitted referenced records stop creation rather than producing incomplete history. Content that the running model has not written to the transcript cannot be copied.

`--parent-last-uuid UUID` selects an earlier complete boundary when the source is the main conversation. It is invalid when the source is a worker.

Ordering uses only records with the same assistant message ID and unique nonnegative integer block indices. When indices are absent, invalid or duplicated, it keeps the recovered order. Unrelated messages are not moved. Tool-result contents and call IDs stay associated; mixed user content or result batches that cannot follow call order without splitting a message also keep their recovered order. Compaction boundaries and their UUID references are preserved.

## Model selection

The default is a built-in fork with `model: inherit`:

```bash
SUPER_SUBAGENT_INVOCATION_ID=REPLACE_WITH_FRESH_32_HEX_DIGITS \
  python3 /home/juraj/claude-skills/super-subagent/scripts/super_subagent.py prepare \
  "Review the proxy retirement tests"
```

To use any other provider model ID:

```bash
SUPER_SUBAGENT_INVOCATION_ID=REPLACE_WITH_FRESH_32_HEX_DIGITS \
  python3 /home/juraj/claude-skills/super-subagent/scripts/super_subagent.py prepare \
  "Review the proxy retirement tests" \
  --model gpt-5.6-sol \
  --fork-another-model
```

Claude Code forces built-in forks to `model: inherit`. An explicitly selected model therefore uses regular-agent resume metadata while retaining the inherited context and fork instructions.

Prompt caches are model-specific. The selected model cannot reuse the parent model's cached prefix, so the inherited conversation may be processed and cached again under that model. The helper prints this warning and requires `--fork-another-model` as acknowledgement.

Optional creation controls:

```text
--name NAME          replace the generated readable name
--agent-id ID        use a predetermined agent ID
--spawn-depth N      override depth; defaults to 1 from main or parent depth plus 1
```

## Resume workflow agents

Workflow agents are stored under:

```text
<session>/subagents/workflows/wf_.../
```

Expose every agent from a workflow to `SendMessage`:

```bash
python3 /home/juraj/claude-skills/super-subagent/scripts/super_subagent.py \
  link-workflow wf_01234567-abc
```

Select individual agents by repeating `--agent-id`:

```bash
python3 /home/juraj/claude-skills/super-subagent/scripts/super_subagent.py \
  link-workflow wf_01234567-abc \
  --agent-id a1111111111111111 \
  --agent-id a2222222222222222
```

The workflow directory must be a direct child of the selected session's `subagents/workflows/` directory. Foreign-session paths are rejected because their `fork-context-ref` records name another main transcript. Each filename must contain a Claude Code recipient ID: `a` plus 16 lowercase hexadecimal digits, optionally preceded by a 1-63 character ASCII name and `-`.

Before publication, the helper parses the selected metadata and transcript. It requires at least one `isSidechain: true` record with the exact filename agent ID, a UUID, and a parseable timestamp. The newest such record must have a complete local `parentUuid` chain because Claude Code uses it as the resume leaf. A completed assistant record and `fork-context-ref` are not required. When a fork reference is present, the helper still verifies that it names this agent and the selected parent session.

By default, the top-level JSONL and metadata are relative symlinks. Existing matching links are accepted. Any different file or link stops the operation before publication.

Override the resumed model without changing the workflow's stored metadata:

```bash
python3 /home/juraj/claude-skills/super-subagent/scripts/super_subagent.py \
  link-workflow wf_01234567-abc \
  --agent-id a1111111111111111 \
  --model gpt-5.6-sol
```

When the source metadata has `isFork: true` or `agentType: fork`, the top-level metadata copy removes `isFork`, uses `agentType: general-purpose`, and stores the selected model so Claude Code preserves it on later resumes. If publication fails partway through several agents, the helper removes every destination created by that invocation and leaves preexisting matching links unchanged.

Continue a linked agent with:

```text
SendMessage({to: "<agent_id>", message: "Continue from your transcript. <directive>"})
```

## Verification

```bash
python3 -m unittest discover \
  -s /home/juraj/claude-skills/super-subagent/tests \
  -v
```
