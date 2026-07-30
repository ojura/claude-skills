# claude.ai export and Claude Code JSONL: format and signature map

Reference for teleporting/merging claude.ai conversation data into Claude Code
session JSONL. Figures measured against a 145-conversation account export
(`conversations.json`, 116 MB) and 220 local CC session JSONLs, then
**adversarially re-verified by a 10-agent pass (2026-06-21)**. Every number below
was reproduced by independent re-measurement except where a ⚠ correction is noted.

---

## 1. Where thinking text + signatures live (by surface / fetch mode)

| Surface | Access | plaintext thinking (=summarized) | **signature** (=encrypted RAW thinking) | content blocks | tree | backend conv object |
|---|---|:--:|:--:|:--:|:--:|:--:|
| **Account export** `conversations.json` | Settings → Export | ✅ | ✅ key 100%, **non-empty 84%** (4481/5337) | ✅ full | ✅ full forest | ❌ (7-key projection) |
| Load API `rendering_mode=messages` | fetcher `get_conversation` | ✅ | ❌ **no `signature` key** | ✅ | ✅ w/ `tree=True` | ✅ (top-level) |
| Load API `rendering_mode=raw` / none | `GET …` | ✅ text-only | ❌ | ❌ no content array | ✅/leaf | ✅ |
| Load API bare (no params) | `GET …` | text | ❌ | ❌ | **active-leaf only (104 vs 144)** | ✅ |
| **Live completion SSE** | `POST …/completion` | ✅ `thinking_delta` | ❌ **no `signature_delta`** | streamed | - | - |
| **CC JSONL** (local) | `~/.claude/projects/…` | ✅ | ✅ key 100% | ✅ (≈1 block/line) | ✅ tree+branches | - |

**Only the account export and the local CC JSONL carry signatures.** No load-API rendering
mode has them and the live stream never sends them. Around a sixth of the thinking blocks in
an export are unsigned, with `signature: null`, and those teleport as unsigned thinking. The
fetcher's load-API path (`rendering_mode=messages`) cannot see signatures at all; its
`export_account` pipeline (§ Programmatic account export) is what gets them.

**In Claude API and Claude Code terms, `signature` holds the encrypted raw thinking; it is
not a signature in the usual sense.** Each block has three pieces. The `signature` is the full
raw reasoning, encrypted: its length varies with the content, running about 2.6 times the
plaintext and ranging from 192 bytes to 110 KB, which is not how a real signature behaves,
since that would be one constant size. The readable `thinking` field is the summarised
thinking, which is the raw reasoning rewritten in the first person and shortened, typically a
few hundred bytes. claude.ai also has a `summaries[].summary` field, which holds short titles
for the UI, appears only in the export, and is never the same text as the plaintext; it is not
what "summarised" means here. So everything other than the export, meaning the load API and
the live `thinking_delta`, gives you the summarised thinking and never the encrypted raw. The
only places the raw reasoning can be recovered from are the export and the CC JSONL. A block
with a null signature has no envelope at all, so only the summary survives. The export writes
`signature: null` where CC writes `signature: ""`; treat them as the same value.

## 2. claude.ai load-API fetch modes (two orthogonal axes)

| Param | Value | Effect (measured) |
|---|---|---|
| `rendering_mode` | `messages` | content-block array; **signature key removed**; blocks `{text:189, thinking:254, tool_use:287, tool_result:287}` on test convo |
| | `raw` / *(absent)* | **text-only**, no `content[]` array |
| `tree` | `True` | the entire **forest**, every branch (144 msgs) |
| | *(absent)* | the **active-leaf path** alone (104 msgs) |
| `render_all_tools` | `true` | include `tool_use`/`tool_result` blocks |

`current_leaf_message_uuid` is present at top-level in **all** live modes; absent from the export.

## 3. Conversation-level object: export vs live backend vs CC session

