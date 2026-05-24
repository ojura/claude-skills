---
name: claude-html-pdf-polisher
description: polish print-style html/css and pdf layouts produced by claude or similar tools. use when the user uploads or references claude-generated html, css, render scripts, or pdfs and asks for layout tweaks, typography fixes, font embedding/localization, page-within-page effects, margin/padding changes, page-break/orphan control, or repeated pdf rendering iterations. prefer the fixed playwright/chromium pipeline in this skill; avoid restarting generic pdf-engine exploration.
---

# Claude HTML PDF Polisher

Use this skill for iterative print-layout work on Claude-generated HTML/CSS that renders to PDF.

## Non-negotiable operating mode

Freeze the pipeline before editing. For a fresh artifact, use the job wrapper:

```bash
scripts/render_job.sh input.html job-slug
```

For repeated edits where the output path is already fixed, `scripts/iterate_layout.sh input.html output.pdf` is still available. Both wrappers invoke the same single-process orchestrator, `scripts/render_pipeline.py`, after the shell-only dependency check. The older stage scripts remain debuggable entry points, but the default hot path must not spawn Python once per stage. The wrappers use:

- Playwright + Chromium
- local HTML via `page.set_content(...)`
- `page.emulate_media(media="print")` called before `page.set_content(...)`, every time, with no per-job decision-making
- fail-fast remote asset preflight before Playwright starts
- a local `<base>` tag for relative assets
- `document.fonts.ready` under print media
- `page.pdf(print_background=True, prefer_css_page_size=True, margin=0)`
- full-page rasterization plus contact sheets
- one Python process for normalize -> font resolution -> preflight -> render -> contact -> summary
- per-stage timing logs printed live and saved to `timings.log`
- a concise render summary with PDF path, page count, page size, raster count, blank/sparse-page check, oversized-page check, contact sheet path, and timings path

Do not re-open the search space to WeasyPrint, wkhtmltopdf, browser availability, HTTP servers, remote fonts, or generic PDF conversion unless the fixed wrapper itself fails with a concrete new error. If a render fails, inspect the wrapper error and repair the wrapper/input; do not start a fresh renderer-selection loop. Do not bypass `render_pipeline.py` during routine work; run individual stage scripts only to debug a concrete failed stage.

Font substitution is prohibited. Do not delete Google Fonts links and replace `Fraunces`, `Source Serif 4`, `JetBrains Mono`, or other requested families with EB Garamond, Noto Serif, DejaVu, system fonts, or any fallback. If font localization needs the bridge and the bridge is unavailable, stop with the generated plan rather than rendering a substitute-font PDF.

Renderer media mode is fixed. Always use print media before loading content and waiting on fonts. The script owns this; do not reason about whether screen media, viewport size, or `page.pdf()` implicit print behavior should be used for a given job. The render log must contain `info: media=print`.

## Workflow

The wrappers honor `$POLISHER_WORK_ROOT` for the work-directory root. If unset, they probe `/mnt/data`, `$TMPDIR`, then `$HOME` and pick the first writable one. The skill-creator path used by `package_with_timings.sh` is discovered via `$SKILL_CREATOR_PATH`, then known vendor locations (`/home/oai/skills/skill-creator`, `/mnt/skills/examples/skill-creator`, `/mnt/skills/public/skill-creator`), then falls back to a built-in zip if none of those exist.

### Initialize a rendering job

Use the job wrapper, not generic PDF conversion. It creates a work directory under `$POLISHER_WORK_ROOT/render_work/<job-slug>/`, preserves the job-local `fonts/` and `font-bridge/` caches across reruns, does the shell-only `deps_check`, then calls `scripts/render_pipeline.py` to copy/normalize the HTML to `source.html`, run the blessed Playwright/Chromium pipeline, rasterize every page, make a contact sheet, write `render-summary.txt`, and place artifacts at predictable paths:

```bash
scripts/render_job.sh /path/to/source.html job-slug
```

Use a large proof contact sheet when the normal sheet is too small to inspect dense text or page-break ambiguity:

```bash
scripts/render_job.sh /path/to/source.html job-slug --large-contact
```

Expected artifacts:

```text
$POLISHER_WORK_ROOT/render_work/job-slug/source-input.html
$POLISHER_WORK_ROOT/render_work/job-slug/source.html          # normalized + resolved/local-font source
$POLISHER_WORK_ROOT/render_work/job-slug/fonts/               # working font cache, not a user deliverable
$POLISHER_WORK_ROOT/render_work/job-slug/font-bridge/         # download plans/cache when needed
$POLISHER_WORK_ROOT/render_work/job-slug/output.pdf
$POLISHER_WORK_ROOT/render_work/job-slug/renders/page-01.png
$POLISHER_WORK_ROOT/render_work/job-slug/contact.jpg
$POLISHER_WORK_ROOT/render_work/job-slug/contact-large.jpg   # only with --large-contact
$POLISHER_WORK_ROOT/render_work/job-slug/render-summary.txt
$POLISHER_WORK_ROOT/render_work/job-slug/timings.log
```

### Iterate

1. Edit `$POLISHER_WORK_ROOT/render_work/<job-slug>/source.html` or copy a new HTML version into the job.
2. Run `scripts/render_job.sh source.html job-slug` again, or use `scripts/iterate_layout.sh input.html output.pdf` for a specific output path.
3. Read `render-summary.txt`. Treat blank/sparse pages, oversized pages, and raster-count mismatches as blockers.
4. Inspect the generated contact sheet and every rendered page image. Generate a larger contact sheet when needed:

```bash
scripts/make_contact_sheet.sh renders/ contact-large.jpg --thumb-width 700 --cols 2
```

5. Fix page-break/orphan/layout issues and render again.
6. Return the final PDF, the final HTML/source, and usually the contact sheet plus summary.

For a detailed review checklist, consult `references/layout-review-checklist.md`. For sandboxed remote-font failures, consult `references/font-bridge-notes.md`.

## Render summary and proofing

After every render, the wrapper prints and writes a summary in this shape:

```text
Render summary
- PDF: $POLISHER_WORK_ROOT/render_work/job/output.pdf
- Pages: 13
- Page size: 8.27 x 11.69 in (595 x 842 pt)
- Rasterized pages: 13
- Blank/sparse pages: none detected
- Oversized pages: none detected
- Contact sheet: $POLISHER_WORK_ROOT/render_work/job/contact.jpg
- Timings: $POLISHER_WORK_ROOT/render_work/job/timings.log

Timing log
[000.00s] start: normalize_html
...
```

Use the summary and its embedded timing tail to reduce inspection overhead, not to replace inspection. The summary also links to the full timings log. Always check all rendered pages for orphaned sections, split pullquotes/interjects, ugly page starts, and deliberate dramatic whitespace.

Contact-sheet defaults:

```bash
scripts/make_contact_sheet.sh renders/ contact.jpg --thumb-width 420 --cols 3
scripts/make_contact_sheet.sh renders/ contact-large.jpg --thumb-width 700 --cols 2
```

Generate the large sheet when text density or layout ambiguity makes the default hard to inspect.


## Timing discipline

The wrappers must never leave the next assistant guessing where time went. Every job prints and saves stage timings like:

```text
[000.00s] start: normalize_html
[000.07s] ok: normalize_html (0.07s)
[000.08s] start: font_resolution
[000.12s] ok: font_resolution (0.04s)
[000.12s] start: asset_preflight
[000.17s] ok: asset_preflight (0.05s)
[000.18s] start: playwright_pdf_and_raster
[000.19s] start: render_playwright
[000.19s] start: chromium_pdf
[000.19s] info: chromium_executable=/usr/bin/chromium
[000.20s] info: media=print
[002.93s] ok: chromium_pdf
[002.93s] start: raster_pdf dpi=150
[004.82s] ok: raster_pdf pages=13 engine=pymupdf
[005.13s] ok: playwright_pdf_and_raster (4.95s)
[005.14s] start: contact_sheet
[006.31s] ok: contact_sheet (1.17s)
[006.32s] start: pdf_inspect
[006.42s] ok: pdf_inspect (0.10s)
[006.42s] start: analyze_page_images
[006.88s] ok: analyze_page_images (0.46s)
[006.88s] start: write_summary
[006.88s] ok: write_summary (0.00s)
```

If a tool timeout interrupts a run, use the last printed timing line as the failure boundary. Do not speculate with words like "likely". Do not blame vague stages such as "summary generation"; the scripts split post-processing into `pdf_inspect`, `analyze_page_images`, and `write_summary`, and `write_summary` should be essentially instant. Rerun only the narrow failing stage or lower proofing cost intentionally, for example by using the default contact sheet instead of `--large-contact` or a lower `--dpi` during iteration.



## Single-process hot path

Keep `scripts/render_job.sh` and `scripts/iterate_layout.sh` as the user-facing entry points, but both must call `scripts/render_pipeline.py` for the main render pipeline. This is deliberate: some environments pay about two seconds of Python startup per process, so spawning one Python process per stage makes routine iteration artificially slow.

