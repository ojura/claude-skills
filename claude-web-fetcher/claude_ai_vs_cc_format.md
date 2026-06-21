# claude.ai export ↔ Claude Code JSONL — format & signature map

Reference for teleporting/merging claude.ai conversation data into Claude Code
session JSONL. Figures measured against a 145-conversation account export
(`conversations.json`, 116 MB) and 220 local CC session JSONLs, then
**adversarially re-verified by a 10-agent pass (2026-06-21)**. Every number below
was reproduced by independent re-measurement except where a ⚠ correction is noted.

---

## 1. Where thinking text + signatures live (by surface / fetch mode)

| Surface | Access | raw thinking | **signature** | content blocks | tree | backend conv object |
|---|---|:--:|:--:|:--:|:--:|:--:|
| **Account export** `conversations.json` | Settings → Export | ✅ | ✅ key 100%, **non-empty 84%** (4481/5337) | ✅ full | ✅ full forest | ❌ (7-key projection) |
| Load API `rendering_mode=messages` | fetcher `get_conversation` | ✅ | ❌ **no `signature` key** | ✅ | ✅ w/ `tree=True` | ✅ (top-level) |
| Load API `rendering_mode=raw` / none | `GET …` | ✅ text-only | ❌ | ❌ no content array | ✅/leaf | ✅ |
| Load API bare (no params) | `GET …` | text | ❌ | ❌ | **active-leaf only (104 vs 144)** | ✅ |
| **Live completion SSE** | `POST …/completion` | ✅ `thinking_delta` | ❌ **no `signature_delta`** | streamed | n/a | n/a |
| **CC JSONL** (local) | `~/.claude/projects/…` | ✅ | ✅ key 100% | ✅ (≈1 block/line) | ✅ tree+branches | n/a |

**Signatures are export-exclusive** — present only in the account export and local
CC JSONL; never on any load-API rendering mode, never in the live stream. ⚠ But
**16% of export thinking blocks are unsigned** (`signature: null`, 856/5337 across
16 conversations) — those teleport only as *unsigned* thinking. The fetcher
(`rendering_mode=messages`) cannot obtain signatures.

## 2. claude.ai load-API fetch modes (two orthogonal axes)

| Param | Value | Effect (measured) |
|---|---|---|
| `rendering_mode` | `messages` | content-block array; **signature key removed**; blocks `{text:189, thinking:254, tool_use:287, tool_result:287}` on test convo |
| | `raw` / *(absent)* | **text-only**, no `content[]` array |
| `tree` | `True` | entire **forest** (all branches) — 144 msgs |
| | *(absent)* | **active-leaf path only** — 104 msgs |
| `render_all_tools` | `true` | include `tool_use`/`tool_result` blocks |

`current_leaf_message_uuid` is present at top-level in **all** live modes; absent from the export.

## 3. Conversation-level object: export vs live backend vs CC session

| Field | Export | Live API | CC session |
|---|:--:|:--:|:--:|
| `uuid` / `name` | ✅ | ✅ | `sessionId` / `aiTitle` |
| `summary` (string) | ✅ | ✅ | `summary`/`away_summary` line |
| `created_at`/`updated_at` | ✅ | ✅ | per-line `timestamp` |
| `account` | ✅ | ✅ | — |
| **`current_leaf_message_uuid`** | ❌ | ✅ | (leaf = file tail) |
| `effective_thinking_mode`/`effort_level`/`settings`/`model` | ❌ | ✅ | `message.model` |
| `is_starred`/`is_temporary`/`platform`/`is_wiggle_enabled` | ❌ | ✅ | — |
| `cwd`/`gitBranch`/`version`/`entrypoint` | ❌ | ❌ | ✅ |

Export conversation object = exactly **7 keys** `{uuid, name, summary, created_at, updated_at, account, chat_messages}` — a trimmed projection of the live backend object.

## 4. Block types & per-type fields: claude.ai export vs CC JSONL

Export block totals (✓ exact): `text` 6607 · `thinking` 5337 · `tool_use` 4493 · `tool_result` 4460 · `flag` 23.

| Block | claude.ai export fields | CC `message.content` fields | core (bijective) | counterpart |
|---|---|---|---|:--:|
| **text** | `text, citations, citations_grouping_mode, flags, start/stop_timestamp, type` | `text, type` | `text` | ✅ |
| **thinking** | `thinking, signature, summaries, cut_off, truncated, alternative_display_type, flags, start/stop_timestamp, type` | `thinking, signature, type` | `thinking`+`signature` | ✅ |
| **tool_use** | `id, name, input, integration_name, integration_icon_url, mcp_server_url, is_mcp_app, approval_key, approval_options, context, display_content, message, icon_name, flags, start/stop_timestamp, type` | `caller, id, input, name, type` (`caller`={type:direct}) | `id`+`name`+`input` | ✅ |
| **tool_result** | `tool_use_id, content, structured_content, meta, name, is_error, integration_*, mcp_server_url, display_content, message, icon_name, flags, start/stop_timestamp, type` | block `{content, tool_use_id, type}` + `is_error` *(optional, 58%)*; line-level `toolUseResult`, `sourceToolAssistantUUID` | `tool_use_id`+`content` | ✅ |
| **flag** | `flag (=self_harm_risk ×23), helpline{…}, flags, start/stop_timestamp, type` | — | — | ❌ claude.ai-only |
| **image** | (inside tool_result content) | `source{data,media_type,type}, type` | — | placement differs |