| Field | Export | Live API | CC session |
|---|:--:|:--:|:--:|
| `uuid` / `name` | ✅ | ✅ | `sessionId` / `aiTitle` |
| `summary` (string) | ✅ | ✅ | `summary`/`away_summary` line |
| `created_at`/`updated_at` | ✅ | ✅ | per-line `timestamp` |
| `account` | ✅ | ✅ | - |
| **`current_leaf_message_uuid`** | ❌ | ✅ | (leaf = file tail) |
| `effective_thinking_mode`/`effort_level`/`settings`/`model` | ❌ | ✅ | `message.model` |
| `is_starred`/`is_temporary`/`platform`/`is_wiggle_enabled` | ❌ | ✅ | - |
| `cwd`/`gitBranch`/`version`/`entrypoint` | ❌ | ❌ | ✅ |

The export's conversation object has exactly seven keys, `{uuid, name, summary, created_at, updated_at, account, chat_messages}`, which is a trimmed version of the live backend object.

## 4. Block types & per-type fields: claude.ai export vs CC JSONL

Export block totals (✓ exact): `text` 6607 · `thinking` 5337 · `tool_use` 4493 · `tool_result` 4460 · `flag` 23.

| Block | claude.ai export fields | CC `message.content` fields | core (bijective) | counterpart |
|---|---|---|---|:--:|
| **text** | `text, citations, citations_grouping_mode, flags, start/stop_timestamp, type` | `text, type` | `text` | ✅ |
| **thinking** | `thinking, signature, summaries, cut_off, truncated, alternative_display_type, flags, start/stop_timestamp, type` | `thinking, signature, type` | `thinking`+`signature` | ✅ |
| **tool_use** | `id, name, input, integration_name, integration_icon_url, mcp_server_url, is_mcp_app, approval_key, approval_options, context, display_content, message, icon_name, flags, start/stop_timestamp, type` | `caller, id, input, name, type` (`caller`={type:direct}) | `id`+`name`+`input` | ✅ |
| **tool_result** | `tool_use_id, content, structured_content, meta, name, is_error, integration_*, mcp_server_url, display_content, message, icon_name, flags, start/stop_timestamp, type` | block `{content, tool_use_id, type}` + `is_error` *(optional, 58%)*; line-level `toolUseResult`, `sourceToolAssistantUUID` | `tool_use_id`+`content` | ✅ |
| **flag** | `flag (=self_harm_risk ×23), helpline{…}, flags, start/stop_timestamp, type` | - | - | ❌ claude.ai-only |
| **image** | (inside tool_result content) | `source{data,media_type,type}, type` | - | placement differs |

Note: `flags` (the per-block field) ≠ `flag` (block type). `flags` is `null` (20905×) or a flat list `['self_harm_risk']` (15×), never a dict.

## 5. `tool_result.content` sub-types (claude.ai) → CC/API

Export `content` is **always a list** (4460/4460); partition (✓ exact, sums to 4460):

| claude.ai sub-type | count | CC/API counterpart |
|---|--:|:--|
| `list<text>` | 3362 | ✅ `text` |
| `list<image>` | 378 | ✅ `image` (item keys `type,file_uuid`) |
| `list<local_resource>` | 506 | ❌ none; these are sandbox file references under `/mnt/user-data/outputs/` |
| `list<knowledge>` | 201 | ❌ none; these are `web_search` results, with a rich payload |
| `list<rag_reference>` | 1 | ❌ none |
| `list<local_resource,text>` | 8 | partial |
| `list<>` (empty) | 4 | - |

**Bidirectional gap:** export has `local_resource`/`knowledge`/`rag_reference` that CC lacks; CC has a `tool_reference` content type (228×) the export lacks. CC `tool_result.content` is bare-string 95% (33837) / list 5% (1964).

## 6. Structural model

| Aspect | claude.ai export | CC JSONL |
|---|---|---|
| Atomic unit | content **block** | roughly one block **per line**, though not always: a small number of assistant lines carry several |
| API response | many blocks in one `chat_message` | lines sharing `message.id`/`requestId` |
| `tool_result` placement | **inside** the assistant message | separate `role:user` line + `toolUseResult` + `sourceToolAssistantUUID` (all 35801) |
| Threading | `parent_message_uuid` | `parentUuid` (+ `logicalParentUuid` cross-session) |
| Structure | a **forest**: branch roots parent off the sentinel `00000000-0000-4000-8000-000000000000`, which is the only parent outside the message set. About half of conversations branch, and some have several roots | a tree, branching where a turn was regenerated |
| Line types (12) | - | `user, assistant, attachment, system, file-history-snapshot, custom-title, queue-operation, last-prompt, ai-title, agent-name, mode, permission-mode` |