Preserve the modular scripts as debugging tools. Do not delete `resolve_fonts.py`, `resolve_fontsource_fonts.py`, `preflight_assets.py`, `render_playwright.py`, `make_contact_sheet.py`, or `render_summary.py`. When editing them, keep callable functions available so `render_pipeline.py` can import and call stage logic directly rather than using subprocess.

`deps_check` stays in the shell wrappers. Do not move pip-install/bootstrap behavior into the orchestrator. Do not try to reuse a persistent Chromium browser in v1; the intended win is collapsing Python startups, not browser lifecycle optimization.

## Skill maintenance and packaging discipline

This section applies only when updating or repackaging this skill itself. It prevents the same unaccounted outer-loop timeout that the render wrapper prevents during user layout work.

Use the timed maintenance helpers instead of ad hoc bundled tests:

```bash
scripts/test_skill_quick.sh
scripts/package_with_timings.sh $POLISHER_WORK_ROOT/skill_dist --quick-test
```

`test_skill_quick.sh` is the default maintenance smoke test. It renders one local-only fixture at low DPI, verifies the fixed print-media render path logs `info: media=print`, and verifies that a known Google Fonts fixture resolves through the deterministic Fontsource/npm embedding path without fallback/system fonts. Do not render a full essay or generate large contact sheets during routine skill packaging unless the user explicitly asks for a full regression.

`package_with_timings.sh` logs the outer build/test/package stages to `package-timings.log`. If a timeout occurs while maintaining the skill, use the last line in that log as the failure boundary. Do not speculate about where time went. If total assistant wall time exceeds the render or package timing logs, say the overhead was outside the instrumented wrapper and add instrumentation before retrying.

Allowed maintenance modes:

- Quick update: edit files, run `scripts/package_with_timings.sh $POLISHER_WORK_ROOT/<dist> --quick-test`, return `skill.zip`.
- Full regression: only when requested, run `scripts/test_skill_full.sh` or a real user HTML through `scripts/render_job.sh` separately and inspect its own `timings.log`. Keep this separate from packaging.

## Layout priorities for print artifacts

Preserve the requested visual model before optimizing mechanically:

- keep the requested visual architecture intact: if the user asks for a page-within-page look (e.g., a white outer page with a colored inner field), preserve the inner-field proportions and ensure body text has sufficient padding so it is not flush against the inner-field edge
- keep cover and colophon pages with their requested full-bleed or framed treatment
- treat user-identified moments as atomic: pullquotes, key interjects, and climactic paragraphs should not split across pages once the user has flagged them
- preserve deliberate dramatic gaps and whitespace when the user has confirmed they prefer them
- fix obvious orphans and ugly page starts (single-word lines, separated conjunctions, isolated punctuation) even if the contact sheet looks acceptable at thumbnail size
- check all pages, not only the ones likely affected by the most recent edit

## Fonts and assets

Font handling is an executable stage in `scripts/render_pipeline.py`, not a policy paragraph. `scripts/render_job.sh` and `scripts/iterate_layout.sh` both call the same orchestrator after shell `deps_check`; the fixed order is:

```text
deps_check                 # shell wrapper, outside Python orchestrator
normalize_html             # render_pipeline.py
font_resolution            # render_pipeline.py; preserves exit-86 bridge/fallback contract
asset_preflight            # render_pipeline.py
playwright_pdf_and_raster  # render_pipeline.py
contact_sheet              # render_pipeline.py
pdf_inspect                # render_pipeline.py
analyze_page_images        # render_pipeline.py
write_summary              # render_pipeline.py
```

The deterministic resolver chain is:

1. `resolve_fonts.py --embed` tries direct Google CSS/font localization. Embedding is required because `page.set_content(...)` may block sibling `file://` font files.
2. If direct Google localization exits `86`, the wrapper immediately runs `resolve_fontsource_fonts.py --embed`. Do not pause to think, search, inspect system fonts, or choose replacements.
3. `resolve_fontsource_fonts.py` maps each requested family to a Fontsource package, installs/uses it through npm, preserves the original family name in the CSS, embeds WOFF2 files as data URLs, and writes `fontsource-resolution.json`. A small set of families has curated CSS-file choices; everything else is auto-discovered by computing the package name from the family (e.g., `Source Serif 4` → `@fontsource-variable/source-serif-4`) and trying both the variable and non-variable package channels.
4. `render_playwright.py` verifies required families via `document.fonts.check(...)` before creating the PDF.
5. `pdffonts`/`render_summary.py` proofing then confirms the requested families appear in the output PDF.

