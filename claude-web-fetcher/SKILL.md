---
name: claude-web-fetcher
description: Fetch conversations, files, artifacts, Claude Code-web sessions, and account data exports (the only surface carrying thinking-block signatures) from claude.ai. Lists/reads conversations and Code-web (epitaxy) sessions, finds uploaded files and sandbox artifacts (wiggle files), triggers + downloads data exports. By default drives the user's real logged-in Chrome via the cdp-daemon (no Cloudflare, no sessionKey); headless patchright fallback available. Use when the user wants to retrieve a file, conversation, Code-web session, or signed-thinking export from their claude.ai history.
---

# Claude.ai Web Fetcher

Retrieves conversations, files, Claude Code-web sessions, and account data
exports from claude.ai.

## Backends

- **`backend="cdp"` (default)** — drives the user's real, logged-in Chrome via the
  **cdp-daemon** (`127.0.0.1:7799`; see the cdp-daemon skill). No Cloudflare
  friction, no `session_key` needed. The daemon is **auto-started** if it isn't
  running (`_ensure_daemon()`); it self-manages the Chrome connection and presses
  Chrome's "Allow remote debugging?" dialog when renderer accessibility is on
  (otherwise: one manual Allow click, once).
- **`backend="patchright"`** — headless fallback that launches its own browser and
  needs a `session_key`. Cloudflare-prone (patchright currently flaky); use only if
  the real-Chrome path is unavailable.

```python
import sys, os
sys.path.insert(0, os.path.expanduser("~/.claude/skills/claude-web-fetcher"))
from claude_web import ClaudeWeb

# Default: real Chrome via the cdp-daemon — no session key.
with ClaudeWeb() as c:
    convos = c.list_conversations(limit=10)

# Fallback: headless patchright (needs a session key).
from claude_web import acquire_session_key
with ClaudeWeb(acquire_session_key(), backend="patchright") as c:
    ...
```

## Usage

```python
with ClaudeWeb() as c:
    convos = c.list_conversations(limit=10)

    # Full conversation tree (forest; thinking present but signature-STRIPPED on reads)
    full = c.get_conversation(convos[0]["uuid"])

    # Files in a conversation (deduped)
    for f in c.find_files(convos[0]["uuid"]):
        c.download_file_to(f, "/tmp/" + f.name)

    # Account data export — the ONLY surface that preserves thinking signatures.
    # Scriptable: trigger -> poll signed url -> download zip (download is browserless). Date-scope to cut size.
    c.export_account("/tmp/export.zip", start_date="2026-06-14",
                     end_date="2026-06-22", skip_files=True)
```

## API summary

### Client
- `ClaudeWeb(session_key=None, backend="cdp", daemon=None)` — context manager.
  `session_key` is only needed for `backend="patchright"`.
- `verify()` — org name/uuid/billing_type/capabilities/rate_limit_tier

### Conversations
- `list_conversations(limit=60)`
- `get_conversation(uuid, full=True)` — full message tree
- `find_files(conversation_uuid)` → `[FileRef]`
- `download_file(ref)` / `download_file_to(ref, dest)`

### Account data export (only surface with thinking-block signatures)
- `trigger_export(start_date=None, end_date=None, skip_file_content=True)` → `nonce`
  (dates `YYYY-MM-DD` or ISO; omit both to export everything; a date-only `end_date` is end-day-inclusive)
- `export_signed_url(nonce)` → signed GCS url, or `None` if not ready — **SINGLE-USE** (a ready 200 with no url, or a spent link, raises)
- `poll_export(nonce, timeout=900, interval=5)` → url (blocks until ready; raises TimeoutError after `timeout`s, or RuntimeError if the link is spent)
- `download_export(signed_url, dest)` → downloads the zip (plain `urllib`; the signed
  GCS URL needs no browser/cookies)
- `export_account(dest, start_date=None, end_date=None, skip_files=True, timeout=900)` — one-shot
  trigger→poll→download; the zip's `conversations.json` carries signatures.

  Flow: `POST /api/organizations/{org}/export_data` → `POST .../export_signed_url/{nonce}`
  → GET the signed `storage.googleapis.com` url. See `claude_ai_vs_cc_format.md`.

### Claude Code-web sessions (epitaxy / cowork)
- `list_sessions()` / `get_session(id)` / `get_session_events(id, limit=1000, after_id=None)`
- **Caveat:** these need the SPA's CCR gating headers (`anthropic-client-feature`,
  `anthropic-beta`), captured only under `backend="patchright"`. Under the default
  CDP backend `_ccr_headers` is empty, so these methods **raise a clear RuntimeError
  before sending any request** — use `backend="patchright"` for Code-web sessions.

Session events have a `type` field: `user`, `assistant`, `tool_use`, `tool_result`,
`env_manager_log`, `control_request`, etc.

## FileRef kinds
- `kind="upload"`: user-uploaded files — `/api/{org}/files/{uuid}/preview`.
- `kind="wiggle"`: sandbox `present_files` outputs (zip-wrapped; single-file zips auto-unwrapped).

## Session key acquisition (patchright backend only)

`acquire_session_key()` tries, in order: `~/claude_desktop_login`, Claude Desktop
Cookies SQLite, Firefox cookies, Chrome via CDP; else raises with paste
instructions. Standalone `extract_from_chrome.py` for manual one-time use (keeps
the value out of conversation context).

## Architecture

- **CDP (default):** every API call is a `fetch(...)` run inside a real claude.ai tab
  via the cdp-daemon (`Runtime.evaluate`; `_evaluate()` dispatches `_get`/`_post`).
  The real session already holds `cf_clearance` + cookies, so Cloudflare and
  TLS-fingerprint issues vanish and no key handling is needed. `_cdp_attach()` finds
  (or opens) a live `claude.ai` tab and `Target.activateTarget`s it (un-freezing
  Memory-Saver tabs). `download_export` bypasses the browser entirely (the signed GCS
  URL is public).
- **patchright (fallback):** requests go through a headless browser context;
  Cloudflare solved on page load. On init it navigates to `/code` and captures the
  CCR headers from the first `/v1/sessions` request, reused for Code-web calls.

## Dependencies
- Default (CDP): the **cdp-daemon** skill + a Chrome with remote debugging (the
  daemon handles it). No Python deps beyond the stdlib.
- Fallback: `patchright` (`pip install patchright && python3 -m patchright install chromium`).

## Notes
- The export is async; `poll_export` waits for the signed URL. `export_signed_url` is
  **single-use** — a successful call consumes the link (re-POST → 404 `export_link_used`).
- Signatures live ONLY in the export (and in local Claude Code JSONL); every live read
  surface strips them. Full format/signature map: `claude_ai_vs_cc_format.md`.
- Reads have the same scope as the logged-in user on claude.ai.
- patchright caveat: do NOT use `add_init_script` on claude.ai (breaks DNS resolution).
