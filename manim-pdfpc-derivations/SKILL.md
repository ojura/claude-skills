---
name: manim-pdfpc-derivations
description: Author mathematical derivations from one Manim scene and ship live pdfpc or HTML presentations, stepped PDFs and compact vector handouts. Use for animated geometry or algebra, lecturer-controlled teaching checkpoints, selectable mathematical text, light and dark themes, report figures, and portable presentation packaging. Includes the reusable authoring, export, TeX, browser and native packaging toolkit.
---

# Manim derivations, handouts and live presentations

Use the bundled toolkit in `scripts/derivation_toolkit/`. Author geometry,
expressions, teaching stops and printable states once; export matching vector
timelines and selectable PDF figures. Place those figures in Beamer or an
article, and present the live scene through pdfpc or the self-contained HTML
player.

## Install or inspect the project toolkit

Read the repository's `AGENTS.md` and presentation build guide before editing.
Check the working tree, publication workflow, supported themes and output paths.
Use an isolated worktree for a repository that syncs edits to Overleaf. Preserve
the existing deck order, notation and lecturer's teaching approach unless the
task requests a broader revision.

Read [Toolkit installation and interfaces](references/toolkit.md) when installing
the suite, adding a project, changing a provider or integrating TeX, HTML or
native packaging. Install an explicit committed skill revision with
`scripts/install.py`. The installer writes the project entry point, complete
vendored package and revision/hash lock. Commit those generated files with the
consumer so a clean checkout and Overleaf have the same implementation.

Inspect `presentations.json`, then discover the installed commands with:

```bash
python3 tools/derivations.py list --json
python3 tools/derivations.py --help
```

Keep subject matter, palette values, branding, source paths and teaching layout
in the consumer. Change reusable mechanisms in this skill, publish them, then
update the consumer through the installer. Its lock check identifies local
changes before an update.

Register a new deck in the repository catalog before using the shared commands.
For an isolated prototype, add that entry only in its detached worktree.

Read the selected provider, scene registration, document figure macros and a
comparable scene. Use the installed `derivation_toolkit.animation` modules. Read
the pinned pdfpc checkout's `VECTOR_ANIMATIONS.md` and current tests when
changing the timeline format or viewer behavior.

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

Read [Teaching and visual review](references/teaching-and-review.md) when
planning the sequence, constructing geometric annotations or reviewing the
pacing and handout layout.

## Author one scene for all outputs

Use `AuthoringScene` for the shared semantic text, emphasis, arrows, coordinate
frames, marked angles, brackets and fitting helpers, or `VectorScene` for a
custom composition. Declare one immutable `Canvas` per scene; it determines
geometry, text metrics and exported dimensions. Set `HandoutLayout` for the
number of columns, available figure height and row spacing.

Use `play()` to record Manim motion and `checkpoint()` to save each printable
state. A checkpoint also creates a live teaching stop by default; use
`pause=False` for a useful printed intermediate that needs no live pause. Supply
compact mobjects and a crop when the printed figure needs a different layout.
Keep the first state at time zero and the final teaching stop at the endpoint.

Keep equations, coordinate transforms and captions in the provider. Define the
light/dark colours once in the project's TeX palette; the Python exporter and
document theme read the same roles. Choose an explicit DVI-compatible figure
font template when matching a document's typography. Check the exported font
identities and selected text against that choice. Keep an article's existing
LuaLaTeX or XeLaTeX workflow when embedding the resulting PDFs.

Use the semantic text factories before copies and transforms so PDF export
preserves the actual glyph geometry and text selection.

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

Use `play()` for motion driven by updaters; `wait()` records a hold. Define
checkpoints after the geometry and text have reached their complete state.

Use vector mobjects supported by the exporter. If an effect requires a raster
image, shader, camera trick or unsupported text morph, adapt the design to the
exporter's supported geometry before building. Export vectors for the live
workflow. Render a video only when the user requests a video deliverable.

## Generate the document editions

Register the scene in the provider's `SCENES`, then use `\DerivationScene{id}` in
the document. The toolkit's TeX module reads the generated metadata and selects
the figure layout for each edition:

- **Live:** a complete poster occupies the animation region, and pdfpc can play,
  pause and navigate the authored checkpoints there.
