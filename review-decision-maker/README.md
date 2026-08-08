# Review Decision Maker

A Claude Code skill for taking a review report from untrusted claims to verified decisions, implemented fixes, tests, and a final resolution ledger.

It also includes a reusable, self-contained HTML decision board derived from a real 50-finding branch review. The board is now data-driven and works with any findings JSON file.

## What it does

- Preserves every source finding and reviewer attribution.
- Verifies each claim against the current branch before editing.
- Uses `Resolved` when a comment is already addressed, `Fix` for accepted but unimplemented work, `Discuss` when reviewer input is still needed, `Defer` for intentional later work, and `Reject` when the requested change will not be adopted.
- Consolidates only findings with the same root cause and correction surface.
- Groups accepted fixes into coherent implementation batches.
- Requires targeted tests and a final seam review.
- Produces a resolution ledger that distinguishes the decision from the actual outcome.
- Generates a local browser board with filtering, notes, persistence, import, JSON and CSV export, and standalone HTML snapshots with decisions embedded.

## Build a board

```bash
python3 scripts/build_decision_board.py \
  examples/findings.json \
  /tmp/example-branch-review-board.html
```

Open `/tmp/example-branch-review-board.html` in a browser. The file has no external dependencies and sends no data anywhere.

The generator uses only the Python standard library.

## Input

The preferred JSON shape is:

```json
{
  "title": "Branch review findings",
  "source": "review-findings.json",
  "generated_from": ["correctness-review", "test-review"],
  "findings": [
    {
      "id": "F001",
      "severity": "high",
      "category": "correctness",
      "title": "Length validation happens after the buffer read",
      "locations": [{"file": "src/frame_reader.cpp", "line": "42-57"}],
      "agents": ["correctness-review"],
      "description": "What is wrong.",
      "evidence": "The concrete failing state.",
      "proposed_resolution": "A suggested correction."
    }
  ]
}
```

A raw findings array also works. See [`SKILL.md`](SKILL.md) for accepted aliases, the complete verification workflow, decision semantics, implementation gates, and the resolution ledger format.

## Board behavior

- The original progress panel sticks to the top after it reaches the viewport, with the progress track above the decision-count buttons.
- Search and filters remain in the normal page flow. Reset local review work sits in this static filter panel.
- The table header sticks immediately below the progress panel.
- The lower action panel stays visible at the bottom and contains Next undecided, Hide decided, and the other review actions.
- On short-height or highly zoomed views, the progress panel returns to normal page flow so findings remain reachable above the bottom controls.
- HTML, JSON, clipboard, and CSV actions form one connected four-part button set with icons and rounded outer corners.
- Decisions and notes are stored in browser local storage under a signature of the finding content.
- Regenerating an unchanged dataset keeps its local state.
- Changing finding content produces a separate state.
- Exported decision JSON can be imported with merge or replace preview. Undo remains available until the next manual decision or note edit.
- Seeded `decision` and `note` fields become the board's reset state.
- **Export HTML** downloads a standalone copy with the current decisions, notes, theme, and density embedded.
- Each exported HTML snapshot uses its own local-storage namespace, so other board state cannot override its embedded decisions.
- Source text is escaped both in HTML and in the embedded JSON script block.
- CSV cells beginning with spreadsheet formula markers are prefixed so imported findings and notes remain literal.

## Test

```bash
python3 -m unittest discover -s test -v
```

The tests cover generation, the Resolved decision category and semantics, the sticky progress panel, static filters, bottom action panel, connected export buttons, embedded HTML snapshots, initial decisions, source-ID preservation, duplicate IDs, category symbols, token-shaped source text, unresolved template tokens, import preview and undo guards, spreadsheet formula markers, and script-breakout escaping.
