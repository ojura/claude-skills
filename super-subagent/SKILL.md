---
name: super-subagent
description: Prepare an addressable Claude Code worker that inherits the calling conversation, then start it with SendMessage. Supports arbitrary model IDs that the Agent tool schema rejects, generates a readable agent name from the description, and links workflow agents into the session's top-level subagents directory for resumption. Use when the user asks for a super subagent, a nested context-inheriting worker, an unrestricted provider model, or recovery of workflow subagents after a workflow failure.
---

# Super subagent

Prepare a child from the conversation that invokes the helper. The helper writes new agent files and prints the agent ID for `SendMessage`.

```bash
python3 /home/juraj/claude-skills/super-subagent/scripts/super_subagent.py prepare \
  "DESCRIPTION"
```

Then start it:

```text
SendMessage({to: "<agent_id>", message: "<directive>"})
```

The helper never edits the parent or source transcript. New files use mode `0600`, and an existing destination stops creation.

## Caller selection

`prepare` selects its source automatically:

- From the main conversation, it writes `fork-context-ref` plus a seed sidechain message. Claude Code loads the main prefix from the session JSONL.
- From an existing worker, it retains the worker's root `fork-context-ref`, copies the completed local sidechain, and assigns new UUIDs and prompt IDs.

The helper identifies the caller by finding the one unresolved Bash tool call that invokes `super_subagent.py prepare`. It checks both the main transcript and top-level agent transcripts. It waits up to five seconds by default for Claude Code to publish the tool call.

Use an explicit source when automatic detection is unavailable or ambiguous:

```text
--from-main
--from-agent-id AGENT_ID
--from-agent-name NAME
```

`--from-agent-name` must match exactly one metadata file. `--from-agent-id` is unambiguous.

Change the discovery wait with:

```text
--caller-discovery-timeout SECONDS
```

The value must be greater than zero and no more than 60 seconds.

## Worker context copying

When the caller is a worker, `prepare`:

1. reads its existing `fork-context-ref`;
2. selects the latest sidechain branch using transcript append order;
3. recovers completed parallel tool calls and their results;
4. excludes the active helper call and every record after the earliest unmatched tool call;
5. copies the completed records into the new transcript;
6. remaps `uuid`, `promptId`, `parentUuid`, local `logicalParentUuid`, and `sourceToolAssistantUUID` references;
7. leaves the source transcript unchanged.

A compacted sidechain is supported when its `logicalParentUuid` resolves inside the source agent JSONL. A missing ancestor stops creation rather than producing incomplete history. Content that the running model has not written to the transcript cannot be copied.

`--parent-last-uuid UUID` selects an earlier complete boundary when the source is the main conversation. It is invalid when the source is a worker.

## Model selection

The default is a built-in fork with `model: inherit`:

```bash
python3 /home/juraj/claude-skills/super-subagent/scripts/super_subagent.py prepare \
  "Review the proxy retirement tests"
```

To use any other provider model ID:

```bash
python3 /home/juraj/claude-skills/super-subagent/scripts/super_subagent.py prepare \
  "Review the proxy retirement tests" \
  --model gpt-5.6-sol \
  --fork-another-model
```

Claude Code 2.1.259 forces built-in forks to `model: inherit`. An explicitly selected model therefore uses regular-agent resume metadata while retaining the inherited context and fork instructions.

Prompt caches are model-specific. The selected model cannot reuse the parent model's cached prefix, so the inherited conversation may be processed and cached again under that model. The helper prints this warning and requires `--fork-another-model` as acknowledgement.

Optional creation controls:

```text
--name NAME          replace the generated readable name
--agent-id ID        use a predetermined agent ID
--spawn-depth N      set the metadata spawn depth, default 1
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

By default, the top-level JSONL and metadata are relative symlinks. Existing matching links are accepted. Any different file or link stops the operation before publication.

Override the resumed model without changing the workflow's stored metadata:

```bash
python3 /home/juraj/claude-skills/super-subagent/scripts/super_subagent.py \
  link-workflow wf_01234567-abc \
  --agent-id a1111111111111111 \
  --model gpt-5.6-sol
```

When required, the top-level metadata copy removes `isFork`, uses `agentType: general-purpose`, and stores the selected model so Claude Code preserves it on later resumes. If publication fails partway through several agents, the helper removes every destination created by that invocation and leaves preexisting matching links unchanged.

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
