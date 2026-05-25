---
name: chatgpt-archive-toolkit
description: Use this skill when the user wants to back up, inspect, validate, or browse their ChatGPT account/workspace data from a live browser/CDP session or existing local dump, including conversations, branch variants, reasoning/tool metadata, files, images, artifacts, raw API captures, and a local dark-mode archive browser.
---

# ChatGPT Archive Toolkit

## Overview

This skill turns a user-owned ChatGPT workspace/account into a local archive and installs a browser UI for exploring the result. It is for active Chrome CDP-backed dumps, existing dump directories, viewer improvements, and validation of whether conversations/media/raw metadata were preserved.

Do not include private dump contents in the skill itself. Use the bundled browser template and scripts as reusable tooling, and keep all user data in the target dump directory the user names.

## Workflow

1. **Establish scope and destination**
   - Use the user's requested destination exactly; default only when they ask you to choose.
   - Confirm that the target account/workspace is user-owned or user-authorized.
   - Bind any local viewer server to `127.0.0.1`, not a public interface.

2. **Use the CDP daemon-backed archiver**
   - Start or reuse the sibling `cdp-daemon` skill first. It should expose `127.0.0.1:7799` and attach to the user's logged-in Chrome remote-debugging port.
   - Run the bundled archiver from this skill directory:
     ```bash
     python3 scripts/chatgpt_archive.py <dump-dir>
     ```
   - The archiver obtains the ChatGPT access token from the live page, fetches backend JSON directly with the account/workspace header, stores raw responses, downloads referenced files/media, installs the local browser, and can resume partial runs.
   - If ChatGPT rejects direct backend calls due to the Sentinel requirements header, prefer passing a real token via `OPENAI_SENTINEL_CHAT_REQUIREMENTS_TOKEN` or `--sentinel-token`. Use `--empty-sentinel-header` only as a compatibility fallback.
   - Only use `--save-sensitive-session` or `--include-browser-storage` when explicitly needed; both can save live auth/session-adjacent material.

3. **Capture comprehensively**
   - Conversations: titles, create/update times, mapping/tree nodes, current node, hidden/system nodes, branch/variant siblings, model/author metadata.
   - Message payloads: content parts, markdown, code, citations/file references, reasoning summaries/signatures when exposed, tool calls, tool outputs, execution outputs, attachments.
   - Media/files: downloaded images, generated media, uploaded files, artifacts/canvas documents, library/file nodes, file IDs, raw metadata and source URLs where available.
   - API evidence: endpoint snapshots, pagination responses, task/media endpoints, failures and retry notes.

4. **Install or update the viewer**
   - `chatgpt_archive.py` installs the viewer by default. For an existing dump, copy `assets/browser-template/` into `<dump>/browser/`, or run:
     ```bash
     python3 scripts/install_viewer.py <dump-dir> --force
     ```
   - The browser expects the dump root to contain `manifest.json` and `indexes/*.json`; see `references/dump-layout.md` when adapting another exporter.

5. **Validate before handing off**
   - Run:
     ```bash
     python3 scripts/check_dump.py <dump-dir>
     ```
   - Check count mismatches, missing referenced files, missing browser assets, empty indexes, and raw API coverage.
   - Start a local server only when useful:
     ```bash
     python3 scripts/serve_dump.py <dump-dir> --port 8877
     ```
   - Report the local URL and the important validation counts/warnings.

## Viewer Standards

The viewer should stay close to ChatGPT's reading experience: restrained dark palette, readable message width, markdown rendering, highlighted and copyable code, and branch arrows for alternate user inputs/assistant generations.

Default-hide noisy technical material such as empty tool messages, raw CSS/JS dumps, huge execution logs, file lists, layout probes, YAML/resource payloads, tracebacks, and internal citations like `filecite` instructions. Make those chunks searchable and available behind disclosure controls rather than deleting them.

When changing the viewer, test at least one conversation with branch variants and one with large tool/code output. Avoid adding decorative UI or bright palettes; this is an archive reader first.

## Resources

- `assets/browser-template/`: static HTML/CSS/JS viewer copied into a dump directory.
- `scripts/chatgpt_archive.py`: CDP daemon-backed ChatGPT archiver and downloader.
- `scripts/install_viewer.py`: installs or updates the browser template in a dump.
- `scripts/check_dump.py`: validates expected dump structure and reports counts/warnings.
- `scripts/serve_dump.py`: serves a dump locally on `127.0.0.1`.
- `references/dump-layout.md`: expected archive layout and index conventions.
