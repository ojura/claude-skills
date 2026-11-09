# Verification and rendering measurements

Run the checks affected by the change, then inspect the actual document and
player outputs. Use the consumer's existing checker for its subject matter and
layout. The toolkit supplies the shared export, text, browser and native gates.

## Use the authoring environment

The installed `ci/assets.Dockerfile` supplies Manim, TeX, font maps, Cairo,
Poppler and the locked Python readers. An existing authoring environment can
use the package's `animation/requirements.lock` and
`animation/requirements-test.lock`. The Poppler area-selection probe runs with
`/usr/bin/python3` so it can use the system GI bindings; Debian/Ubuntu needs
`poppler-utils`, `python3-gi` and `gir1.2-poppler-0.18`.

Run from the consumer root. For the complete shared source gate:

```bash
PYTHONPATH="$PWD/tools/vendor/manim_pdfpc" python3 -B -m unittest discover \
  -s tools/vendor/manim_pdfpc/derivation_toolkit/tests \
  -t tools/vendor/manim_pdfpc -v
```

Use the relevant test modules while developing a focused change. Keep
consumer-specific equations, labels, page counts and teaching expectations in
the consumer's tests.

## Check generated documents and committed figures

After building the requested editions:

```bash
python3 tools/derivations.py verify example --theme both --mode all
PYTHONPATH="$PWD/tools/vendor/manim_pdfpc" python3 -B \
  -m derivation_toolkit.verify.verify_exports --project "$PWD" example
```

The document verifier checks page geometry, scene bindings and visible text
bounds. The export verifier regenerates both themes into a fresh review
directory, then compares them with the catalog's tracked assets. It checks
timeline and TeX metadata byte-for-byte and follows the PDF's active object
graph: drawing streams, glyph programs, advances, bounds and Unicode maps.

PDF comparison excludes document dates/IDs, xref numbering and stream
compression. Reports retain exact graph equality, digests and every classified
difference. The shared drawing-precision policy permits warnings for bounded
numeric changes in parsed page or glyph drawing operands; changed text,
operators, resources, structure or metadata remain errors. Font dictionaries
and Unicode maps use exact values. Read the reported limit and affected
operands before accepting a warning.

The fresh export, log and `report.json` remain under
`build/decks/<deck-id>/review/export-consistency/`. `--output-dir` selects
another build or external review directory; `--committed-dir` selects the
reviewed asset baseline.

## Verify selectable text against visible glyphs

The scene's semantic TeX helpers bind each glyph to its original character
cluster, font source, line box and baseline. TeX supplies word spaces and line
height/depth. Copies, groups and affine transforms carry those records into
full and compact snapshots. Expression changes need a new semantic text object
and an explicit transition.

The PDF sink writes the visible outlines as embedded Type 3 glyphs with Unicode
maps. Source identity survives Cairo subsetting, allowing finalization to set
the correct line bounds, font descriptor and glyph program for each instance.
Programs use a consistent 1000-unit glyph space; their control-point hulls are
rounded outward with a serialization margin. Page placement and physical
advances remain tied to the authored geometry.

Checks should cover the actual exported PDF through both MuPDF and Poppler:
read text, search a phrase, copy only its rectangle, and inspect the rendered
ink. Include spaces, ligatures, inline math, coloured fragments, copied objects,
transforms and cropped panels when those features change. A correct metadata
label can coexist with a removed or transparent object, so a visibility
regression must inspect painted output.

The shared Type 3 validator follows every glyph program's transforms and
painted curves. Both its `d1` box and the enclosing `FontBBox` must strictly
contain the drawing. Empty programs must represent whitespace. Its focused
controls include exact baseline edges, transformed glyphs, malformed bounds
and empty glyphs.

For composite operators such as `\Longrightarrow`, use opaque static
checkpoints and consistent component transforms and paint order. The exporter
checks those conditions before combining the components into one semantic
glyph. Ordinary filled text retains fractional opacity. A stroke-only semantic
glyph or an unsupported font-map transform produces an authoring error.

The DVI-based authoring template uses mapped Type 1 fonts with the standard
font matrix. Match a report through an explicit tested template, as in the
Pagella/Heros/Pazo toaster example. The host article can retain its own
LuaLaTeX and OpenType fonts. In direct pdfTeX output, `tex/theme.tex` loads the
maintained `glyphtounicode-cmex.tex` table for extensible Computer Modern
symbols. The authoring image supplies that table through `texlive-latex-base`.
The PDF regression checks real-reader extraction and unchanged rendered pixels.

## Exercise the standalone browser document

Install the package's `verify/requirements-browser.lock` in the test
environment, and provide Chrome and Firefox. After building the HTML:

```bash
toolkit="$PWD/tools/vendor/manim_pdfpc/derivation_toolkit"
node --test "$toolkit/web/test/"*.test.mjs
PYTHONPATH="$toolkit/.." python3 -B -m derivation_toolkit.verify.browser \
  --html build/decks/example/html5/index.html \
  --output-dir build/decks/example/review/browser \
  --browser both --protocol both
```

