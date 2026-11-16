# Toaster report and derivations

This consumer uses the fictional toaster report in `markdown-latex-report` and
one geometric-mean scene. It produces a portrait article with compact derivation
panels, animated and static companion decks, and portable HTML/native packages.
The scene has a 1280 by 720 canvas, five printable states, three live stops and
a three-column handout. The report keeps its Pandoc/LuaLaTeX typography and checker.

Copy this directory into a new Git project. Install a committed toolkit revision
with the skill's `scripts/install.py --project PROJECT --source SKILLS_REPO
--revision REVISION --ci`, and commit the generated entry point, vendor directory,
lock and managed workflow. See the [toolkit guide](../../references/toolkit.md#run-the-toaster-example)
for the local build and report-embedding commands.

## Continuous integration

The workflow compares fresh scene exports with the consumer's declared figure
inputs. Export both themes to `animations/generated` and commit those source
assets before enabling CI:

```bash
python3 -B tools/derivations.py export toaster-demo --theme both \
  --output-dir animations/generated
git add animations/generated
```

Use the report authoring image described in the toolkit guide for that export;
its Pagella/Heros/Pazo figure fonts are required. The generated source figures
are distinct from ignored `build/` products.

Copy `ci.yml` to `.github/workflows/toaster.yml`. Its caller selects the existing
`report.Dockerfile`, which extends the workflow's loaded toolkit image with Pandoc,
LuaLaTeX and the locked report Python environment. `ci_build.py prepare` fetches
the exact report revision named in the caller into the runner cache. This is the
networked host phase; it does not depend on a prior local report build.

The complete authoring step runs as a nonroot user with networking disabled and
the consumer mounted read-only at `/src`. It tests the installed toolkit and
compares the declared figures before `ci_build.py build` copies the consumer's
sources into `/cache/toaster-project`. That command generates the companion
editions, uses `prepare_report.py` to embed the panels in an isolated copy of the
committed report, and runs its original `checked-report` command. The report
Makefile uses the image's `/opt/report/bin/python3` for charts and checks; the
derivation exporter uses `/opt/manim/bin/python`.

All twelve companion PDFs go under `/assets/toaster-demo` and
`/assets/toaster-static`. The checked article and its physical page map go under
`/assets/article`. `inputs.json` supplies that light-only article to the shared
artifact verifier and both native package builders; the companion inputs retain
their normal per-deck paths. The HTML job presents the animated companion.

The source-preparation and staging checks are also runnable locally:

```bash
python3 -B test_ci_build.py
python3 -B ci_build.py prepare --cache-root build/ci-cache \
  --report-revision 254c467d900a4880d6592bae5e4014402071f0b3
```

The selected report revision contains the maintained `markdown-latex-report`
source. Update that full Git revision deliberately when changing the report
input. Preparation and staging require fresh destinations and leave existing
work intact. The CI cache is separate from the read-only checkout; a local
container run should mount `build/ci-cache` at `/cache` and an output directory
at `/assets`, matching the managed workflow.
