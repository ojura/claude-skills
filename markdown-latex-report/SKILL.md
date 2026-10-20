---
name: markdown-latex-report
description: Turn a single Markdown file into a polished, book-quality PDF via pandoc and lualatex. Handles wide tables (auto-sized columns, no overflow), code listings that wrap long lines instead of bleeding off the page, identifier-aware line breaking in inline code, numbered sections with lettered appendices, running heads, mathematics, a table of contents, and widow/orphan control; checks the built PDF page by page and the prose for the faults reviews keep finding. Use when the user wants a long technical report, review, whitepaper, or document rendered to a clean PDF from Markdown, especially one with many tables, code blocks, citations and charts.
---

# Markdown to LaTeX Report

Builds a polished PDF from one Markdown file. The value is in the typographic
discipline that plain `pandoc -o out.pdf` does not give you: wide tables that
size their own columns and never overflow the margin, code blocks that wrap
long lines at sensible points, inline code that breaks at identifier boundaries
(`camelCase`, `::`, `_`, `.`, `/`) instead of running off the edge, a clean
table of contents, running heads, and widow and orphan control. The second half of
the skill is a checker that reads the built PDF and the Markdown and names
every page and line that a reader would stumble on, so that a report can be
finished in one pass instead of after a reviewer's second.

## Components

- `Makefile`: the build. `make` renders `REPORT` (default `test/REPORT.md`)
  to a PDF. Override the source: `make REPORT=path/to/doc.md`. `make check`
  builds the report with the LaTeX log kept and runs the checker (below).
  `make venv` creates the Python environment the charts and the checker use.
- `header.tex`: the LaTeX preamble: fonts (TeX Gyre Pagella body, configurable),
  table styling (booktabs + faint row rules), link colors, code-listing setup,
  running head (the first section that starts on the page), figure placement
  (top or bottom of the page, text filling the rest), PDF bookmarks, widow/orphan
  penalties, the space a heading reserves before it (7, 6 and 5 baselines for
  levels 1 to 3), and a patch to longtable that moves a heading to the next page
  together with a table that directly follows it when the table cannot start
  on the heading's page.
- `bblopts.cfg`, `hyph-en-gb.tex`: British hyphenation for documents with
  `lang: en-GB` on a TeX installation without the package hyphen-english. babel
  reads `bblopts.cfg` while it loads; the file registers the language
  `ukenglish` with the patterns in `hyph-en-gb.tex` (from hyph-utf8, MIT
  licence), which babel then loads. Without them TeX does not hyphenate such
  a document. An installation that has `ukenglish` keeps its own patterns.
- `filter.lua`: the pandoc Lua filter, which does most of the work:
  1. renders every Markdown table as a raw-LaTeX longtable with column widths
     computed from content (long-text columns dampened, short columns floored,
     any single column capped), or taken from the separator dashes for a table
     inside a `::: {.widths-from-dashes}` div, cells aligned as the Markdown
     marks them (left by default, `---:` right, `:-:` centre), and words not
     hyphenated; a continuation page repeats the header under a
     "Table N (continued)" line, and the rule between two rows stays under the
     last row of a page; a caption ending in `{#tbl:slug}` gives the table a
     label;
  2. inserts break opportunities inside inline code and link text at identifier
     boundaries, ranked by penalty: a break at a "/" or a space is preferred
     to one after a "_", which is preferred to one before a "(" and to one
     after a "-" or inside a camelCase name; very long alphabetic runs get
     last-resort letter breaks. A decimal number stays whole, a leading "/"
     stays with the name after it, and a file name never splits before its
     extension and splits at its other dots, as a line range at its "-",
     only when the other break points do not fit;
  3. discourages breaks inside bracketed groups;
  4. puts a no-break space between a number and its unit ("28 ms", "5 percent"),
     starts the lettered appendices at the first level-1 heading with the class
     `appendix`, and inserts the hyphenation exceptions of the metadata field
     `hyphenation`.
- `tools/check_report.py`: the checker that `make check` runs: reads the log,
  the PDF and the Markdown, renders the pages, and reports stretched and
  overfull lines with page and source line, wrong running heads, pages that
  hold only a figure or end early, headings at a page foot, inline code broken
  inside a name, quoted section titles that differ from their headings, cited
  paths that are missing or untracked, images that are missing or raster, and
  captions longer than three sentences. See "Checking the report".