**Turning the forest into a tree.** A conversation with several sentinel-parented branches got them by editing or retrying its first message: every one of those roots is a `human` message, and one conversation re-sends its opening seventeen times. CC needs a single root, so `to_cc` adds one root node of its own (`isVirtualRoot`, content `"."`, `parentUuid:null`) and parents every branch off it, which connects the forest into one tree. A conversation that already has a single root gets no extra node. The added node carries no `exportEscrow`, so `to_export` leaves it out and the reverse still matches. Without it CC sees several parentless roots, and walking back from the active leaf never reaches the other branches.

## 7. Tool vocabulary (names disjoint, capabilities overlap)

claude.ai (20 names) ∩ CC (44 names) = **∅** (✓).

| Capability | claude.ai | Claude Code |
|---|---|---|
| web search / fetch | `web_search`, `web_fetch` | `WebSearch`, `WebFetch` |
| shell | `bash_tool` | `Bash` |
| file write/edit/view | `create_file`, `str_replace`, `view` | `Write`, `Edit`, `Read` |
| artifacts | `artifacts` | `artifacts` |
| ask user | `ask_user_input_v0` | `AskUserQuestion` |
| tool search | `tool_search` | `ToolSearch` |
| no counterpart | `google_drive_search`, `conversation_search`, `recent_chats`, `present_files`, `memory_user_edits`, `visualize:*`, `launch_extended_search_task`, `message_compose_v1`, `user_time_v0`, `search_mcp_registry` | `Agent`, `Grep`, `Skill`, `Workflow`, `TaskCreate/Update/Stop/List/Output`, `TodoWrite`, `SendMessage`, `ExitPlanMode`, `mcp__*` |

This does not get in the way of the bijection: the API accepts any historical tool name, so tools pass through unchanged. Note that CC's orchestration tool is `Agent`, not `Task`.

## 8. Timing (symmetric mismatch)

| | claude.ai export | CC JSONL |
|---|---|---|
| per-message | `created_at`, `updated_at` | per-line `timestamp` (exactly one) |
| per-block | `start_timestamp` + `stop_timestamp` (interval) | - |
| durations | - | `durationMs` on `system` lines (2792) + `toolUseResult` (35, on user lines); **assistant lines: 0** |

## 9. Bijection ledger

| Class | Items | Round-trips? |
|---|---|:--:|
| **Native core** | role, block order, `text`, `thinking`+`signature`, `tool_use`(id/name/input), `tool_result`(id/content), parent-pointer tree, sentinel ↔ `parentUuid:null` | ✅ |
| **Escrow** (one side only) | export: timestamps, `flags`, `summaries`, `cut_off`/`truncated`, `alternative_display_type`, `citations`, integration/MCP/approval/`structured_content`/`meta`, conv `summary` · CC: `usage`, `requestId`, `message.id`, `model`, `stop_reason`, `version`/`cwd`/`gitBranch`, `toolUseResult`, `sourceToolAssistantUUID`, `isSidechain`/`forkedFrom`/`slug`, sidecar lines | ⚠ only with escrow |
| **Hard gaps** | →CC: `flag`, `local_resource`/`knowledge`/`rag_reference` · →claude.ai: `toolUseResult` richness, `tool_reference`, image-as-direct-content, sidecar lines · both: active-leaf (`current_leaf_message_uuid`) | ❌ irreducible |

**Verdict:** clean tree/forest isomorphism on the native core; metadata = symmetric escrow; irreducible losses = a few claude.ai-only content types + CC tool-execution richness.

To get everything about a conversation, merge the two sources on message uuid. The export
gives you the signatures holding the encrypted raw thinking, the summarised thinking, the full
block metadata and the whole forest; a single live fetch adds the active leaf and the backend
object. The fetcher can do both halves: `export_account` for the signatures and
`get_conversation` for the active leaf and backend object.

### Corrections to §9

What §9 gets wrong, and what actually holds:

- The mapping is not a clean node-for-node bijection. Where one chat_message ends and the next begins, how a tool_result merges back in, the order of blocks inside a node and the order of siblings all come out of escrow rather than out of the role and the parent tree. Two keys do the grouping: `message.id` marks the node boundary, and an explicit ordinal gives the order of blocks within a node. The `parentUuid` links inside a node cannot carry that order, because they fan out from one line rather than running in a chain.
- **Four ledger omissions** (export→CC→export is NOT identity without them):
  - The top-level `text` on an assistant message is a summarised digest that cannot be derived from anything else. It differs from the concatenated content text in three quarters of assistant messages, and it never equals the first text block. It has to be escrowed, and the round-trip is only correct if the top-level `text` matches too, not merely the concatenated blocks. Human messages never differ this way.
  - `files`, which appears on a few hundred messages, has to be escrowed.
  - `attachments` and their `extracted_content` hold the text of what the user uploaded, so flatten `extracted_content` into text rather than dropping it.
  - Sibling order comes from `created_at`, because the export's own `index` field is always None, and it has to be escrowed.
- Escrow rides on the line envelope, as `exportEscrow`, and never inside the `message.content` blocks. The API rejects unknown keys inside content, which stops the session resuming.
- Per-block uuids have to be derived from the input rather than generated freshly, because the merge matches on them and a random one can never be matched again. Only `tool_use` carries an id of its own; text, thinking and tool_result blocks have none and need one synthesised. Escrow keeps, for each node, the ordered list of line uuids, the `message.id`, and the `parentUuid` of every line. The first and last are not enough, because the links inside a node fan out rather than forming a chain.
- Fields the two formats write differently are normalised rather than carried. The export always writes `is_error`, including when it is false, while CC leaves it out to mean the same thing, so add it going one way and strip the false ones going the other. A null `signature` and an empty one are likewise the same value.
- Block types that hold text but have no CC equivalent are flattened into text, which keeps the words and loses the citation, url and link structure around them: `knowledge` and `attachment.extracted_content`. `local_resource`, `rag_reference` and `flag` are dropped, since they carry almost nothing. `display_content` is recomputed rather than escrowed; it can be derived, and it would otherwise be most of the escrow by volume.
- **Replay:** signed blocks replay verbatim; unsigned ones emit `signature:""`. Either way `thinking='strip'` remains available as the no-thinking mode.
- The round-trip has to be checked on the set of (child, parent) pairs and on the top-level `text`. Comparing nodes or concatenated text alone passes while the tree wiring degrades underneath.
- Thinking exists at two levels and both survive the round-trip when the block is signed. The readable `thinking` field is a summary: the raw reasoning rewritten in the first person and shortened. The `signature` holds the raw reasoning, encrypted. claude.ai's `summaries[]` are short titles for the UI and matter much less. Both levels are carried unchanged. What cannot be recovered is the raw level of a block whose signature is null or empty, where only the summary survives, and the `cut_off`, `truncated`, `alternative_display_type` and `summaries[]` fields, which are escrowed or dropped.

So: a bijection that leans on escrow. It is buildable, and it is enough for the export-to-CC direction that actually gets used. What it cannot carry is CC's internal `toolUseResult` diff scaffolding, which only matters in the other direction.

---

### Corrections to the numbers above
- Signatures: key present 5337/5337, **non-empty 4481/5337 (84%)**; 856 `null` across 16 convos (was claimed 100%).
- One block per line in CC is the overwhelming majority but not a rule: a few dozen assistant lines across a dozen sessions carry several blocks.
- Load-API "0 signatures" holds; the **"1591" denominator was spurious** (test convo has 254 thinking blocks).
- `current_leaf_message_uuid` in export appears in **4 messages** of conv `49b703ef` (3 human text + 1 assistant `view` tool_result), not "once/once".
- `"compacted"` ×11: **10 in message content, 1 in a conversation `summary` field** (`f44d47d4`).
- CC `tool_result.is_error` is **optional** (58%).

---

## Whether any live API surface exposes a signature

No. Every read surface a client can reach strips the signature on the server, and the export's serializer, which does not go through the render path, is the only thing that carries it. The enumeration below is exhaustive, so this does not need investigating again.

