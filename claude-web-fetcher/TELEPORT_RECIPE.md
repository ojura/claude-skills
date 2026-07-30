# Teleporting a claude.ai conversation into Claude Code

Goal: reconstruct one of *your own* claude.ai conversations locally as a Claude Code session
you can `claude --resume`, together with its sandbox working tree, so you can keep working.
Four independent pieces, each through its own channel.

## 1. Messages + thinking (the conversation)

- **Source:** the account data export. `POST /api/organizations/{org}/export_data` returns a `{nonce}`, `POST export_signed_url/{nonce}` turns that into a GCS zip, and the zip holds `conversations.json`. `claude_web.py` (`export_account`) does this. Only the export carries thinking-block `signature`s; every live read strips them.
- **Transform:** `bijection.py` `to_cc(convo)` -> a loadable CC session JSONL (verified to load + resume via `claude -p --resume`, across a feature matrix). It:
  - synthesizes the metadata CC needs (per-round `message.id`, `model`, `stop_reason`, `usage`, `requestId`, and the active-leaf `last-prompt`/`leafUuid`);
  - threads a single linear `parentUuid` spine in execution order through the role:user tool_result lines;
  - rewrites `tool_result.content` into items the API accepts: it drops the `uuid` field that causes a 400, resolves `{type:image, file_uuid}` references into base64 `source` blocks using bytes from `/files/{uuid}/preview` (an image it cannot fetch becomes a text placeholder, and `_orig_content` keeps the reverse exact), and turns the types the API does not know, `knowledge`, `local_resource` and `rag_reference`, into text;
  - synthesizes valid `tool_use.id` where claude.ai left them null (sentinel-prefixed `toolu_synth_…`), drops empty text blocks, and puts a `"."` placeholder in otherwise-empty user turns (the API rejects empty user content);
  - parks everything non-API in a **line-level `exportEscrow`** key so `to_export()` reverses losslessly (full-corpus round-trip green).
- **thinking mode:** `carry`, the default, replays `{thinking, signature}` unchanged, and it loads and continues without a 400, including where a tool call spans the boundary. `strip` drops the thinking blocks and loses the raw reasoning. A block whose signature is null goes out as `signature:""`, which the API accepts.

## 2. `/home/claude`, the working tree

- **Bytes:** `wiggle/download-file?path=<abs path>`; `download-files?paths=&paths=` zips a batch (all-or-nothing).
- **Listing the tree needs the model.** No client-side call lists `/home/claude`, and the directory listings that happen to appear in the export's own `tool_result`s cover only a small part of it, because most files were written by tools that never printed a listing. So running `find` or `tar` through the model's bash is the normal way to enumerate, not a fallback. The `ls` and `find` output already in the export is worth using as a partial index. Note that `local_resource.file_path` is not part of that index: every one of those points under `/mnt/user-data/outputs` rather than `/home/claude`, so they belong to §3 and `find_files` already covers them. For everything else, one model turn runs `find /home/claude > manifest`, after which you fetch each path with `download-file`, or tars the tree into `/mnt/user-data/outputs`.
- **Size cap.** The file API limits how large a response it will serve: above the limit `download-file` and `download-files` return `413 "output_size_exceeded"`. A 252.8 MB single file and a 286 MB zip both come back `200`, while a 551 MB `.git` pack is rejected, which puts the limit somewhere above 286 MB and at or below 551 MB, probably a round 500 MB. The limit is on the response and does not depend on the path, so tarring `/home/claude` into `/mnt` does not get around it; a tarball that large is rejected the same way, which is why one ended up stranded in `/tmp` and never reached `outputs/`. For a tree above the limit, fetch the files one at a time, since each individual file is below it, or split the archive. Writing has a separate and much smaller limit: `upload-file` refuses anything over 35 MB with `"File size exceeds 35MB"`.
- `/home/claude` holds whatever the conversation built, which in some cases includes a git repository the conversation created itself, with one commit per revision.

## 3. `/mnt/user-data`, inputs and presented outputs

- **Bytes:** `download-file?path=/mnt/user-data/…` reads the whole mount off the live VM, uploads as well as presented outputs, by the same mechanism as any other absolute path. `find_files(conv)` supplies the index of what is there, and each entry is then fetched by its path. For wiggle outputs `FileRef.path` is already the `/mnt/user-data/outputs/…` path. For uploads `FileRef.path` is a uuid instead, and the file itself sits at `/mnt/user-data/uploads/<FileRef.name>`.
- **Do not use `/files/{uuid}/preview` for uploads.** That asset store deletes them, so it starts returning 404 after a few weeks, and an export taken with `skip_files=True` has no bytes in it either. The VM mount keeps the file, so `download-file` still returns it. Tool-result images are the exception: they are not files on the mount, so `/files/{uuid}/preview` is the only way to get them.
- The text of an uploaded document is also available as the `convert_document` output, or as `extracted_content` on the export's attachment.

## 4. Base environment

- Rebuild the environment from the manifest rather than copying the root filesystem: start `FROM ubuntu:24.04` and install the captured `apt`, `pip`, npm global and uv lists. The runtimes to match are Python 3.12, Node 22 and OpenJDK 21.

## Hydration layout