- `tools/prose_check.py`: the prose scan: dashes, "whose" clauses, negation
  on a noun, headings that are not plain labels, long
  sentences and paragraphs, number and spelling conventions, identifiers in
  plain text, coined compounds, banned and process vocabulary, terminology
  drift against a terms file. See "Writing the report".
- `chartstyle.py`: the house style for matplotlib charts: figure width, font
  sizes that print readably, one palette, and a `save` that writes the PNG and
  the PDF. See "Charts".
- `tools/sample.py`, `docs/`, `hooks/pre-commit`: the showcase. `docs/` holds
  the fixture's PDF and three preview pages, which the README links; `make
  sample` writes them from a fresh build, `make check` on the fixture fails
  while they differ from one, and the hook refuses a commit that changes the
  pipeline while they differ. See "The showcase and the commit routine".
- `templates/report.md`: a starter document with the front matter that works,
  the structure of a review (verdict first, findings, recommendations,
  evidence, limits, an evidence index and a terms appendix), and the
  conventions as comments. It builds as it is.
- `md_table_to_latex.py`: standalone helper to convert one specific pipe table
  to a fixed-width LaTeX longtable by anchor text and explicit column widths.
  Use when the filter's auto-sizing needs a manual override for a particular
  table. Not invoked by the default build.
- `fvextra.sty`, `upquote.sty`, `widows-and-orphans.sty`, `needspace.sty` -
  LaTeX packages bundled locally (all LPPL) so the build does not require a
  full texlive install or root to apt-install them.

## Build

```bash
make                      # builds test/REPORT.md -> REPORT.pdf
make REPORT=mydoc.md      # builds your own Markdown
make check REPORT=mydoc.md CHARTS_DIR=path/to/charts   # build with the log kept, then check
make rebuild              # force a clean rebuild (also redraws charts)
make clean                # remove PDF + LaTeX temp files + chart sentinel
make venv                 # ~/.venvs/mlr with matplotlib, numpy and PyMuPDF
make sample               # rewrite docs/sample-report.pdf and docs/preview/ from the fixture
make hooks                # install hooks/pre-commit through core.hooksPath
```

A document without charts does not need a plot script: when `PLOT` does not exist,
the chart step is skipped.

## Charts

Charts are generated by a Python script and embedded as images. The Makefile
runs `CHARTS_DIR/plot_findings.py` (default `test/charts/plot_findings.py`)
once and records a `.stamp` file beside the charts, so editing the Markdown
alone does not redraw them.
Point `PLOT` and `CHARTS_DIR` at your own script and output directory, or drop
charts in and reference them with `![caption](charts/NAME.png){width=90%}`.

Draw the charts with `chartstyle.py` (import it from the skill directory or
copy it beside the plot script): `cs.apply()` sets the defaults, `cs.figure()`
makes a figure of the right width, `cs.style(ax)` dresses an axis and
`cs.save(fig, name, outdir)` writes both files. The report includes a figure
at 90 percent of its text width; a figure drawn 6.3 in wide prints at a scale
of 0.92, so 9 pt text prints at 8.3 pt, close to the footnote size. Text
drawn smaller than 9 pt, or a figure drawn wider and scaled down, prints too
small to read. Legends and axis labels use the words of the text, not the
names of the data columns; the same color means the same series in every
figure; a scatter of thousands of points is rasterized at 300 dpi; the data
each figure draws lives beside the script in a committed file with a header
naming its source, so that the figure can be redrawn later. The fixture's
`test/charts/plot_findings.py` shows the use.

## Vector figures

Plot scripts write every chart **twice with the same basename in the same
directory**: a raster `.png` and a vector `.pdf`
(`fig.savefig("charts/foo.png", dpi=150)` then `fig.savefig("charts/foo.pdf")`).
The Markdown references only the `.png`, so plain-Markdown preview (GitHub,
editors) keeps working. At LaTeX time `filter.lua`'s `Image` handler promotes
any `.png` image with a `.pdf` sibling on the resource path to the
vector file, so the shipped PDF gets sharp, zoomable, searchable plots
instead of re-rasterized bitmaps. Photographs, renders and screenshots stay
raster by not shipping a sibling; the rule only touches plots that
opted in. Why: raster plots blur at print zoom and bloat at high dpi; vector
axis text stays crisp at any size and usually weighs less. (Raster payloads
inside a vector figure, e.g. an imshow heatmap, correctly remain raster while
their axes and labels go vector.)

### Two hazards learned in production

**Broken PDF image encoding in old matplotlib.** Ubuntu 24.04's system
matplotlib (3.6.3) writes corrupt image XObjects for `imshow` content in PDF
output on some systems: rows decode with a shifted stride and the image
shears into diagonal wrap-around bands, while the PNG (Agg) output of the
same figure stays correct, and all PDF renderers (poppler, mupdf, gs) agree
on the corruption because the file itself is bad. Generate figures with
matplotlib >= 3.8 (`make venv` gives one) and always render one PDF page
(`pdftoppm`) and compare it against the PNG before shipping. Text-and-line
vector figures are unaffected; only embedded raster payloads shear.

**Stale embeds.** Images are embedded at pandoc time, and the `all` target
echoes "built" even when the PDF was up to date, so a regenerated figure
reaches the PDF only when the PDF is rebuilt. The Makefile lists `img/*` next
to the report as dependencies; if your figures live elsewhere, `make rebuild`
after regenerating them.

## Markdown conventions the pipeline expects

- Title block: pandoc's `% Title` / `% Author` / `% Date` three-line form at the
  top, or a `---` YAML metadata block. `templates/report.md` has a YAML block
  with the fields a long report needs: `lang: en-GB`, `papersize: a4`,
  `numbersections: true`, `graphics: true`, `hyphenation: [...]`.
- Headings carry labels: `## Served-pose age {#sec:age}`. Refer to a section
  as `Section\ \ref{sec:age} (Served-pose age)`: the backslash-space is a
  no-break space in pandoc Markdown, and `Section~\ref` prints a literal tilde.
  The same for `Figure\ \ref{fig:x}`, `Table\ \ref{tbl:x}` and
  `Appendix\ \ref{sec:x}`.
- Appendices: give the first appendix heading the class `appendix`
  (`# Evidence index {.appendix #sec:evidence}`). From there the level-1
  headings are lettered and figures and tables are numbered A.1, B.2. The
  heading keeps `#`; do not write "Appendix A" in it.
- Tables: standard pipe tables. The filter sizes columns automatically; you do
  not write LaTeX. Mark numeric columns `---:` so that their digits line up.
  The caption line `Table: Caption. {#tbl:slug}` gives the table its label.
  To fix the relative widths of one table, put it inside a
  `::: {.widths-from-dashes}` ... `:::` div and draw the separator dashes in
  the proportions you want (`|--|------|` for 1:3). Pandoc reads the dashes
  only when a line of the table is longer than 72 characters.
- Code: fenced blocks with a language tag. Long lines wrap automatically. A
  code block stays on one page (`samepage`), so a block taller than the space
  left moves whole to the next page and leaves the foot of the page empty;
  split a long listing in two, or move it.
- Images: `![caption](charts/NAME.png){#fig:slug width=90%}`. Resolved against the
  Markdown file's directory and the charts directory. A figure floats to the
  top or the bottom of its page or the next one, and a figure that needs a
  page of its own sits at the top of that page; a document that wants every
  figure exactly where it stands sets `include-before: |` with
  `\floatplacement{figure}{H}` and accepts the empty page feet.
- Units: write "28 ms" with an ordinary space; the filter makes it a no-break
  space (U+00A0) for the units it knows (ms, s, min, h, Hz, m, mm, km, rad,
  deg, °, percent, %, pt, px, dpi, kB, MB, GB, V, A, W, g, kg and their
  prefixes). `nbsp-units: false` in the metadata turns this off.
- Hyphenation exceptions: `hyphenation: [look-ahead, lo-cal-iza-tion]` in the
  metadata for words the patterns break badly (the checker's underfull lines
  show the breaks TeX chose).
- Running head: by default the first section that starts on the page, or the
  section in force. `include-before: |` with `\renewcommand{\runninghead}{Short
  title}` sets a fixed one, `\renewcommand{\runninghead}{}` removes it. A YAML
  `header-includes` value does not reach the preamble, because the Makefile's
  `--include-in-header` replaces it; `include-before` works for commands that
  may follow `\begin{document}`.
- A Unicode Greek letter in a caption (θ) makes LaTeX report "Label(s) may have
  changed" on every run; write `$\theta$`.
- Mathematics: `$...$` inline and `$$...$$` for a display; see Mathematics
  below.

## Mathematics

Write formulas as mathematics, `$...$` inline and `$$...$$` on a line of its
own for a display, not as inline code; keep inline code for text quoted from
source code or configuration. Pandoc passes the formulas to LuaLaTeX, and
unicode-math sets them in the font that `PANDOC_MATHFONT` names, TeX Gyre
Pagella Math by default, which matches the Pagella text. Without a math font
the formulas would print in Latin Modern Math. To use another math font, set
`PANDOC_MATHFONT` (`make PANDOC_MATHFONT="Latin Modern Math"`); a `mathfont`
field in the document's metadata does not override the Makefile's value.

- Put a formula that does not fit on its line on a line of its own as display
  mathematics. TeX breaks inline mathematics only after a relation such as `=`
  or a binary operator such as `+`, so a long inline formula breaks at an
  awkward place or leaves the line before it stretched.
- Write exponents in mathematics, as in `$10^{-5}$` and `$2^{31}$`. TeX Gyre
  Pagella has the superscript digits ¹, ² and ³ but not ⁰, ⁴ to ⁹, the
  superscript minus or ᵀ, so a number written as 10⁻⁵ prints with missing
  characters, and pandoc reports each one as a `[WARNING] Missing character`
  line. A unit in running text such as m² keeps its Unicode superscript.
- Keep a unit that follows a formula outside the mathematics, after a no-break
  space: `$t = 1$\ s`. In pandoc Markdown a backslash before a space makes it a
  no-break space; the character U+00A0 works too. Inside a formula, write the
  unit upright after a thin space: `$u + 22\,\mathrm{ms}$`. Write microseconds
  as `\mathrm{µs}` with the micro sign, because `\mu` is the italic Greek
  letter and slants the unit.
- The filter sets table header cells in bold, but mathematics in a header cell
  prints in normal weight, because TeX Gyre Pagella Math does not have a bold
  version. `\boldsymbol` and `\boldmath` do not change that and add font
  warnings to the log. The unicode-math commands `\symbfit` and `\symbfup`
  print bold letters in the PDF, but KaTeX does not support them.
- GitLab renders `$...$` and `$$...$$` in a Markdown file with KaTeX. If the
  Markdown is also read there, use commands that KaTeX supports: its support
  table does not list the unicode-math commands `\symbfit`, `\symbfup` and
  `\upmu`, which work only in the PDF.
- One symbol for one quantity through the whole document, defined where it
  first appears; a scalar times the identity gets a thin space
  (`$10^{-12}\,I$`); a sum over a sum gets braces (`${\sum a}/{\sum b}$`) so
  that the slash is spaced evenly.

## Writing the report

The faults that reviews of long reports built with this skill found, and the
rule each gave. `tools/prose_check.py REPORT.md` counts the ones that can be
counted; `templates/report.md` is laid out on them.

- Lead with the verdict and the numbers behind it. The summary opens with
  the conclusion in one sentence, a table of the decisive measurements with
  their units and their section, and one bullet per finding ending with the
  section that holds the evidence. Findings come before the evidence chapters
  and the recommendations before the evidence, so that a reader who stops
  after ten pages has the result.
- One name per concept, and a terms appendix that defines each. A document
  that calls one thing "the raw run", "raw serving", "the pure run" and "the
  raw-serving test" makes the reader check whether four things are meant.
  Pick the name, put it in the terms table, and do not use a synonym. Give the terms
  file to the prose checker (`--terms`) to find the strays.
- State a fact once and refer to it. A number that appears in four places
  drifts into three values; a limitation repeated in every chapter is skipped
  in each. The one statement carries the citation; the others say "(Section N)".
- One finding per paragraph, under 200 words; one idea per sentence, under
  about 25 words. A paragraph that holds the mechanism, the measurement and
  the recommendation is three paragraphs.
- Every fact carries its citation, as `repository/path/file.cpp:118-129` at a
  commit. Cite a file in full once in a parenthesis; inside the same sentence
  the short form `:139-149` may follow. When one sentence cites three files of
  one directory, name the directory once and the files by name, or the line
  stretches around the paths. Keep the citation out of the sentence's first
  words: "The converter applies p = R(-h) p (file:61-66)" reads; "(file:61-66)
  applies ..." does not.
- Captions of at most three sentences: what is plotted, from which data, and
  what the figure does not show. The legend uses the words of the text.
- Headings are plain labels of their section's content, with a label for
  cross-references. A comma that lists the section's topics is fine; a comma
  or colon that withholds the point ("The lag, and why it matters") is not;
  a question is not a heading.
- Numbers: thousands separators (24,436); three significant figures for a
  measurement; the unit after a space; the degree sign on both ends of a
  range (0.05° to 0.08°); the minus sign U+2212, not a hyphen; exponents in
  mathematics; "percent" or "%" but not both; one marker for a missing value
  in a table ("not recorded"). A number alone in code font is a recorded
  field value or a log value, not a measurement.
- Identifiers, paths, parameters, topics, state names and quoted code go in
  code font; a sentence does not open with one ("The function `add` ..." not
  "`add` ..."). A number is not code.
- Do not write dashes (em or en, or `--`, which pandoc turns into one); write
  a comma, a colon or a new sentence instead.
- Do not write a "whose" clause: state the fact as a statement or a condition, with the
  thing found as the verb's subject or object ("a lock is taken when its owner
  is gone", not "a lock whose owner is gone is taken"). Do not front an object
  before its verb ("drain each queued scan once the buffer covers its last
  beam", not "drain the scans whose last beam the buffer covers").
- Negate the verb, not the noun ("the script does not write bytecode", not
  "the script writes no bytecode"; "every commit is kept", not "no commit is
  lost").
- Do not decorate: "surface", "stranded", "gated", "load-bearing", "blast
  radius", "carve-out", "seam" and "landed" add tone without information, and a
  cost word ("cheap", "pragmatic", "good enough", "for now") stands in for a
  measured cost; name the cost instead. Do not coin a compound for one sentence
  ("end-plus-25-ms stamp conversion"): say what it is.
- Keep the authoring process out of the text: scratch paths, worker or agent
  names, pass numbers, "now", "no longer" and "the earlier version" do not
  belong in it. Say what is true and why, not what was.
- Decisions taken by others after the report was written are not narrated in
  it; one sentence says where they are recorded.
- A review states what the reviewers found, accepted and recommend at the
  reviewed commits, and what it did not do; a limits chapter closes it.

## Checking the report

`make check REPORT=doc.md CHARTS_DIR=charts` builds the document a second time
with the LaTeX log kept, renders every page at 60 dpi into
`/tmp/markdown-latex-report-check/<name>/render/`, and prints
`/tmp/markdown-latex-report-check/<name>/check.md`:

- the log's counts: overfull and underfull boxes, each with its page and the
  Markdown line it comes from, missing characters, undefined references, the
  rerun warning, float warnings, the hyphenation rules in force;
- the running head of every page against the first section that starts on it;
- pages that hold only a figure, pages that end more than 100 pt early with
  the reason (a table row or a code block that did not fit, a figure, a
  heading moved to the next page), pages that end in a heading, headings with
  fewer than three lines after them;
- the pages on which a table continues, and a doubled rule at a table break;
- inline code broken at a line end inside a name, by kind (a hyphen in a name,
  a line range, a file extension, a lone slash, a colon, a decimal point, a
  camelCase hump), beside the counts of the filter's intended break points;
- in the source: quoted section titles that differ from their headings, a
  tilde cross-reference, cited paths under `--root` (default: the report's
  directory) that do not exist or are untracked in git, images that are
  missing or lack a `.pdf` sibling, captions longer than three sentences.

For the fixture (the default `REPORT`) the check also compares the committed
showcase under `docs/` with the fresh build and fails while they differ; a
check of another document does not run that comparison.

Pass extra options with `CHECK_ARGS="--root path/to/evidence --render 100"`.
The check exits 1 when the PDF has lost or garbled content: an overfull
vertical box (text below the page edge), a missing character, an undefined
reference, a missing image. The other findings are for reading. Then look at
the rendered pages the report names, and at a sample of the others; the
checker cannot see a figure that overlaps its caption or a chart with text too
small to read.

`python3 tools/prose_check.py REPORT.md [--terms terms.txt] [--banned words.txt]`
scans the Markdown for the countable faults of "Writing the report" and prints
them by section with line numbers. It cannot judge a noun stack or a fronted
clause; those need a reader.

A one-shot report: write it on the template, draw the charts with
`chartstyle.py`, run the prose check and fix what it lists, `make check`, fix
the pages it names, look at the renders, and only then call it finished.

## The showcase and the commit routine

`docs/sample-report.pdf` and the three previews in `docs/preview/` are the
fixture built at the committed pipeline, and the README links them as the
showcase. `make sample` builds the fixture and writes them: the PDF is a copy
of the build, and the previews are the title page with the contents, the
first page with a code listing that wraps a line, and the first page that
holds a figure and a table, rendered at 220 dpi. The pages are chosen by
content, so they follow the fixture when its pagination changes. The files
are never edited by hand.

`make check` on the fixture compares the committed showcase with the fresh
build: the page count, the text of every page, the pages rendered at 60 dpi
and the three previews. It fails with `sample report is stale: run make
sample` while they differ.

A change to the pipeline (the Makefile, `header.tex`, `filter.lua`, the
`.sty` files, `bblopts.cfg`, `hyph-en-gb.tex`, `chartstyle.py`, the fixture
under `test/`) therefore goes with `make sample` and the regenerated `docs/`
in the same commit; until then `make check` fails. `make hooks` installs
`hooks/pre-commit` through `core.hooksPath` of the repository that holds the
skill. The hook runs the comparison when a staged path touches the pipeline
and refuses the commit while the showcase is stale. It reads the working
tree, not the index, so stage the regenerated `docs/` with the change. A
commit made with `git commit-tree` (the private-index routine) bypasses
hooks, so the check target is the guard that counts: run `make check` before
such a commit.

## Test fixture

`test/REPORT.md` plus `test/charts/plot_findings.py` are a self-contained
fixture (a fictional toaster-firmware performance report) that exercises the
pipeline's features: a wide multi-column speedup table, multi-page pagination, fenced code
listings with long wrapping lines, identifier-heavy inline code, six embedded
charts drawn with `chartstyle.py`, deep headings, and a table of contents. It
sets `lang: en-GB`, so it also tests the British hyphenation, and its notation
table (Appendix C) uses the widths-from-dashes div. Appendices A.3, F.1 and
I.1 hold inline formulas, a table header cell with mathematics and a display
equation, which test the math font. `make` without arguments builds it, which
doubles as the pipeline smoke test, and `make check` checks it. The content is
fiction and does not describe any real system.

## Dependencies

- `pandoc`, a LuaLaTeX engine (`lualatex` from texlive), the TeX Gyre fonts and
  TeX Gyre Pagella Math (the Debian and Ubuntu packages `fonts-texgyre` and
  `fonts-texgyre-math`), and DejaVu Sans Mono. A font that is not installed
  stops the build with the fontspec error `The font "..." cannot be found`;
  install it, or set `PANDOC_MAINFONT`, `PANDOC_SANSFONT`, `PANDOC_MONOFONT`
  or `PANDOC_MATHFONT` to an installed font.
- `python3` with `matplotlib` and `numpy` for the charts, and `pymupdf` for the
  checker; `make venv` installs the three into `~/.venvs/mlr`, which the
  Makefile uses when it exists (`PYTHON=...` overrides it).
- `poppler-utils` (`pdftoppm`) for the page renders of the check.

## Notes

- The bundled `.sty` files mean you do not need the full texlive-extra packages
  for fvextra/needspace/etc.
- Fonts are set via pandoc variables in the Makefile; change `PANDOC_MAINFONT`,
  `PANDOC_SANSFONT`, `PANDOC_MONOFONT` or `PANDOC_MATHFONT` to retarget. These
  values take precedence over font fields in the document's metadata.
