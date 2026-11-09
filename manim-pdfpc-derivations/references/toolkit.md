# Toolkit installation and interfaces

- [Install a pinned copy](#install-a-pinned-copy)
- [Describe the project](#describe-the-project)
- [Author and export a scene](#author-and-export-a-scene)
- [Include figures in TeX](#include-figures-in-tex)
- [Build the document editions](#build-the-document-editions)
- [Package HTML](#package-html)
- [Use native presentation tools](#use-native-presentation-tools)
- [Run the toaster example](#run-the-toaster-example)
- [Keep dependencies and products reproducible](#keep-dependencies-and-products-reproducible)

## Install a pinned copy

Run the skill's installer with an explicit source commit. `SKILLS_REPO` below
is the canonical Git checkout containing this skill; `PROJECT` is the consumer.

```bash
python3 "$SKILLS_REPO/manim-pdfpc-derivations/scripts/install.py" \
  --project "$PROJECT" --source "$SKILLS_REPO" --revision "$REVISION"
```

Commit the three generated paths with the project:

- `tools/derivations.py`: the project entry point.
- `tools/derivation-toolkit.lock.json`: source revision and file hashes/modes.
- `tools/vendor/manim_pdfpc/derivation_toolkit/`: the complete runtime package.

The entry point verifies its installed inventory before loading the CLI. Check
an installation independently with the same installer and `--check --project`;
add `--source` to compare the lock with its committed canonical source. An update
requires the previous installation to match its lock. Preserve local edits in
the canonical skill, publish them, then install the new revision.

## Describe the project

Keep `presentations.json` at the project root. A minimal animated Beamer entry is:

```json
{
  "version": 2,
  "project": {"title": "Example derivations"},
  "default": "example",
  "palette": {"file": "palette.tex", "prefix": "example"},
  "decks": [
    {
      "id": "example",
      "title": "An example derivation",
      "source": "slides/example.tex",
      "language": "en",
      "animation": {
        "provider": "animations/scenes.py",
        "assets": "animations/generated",
        "manifest": "example.vector.json"
      }
    }
  ]
}
```

Use unique lowercase deck IDs and paths inside the project. Omit `animation` for
a static deck. Keep subject-specific fields in `metadata`. For an article built
by an existing tool, set its entry's `build.kind` to `external`; export its scene
assets with this toolkit and retain the article's own build command.

Configure TeX through the optional top-level `tex` object: `engine`, `packages`,
`commands` and `search_paths` describe the environment. `bindings` can map the
roles `theme`, `repository_root`, `minted_output_dir`, `assets`, `live` and
`handout` to an existing consumer macro; the builder also defines the generic
macros. A deck's `build.engine` overrides the project engine.

`tex.packages` lists resources checked with `kpsewhich`, such as `beamer.cls`.
For optional `build --fetch-tex`, supply a nonempty `tex.debian_packages` list
of Debian selectors: `name`, `name:architecture`, or either with `=version`.
When required resources are missing, this option downloads and extracts those
exact archives into `build/texlive/tree`. Dependency packages and font-map
configuration belong in the project's environment; the authoring container
provides a complete baseline for the bundled templates.

Define palette values in flat TeX `\definecolor` lines. For each `Light` and
`Dark` variant, provide `Background`, `Foreground`, `Accent`, `Muted`, `Guide`
and `Faint`, prefixed by the configured name. For example:

```tex
\definecolor{exampleLightAccent}{HTML}{2A78D6}
\definecolor{exampleDarkAccent}{HTML}{6BA9F1}
```

The Python loader and TeX theme read that file. Optional `Panel`, `Console`,
`Title`, `AxisX`, `AxisY` and `AxisZ` roles refine document styling; omitted roles
derive from the six required colours.

## Author and export a scene

Import from the installed package in the provider:

```python
from derivation_toolkit.animation.authoring import AuthoringScene, article_template
from derivation_toolkit.animation.layout import Canvas, HandoutLayout

class Example(AuthoringScene):
    scene_id = "example"
    canvas = Canvas(width=1280, height=720, scale=100)
    handout_layout = HandoutLayout(columns=3, max_height_fraction=.65, row_gap_mm=1)
    tex_template = article_template()

    def construct(self):
        # Construct the mathematics and call checkpoint() at authored states.
        ...

SCENES = [Example]
```

Use `math()` and `prose()` for semantic TeX, `alert_terms` for emphasis, and the
shared `arrow`, `line`, `frame`, `marked_angle`, `brackets`, `compact` and `fit`
helpers where they match the intended composition. Prose strings are TeX input.
Override `SceneLayout` or explicit placement arguments for the teaching layout.

`checkpoint(label, caption, crop=None, handout=None, pause=True)` saves a printed
state. `label` is the live control label; `caption` is authored TeX for the printed
panel. `handout` accepts compact mobjects, and `crop` is `[x, y, width, height]` in
canvas coordinates. `pause=False` retains that printable state while omitting
the live pause. The optional `REFERENCE_TIMES` provider dictionary sets review
sample times by scene ID; `RESULTS` supplies named reusable TeX expressions.

`article_template()` uses Latin Modern by default. Supply a validated
DVI-compatible preamble to match figure typography. For a Pagella/Heros report,
the tested figure template is:

```python
tex_template = article_template(
    r"\usepackage[T1]{fontenc}\usepackage{mathpazo}"
    r"\usepackage{tgpagella}\usepackage{tgheros}"
    r"\usepackage{amsmath}\pagestyle{empty}"
)
```

This uses Pagella/Heros text and Pazo mathematics in the figure PDFs. Keep the
host report's LuaLaTeX/Pagella Math setup and embed those PDFs directly.

Export with the registered deck ID:

```bash
python3 tools/derivations.py export example --theme both --previews
```

The default output is `build/decks/example/animations/`; previews use the sibling
`previews/` directory. The export contains a timeline per theme, selectable
compact/full PDF figures, `steps.json` and `steps.tex`. `--scene` selects one
registered scene. Use `--output-dir` and `--preview-output` for explicit paths.
An export to the catalog's declared assets directory publishes figures for
direct document builds, including Overleaf.

## Include figures in TeX

Load the installed modules after the document class. With compilation starting
at the project root, the basic setup is:

```tex
\providecommand{\DerivationRepositoryRoot}{.}
\providecommand{\DerivationTheme}{light}
\providecommand{\DerivationAssets}{animations/generated}
\providecommand{\DerivationPaletteFile}{palette.tex}
\providecommand{\DerivationPalettePrefix}{example}
\input{\DerivationRepositoryRoot/tools/vendor/manim_pdfpc/derivation_toolkit/tex/theme.tex}
\input{\DerivationRepositoryRoot/tools/vendor/manim_pdfpc/derivation_toolkit/tex/derivations.tex}
```

The builder supplies these paths and the selected theme/mode. Use
`\DerivationScene{example}` in a Beamer frame for steps, define `\DerivationLive`
for a linked poster, or define `\DerivationCompactHandout` for compact panels.
Place one linked live scene on each PDF page. An article defaults to the compact
layout. An explicit report figure can use:

```tex
\begin{figure}[tbp]
  \centering
  \DerivationHandout{example}
  \caption{The authored derivation and its result.}
\end{figure}
```

Keep a report's existing fonts and build filters. A Pandoc report can load this
setup through an additional `--include-in-header` file and place the figure in
a raw LaTeX block.

The figure API is:

| Command | Use |
| --- | --- |
| `\DerivationGraphic[options]{file}` | Include a figure from the selected theme directory. |
| `\DerivationPoster{id}` | Retrieve the full initial figure filename. |
| `\DerivationFigure{id}{index}` | Retrieve a compact panel filename. |
| `\DerivationCaption{id}{index}` | Retrieve its authored caption. |
| `\DerivationResult{id}` | Retrieve optional provider result expressions. |
| `\DerivationScene{id}` | Select live, steps or compact handout output. |

Override `\DerivationPanel{id}{index}` or `\DerivationHandout{id}` for a custom
composition. `\DerivationPanelGap` controls horizontal spacing; layout metadata
sets the row count and figure height. `\DerivationGraphicOptions` defaults to
`width=\linewidth,height=.8\textheight,keepaspectratio` for full figures.

The theme exposes active `derivationBackground`, `derivationForeground` and
the other palette roles. It applies Beamer colours and covered-item fading;
article fonts and page colours remain under the report's control.
`\DerivationLegacyGraphics` supplies a white backing for legacy figures in dark
mode. `\DerivationUncoverGraphics` provides a fading overlay-aware figure.

Load `tex/code.tex` after the theme when listings are needed. It defines
`derivationconsole` and `derivationconsoletext`, each accepting an optional
minted option list. Use the text lexer with `escapeinside` for TeX substitutions.
`\DerivationDefineConsole{name}{lexer}` provides consumer aliases; configure
`\DerivationCodeFontFamily`, `\DerivationLightCodeStyle` and
`\DerivationDarkCodeStyle` before loading. The default font family is the
document's `\ttdefault`.

## Build the document editions

```bash
python3 tools/derivations.py build example --theme both --mode all --regenerate
```

`--regenerate` exports fresh scene assets under `build/` and compiles from that
same result. Without it, builds use the catalog's declared assets. For a separate
export, pass its directory with `build --assets-dir`.

The builder preserves source-relative paths in a private view and writes TeX
auxiliaries under the build root. Each live PDF receives its matching
`.vector.json` sidecar. A `.presentation.json` file maps physical pages to logical
frames using Beamer's actual navigation metadata. Handouts require one physical
page per logical frame.

## Package HTML

Install the pinned PDF.js dependency under `build/tools/html5` by copying the
toolkit's `web/package.json` and `web/package-lock.json` there and running
`npm ci --prefix build/tools/html5`. Alternatively, supply the installed pinned
distribution with `--pdfjs-dist`.

```bash
python3 tools/derivations.py html example --theme both
```

The output contains a self-contained `index.html`, its license and instructions;
an adjacent ZIP includes the same files. Open `index.html` directly in Chrome
or Firefox. The embedded PDF.js, PDFs and vectors also support a synchronized
projector window.

For an externally compiled document, pass its PDF and matching vector timeline:

```bash
python3 tools/derivations.py html example --theme light \
  --pdf build/report.pdf --vectors build/report.vector.json
```

Omit `--vectors` for a static PDF. Supply `--page-map` when physical and logical
page numbers differ; otherwise the builder sidecar or physical page order
supplies the mapping. Explicit maps use version 1, `logicalSlides`, and a `pages`
list with `number` and `logical` integers.

## Use native presentation tools

Use `run example --theme light` to build and open the live deck in the pinned
pdfpc viewer. Arguments after `--` go to pdfpc. The `native` and `package`
commands build the viewer and portable AppImage; inspect their `--help` for the
selected asset and output paths. Keep the compiled PDF and timeline together.

The optional `native` catalog object selects an exact viewer `repository` and
40-character `revision`; the shipped pin is the default. The `package` object
sets the application `id`. Keep the consumer's title and default deck in the
project catalog.

Build a portable Linux package from the consumer root with the supplied Focal
container:

```bash
docker build --platform linux/amd64 -t derivation-focal-builder \
  -f tools/vendor/manim_pdfpc/derivation_toolkit/ci/focal-builder.Dockerfile \
  tools/vendor/manim_pdfpc/derivation_toolkit
mkdir -p build/cache
docker run --rm --platform linux/amd64 --user "$(id -u):$(id -g)" \
  -e XDG_CACHE_HOME=/project/build/cache \
  --mount type=bind,source="$PWD",target=/project --workdir /project \
  derivation-focal-builder python3 -B tools/derivations.py package example \
  --assets /project/build/decks
```

The first build fetches the pinned viewer commit and packaging tools. A complete
warm cache supports `--network none`. The default output is
`build/appimage/<package.id>.appimage`.

Use `--inputs /project/package-inputs.json` for externally compiled documents.
Paths in this map resolve from the consumer root. A static article available
only in the light theme uses:

```json
{"article": {"light": {"pdf": "build/report/REPORT-derivations.pdf"}}}
```

An animated entry also supplies a `vectors` path beside `pdf`.

For a host that compiles documents but cannot execute uploaded packaging tools,
use [Data-only assembly](data-only-packaging.md). Its `package-template`,
`package-prepare` and `package-assemble` commands keep the pinned runtime intact
and append fresh catalog data with installed Python and TeX.

## Run the toaster example

Use [the toaster source](../examples/toaster/) for a complete independent
consumer. Copy its contents into an empty project directory, then install the
toolkit there using the pinned revision. It derives a geometric mean from two
illustrative trial ratios in the fictional `markdown-latex-report` showcase.
The data lives in `trials.json`; the scene and report insertion read it.

The report container extends the toolkit's authoring image with Pandoc 3.1.3,
LuaLaTeX, TeX Gyre fonts and the locked chart/check environment. Build both
dependency images from the copied example project:

```bash
docker build -t derivation-assets:local \
  -f tools/vendor/manim_pdfpc/derivation_toolkit/ci/assets.Dockerfile \
  tools/vendor/manim_pdfpc/derivation_toolkit
docker build -t toaster-report:local -f report.Dockerfile .
```

Pass `--build-arg AUTHORING_IMAGE=IMAGE` to reuse an existing authoring image.
The report image keeps Manim in `/opt/manim` and its chart/check packages in
`/opt/report`; `python3` selects Manim, and the report Makefile's `PYTHON` selects
the latter. Installed versions are recorded under `/opt/report-environment`.

Prepare the article on the host from a committed checkout of the actual report
skill. `PROJECT` must be an absolute path:

```bash
python3 prepare_report.py prepare \
  --source-skill "$SKILLS_REPO/markdown-latex-report" --project "$PROJECT"
mkdir -p "$PROJECT"/build/runtime/{cache,matplotlib,texmf-var,texmf-config,texmf-cache}
toaster_run() {
  docker run --rm --network none --user "$(id -u):$(id -g)" \
    -e XDG_CACHE_HOME="$PROJECT/build/runtime/cache" \
    -e MPLCONFIGDIR="$PROJECT/build/runtime/matplotlib" \
    -e TEXMFVAR="$PROJECT/build/runtime/texmf-var" \
    -e TEXMFCONFIG="$PROJECT/build/runtime/texmf-config" \
    -e TEXMFCACHE="$PROJECT/build/runtime/texmf-cache" \
    --mount type=bind,source="$PROJECT",target="$PROJECT" --workdir "$PROJECT" \
    toaster-report:local "$@"
}
toaster_run python3 tools/derivations.py build toaster-demo \
  --theme both --mode all --regenerate
toaster_run python3 tools/derivations.py build toaster-static --theme both --mode all
toaster_run make -C build/report-source -f Makefile -f fixture.mk checked-report
```

Mount the consumer at the same absolute path used during preparation so the
generated TeX header resolves its figure and toolkit paths. These runs need only
that consumer mount and use no network.

The animated source uses a 1280 by 720 canvas, five printed states, three live
stops and a three-column handout. Its compact panel definitions and configured
Pagella/Heros/Pazo template are in `scene.py`.

The adapter copies the full tracked report tree into the project's build
directory so chart generation stays inside the copy. Its local Git snapshot
must match the committed source tree exactly before the adapter adds the derived
report, header and build recipe. This lets the original checker inspect tracked
paths inside the copy. The adapter inserts the generated handout beside
Appendix A.3; the source report and its committed showcase remain in place.

The checked article is `build/report/REPORT-derivations.pdf`. Package the article
and animated companion with:

```bash
python3 tools/derivations.py html article --theme light \
  --pdf build/report/REPORT-derivations.pdf
python3 tools/derivations.py html toaster-demo --theme both
```

`prepare_report.py page-map --pdf PATH --output PATH` creates explicit physical
page metadata when needed.

## Keep dependencies and products reproducible

The package supplies locked Python authoring dependencies, pinned PDF.js, the
native viewer revision, native packaging inputs and container recipes. Keep all
environments, media, previews, packages and review outputs under project `build/`
or an explicit external output root. Use `DERIVATION_CACHE_ROOT` and
`DERIVATION_BUILD_DIR`, or the corresponding CLI options, for those overrides.

Preserve the source publication workflow of the consumer. After changing a
reusable mechanism, publish the canonical skill revision, reinstall it into
each affected consumer, and exercise the actual generated document/player
interfaces. Use the independent consumer's original build checker as well as
the toolkit's export and runtime checks.
