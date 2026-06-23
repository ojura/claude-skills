# Teleporting a claude.ai conversation into Claude Code

Goal: reconstruct one of *your own* claude.ai conversations locally as a Claude Code session
you can `claude --resume`, together with its sandbox working tree, so you can keep working.
Four independent pieces, each through its own channel.

## 1. Messages + thinking (the conversation)

- **Source:** the account data export — `POST /api/organizations/{org}/export_data` → `{nonce}` → `POST export_signed_url/{nonce}` → GCS zip → `conversations.json`. Scripted in `claude_web.py` (`export_account`). The export is the **only** surface that carries thinking-block `signature`s; every live read strips them.
- **Transform:** `bijection.py` `to_cc(convo)` → a loadable CC session JSONL (verified to load + resume via `claude -p --resume`, across a feature matrix). It:
  - synthesizes the metadata CC needs (per-round `message.id`, `model`, `stop_reason`, `usage`, `requestId`, and the active-leaf `last-prompt`/`leafUuid`);
  - threads a single linear `parentUuid` spine in execution order through the role:user tool_result lines;
  - reduces `tool_result.content` to API-valid items — sheds the `uuid` the dev API 400s on, **resolves `{type:image, file_uuid}` references to native base64 `source` blocks** (bytes from `/files/{uuid}/preview`; unresolved → text placeholder; reverse stays lossless via `_orig_content`), converts the invalid types `knowledge`/`local_resource`/`rag_reference` to text;
  - synthesizes valid `tool_use.id` where claude.ai left them null (sentinel-prefixed `toolu_synth_…`), drops empty text blocks, and puts a `"."` placeholder in otherwise-empty user turns (the API rejects empty user content);
  - parks everything non-API in a **line-level `exportEscrow`** key so `to_export()` reverses losslessly (full-corpus round-trip green).
- **thinking mode:** `carry` replays `{thinking, signature}` verbatim. In testing it loaded and continued without a 400, including in the tool-use continuity position; whether the dev API cryptographically *verifies* a claude.ai-minted signature vs merely tolerates it is unproven (call it D1). `strip` is the guaranteed-safe fallback. Null-signature blocks (16%) emit as `signature:""`, which is tolerated.

## 2. `/home/claude` — the working tree

- **Bytes:** `wiggle/download-file?path=<abs path>`; `download-files?paths=&paths=` zips a batch (all-or-nothing).
- **Names (enumeration needs the model):** there is **no client-side directory-listing** for `/home/claude`, *and* the export's own `tool_result` listings cover only a fraction of the tree (HRZZ: ~5% — most files were created by tools that never printed a full listing). So a live `find`/`tar` via the model's bash is the **standard** enumeration step, not a fallback. Use the export's `ls`/`find` outputs as a free partial index (note `local_resource.file_path` is **not** part of it — every one points under `/mnt/user-data/outputs`, never `/home/claude`; it's a §3 `/mnt` index, already covered by `find_files`); for the rest, one model turn runs `find /home/claude > manifest` (then batch `download-file` by path) or tars the tree to `/mnt/user-data/outputs`.
- **Size cap (measured):** the file API enforces a **server-side output-size limit** — `download-file`/`download-files` return **`413 "output_size_exceeded"`** above it. Bisected with real files: a 252.8 MB single file and a 286 MB zip both serve (`200`); the 551 MB `.git` pack rejects (`413`) → cap ≈ **(286, 551] MB** (a round ~500 MB, unconfirmed). It's the *response* size and it's path-agnostic, so tarring `/home/claude` to `/mnt` does **not** help — the tarball exceeded it (which is why claude.ai stranded it in `/tmp`, never reaching `outputs/`). For a tree over the cap, pull files individually by path (each is under it) or split. **Separately, `upload-file` caps writes at 35 MB** (`"File size exceeds 35MB"`) — a different, smaller limit.
- Note `/home/claude` holds whatever the conversation built — including, in some, a self-made git repo with a commit per revision (full provenance).

