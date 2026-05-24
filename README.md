# claude-skills

A collection of Claude Code skills.

## Skills

### claude-html-pdf-polisher

Renders HTML into magazine-quality PDFs with embedded fonts and deterministic,
print-correct layout. A fixed Playwright pipeline, strict font discipline (no
silent substitution), timing instrumentation, and a layout-review checklist.
Built through a cross-environment iteration between an OpenAI model, an
Anthropic model, and the author. See its SKILL.md and README for usage.

### claude-web-fetcher

Fetches conversations, files, and Claude Code-web sessions from claude.ai
using the session cookie. Solves Cloudflare transparently via patchright,
captures feature-gating headers from the SPA, and provides a clean Python API
for listing conversations, downloading file attachments, and reading Code-web
session event streams. Only needs the sessionKey cookie.

## Installation

Copy a skill directory into your Claude Code skills location, or install the
packaged .skill from its release.

## License

See each skill's LICENSE.