**REST reads, none of which return a signature:**
- content GET `rendering_mode=messages` → thinking blocks ship with **no `signature` key** (oracle msg `019eb6fc`: absent, not null).
- Adding parameters that might ask for it (`include_signatures`, `scan_mode=indexed`, `block_policy=full`, `fields=signature`, `omit=none`, `raw_thinking`, `debug`, `since=0` and others) returns a byte-identical response. They are ignored, without an error.
- `rendering_mode=raw` (GET) → content-stripped (0 thinking blocks).
- `rendering_mode=export/full/stored/debug`, `consistency=linearizable/full` → **400**; `consistency=eventual`/dropped → identical stripped.
- v2-singular, per-message (`/messages/{uuid}` raw+bare), `/messages`, `/blocks`, `chat_messages/{uuid}`, `messages/{uuid}`, `data_exports` (org+account), `/export`, `compliance/{conv}` → **404**; `current_user_access` → 200 with no export href.

**SSE streams (all client-reachable routes enumerated):**
- `completion` → `thinking_delta`, **no `signature_delta`** (signature is computed post-generation, never streamed).
- `debug_block` ("BlockScan") returns 403 permission_error, being an internal feature nobody outside has access to. The client bundle shows it is a moderation scanner rather than a way to read content: it posts an empty body, emits `block_scan_progress` and `block_scan_result` for attributing text to a surface, and its parser never mentions `signature`. The single POST that was tried returned 403 and wrote nothing.
- `side_question` → generation-class (streams new blocks; no stored-block echo).

**Why it works this way:** on claude.ai the server holds the conversation, so the client never has to replay signed thinking back to it. The developer API is the opposite: there the client owns the state and must replay it, which is why signatures exist in that direction at all. Since no claude.ai client needs them, the render layer leaves them out of every readable response, and they survive only in the export, which is built by a different serializer.

## Triggering the account export from code

The account export is the only thing that carries signatures, and the whole flow can be driven from code: no email link to wait for, no clicking through Settings. `claude_web.py` implements it as `trigger_export`, `export_signed_url`, `poll_export` and `download_export`, with `export_account` doing all four.

```
1. POST /api/organizations/{org}/export_data              -> 202 {"nonce": "..."}
     body: { conversations_start_date?, conversations_end_date?, skip_file_content? }
            ISO8601 dates; omit both to export everything. (UI period helpers: 30d / 90d / custom)
2. POST /api/organizations/{org}/export_signed_url/{nonce} -> 200 {... storage.googleapis.com signed url ...}
     SINGLE-USE: a successful POST consumes the link (re-POST -> 404 "export_link_used").
     GET -> 405; while still processing -> non-200, so poll until 200.
3. GET  <signed GCS url>                                   -> the zip (public/signed; no Cloudflare, no cookies)
```

The zip holds `conversations.json`, along with `users.json`, `memories.json` and `projects/`. It has the same structure as the manual export and it has signatures in it. Verified end-to-end: a date-scoped, files-skipped export (06-14..06-22) downloaded to 8 conversations / 235 thinking blocks / 138 non-empty signatures.

```python
with ClaudeWeb() as c:   # default CDP backend: real Chrome, no session_key
    c.export_account("/tmp/export.zip", start_date="2026-06-14", end_date="2026-06-22", skip_files=True)
```

So the **super-complete dump is scriptable** (no email, no manual Settings click): `export_account(...)` for signatures ⊕ a live `get_conversation(...)` for the active-leaf/backend object, joined on message uuid.

### Known rough edges, none urgent
- patchright's `_evaluate` ignores `timeout`, because Playwright has no per-call eval timeout. The two backends differ here harmlessly.
- `_init_patchright` leaks the Playwright driver if `launch()` fails after `start()` (pre-existing; wants a try/finally).
- `_get` and `_post` treat a `path` that does not start with `/` as a complete URL. No caller does that today, and an assertion on the leading slash would rule it out.
- The CDP backend opens a `claude.ai/new` tab when none exists and never closes it (by design; could track + close in `close()`).
- `_ensure_daemon` does not `POST /reconnect` when the daemon answers but is in a failed or no_chrome state. The daemon's own retry usually recovers it.
- Enhancement: the CDP backend could capture the CCR gating headers off the live tab (Network domain) so Code-web `list_sessions` works without the patchright fallback.
