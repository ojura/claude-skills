# ChatGPT Archive Toolkit

Archive a logged-in ChatGPT account/workspace through the repo's `cdp-daemon`,
then browse the result locally.

## What it does

- Reuses the user's real Chrome session through `127.0.0.1:7799` from
  `cdp-daemon`.
- Captures raw backend conversation JSON, conversation list pages, account/model
  endpoint snapshots, library/file nodes, tasks/media metadata, and references.
- Signs and downloads referenced `file_...` payloads where ChatGPT still serves
  them.
- Installs a static dark archive browser with markdown/code rendering, branch
  arrows for variants, raw API/file views, and noisy technical chunks collapsed.
- Validates the dump and serves it on localhost for inspection.

## Quick start

Start the daemon from the sibling skill first:

```bash
cd ../cdp-daemon
python3 cdp_daemon.py & disown
curl 127.0.0.1:7799/status
```

Then archive ChatGPT:

```bash
cd ../chatgpt-archive-toolkit
python3 scripts/chatgpt_archive.py ~/chatgpt-archive
python3 scripts/check_dump.py ~/chatgpt-archive
python3 scripts/serve_dump.py ~/chatgpt-archive --port 8877
```

Open `http://127.0.0.1:8877/browser/index.html`.

If backend requests fail because ChatGPT expects the Sentinel requirements
header, pass a real value via `OPENAI_SENTINEL_CHAT_REQUIREMENTS_TOKEN` or
`--sentinel-token`. `--empty-sentinel-header` exists only as a compatibility
fallback for backend states where the header name is required but the value is
not checked.

## Sensitive options

By default the archiver writes a redacted session summary but not the live access
token. Use `--save-sensitive-session` only when you intentionally want
`auth/session_raw_sensitive.json`. Use `--include-browser-storage` only when you
need full localStorage/sessionStorage; it may contain sensitive material.
