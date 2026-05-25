---
name: claude-web-fetcher
description: Fetch conversations, files, artifacts, and Claude Code-web sessions from claude.ai. Lists conversations, finds uploaded files and sandbox-generated artifacts (wiggle files), lists/reads Code-web (epitaxy) sessions and their events, and downloads files. Uses patchright to solve Cloudflare; only needs a sessionKey. Use when the user wants to retrieve a file, conversation, or Code-web session from their claude.ai history.
---

# Claude.ai Web Fetcher

Retrieves conversations, files, and Claude Code-web sessions from claude.ai
using the session cookie.

## Session key acquisition

Use `acquire_session_key()` which tries all sources automatically:

```python
import sys, os
sys.path.insert(0, os.path.expanduser("~/.claude/skills/claude-web-fetcher"))
from claude_web import acquire_session_key

session_key = acquire_session_key()  # tries file, Desktop, Firefox in order
```

Sources tried (in order):
1. `~/claude_desktop_login` file (if previously saved)
2. Claude Desktop's Cookies SQLite (`~/.config/Claude/Cookies`, unencrypted)
3. Firefox cookies (`~/.mozilla/firefox/*/cookies.sqlite`, unencrypted)
4. Chrome via CDP (`DevToolsActivePort`, needs claude.ai tab open + `websocket-client`)
5. Raises with instructions to paste from Chrome DevTools

A standalone extractor script is also available at `extract_from_chrome.py` for manual one-time use (keeps the value out of conversation context).

## Usage

```python
import sys, os
sys.path.insert(0, os.path.expanduser("~/.claude/skills/claude-web-fetcher"))
from claude_web import ClaudeWeb, acquire_session_key
from pathlib import Path

with ClaudeWeb(acquire_session_key()) as client:
    # List conversations
    convos = client.list_conversations(limit=10)

    # Find files in a conversation (deduped)
    files = client.find_files(convos[0]["uuid"])

    # Download a file
    for f in files:
        if "something" in f.name:
            client.download_file_to(f, Path.home() / f.name)

    # List Claude Code-web sessions
    sessions = client.list_sessions()

    # Get session events (messages/tool calls)
    events = client.get_session_events(sessions[0]["id"], limit=100)
```

## API summary

### Conversations (claude.ai chats)

- `ClaudeWeb(session_key: str)` - creates client (context manager supported)
- `client.verify()` - checks session validity, returns org name/plan/capabilities
- `client.list_conversations(limit=60)` - returns list of conversation dicts
- `client.get_conversation(uuid, full=True)` - returns full conversation with messages
- `client.find_files(conversation_uuid)` - returns deduplicated list of `FileRef` objects
- `client.download_file(ref)` - returns file bytes
- `client.download_file_to(ref, dest_path)` - saves to disk

### Claude Code-web sessions (epitaxy / cowork)

- `client.list_sessions()` - returns list of session dicts (id, title, status, env, repo info)
- `client.get_session(session_id)` - returns full session metadata
- `client.get_session_events(session_id, limit=1000, after_id=None)` - returns session events (messages, tool calls, env logs). Paginate with `after_id`.

Session events have `type` field: `user` (user message), `assistant` (model response), `tool_use`, `tool_result`, `env_manager_log`, `control_request`, etc.

## FileRef kinds

- `kind="upload"`: user-uploaded files (pdfs, zips, images). Downloaded via `/api/{org}/files/{uuid}/preview`.
- `kind="wiggle"`: sandbox-generated files from `present_files` tool results. Server returns a zip wrapper; the client unwraps single-file zips automatically.

## Architecture

All HTTP requests go through the patchright browser context via `page.evaluate(fetch(...))`. This solves:
1. Cloudflare JS challenge (cf_clearance obtained during page load)
2. TLS fingerprint matching (avoids 401s from fingerprint mismatch)

On init, the client navigates to `claude.ai/code` and uses `page.expect_response` to capture the CCR (Claude Code Remote) feature-gating headers from the SPA's first `/v1/sessions` request. These headers are then reused for all subsequent session API calls. No hardcoded header values.

The `/v1/sessions` endpoint is gated behind headers that the SPA adds to requests (`anthropic-client-feature`, `anthropic-beta`, etc.). Without them the endpoint returns 404. The client captures these dynamically so it stays compatible across deploys.

## Dependencies

- `patchright` (pip install patchright && python3 -m patchright install chromium)

## Notes

- Patchright launch adds ~5-8s cold start (Cloudflare solving + SPA load on `/code`). Use context manager (`with ClaudeWeb(...) as c:`) to ensure cleanup.
- The sessionKey has the same scope as the logged-in user on claude.ai. It can list/read all conversations and Code-web sessions in the active org.
- Do NOT use `add_init_script` with patchright on claude.ai. It breaks DNS resolution.