The verifier derives themes, pages, scenes, checkpoints and geometry from the
actual embedded bundle. It copies only the HTML file into an isolated path,
then exercises real keys/buttons over `file://` and HTTP. The cases include
checkpoint and continuous playback, intermediate and completed blanked states,
ordinary navigation, sole-page controls, theme changes, resize and a synchronized
projector window.

Transition probes sample rendered frames as well as settled state. They detect
loading overlays or mixed PDF/vector/text surfaces, including backward entry
into a completed scene. Before opening the projector, the verifier checks that
its button is visible, enabled and unobscured, then uses real pointer input.
Logs and screenshots remain in the chosen directory. `progress.json` records
the input HTML hash, completed browser/protocol cases and any later failure.
The harness closes its owned browsers; `--chrome` and `--firefox` select
explicit executables.

## Compare the native renderer

Build the pinned probe and export reference previews from the same provider:

```bash
probe="$(python3 tools/derivations.py native --probe)"
python3 tools/derivations.py export example --theme both --previews
PYTHONPATH="$PWD/tools/vendor/manim_pdfpc" python3 -B \
  -m derivation_toolkit.verify.compare_exports "$probe" \
  build/decks/example/previews/light \
  --assets build/decks/example/animations/light \
  --output build/decks/example/review/parity-light
ctest --test-dir "$(dirname "$(dirname "$probe")")" --output-on-failure
```

Repeat parity for the dark theme. The comparer reads the scene and manifest
names from exported `steps.json`, renders each exported full-scene state through
native Cairo at the reference size, and records pixel differences. Add
`--midpoints` when the provider declares reference times, or `--scene ID` to
select one scene.

The native repository owns interpolation, data validation and viewer-control
tests. Read its pinned `VECTOR_ANIMATIONS.md` for those interfaces and the
required GUI dependencies. The tests cover paths, opacity, glyph holes,
scaling, navigation, checkpoint skipping and presenter/audience timing.

For a drawing-cost measurement, choose the actual scene and output size:

```bash
"$probe" benchmark build/decks/example/animations/light/example.vector.json \
  example 1920 1080 1000
```

## Measure delivery on a display

Use the GUI profiler when changing draw scheduling or performance. From a
desktop terminal, build the live PDF and native viewer, then run:

```bash
pdfpc="$(python3 tools/derivations.py native)"
PYTHONPATH="$PWD/tools/vendor/manim_pdfpc" python3 -B \
  -m derivation_toolkit.verify.profile_gui "$pdfpc" \
  build/decks/example/example-light-live.pdf \
  --real-display --output build/decks/example/review/profile-light-desktop
```

The profiler uses the X11 GTK backend and numeric PDF page labels to select
logical slides. It starts its own D-Bus session and viewer, waits for their
readiness, and plays the selected scenes. Presenter and audience share a
monotonic playback anchor; each draw samples the actual monotonic clock.
GTK frame clocks schedule redraws and report presentation feedback.

The default requested windows are 1440×1080 for the audience and 1000×800 for
the presenter. They scale together to fit the desktop work area. The helper
records their actual decorated rectangles and checks overlap and bounds.
Keep both windows fully visible throughout a physical timing comparison:
occlusion changes display scheduling. Record the power source and active power
profile alongside display mode and window geometry.

Omit `--real-display` for a private Xvfb display, or use `--scene ID` for one
derivation. The helper checks its dependencies before starting: `pypdf`,
`xdotool`, `dbus-run-session`, `dbus-send`, ImageMagick's `import`,
`xrandr`, `xprop`, `xwininfo`, Git, `ps` and, for a private display, Xvfb.
It collects final frame feedback, closes its own process group, and restores
the previous focus when its viewer still owns focus.

Outputs include `report.json`, `trace.jsonl`, monitor details and per-scene
screenshots. Binary, PDF and timeline hashes tie measurements to their inputs.

| Field | Measurement |
| --- | --- |
| `native_draw_ms` | Wall time spent in native drawing |
| `native_cpu_ms` | Drawing-thread CPU time, when that clock is available |
| `sample_interval_ms` | Intervals between monotonic draw samples |
| `presentation_interval_ms` | Delivery intervals with continuous known display feedback |
| `unknown_feedback_span_ms` | Spans crossing missing, expired or unavailable feedback |
| `feedback_coverage` | Fraction of selected draws with positive presentation timestamps |
| `observed_positive_fps` | Rate of observed distinct presentation timestamps |
| `presented_fps` | Presentation rate when all selected draws have feedback |

Compare mean rate with the 99th-percentile and maximum delivery intervals,
feedback coverage and completion timing. The private-display tests exercise
controls and timing; a visible real-display run measures delivery on the
lecturer's monitor. Retain the raw trace with the report when investigating a
slow interval. The native format guide defines frame identities, delayed
feedback attribution and drain/close records.

## Check a portable package

Use the reusable workflow's native runtime gate or the commands in
[data-only packaging](data-only-packaging.md) for the corresponding package
format. Exercise the delivered artifact with networking disabled and verify
the catalog, selected PDFs, both supported themes and representative live
controls. Keep the consumer's article checker and typography checks when an
external report is one of the packaged documents.