Note: `flags` (the per-block field) ≠ `flag` (block type). `flags` is `null` (20905×) or a flat list `['self_harm_risk']` (15×), never a dict.

## 5. `tool_result.content` sub-types (claude.ai) → CC/API

Export `content` is **always a list** (4460/4460); partition (✓ exact, sums to 4460):

| claude.ai sub-type | count | CC/API counterpart |
|---|--:|:--|
| `list<text>` | 3362 | ✅ `text` |
| `list<image>` | 378 | ✅ `image` (item keys `type,file_uuid`) |
| `list<local_resource>` | 506 | ❌ none — sandbox file refs (`/mnt/user-data/outputs/*`) |
| `list<knowledge>` | 201 | ❌ none — `web_search` results (rich payload) |
| `list<rag_reference>` | 1 | ❌ none |
| `list<local_resource,text>` | 8 | partial |
| `list<>` (empty) | 4 | — |

**Bidirectional gap:** export has `local_resource`/`knowledge`/`rag_reference` that CC lacks; CC has a `tool_reference` content type (228×) the export lacks. CC `tool_result.content` is bare-string 95% (33837) / list 5% (1964).

## 6. Structural model

| Aspect | claude.ai export | CC JSONL |
|---|---|---|
| Atomic unit | content **block** | ≈ one block **per line** ⚠ (99.9% — 61 multi-block assistant lines exist corpus-wide) |
| API response | many blocks in one `chat_message` | lines sharing `message.id`/`requestId` |
| `tool_result` placement | **inside** the assistant message | separate `role:user` line + `toolUseResult` + `sourceToolAssistantUUID` (all 35801) |
| Threading | `parent_message_uuid` | `parentUuid` (+ `logicalParentUuid` cross-session) |
| Shape | **forest** — root sentinel `00000000-0000-4000-8000-000000000000` (sole out-of-set parent, 261×); 69/145 (48%) branch; HRZZ = 15 branch pts/max 4/1 root; 39 multi-root | tree; branches via regen |
| Line types (12) | — | `user, assistant, attachment, system, file-history-snapshot, custom-title, queue-operation, last-prompt, ai-title, agent-name, mode, permission-mode` |

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

