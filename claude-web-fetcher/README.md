# Claude Web Fetcher

Retrieves your own conversations, file attachments, and Claude Code-web
(epitaxy) sessions from claude.ai using your logged-in session cookie. It drives
a real browser (patchright/Chromium) so Cloudflare and TLS fingerprinting are
handled the same way they are for any logged-in tab, then calls the same web
endpoints the claude.ai app itself uses.

## What it does

- Lists your claude.ai conversations and reads full message trees.
- Finds files in a conversation: both user uploads and sandbox-generated
  artifacts (the "wiggle" files from `present_files` tool results).
- Downloads those files to disk, unwrapping single-file zip responses
  automatically.
- Lists Claude Code-web sessions and reads their event streams (user messages,
  assistant turns, tool calls, environment logs).
- Needs only the `sessionKey` cookie. Everything else (Cloudflare clearance,
  org id, the feature-gating headers the app sends) is derived at runtime.

## When to use it

For getting your own data back out of claude.ai when the UI does not give you a
clean path: pulling a file attachment out of an old chat, archiving a
conversation, or reading the event log of a Code-web session programmatically.
It acts entirely as you, on your own account.

## Session key acquisition

`acquire_session_key()` tries several sources in order: a saved
`~/claude_desktop_login` file, the Claude Desktop cookie store, Firefox cookies,
and a running Chrome instance over CDP. A standalone `extract_from_chrome.py` is
also provided for a one-time manual grab that keeps the value out of your
terminal scrollback.

## Quick start

```python
import sys
sys.path.insert(0, "/path/to/claude-web-fetcher")
from claude_web import ClaudeWeb, acquire_session_key
from pathlib import Path

with ClaudeWeb(acquire_session_key()) as client:
    convos = client.list_conversations(limit=10)
    files = client.find_files(convos[0]["uuid"])
    for f in files:
        client.download_file_to(f, Path.home() / f.name)

    sessions = client.list_sessions()
    events = client.get_session_events(sessions[0]["id"], limit=100)
```

See [`SKILL.md`](SKILL.md) for the full API surface, the `FileRef` kinds, and
the architecture notes (how the CCR feature headers are captured and why all
requests route through the browser context).

## Dependencies

- `patchright` (`pip install patchright && python3 -m patchright install chromium`)
- `websocket-client` only if you use the Chrome-over-CDP key source.

## Scope and intent

This pulls your own data from your own account using your own session. The
`sessionKey` carries full account scope, so treat it like a password: do not
paste it where it can be logged, and rotate it if it leaks. The tool does not do
anything you could not do by hand in a logged-in browser; it just makes it
scriptable.
