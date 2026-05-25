# ChatGPT Archive Layout

The bundled viewer is static and loads JSON relative to the dump root. Keep private account data in the dump directory, not in the skill.

Expected files:

- `manifest.json`: created time, source target, account/workspace metadata when available, declared counts, key file paths, and privacy notes.
- `indexes/conversations.json`: list of conversations with `id`, `title`, timestamps, status fields, message-node counts, and `path` to `conversations/<id>.json`.
- `conversations/*.json`: raw backend conversation responses. Preserve `mapping`, `current_node`, message metadata, branch siblings, attachments, tool calls, reasoning metadata, and system/hidden nodes.
- `indexes/conversation_summaries.json`: raw or normalized sidebar/list metadata.
- `indexes/library_nodes.json`: files/library entries returned by ChatGPT file/library endpoints.
- `indexes/artifact_references.json`: extracted file IDs, asset pointers, citations, and JSON pointers back to source payloads.
- `indexes/file_downloads.json`: one row per attempted file download, including `file_id`, status, raw signing response path, output `path`, byte count, hash, and source references.
- `indexes/media_downloads.json`: generated-image/media download attempts and saved paths.
- `indexes/endpoint_snapshots.json`: raw API endpoint captures with `name`, `url`, `status`, `ok`, and `path`.
- `raw_api/*.json`: full raw responses for account, models, files, tasks, memories, project/library endpoints, conversation pages, and similar API calls.
- `raw_file_downloads/*.json`: signing/download metadata for file IDs where available.
- `auth/session_redacted.json`: redacted session/account summary.
- `auth/session_raw_sensitive.json`: optional raw session response with live auth material, written only when explicitly requested.
- `files/` and `media/`: downloaded payloads.
- `browser/`: static browser installed from `assets/browser-template/`.

Recommended capture behavior:

- Store raw backend payloads first; derive indexes from raw data afterward.
- Never discard fields just because the viewer does not currently render them.
- Keep failed HTTP/API attempts with status and response body. They are useful evidence for coverage gaps.
- Redact or isolate live auth/session material. If raw session data must be saved, name it clearly and warn that it is sensitive.
- Use relative paths in indexes when possible. The viewer tolerates absolute paths for validation, but relative paths make archives portable.
