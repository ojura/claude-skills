# Font bridge notes

Use these notes only when the normal wrapper reports a font-resolution problem.

## Goal

Font remediation is an executable stage, not an open-ended research task. Produce deterministic local/embedded-font HTML before rendering. The PDF render must not depend on live Google Fonts, Chromium network fetches, HTTP servers, or fallback fonts.

## Normal path: Fontsource/npm fallback

The wrappers already own this sequence:

```text
resolve_fonts.py --embed
if exit 86: resolve_fontsource_fonts.py --embed
preflight_assets.py
render_playwright.py
```

For known families, `resolve_fontsource_fonts.py` must be allowed to run before any manual bridge work. It installs/uses Fontsource variable packages from npm/artifactory, preserves the requested family names, and embeds WOFF2 as data URLs so `page.set_content(...)` does not have to load sibling `file://` font assets.

Known mappings:

```text
Fraunces       -> @fontsource-variable/fraunces
Source Serif 4 -> @fontsource-variable/source-serif-4
JetBrains Mono -> @fontsource-variable/jetbrains-mono
```

Successful Fontsource fallback writes:

```text
font-bridge/fontsource-resolution.json
<style id="polisher-fontsource-fonts"> in the rewritten HTML
<meta name="polisher-required-fonts" ...> in the rewritten HTML
```

The renderer then verifies the requested families with `document.fonts.check(...)` before creating the PDF.

## Hard rule: no substitution

Do **not** solve a font problem by changing the design to installed fonts. Do not replace requested families such as `Fraunces`, `Source Serif 4`, or `JetBrains Mono` with EB Garamond, Noto Serif, DejaVu, Tinos, Inter, Lato, or any other local/system font. Do not remove Google Fonts links and proceed. A substitute-font PDF is a failed job.

The only acceptable outcomes are:

1. Render after the requested fonts are localized/embedded and verified.
2. If both direct Google localization and deterministic Fontsource/npm fallback fail, stop and report the generated blocker files.

## If Fontsource/npm fails

Read the generated files and stop unless the user explicitly asks you to try the Google view/download bridge:

```bash
cat $POLISHER_WORK_ROOT/render_work/job-slug/font-bridge/FONTSOURCE_RESOLUTION_FAILED.md
cat $POLISHER_WORK_ROOT/render_work/job-slug/font-bridge/fontsource-resolution-failed.json
```

Do not search for package names manually; the resolver already knows the supported mapping. If the family is unsupported, report the unsupported family list.

## Optional Google view/download bridge

Use this only when Fontsource/npm fallback is unavailable or unsupported.

1. Open the generated hard-stop files:

```bash
cat $POLISHER_WORK_ROOT/render_work/job-slug/font-bridge/FONT_BRIDGE_REQUIRED.md
cat $POLISHER_WORK_ROOT/render_work/job-slug/font-bridge/font-download-plan.json
cat $POLISHER_WORK_ROOT/render_work/job-slug/font-bridge/open-before-download-urls.txt
cat $POLISHER_WORK_ROOT/render_work/job-slug/font-bridge/font-bridge-actions.jsonl
cat $POLISHER_WORK_ROOT/render_work/job-slug/font-bridge/container-download-actions.jsonl
```

2. For every planned URL, perform the bridge in this exact order:

   a. Open/view the exact URL from `open-before-download-urls.txt` or the `view_url_first` action.

   b. Only after the view/open step, stage exactly that same `url` to exactly `filepath` using the matching payload in `container-download-actions.jsonl`.

3. Rerun the same wrapper command. If the resolver emits a second plan for font binaries after Google CSS is staged, repeat the same view-then-download bridge.

4. Only after `font_resolution` succeeds and `preflight_assets.py` confirms no remote render-time assets remain may Playwright render.

## What not to do

- Do not switch to WeasyPrint, wkhtmltopdf, HTTP servers, or generic PDF converters because fonts failed.
- Do not browse for Fontsource package names manually during routine use.
- Do not assume Python `urllib`, Chromium, curl, or wget has internet access.
- Do not use installed/system fonts as stand-ins.
- Do not share raw font binaries with the user. Use staged/embedded fonts only to render PDFs or produce working HTML.

## Failure reporting

If a resolver writes a blocker file, the problem is not mysterious. Report the failing stage as `font_resolution`, include the blocker path, and stop. If tool wall-clock time exceeds `timings.log`, the excess happened outside the measured render wrapper; add instrumentation before retrying.