Not a bijection obstacle — the API accepts arbitrary historical tool names; tools pass through verbatim. (Note: CC's orchestration tool is `Agent`, not `Task`.)

## 8. Timing (symmetric mismatch)

| | claude.ai export | CC JSONL |
|---|---|---|
| per-message | `created_at`, `updated_at` | per-line `timestamp` (exactly one) |
| per-block | `start_timestamp` + `stop_timestamp` (interval) | — |
| durations | — | `durationMs` on `system` lines (2792) + `toolUseResult` (35, on user lines); **assistant lines: 0** |

## 9. Bijection ledger

| Class | Items | Round-trips? |
|---|---|:--:|
| **Native core** | role, block order, `text`, `thinking`+`signature`, `tool_use`(id/name/input), `tool_result`(id/content), parent-pointer tree, sentinel ↔ `parentUuid:null` | ✅ |
| **Escrow** (one side only) | export: timestamps, `flags`, `summaries`, `cut_off`/`truncated`, `alternative_display_type`, `citations`, integration/MCP/approval/`structured_content`/`meta`, conv `summary` · CC: `usage`, `requestId`, `message.id`, `model`, `stop_reason`, `version`/`cwd`/`gitBranch`, `toolUseResult`, `sourceToolAssistantUUID`, `isSidechain`/`forkedFrom`/`slug`, sidecar lines | ⚠ only with escrow |
| **Hard gaps** | →CC: `flag`, `local_resource`/`knowledge`/`rag_reference` · →claude.ai: `toolUseResult` richness, `tool_reference`, image-as-direct-content, sidecar lines · both: active-leaf (`current_leaf_message_uuid`) | ❌ irreducible |

**Verdict:** clean tree/forest isomorphism on the native core; metadata = symmetric escrow; irreducible losses = a few claude.ai-only content types + CC tool-execution richness.

**"Super-complete dump"** = merge **export** (signatures + raw thinking + full block metadata + forest) with one **live fetch** (active-leaf + backend object), keyed on message uuid. The fetcher alone cannot supply signatures.

---

### 10-agent verification corrections (2026-06-21)
- Signatures: key present 5337/5337, **non-empty 4481/5337 (84%)**; 856 `null` across 16 convos (was claimed 100%).
- CC one-block-per-line is **~99.9%**, not absolute — 61 multi-block assistant lines exist (12 sessions, CC 2.1.132–2.1.183).
- Load-API "0 signatures" holds; the **"1591" denominator was spurious** (test convo has 254 thinking blocks).
- `current_leaf_message_uuid` in export appears in **4 messages** of conv `49b703ef` (3 human text + 1 assistant `view` tool_result), not "once/once".
- `"compacted"` ×11: **10 in message content, 1 in a conversation `summary` field** (`f44d47d4`).
- CC `tool_result.is_error` is **optional** (58%).

---

## Live-API signature enumeration — exhaustive negative (2026-06-21)

Question: does **any** live claude.ai read surface expose the thinking-block `signature`? Driven via CDP against the real logged-in client (conv `79f1c713`, org `54e1eaf8`), 10-director adversarial sweep + 1 dedicated prober. **Answer: no.** Every client-reachable read surface strips it server-side; the export's non-render serializer is the only carrier. Do not re-investigate.

**REST reads — 37 GETs, 0 signatures:**
- content GET `rendering_mode=messages` → thinking blocks ship with **no `signature` key** (oracle msg `019eb6fc`: absent, not null).
- +13 signature-fuzz params (`include_signatures`, `scan_mode=indexed`, `block_policy=full`, `fields=signature`, `omit=none`, `raw_thinking`, `debug`, `since=0`, …) → **byte-identical** response — silently ignored.
- `rendering_mode=raw` (GET) → content-stripped (0 thinking blocks).
- `rendering_mode=export/full/stored/debug`, `consistency=linearizable/full` → **400**; `consistency=eventual`/dropped → identical stripped.
- v2-singular, per-message (`/messages/{uuid}` raw+bare), `/messages`, `/blocks`, `chat_messages/{uuid}`, `messages/{uuid}`, `data_exports` (org+account), `/export`, `compliance/{conv}` → **404**; `current_user_access` → 200 with no export href.

**SSE streams (all client-reachable routes enumerated):**
- `completion` → `thinking_delta`, **no `signature_delta`** (signature is computed post-generation, never streamed).
- `debug_block` ("BlockScan") → **403 permission_error** (access-gated internal feature). Per the bundle it's a *moderation* scanner anyway (body `{}`; emits `block_scan_progress`/`block_scan_result` surface-attribution; `Dke` is its surface-indexing map, never serialized onto the wire; parser references no `signature`) — not a content reader. One approved POST fired → 403, **zero write-footprint**.
- `side_question` → generation-class (streams new blocks; no stored-block echo).

**Why:** claude.ai is **server-authoritative** — the client never replays signed thinking (unlike the dev API, where the client owns state and must replay it), so the signature is omitted at the render layer across every reachable read serializer and survives only in the export's distinct non-render projection.

## Programmatic account export (signatures, scriptable) — verified 2026-06-21

The account export is the only signature-bearing surface, and it is **fully scriptable** — no email, no manual Settings click. Implemented in `claude_web.py` as `trigger_export` / `export_signed_url` / `poll_export` / `download_export` (one-shot: `export_account`).

```
1. POST /api/organizations/{org}/export_data              -> 202 {"nonce": "..."}
     body: { conversations_start_date?, conversations_end_date?, skip_file_content? }
            ISO8601 dates; omit both to export everything. (UI period helpers: 30d / 90d / custom)
2. POST /api/organizations/{org}/export_signed_url/{nonce} -> 200 {... storage.googleapis.com signed url ...}
     SINGLE-USE: a successful POST consumes the link (re-POST -> 404 "export_link_used").
     GET -> 405; while still processing -> non-200, so poll until 200.
3. GET  <signed GCS url>                                   -> the zip (public/signed; no Cloudflare, no cookies)
```

The zip holds `conversations.json` (+ `users.json`, `memories.json`, `projects/`) — same structure as the manual export, **with signatures**. Verified end-to-end: a date-scoped, files-skipped export (06-14..06-22) downloaded to 8 conversations / 235 thinking blocks / 138 non-empty signatures.

```python
with ClaudeWeb() as c:   # default CDP backend — real Chrome, no session_key
    c.export_account("/tmp/export.zip", start_date="2026-06-14", end_date="2026-06-22", skip_files=True)
```

So the **super-complete dump is scriptable** (no email, no manual Settings click): `export_account(...)` for signatures ⊕ a live `get_conversation(...)` for the active-leaf/backend object, joined on message uuid.

### Deferred nits (acknowledged in the 10-Opus review 2026-06-21; low priority)
- patchright `_evaluate` ignores `timeout` (Playwright has no per-call eval timeout) — harmless backend asymmetry.
- `_init_patchright` leaks the Playwright driver if `launch()` fails after `start()` (pre-existing; wants a try/finally).
- `_get`/`_post` use a non-`/` `path` verbatim as an absolute URL — latent foot-gun; no current caller (could assert `path.startswith("/")`).
- The CDP backend opens a `claude.ai/new` tab when none exists and never closes it (by design; could track + close in `close()`).
- `_ensure_daemon` doesn't `POST /reconnect` when the daemon is reachable but in a terminal state (failed/no_chrome) — usually self-heals via the daemon's own retry.
- Enhancement: the CDP backend could capture the CCR gating headers off the live tab (Network domain) so Code-web `list_sessions` works without the patchright fallback.