- **Steps:** successive printable checkpoint states appear as Beamer overlays on the
  same logical frame.
- **Handout:** the useful derivation states share one printed slide. Label the
  reading order, keep the text readable at slide size and use an intentional
  final state for replacement photographs or figures.

In an article, `\DerivationScene` defaults to the compact handout.
`\DerivationHandout{id}` also embeds that layout explicitly in a report figure.
Use the `\DerivationPanel` or `\DerivationHandout` override for an authored
composition such as panels followed by a separate result. Reuse the provider's
expressions, compact geometry and captions in each edition.

Use repository-root `build/` for products, previews, environments and native
binaries; deck products go under `build/decks/<catalog-id>/`. Explicit cache and
output overrides can point outside the repository. Commit the source, installed
toolkit and figure assets needed by the document's build. Publish tracked
figures by exporting explicitly to the catalog's animation assets directory.

Check the syncing tool's own ignore rules before building in a live replica.
Overleaf Workshop's replica does not consult `.gitignore`; its project sync
ignore patterns must include `**/build/**`. Keep work in the isolated worktree
until that supported setting is confirmed.

## Build, inspect and exercise

Follow the repository's publication order. Publish a reviewable source candidate
before lengthy downstream validation when the project requests that workflow.

Build the changed scene's light and dark live, stepped and handout editions
through the shared command. Use `--regenerate` to export and compile from the
fresh result. For example, with a catalog deck ID in `DECK`:

```bash
python3 tools/derivations.py build "$DECK" --theme both --mode all --regenerate
```

To inspect or reuse a separate export, use `export` with an explicit output
directory and pass the same directory to `build --assets-dir`. Default builds
use the tracked assets. Read the reported output paths and launch the generated
live PDF with the pinned viewer. Build the HTML edition from the same PDF and
timeline pair. For a report, retain its existing build/check commands and pass
the compiled PDF to the HTML packager; page mapping can use explicit metadata
or physical page order.

Check the mathematics independently, including a simple numerical example,
signs, basis directions, inverse relationships and dimensions. Inspect every
changed checkpoint and representative intermediate times in both themes.
Render the final document pages as well as the standalone figures. Check copied
text from the exported figures and final handout when text or transforms change.

For a visible description sequence, check the exported text and painted colours
at every stage, including the removal of earlier descriptions at the result.
Keep a regression whose negative control removes or hides the text while leaving
the checkpoint metadata valid.

Use [Verification and rendering measurements](references/verification.md) for
the shipped source, semantic-text, PDF, browser and native commands. Run the
checks relevant to the change.
Follow small numeric drawing differences through to their visible effect; use
the repository's established warning policy. Choose additional tests for the
remaining risk rather than repeating completed checks.

Exercise the real presenter and audience windows when changing timing,
bindings or controls. Check Next, Previous, play/pause, replay, continuous play,
final state and return to slide navigation. Confirm both windows share the
timeline and that freeze, blank or hidden states behave as documented. Use
private display and bus sessions for automated checks.

For HTML, open the packaged file directly in Chrome and Firefox and exercise
its projector window. Sample transition frames as well as settled states:
page, theme and resize changes must commit their PDF, vector and text surfaces
together. Check backward entry into a completed scene in both windows.

When changing the reusable suite, also build an independent consumer with
different canvas geometry and an odd handout panel count. Exercise unequal
printable/live checkpoint sets and sequential exports of differing canvases.
Keep the consumer's existing document checks and typography intact.

If the task changes rendering performance, measure the actual target display
with the viewer visible. Report the display refresh rate, window geometry,
power conditions, mean rate and slow intervals. Attribute display feedback,
draw timestamps and CPU work to their actual measurements. Retain a material
slow interval in the report and investigate its cause.

## Deliver the usable result

Finish the applicable repository checks, review, commits and authorized
publication. Provide the lecturer's launch command, themes, controls, handout
and stepped PDFs, plus the standalone HTML or portable native package requested
by the user. Verify a packaged viewer from its delivered artifact when packaging
changed, and report concrete remaining issues.

Remove owned temporary processes and disposable build output after their use.
Keep user windows, unrelated edits, needed figure assets and the requested
worktree intact.
