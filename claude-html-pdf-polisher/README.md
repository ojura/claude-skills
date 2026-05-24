# Claude HTML PDF Polisher

A skill for iterating LLM-generated HTML/CSS print layouts into polished PDFs. Designed for the workflow where a model produces an HTML draft of a magazine, report, or document and the user wants tight, repeatable control over print typography: page-within-page geometry, font embedding, page-break and orphan control, full-bleed special pages, and pixel-level layout review.

The skill provides a frozen Playwright/Chromium pipeline plus the discipline rules that keep iteration loops short.

## What it does

- One job wrapper, one Playwright/Chromium pipeline. No engine roulette (no WeasyPrint, wkhtmltopdf, or generic PDF conversion).
- Single Python process for the hot path (normalize → font resolution → preflight → render → contact sheet → summary), with the older stage scripts kept as debug entry points.
- Deterministic Google Fonts embedding via [Fontsource](https://fontsource.org). A curated set of families is mapped explicitly; everything else is auto-discovered by computing the package name from the family.
- Fail-fast asset preflight so remote-asset surprises do not blow up Playwright mid-render.
- Per-stage timing instrumentation with a concise render summary (page count, sizes, blank/sparse-page detection, contact-sheet link).
- Layout-review checklist that names the failure modes worth catching: orphans, split pullquotes, bad first words at page top, inner-panel proportions, full-bleed handling.

## When to use it

This skill is for **iterative print layout work**, not one-shot PDF conversion. If you have a single HTML file and want a PDF once, plain `chrome --headless --print-to-pdf` works fine. The skill earns its place when you are going to render-inspect-edit the same document fifteen times and want the inspection loop to be cheap and the discipline rules to stick.

## Quick start

```bash
# 1. install runtime deps
pip install playwright pypdfium2 pymupdf pillow
python -m playwright install chromium

# 2. install Node for Fontsource fallback (only needed if Google Fonts are blocked)
#    any recent npm works

# 3. render a job
bash scripts/render_job.sh path/to/your.html job-slug

# 4. read the summary
cat $POLISHER_WORK_ROOT/render_work/job-slug/render-summary.txt
```

`POLISHER_WORK_ROOT` defaults to `/mnt/data` if writable, else `$TMPDIR`, else `$HOME`. Set it explicitly to control where work directories live.

## Architecture

```
scripts/render_job.sh         # shell wrapper: deps_check, then calls the orchestrator
scripts/render_pipeline.py    # single-process Python orchestrator (default hot path)

scripts/resolve_fonts.py             # debug entry: direct Google CSS/font localization
scripts/resolve_fontsource_fonts.py  # debug entry: Fontsource npm fallback (with auto-discovery)
scripts/preflight_assets.py          # debug entry: remote-asset preflight
scripts/render_playwright.py         # debug entry: Playwright render + raster
scripts/make_contact_sheet.py        # debug entry: page-thumbnail contact sheet
scripts/render_summary.py            # debug entry: PDF inspection + summary
```

The shell wrapper does the dependency check and parameter plumbing; the Python orchestrator does the actual work in one process. The standalone stage scripts remain runnable and importable; call them when debugging a specific failed stage, but do not bypass the orchestrator during routine iteration.

See [`SKILL.md`](SKILL.md) for the operational discipline rules and [`references/layout-review-checklist.md`](references/layout-review-checklist.md) for the after-render review checklist.

## Font handling

Fonts are an executable stage, not a policy paragraph. The pipeline:

1. Tries direct Google CSS/font localization (`resolve_fonts.py`).
2. If Google is blocked, falls through to Fontsource via npm (`resolve_fontsource_fonts.py`).
3. For each requested family, checks the curated mapping first, then auto-discovers the package by computing the kebab-case name (`Source Serif 4` → `@fontsource-variable/source-serif-4`). Tries the variable channel first, then the non-variable channel.
4. Embeds WOFF2 files as data URLs in the source HTML so Playwright's `page.set_content(...)` does not lose them to file:// access restrictions.
5. Verifies the resulting PDF actually contains the requested family names via `pdffonts`.

Substitution is never accepted. If a family genuinely cannot be resolved, the pipeline writes a blocker report (`FONTSOURCE_RESOLUTION_FAILED.md`) and stops. The expected response is to either install/stage the font another way or to choose a different family in the source HTML, not to silently render with a fallback.

## Configuration

| Env var | Purpose | Default |
|---|---|---|
| `POLISHER_WORK_ROOT` | Root directory for `render_work/`, `polisher_skill_quick_test/`, `polisher_skill_full_test/` | First writable of `/mnt/data`, `$TMPDIR`, `$HOME` |
| `SKILL_CREATOR_PATH` | Where `package_with_timings.sh` finds the `skill-creator` packager (maintenance only) | Auto-discovered from common vendor paths, then falls back to built-in `zipfile` zipper |

## Tests

```bash
# Quick smoke (~5-10 s warm, ~15-20 s cold including pymupdf install)
bash scripts/test_skill_quick.sh

# Full smoke (also verifies fonts survive into output PDF via pdffonts)
bash scripts/test_skill_full.sh
```

## Provenance

This skill emerged from a cross-environment iteration: an OAI ChatGPT and an Anthropic Claude built and validated parts of it in parallel through a single human integrator. Each AI had visibility into only its own runtime environment; cross-environment correctness was achieved by running both validators against every change. The pattern that worked: AI builds → other AI reviews/extends → human relays results across the boundary. Several env-specific bugs were caught only because the AIs could not validate each other's environments themselves.

The resulting skill enforces this discipline structurally: the wrapper auto-probes for writable work roots, the packager auto-discovers `skill-creator` across vendor paths and falls back to a built-in zipper, and the test suite verifies font embedding survives into the output PDF rather than just into intermediate HTML.

## License

MIT. See [`LICENSE`](LICENSE).