Curated families (kept for deterministic CSS-file selection where the package has multiple valid axis files):

```text
Fraunces       -> @fontsource-variable/fraunces        -> full.css + full-italic.css
Source Serif 4 -> @fontsource-variable/source-serif-4  -> opsz.css + opsz-italic.css
JetBrains Mono -> @fontsource-variable/jetbrains-mono  -> wght.css + wght-italic.css
```

Auto-discovery tries `@fontsource-variable/<kebab-family>` first, then `@fontsource/<kebab-family>`, then picks the best available CSS-file pattern (`full.css`/`full-italic.css`, then `opsz.css`/`opsz-italic.css`, then `wght.css`/`wght-italic.css`, then `index.css`). Discovered families are recorded in `fontsource-resolution.json` under `auto_discovered_families`. A family that cannot be installed under either channel still hits the bridge-required hard stop.

A substitute-font render is never acceptable for this workflow. Removing Google links and changing `font-family` to EB Garamond, Noto Serif, DejaVu, Tinos, system serif, or any fallback is a hard failure, even if the PDF renders and the contact sheet looks passable. The correct outcomes are only: (1) localize/embed the requested fonts and render, or (2) stop with a generated blocker report.

### Fontsource/npm fallback protocol

Routine supported Google Fonts must not trigger manual research. If `resolve_fonts.py` cannot reach Google, let the wrapper run:

```bash
scripts/resolve_fontsource_fonts.py input.html output.html --font-dir fonts --cache-dir font-bridge/fontsource-cache --bridge-dir font-bridge --embed
```

For supported families, this should produce:

```text
font-bridge/fontsource-resolution.json
source.html with <style id="polisher-fontsource-fonts">
<meta name="polisher-required-fonts" ...>
```

Then preflight and render continue automatically. The assistant should not inspect installed fonts, search GitHub/CDN URLs, or hand-build `@font-face` rules during normal work.

If Fontsource/npm fallback fails, read only the generated blocker files and stop:

```bash
cat $POLISHER_WORK_ROOT/render_work/<job-slug>/font-bridge/FONTSOURCE_RESOLUTION_FAILED.md
cat $POLISHER_WORK_ROOT/render_work/<job-slug>/font-bridge/fontsource-resolution-failed.json
```

Report the blocker and do not render.

### Google view/download bridge protocol

The Google view/download bridge remains a fallback for unsupported Fontsource cases or environments without npm/artifactory. When `font_resolution` exits `86` and Fontsource also cannot resolve, inspect the exact plan path it prints, usually:

```text
$POLISHER_WORK_ROOT/render_work/<job-slug>/font-bridge/font-download-plan.json
```

When a bridge plan is the only remaining path, open the generated hard-stop/action files:

```bash
cat $POLISHER_WORK_ROOT/render_work/<job-slug>/font-bridge/FONT_BRIDGE_REQUIRED.md
cat $POLISHER_WORK_ROOT/render_work/<job-slug>/font-bridge/open-before-download-urls.txt
cat $POLISHER_WORK_ROOT/render_work/<job-slug>/font-bridge/font-bridge-actions.jsonl
cat $POLISHER_WORK_ROOT/render_work/<job-slug>/font-bridge/container-download-actions.jsonl
```

Use the exact view-then-download sequence, then rerun the same wrapper command. Do not browse/search unless an exact planned URL itself fails. If no bridge is available, stop and tell the user the font bridge is blocked; do not substitute fonts. See `references/font-bridge-notes.md` for details.

For explicit inspection:

```bash
scripts/resolve_fonts.py input.html output.html --dry-run
scripts/resolve_fontsource_fonts.py input.html output.html --dry-run
```

Do not include font binaries in the skill package and do not return raw font files to the user. Embedded fonts inside final PDFs/working HTML are output artifacts, not shared raw font files. Prefer returning the PDF as the authoritative artifact.

## Response discipline

For routine iterations, do not explain internal process. State the result and provide links.

Good response shape:

> Done. I adjusted the inner-panel width and padding, kept the flagged pullquote atomic, checked all pages, and preserved the full-bleed colophon.
>
> [PDF](...) [HTML](...) [contact sheet](...)

Only explain failures when `scripts/render_job.sh` or `scripts/iterate_layout.sh` itself returns a concrete error that blocks progress. If the preflight blocks remote assets, treat that as initialization work: localize or stage assets once, then resume cheap iteration. If `font_resolution` exits 86 and bridge downloads cannot be staged, report the bridge-required status and include the plan/actions paths. Do not provide a substitute-font PDF.
