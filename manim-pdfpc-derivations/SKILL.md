---
name: manim-pdfpc-derivations
description: Author and revise mathematical derivations for Beamer using Manim vector timelines rendered inside pdfpc. Use when a lecture needs animated geometry or algebra, lecturer-controlled teaching checkpoints, matching stepped PDFs and a compact printable handout, including light and dark themes and selectable mathematical text.
---

# Manim derivations for Beamer and pdfpc

Use the repository's Manim exporter to produce a vector timeline and matching PDF
figures. Beamer places the figures on the lecture slide; pdfpc draws the animated
paths in the same region during presentation.

## Find the project and its commands

Read the repository's `AGENTS.md` and presentation build guide before editing.
Check the working tree, publication workflow, supported themes and output paths.
Use an isolated worktree for a repository that syncs edits to Overleaf. Preserve
the existing deck order, notation and lecturer's teaching approach unless the
task requests a broader revision.

For PSR lectures, read `docs/presentations.md` and inspect the selected entry in
`presentations.json`. Discover the current commands with:

```bash
python3 tools/presentations/presentations.py list --json
python3 tools/presentations/presentations.py --help
```

The catalog identifies each source, language, ROS generation and animation
provider. Select the requested edition explicitly. Historical ROS examples keep
their historical versions; current ROS2 material reads the repository setting.

Register a new deck in the repository catalog before using the shared commands.
For an isolated prototype, add that entry only in its detached worktree.

Read the selected provider, scene registration, Beamer figure macros and a
comparable existing scene. Reuse the shared authoring modules under
`tools/presentations/animation/`. The pdfpc checkout's
`VECTOR_ANIMATIONS.md` owns the timeline format and viewer behavior. Consult its
current source and tests when changing those interfaces.

## Plan the reasoning before the motion

Read the target slides and their immediate prerequisites and payoff. State what
students should be able to explain at the end. Identify the quantities that
stay fixed, those that change, the coordinate convention, units and notation.

Write the proposed teaching stops as complete statements or questions. For each
stop, identify the visible geometry, the corresponding expression and the next
prediction the lecturer can ask students to make. A stop should leave a coherent
picture that can remain on screen during discussion.

Allocate time according to the slide's purpose. A detailed construction needs
time to follow each contribution. A cumbersome scalar calculation used to
motivate a matrix can move quickly through its intermediate algebra. Preserve
the conclusion that justifies the new representation.

See [Teaching and visual review](references/teaching-and-review.md) when choosing
the sequence, pacing or handout layout.

## Author one scene for all outputs

Use the supplied `VectorScene` lifecycle. Its `play()` records vector geometry
during Manim animations; its `checkpoint()` records the current figure and,
when requested, a live teaching stop. Use the current method signatures from the
shared module. The exporter runs that lifecycle with movie writing disabled.

Keep equations, coordinate transforms, captions and theme colours in their
existing shared definitions. Use the palette loader and semantic text helpers.
Register TeX semantics when constructing text, before copies and transforms, so
PDF export preserves the actual glyph geometry and text selection.

Author visible teaching descriptions and their checkpoint labels from one text
definition. The shared `StepDescriptions` helper reveals each declared step,
dims earlier descriptions and checks their presence before clearing the sequence
for the result. Supply its text factory to control typography and placement.
Checkpoint metadata also serves the presenter controls; student-facing
instructions need actual text objects in the scene.

Move existing tokens when the mathematical expression stays the same. When an
expression changes, construct the new semantic text and animate its introduction
or replacement with a supported operation. Preserve the intended object order
and explicit z ordering. Check brackets, filled paths, masks and fading objects
at intermediate times.

Use `play()` for motion driven by updaters. The vector implementation's
`wait()` records a hold. Keep each scene's first teaching stop at its initial
time and its final stop at the intended endpoint. Define checkpoints after the
geometry and text have reached their complete state.

Use vector mobjects supported by the exporter. If an effect requires a raster
image, shader, camera trick or unsupported text morph, adapt the design to the
exporter's supported geometry before building the lecture. Do not invoke a movie
render for this workflow.

## Integrate the three lecture editions

Register the scene in the deck's animation provider and connect its ID to the
Beamer figure region. Use the existing macros and generated metadata to obtain
the native `pdfpc-vector:` link and matching poster.

- **Live:** a complete poster occupies the animation region, and pdfpc can play,
  pause and navigate the authored checkpoints there.
- **Steps:** successive exported checkpoints appear as Beamer overlays on the
  same logical frame.
- **Handout:** the useful derivation states share one printed slide. Label the
  reading order, keep the text readable at slide size and use an intentional
  final state for replacement photographs or figures.

Keep the handout's compact geometry in the scene definition when it needs a
different arrangement. Choose its panel count to fit the reasoning. Reuse the
authored expressions and checkpoint captions in each edition.

Build products, previews, environments and native binaries use external cache
directories. Commit only source and the figure assets that Overleaf needs to
compile. Use the repository's explicit asset publication command or output
directory when updating those tracked figures.

## Build, inspect and exercise

Follow the repository's publication order. Publish a reviewable source candidate
before lengthy downstream validation when the project requests that workflow.

Build the changed scene's light and dark live, stepped and handout editions
through the shared command. Use `--regenerate` to export and compile from the
fresh result. For example, with a catalog deck ID in `DECK`:

```bash
python3 tools/presentations/presentations.py build "$DECK" --theme both --mode all --regenerate
```

To inspect or reuse a separate export, use `export` with an explicit output
directory and pass the same directory to `build --assets-dir`. Default builds
use the tracked assets. Read the reported output paths and launch the generated
live PDF with the repository's pinned viewer so the inspected and presented
files are the products of the changed source.

Check the mathematics independently, including a simple numerical example,
signs, basis directions, inverse relationships and dimensions. Inspect every
changed checkpoint and representative intermediate times in both themes.
Render the final Beamer pages as well as the standalone figures. Check copied
text from the exported figures and final handout when text or transforms change.

For a visible description sequence, check the exported text and painted colours
at every stage, including the removal of earlier descriptions at the result.
Keep a regression whose negative control removes or hides the text while leaving
the checkpoint metadata valid. The inverse scene's semantic tests demonstrate
this check in both themes.

Run the relevant existing source, semantic-text, PDF and native-render checks.
Follow small numeric drawing differences through to their visible effect; use
the repository's established warning policy. Choose additional tests for the
remaining risk rather than repeating completed checks.

Exercise the real pdfpc presenter and audience windows when changing timing,
bindings or controls. Check Next, Previous, play/pause, replay, continuous play,
final state and return to slide navigation. Confirm both windows share the
timeline and that freeze, blank or hidden states behave as documented. Use
private display and bus sessions for automated checks.

If the task changes rendering performance, measure the actual target display
with the viewer visible. Report the display refresh rate, window geometry,
power conditions, mean rate and slow intervals. Attribute display feedback,
draw timestamps and CPU work to their actual measurements. Retain a material
slow interval in the report and investigate its cause.

## Deliver the usable result

Finish the applicable repository checks, review, commits and authorized
publication. Provide the lecturer's launch command, themes, controls, handout
and stepped PDFs, plus a precise account of remaining issues. Verify a packaged
viewer from its downloaded artifact when packaging changed.

Remove owned temporary processes and disposable build output after their use.
Keep user windows, unrelated edits, needed figure assets and the requested
worktree intact.
