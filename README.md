# claude-skills

A collection of Claude Code skills.

## Skills

### claude-html-pdf-polisher

Renders HTML into magazine-quality PDFs with embedded fonts and deterministic,
print-correct layout. A fixed Playwright pipeline, strict font discipline (no
silent substitution), timing instrumentation, and a layout-review checklist.
Built through a cross-environment iteration between an OpenAI model, an
Anthropic model, and the author. Especially useful for getting ChatGPT to
iterate fast on non-trivial HTML renders: it pins the rendering engine and
forces deterministic font embedding, so a one-line edit does not turn into an
engine switch or a silent font substitution. See its SKILL.md and README for
usage.

### claude-web-fetcher

Fetches conversations, files, and Claude Code-web sessions from claude.ai
using the session cookie. Solves Cloudflare transparently via patchright,
captures feature-gating headers from the SPA, and provides a clean Python API
for listing conversations, downloading file attachments, and reading Code-web
session event streams. Only needs the sessionKey cookie.

### cdp-daemon

Drives an already-running Chrome over the DevTools Protocol from scripts without
triggering a permission modal on every call. Holds one persistent CDP
WebSocket, auto-presses Chrome's "Allow remote debugging?" dialog via AT-SPI,
and exposes a small local HTTP API for targets, attach, eval, arbitrary CDP
calls, and a buffered event stream. Useful for reading cookies, evaluating JS,
navigating, or watching network traffic in the user's real logged-in browser.

### chatgpt-archive-toolkit

Archives a logged-in ChatGPT account/workspace through `cdp-daemon`, preserving
raw conversation JSON, branch variants, reasoning/tool metadata returned by the
backend, file/media references, Library nodes, endpoint snapshots, and signed
downloads. Installs a static dark local browser for reading conversations,
collapsing noisy technical payloads, browsing files/artifacts/raw API captures,
and validating archive coverage.

### markdown-latex-report

Turns a single Markdown file into a polished, book-quality PDF via pandoc and
lualatex. A Lua filter auto-sizes table columns (wide tables never overflow) and
breaks long identifiers in inline code; the preamble adds code listings that wrap
long lines, a table of contents, running headers, and widow/orphan control.
Bundles the needed LaTeX packages locally, so no full texlive install. Ships a
self-contained test fixture that doubles as the smoke test.

Sample pages from that fixture (full PDF: [`markdown-latex-report/docs/sample-report.pdf`](markdown-latex-report/docs/sample-report.pdf)):

<p align="center">
  <a href="markdown-latex-report/docs/sample-report.pdf"><img src="markdown-latex-report/docs/preview/01-title-toc.png" width="270" alt="title and contents"></a>
  <a href="markdown-latex-report/docs/sample-report.pdf"><img src="markdown-latex-report/docs/preview/02-code-wrapping.png" width="270" alt="code listing with line wrapping"></a>
  <a href="markdown-latex-report/docs/sample-report.pdf"><img src="markdown-latex-report/docs/preview/03-chart-and-table.png" width="270" alt="chart and a content-rich table"></a>
</p>
<p align="center"><em>Title and contents &middot; code with line wrapping &middot; a generated chart with a column-sized table.<br>Click any page for the full PDF.</em></p>

### archive-conversation-forks

Declutters the Claude Code session picker by grouping a project's session JSONLs
into fork families, keeping the canonical (most complete) session per family, and
moving redundant forks out to `~/claude-archive` with a restore manifest - moved,
never deleted. Builds raw and prose distinct-content fingerprints to tell true
forks from sessions that merely share tool-edits, protects every cross-file and
phantom `logicalParentUuid` ancestor a kept session needs for scrollback, and
archives only what is provably redundant or read-and-confirmed disposable (a
single message is never dropped on count alone). Optionally titles retained
sessions for a themed, chronological picker, mtime-neutrally so none are exposed
to the mtime-keyed retention sweep.

### recover-deleted-sessions-ext4

Recovers Claude Code session transcripts deleted by `rm`, `find -delete`, or a
retention sweep. Volatility-ordered triage: stop writes to the affected
filesystem, grab the volatile sources first (open fds, live `claude --resume`
process memory, the webview renderer's in-memory session state over CDP), take
the cheap byte-perfect wins (out-of-tree backups, the ext4 journal), then carve
the raw block device by content pattern and recover the id-less records via ext4
journal inode extents. Merges, dedupes, validates, repairs app-truncated
survivors, and restores only on explicit OK. Bundles the proven (scrubbed)
forensic scripts from a real 60/66-session recovery.

## Installation

Copy a skill directory into your Claude Code skills location, or install the
packaged .skill from its release.

## License

See each skill's LICENSE.