## 3. `/mnt/user-data` — inputs and presented outputs

- **Bytes — `download-file?path=/mnt/user-data/…` reads the whole mount**, uploads *and* presented outputs, straight off the live VM (verified `200` + raw bytes — the same mechanism as any rootfs path). `find_files(conv)` is the **index**: it enumerates `/mnt/user-data`; then `download-file` each by its path. Wiggle outputs: `FileRef.path` is already `/mnt/user-data/outputs/…`. Uploads: `FileRef.path` is a uuid, but the file sits at `/mnt/user-data/uploads/<FileRef.name>`.
- **Do NOT use `/files/{uuid}/preview` for uploads.** That *asset-store* endpoint purges (404s weeks later), and a `skip_files=True` export carries no bytes — but the **VM mount persists**, so `download-file` still returns them. (Tool-result *images* are the exception: they aren't files in the mount, so they come via `/files/{uuid}/preview`.)
- Uploaded-document text is also the `convert_document` output, or the export's attachment `extracted_content`.

## 4. Base environment

- Rebuild from the manifest, don't ship the rootfs: `FROM ubuntu:24.04` + the captured `apt` (~866) / `pip` (~130) / npm-global / uv lists. Runtimes to match: Python 3.12, Node 22, OpenJDK 21.

## Hydration layout

```
~/.claude/teleports/<conv8>/home/      ← the /home/claude tree, == the session cwd
  CLAUDE.md                            ← generated orientation (path remap + sandbox→CC tool map)
  mnt/user-data/{outputs,uploads}/     ← presented outputs + (surviving) uploads, under the cwd
```

- **`teleport.py` does all of this in one call:** `teleport(conv_uuid, export_json, home_src=<dir|tarball>, client=ClaudeWeb())` → emits the session JSONL (images resolved), hydrates the tree + `/mnt/user-data`, writes `CLAUDE.md`, and returns the `claude --resume` command. Idempotent: deterministic `sessionId`; the home-tree copy is completion-sentinel-gated (a partial run re-converges); the orientation is appended once (sentinel-marked) to any pre-existing `CLAUDE.md` rather than suppressed.
- The JSONL lands in `~/.claude/projects/<slug-of-cwd>/<sessionId>.jsonl`, where the slug = the absolute cwd with every non-alphanumeric char replaced by `-`. `cd` to the home and `claude --resume <sessionId>` (or pick it from the resume list).
- **Orientation:** the generated `CLAUDE.md` (auto-injected by CC every turn) tells the model that the transcript's `/home/claude` and `/mnt/user-data` now live under this cwd — so it doesn't flail looking for the old sandbox paths.
- For literal-path fidelity (scripts that hard-code `/home/claude`, `/mnt/user-data`): run the session inside Docker off the manifest image with `-v …/home:/home/claude -v …/home/mnt/user-data:/mnt/user-data`. Same teleport dir either way.

## Reverse direction (seeding a conversation)

- `upload-file` (binary blob → `/mnt/user-data/uploads`) and `convert_document` (document → `extracted_content`) let you push files into a *fresh* conversation's sandbox.
- You **cannot** inject messages or signatures: claude.ai is server-authoritative — there is no client write that adds an assistant turn, and the signature is minted server-side and never accepted from a client (verified by probe: every injection route 400s/404s).

## What needs the model, and what doesn't

- No model: messages + signatures (export), file bytes (`download-file`/`find_files`), enumeration (export tool_results), base env (manifest).
- Model: only a live `find`/`tar`, and only if the export's own listings don't cover the tree.

## Notes on the file API (reference)

| op | client | |
|---|---|---|
| `download-file` / `download-files` | ✅ | reads any absolute path, file-only |
| `list-files` | ✅ | `/mnt/user-data` only |
| `upload-file` | ✅ | blob → `/mnt/user-data/uploads` |
| `delete-file` | ✅ | `POST {file_uuid}` |
| `convert_document` | ✅ | document text extraction |
| `write-file` | ❌ | backend-only (403 to clients) |