All of an org's conversations live in one CC project, so `claude --resume` lists them together. CC works out the project directory from the session's working directory, which is why they share a working directory:

```
~/.claude/teleports/<org-uuid>/                  <- the single cwd == the single project
  CLAUDE.md                                      <- shared orientation + conversation index
  .teleport-sessions.json                        <- session_id -> conversation (hook index)
  .claude/{settings.json,orient.py}              <- SessionStart re-orientation hook
  <conv-uuid>/CLAUDE.md                          <- per-conversation path remap
  <conv-uuid>/home/claude/                       <- the /home/claude tree             (§2)
  <conv-uuid>/mnt/user-data/{outputs,uploads}/   <- presented outputs + uploads       (§3)
```

- The org uuid can be read offline, from the export directory name: `data-<org-uuid>-<unix-ts>-<job-nonce>-batch-0000`. The leading uuid is the same for every export of one account and differs between accounts. It is the same `{org}` the file API is addressed by.
- Each conversation's directory mirrors the sandbox's absolute paths, so a sandbox path `/X` is `<conv-uuid>/X` locally and `hydrate_mnt` builds the destination by joining the two.
- `teleport.py` does all of this. Call `teleport_export(export_json, client=ClaudeWeb())` for a whole export or `teleport(conv_uuid, export_json, home_src=<dir|tarball>, …)` for a single conversation. Either one writes the session JSONL with images resolved, fills in the tree and `/mnt/user-data`, writes the orientation, and returns the `claude --resume` command. The session id is derived from the conversation uuid, so running it again overwrites rather than accumulates, and a marker file records which source the tree was copied from, so an interrupted copy is repeated rather than assumed complete. Teleporting one conversation merges it into the org root's session index and rebuilds the shared `CLAUDE.md`, leaving the conversations already there working.
- The JSONL is written to `~/.claude/projects/<slug-of-cwd>/<sessionId>.jsonl`, where the slug is the absolute working directory with every character that is not a letter or digit replaced by `-`. `cd` to the org root and run `claude --resume` to pick any of its conversations from one list.
- Sharing a working directory means the `CLAUDE.md` CC injects from it is common to every session and cannot name the conversation a given session belongs to. Three things carry that identity instead, each taking over where the one before it stops applying:
  1. An `isMeta` `<system_notice>` written as the root of each transcript, naming that conversation and its directory. CC uses `{type:user, isMeta:true}` for context it injects itself and writes such a line at `parentUuid:None` head position for the `<local-command-caveat>`, so this reuses an existing line type. It carries no `node_uuid` in escrow, so `to_export` leaves it out and the reverse still matches the export.
  2. A `SessionStart` hook in the project's own `.claude/settings.json`, matching `startup|resume|clear|compact`, which looks the `session_id` up and returns that conversation's notice as `additionalContext`. It is needed because CC's `compact_boundary` line carries `parentUuid:None`: after a compaction nothing before that line is sent to the API, so the notice at the head of the transcript is out of context, not merely summarised. It remains reachable through `logicalParentUuid`, which the display uses. Living in the project, the hook does nothing elsewhere on the machine, and a session it does not recognise gets `{}`.
  3. The shared `CLAUDE.md`, which is always present but can only give the general rule for translating a path.
- If the conversation left behind scripts with `/home/claude` or `/mnt/user-data` written into them, run the session in Docker from the manifest image with `-v …/<conv-uuid>/home/claude:/home/claude -v …/<conv-uuid>/mnt/user-data:/mnt/user-data`, and those paths resolve as written. The teleport directory is the same either way.

## Reverse direction (seeding a conversation)

- `upload-file`, which puts a binary blob into `/mnt/user-data/uploads`, and `convert_document`, which returns a document's `extracted_content`, are how claude.ai lets you push files into a fresh conversation's sandbox. Neither is implemented here; this client has only `download-file`, `download-files`, `find_files` and the export methods, so anything that seeds a conversation has to add them.
- You cannot inject messages or signatures. claude.ai decides what a conversation contains: no client-side call adds an assistant turn, and the signature is generated on the server and never accepted from a client. Every route that might do either returns a 400 or a 404.

## What needs the model, and what doesn't

- No model: messages + signatures (export), file bytes (`download-file`/`find_files`), enumeration (export tool_results), base env (manifest).
- Model: only a live `find`/`tar`, and only if the export's own listings don't cover the tree.

## Notes on the file API (reference)

"reachable" = exists on claude.ai's client-facing API surface (a client *could* call it).
"impl here" = a method exists in *this* repo's `claude_web.py`. Today only `download-file`/
`download-files` (via `download_file`/`find_files`) and the export methods are implemented;
`list-files`/`upload-file`/`delete-file`/`convert_document` are reachable but unimplemented.

| op | reachable | impl here | |
|---|---|---|---|
| `download-file` / `download-files` | ✅ | ✅ | reads any absolute path, file-only |
| `list-files` | ✅ | ❌ | `/mnt/user-data` only |
| `upload-file` | ✅ | ❌ | blob -> `/mnt/user-data/uploads` |
| `delete-file` | ✅ | ❌ | `POST {file_uuid}` |
| `convert_document` | ✅ | ❌ | document text extraction |
| `write-file` | ❌ | ❌ | backend-only (403 to clients) |

