#!/usr/bin/env python3

import base64
import importlib.util
import json
import re
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_FILE = PACKAGE_ROOT / "scripts" / "build_decision_board.py"
TEMPLATE_FILE = PACKAGE_ROOT / "assets" / "decision-board-template.html"
EXAMPLE_FILE = PACKAGE_ROOT / "examples" / "findings.json"
README_FILE = PACKAGE_ROOT / "README.md"
SKILL_FILE = PACKAGE_ROOT / "SKILL.md"

spec = importlib.util.spec_from_file_location("build_decision_board", SCRIPT_FILE)
builder = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(builder)


# A 1x1 PNG image.
PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def embedded_data(rendered):
    match = re.search(
        r'<script id="findings-data" type="application/json">(.*?)</script>',
        rendered,
        flags=re.DOTALL,
    )
    assert match is not None
    return match.group(1)


class DecisionBoardBuilderTest(unittest.TestCase):
    def test_example_builds_a_self_contained_board(self):
        raw = json.loads(EXAMPLE_FILE.read_text(encoding="utf-8"))
        payload = builder.normalize_payload(raw, EXAMPLE_FILE, None)
        rendered = builder.render_board(payload, TEMPLATE_FILE)

        self.assertEqual(rendered.count('class="finding-row"'), 3)
        self.assertIn("Example branch review findings", rendered)
        self.assertIn("Critical", rendered)
        self.assertIn("src/frame_reader.cpp:<strong>42-57</strong>", rendered)
        self.assertNotRegex(rendered, builder.TOKEN_PATTERN)
        self.assertNotIn("1887", rendered)

        match = re.search(
            r'<script id="findings-data" type="application/json">(.*?)</script>',
            rendered,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(match)
        embedded = json.loads(match.group(1))
        self.assertEqual(embedded["initial_decisions"], {"F002": "discuss"})
        self.assertEqual(
            embedded["initial_notes"],
            {"F002": "Confirm whether this API is public before choosing the rename scope."},
        )
        self.assertIn('value="resolved"', rendered)
        self.assertIn('data-stat-decision="resolved"', rendered)
        self.assertIn('<option value="resolved">Resolved</option>', rendered)
        self.assertEqual(embedded["decision_schema_version"], 2)

    def test_resolved_is_a_valid_seeded_decision(self):
        raw = {
            "findings": [
                {
                    "id": "F001",
                    "title": "The comment is already addressed",
                    "decision": "resolved",
                    "note": "The current test covers the reported case.",
                }
            ]
        }
        payload = builder.normalize_payload(raw, Path("findings.json"), None)
        self.assertEqual(payload["initial_decisions"], {"F001": "resolved"})
        self.assertEqual(
            payload["initial_notes"],
            {"F001": "The current test covers the reported case."},
        )
        rendered = builder.render_board(payload, TEMPLATE_FILE)
        self.assertIn('class="decision-option decision-resolved"', rendered)
        self.assertIn("const DECISION_VALUES = ['undecided', 'resolved', 'fix', 'discuss', 'defer', 'reject'];", rendered)

    def test_unknown_decision_is_rejected(self):
        raw = {"findings": [{"id": "F001", "title": "Claim", "decision": "done"}]}
        with self.assertRaisesRegex(ValueError, "decision must be one of"):
            builder.normalize_payload(raw, Path("findings.json"), None)

    def test_html_snapshot_export_embeds_current_review_state(self):
        template = TEMPLATE_FILE.read_text(encoding="utf-8")
        self.assertIn('id="export-html"', template)
        self.assertIn("document.getElementById('export-html').addEventListener('click', exportHtmlSnapshot);", template)
        self.assertIn("function exportHtmlSnapshot() {", template)
        self.assertIn("initial_decisions: Object.fromEntries", template)
        self.assertIn("initial_notes: Object.fromEntries", template)
        self.assertIn("initial_theme: state.theme", template)
        self.assertIn("initial_density: state.density", template)
        self.assertIn("embedded_snapshot_id: embeddedAt", template)
        self.assertIn("dataScript.textContent = jsonForEmbeddedScript(embeddedSnapshotData());", template)
        self.assertIn("branch-review-findings-snapshot-v1", template)
        self.assertIn("return `<!doctype html>\\n${root.outerHTML}\\n`;", template)

    def test_progress_filters_and_bottom_actions_keep_the_requested_layout(self):
        template = TEMPLATE_FILE.read_text(encoding="utf-8")
        progress = re.search(
            r'<section class="panel progress-panel".*?</section>',
            template,
            flags=re.DOTALL,
        )
        filters = re.search(
            r'<section class="panel filter-panel".*?</section>',
            template,
            flags=re.DOTALL,
        )
        bottom_controls = re.search(
            r'<section class="panel bottom-control-panel".*?</section>',
            template,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(progress)
        self.assertIsNotNone(filters)
        self.assertIsNotNone(bottom_controls)
        self.assertLess(progress.start(), filters.start())
        self.assertLess(filters.start(), bottom_controls.start())
        self.assertLess(bottom_controls.start(), template.index('<section class="panel table-panel"'))

        self.assertIn('id="progress-track"', progress.group(0))
        self.assertIn('class="summary-stats"', progress.group(0))
        self.assertNotIn('id="search"', progress.group(0))

        self.assertIn('id="search"', filters.group(0))
        self.assertIn('id="reset-decisions"', filters.group(0))
        self.assertNotIn('id="hide-decided"', filters.group(0))

        self.assertIn('id="next-undecided"', bottom_controls.group(0))
        self.assertIn('id="hide-decided"', bottom_controls.group(0))
        self.assertNotIn('id="reset-decisions"', bottom_controls.group(0))
        self.assertIn('class="segmented-export"', bottom_controls.group(0))
        self.assertNotIn('id="export-menu"', template)
        self.assertNotIn('<details class="export-menu"', template)
        for control_id in ("export-html", "export-json", "copy-json", "export-csv"):
            self.assertEqual(template.count(f'id="{control_id}"'), 1)
            segment = re.search(
                rf'<button class="button" id="{control_id}".*?</button>',
                bottom_controls.group(0),
                flags=re.DOTALL,
            )
            self.assertIsNotNone(segment)
            self.assertIn('class="button-icon"', segment.group(0))

        self.assertRegex(template, r"\.progress-panel \{\s+position: sticky;")
        self.assertRegex(template, r"\.filter-panel \{\s+position: relative;")
        self.assertRegex(template, r"\.bottom-control-panel \{\s+position: fixed;")
        self.assertIn("top: var(--sticky-controls-offset, 0px);", template)
        self.assertIn("progressPanel.getBoundingClientRect().height", template)
        self.assertIn("bottomControlPanel.getBoundingClientRect().height", template)
        self.assertIn("progressPanelResizeObserver.observe(progressPanel)", template)
        self.assertIn("bottomControlResizeObserver.observe(bottomControlPanel)", template)
        self.assertIn("padding-bottom: var(--bottom-controls-offset, 0px);", template)
        self.assertIsNotNone(
            re.search(
                r"\.segmented-export \.button \{.*?border-radius: 0;",
                template,
                flags=re.DOTALL,
            )
        )
        self.assertIn(".segmented-export .button:first-child", template)
        self.assertIn(".segmented-export .button:last-child", template)
        self.assertNotIn("mobile-next-undecided", template)
        self.assertNotIn("mobile-show-filters", template)

    def test_responsive_progress_layout_avoids_tablet_overflow_and_short_viewport_trapping(self):
        template = TEMPLATE_FILE.read_text(encoding="utf-8")
        self.assertIn(
            "@media (max-width: 1200px) {\n      .summary-stats { grid-template-columns: repeat(3, minmax(8rem, 1fr)); }",
            template,
        )
        self.assertIn(
            "@media (max-width: 600px) {\n      .summary-stats { grid-template-columns: repeat(2, minmax(8rem, 1fr)); }",
            template,
        )
        self.assertIn(
            "@media (max-height: 520px) {\n      .progress-panel { position: relative; top: auto; }",
            template,
        )

    def test_documentation_describes_sticky_behavior_and_short_height_fallback(self):
        readme = README_FILE.read_text(encoding="utf-8")
        skill = SKILL_FILE.read_text(encoding="utf-8")
        self.assertIn("progress panel sticks to the top", readme)
        self.assertIn("table header sticks immediately below", readme)
        self.assertIn("short-height or highly zoomed views", readme)
        self.assertIn("progress panel sticky at the top", skill)
        self.assertIn("table header sticky immediately below", skill)
        self.assertIn("short-height fallback", skill)
        self.assertNotIn("progress panel fixed at the top", readme)
        self.assertNotIn("progress panel fixed at the top", skill)

    def test_javascript_literal_element_ids_exist_in_the_template(self):
        template = TEMPLATE_FILE.read_text(encoding="utf-8")
        declared_ids = set(re.findall(r'\bid="([^"]+)"', template))
        referenced_ids = set(re.findall(r"getElementById\('([^']+)'\)", template))
        # The builder adds these when it embeds pdf.js; the page works without them.
        builder_ids = {"pdfjs-module", "pdfjs-worker"}
        self.assertEqual(referenced_ids - declared_ids - builder_ids, set())
        self.assertIn('id="{element_id}"', SCRIPT_FILE.read_text(encoding="utf-8"))

    def test_resolved_is_purple_and_fix_is_green(self):
        template = TEMPLATE_FILE.read_text(encoding="utf-8")
        self.assertIn("--good: #0ca30c;", template)
        self.assertIn("--purple: #7c3aed;", template)

        raw = {"findings": [{"id": "F001", "title": "Claim"}]}
        payload = builder.normalize_payload(raw, Path("findings.json"), None)
        rendered = builder.render_board(payload, TEMPLATE_FILE)
        self.assertIn(".stat-resolved .stat-mark { background: var(--purple); }", rendered)
        self.assertIn(".stat-fix .stat-mark { background: var(--good); }", rendered)
        self.assertIn(".decision-resolved { --row-choice: var(--purple); }", rendered)
        self.assertIn(".decision-fix { --row-choice: var(--good); }", rendered)
        self.assertIn('tbody tr[data-decision="resolved"] { --row-status: var(--purple); }', rendered)
        self.assertIn('tbody tr[data-decision="fix"] { --row-status: var(--good); }', rendered)
        self.assertIn('tbody tr[data-decision="undecided"] { --row-status: var(--neutral); }', rendered)

    def test_custom_decisions_render_across_the_board(self):
        raw = {
            "decisions": [
                {"value": "wipe", "label": "Wipe completely", "slot": "red"},
                "trim:Trim",
                "merge:Merge with…",
                "keep:Keep",
                "discuss:Discuss",
            ],
            "findings": [
                {"id": "M001", "title": "Stale entry", "decision": "wipe", "note": "Superseded."},
                {"id": "M002", "title": "Long entry"},
            ],
        }
        payload = builder.normalize_payload(raw, Path("findings.json"), None)
        self.assertEqual(payload["initial_decisions"], {"M001": "wipe"})
        self.assertEqual(
            [entry["value"] for entry in payload["decisions"]],
            ["wipe", "trim", "merge", "keep", "discuss"],
        )
        rendered = builder.render_board(payload, TEMPLATE_FILE)
        self.assertIn('class="decision-option decision-wipe"', rendered)
        self.assertIn("<span>Wipe completely</span>", rendered)
        self.assertIn('data-stat-decision="wipe"', rendered)
        self.assertIn('<option value="merge">Merge with…</option>', rendered)
        self.assertIn(
            "const DECISION_VALUES = ['undecided', 'wipe', 'trim', 'merge', 'keep', 'discuss'];",
            rendered,
        )
        self.assertIn("const DECISION_RANK = {'undecided': 0, 'wipe': 1", rendered)
        self.assertIn(".stat-wipe .stat-mark { background: var(--critical); }", rendered)
        self.assertNotIn('value="resolved"', rendered)
        self.assertNotRegex(rendered, builder.TOKEN_PATTERN)

    def test_custom_decisions_reject_reserved_duplicate_and_malformed_entries(self):
        finding = [{"id": "F001", "title": "Claim"}]
        cases = {
            "reserved": [{"value": "undecided", "label": "Undecided"}],
            "duplicate": ["keep:Keep", "keep:Keep again"],
            "bad value": ["Keep:Keep"],
            "bad slot": [{"value": "keep", "label": "Keep", "slot": "mauve"}],
            "too few": ["keep:Keep"],
        }
        for name, decisions in cases.items():
            with self.subTest(case=name):
                raw = {"decisions": decisions, "findings": finding}
                with self.assertRaises(ValueError):
                    builder.normalize_payload(raw, Path("findings.json"), None)
        with self.assertRaisesRegex(ValueError, "decision must be one of"):
            builder.normalize_payload(
                {
                    "decisions": ["keep:Keep", "wipe:Wipe"],
                    "findings": [{"id": "F001", "title": "Claim", "decision": "resolved"}],
                },
                Path("findings.json"),
                None,
            )

    def test_cli_decisions_string_overrides_the_input_field(self):
        raw = {
            "decisions": ["keep:Keep", "wipe:Wipe"],
            "findings": [{"id": "F001", "title": "Claim"}],
        }
        payload = builder.normalize_payload(
            raw, Path("findings.json"), None, "adopt:Adopt,park:Park:gray"
        )
        self.assertEqual(
            [(entry["value"], entry["label"], entry["slot"]) for entry in payload["decisions"]],
            [("adopt", "Adopt", "purple"), ("park", "Park", "gray")],
        )

    def test_script_writes_the_requested_output(self):
        with tempfile.TemporaryDirectory() as directory:
            output_file = Path(directory) / "board.html"
            raw = json.loads(EXAMPLE_FILE.read_text(encoding="utf-8"))
            payload = builder.normalize_payload(raw, EXAMPLE_FILE, "Override title")
            output_file.write_text(builder.render_board(payload, TEMPLATE_FILE), encoding="utf-8")
            self.assertTrue(output_file.is_file())
            self.assertIn("Override title", output_file.read_text(encoding="utf-8"))

    def test_token_shaped_source_text_is_not_replaced(self):
        raw = {
            "title": "Review @@FINDING_COUNT@@ literally",
            "source": "@@SOURCE_NAME@@.json",
            "findings": [
                {
                    "id": "F001",
                    "title": "Keep @@BOARD_TITLE@@ in the finding",
                    "description": "The source contains @@AGENT_OPTIONS@@ as plain text.",
                }
            ],
        }
        payload = builder.normalize_payload(raw, Path("findings.json"), None)
        rendered = builder.render_board(payload, TEMPLATE_FILE)
        self.assertIn("Review @@FINDING_COUNT@@ literally", rendered)
        self.assertIn("Keep @@BOARD_TITLE@@ in the finding", rendered)
        self.assertIn("The source contains @@AGENT_OPTIONS@@ as plain text.", rendered)
        self.assertIn("@@SOURCE_NAME@@.json", rendered)

    def test_category_symbols_are_preserved_without_collisions(self):
        raw = {
            "findings": [
                {"id": "F001", "title": "C plus plus", "category": "C++"},
                {"id": "F002", "title": "C sharp", "category": "C#"},
                {"id": "F003", "title": "API", "category": "api-naming"},
            ]
        }
        payload = builder.normalize_payload(raw, Path("findings.json"), None)
        rendered = builder.render_board(payload, TEMPLATE_FILE)
        self.assertIn('<option value="C++">C++</option>', rendered)
        self.assertIn('<option value="C#">C#</option>', rendered)
        self.assertIn('<option value="api-naming">API Naming</option>', rendered)

    def test_template_token_mismatch_is_rejected(self):
        raw = {"findings": [{"id": "F001", "title": "First"}]}
        payload = builder.normalize_payload(raw, Path("findings.json"), None)
        with tempfile.TemporaryDirectory() as directory:
            broken_template = Path(directory) / "template.html"
            template_text = TEMPLATE_FILE.read_text(encoding="utf-8")
            broken_template.write_text(
                template_text.replace("@@SOURCE_NAME@@", "source", 1),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "template token mismatch"):
                builder.render_board(payload, broken_template)

    def test_present_falsy_ids_are_rejected_instead_of_replaced(self):
        for invalid_id in (0, "", None):
            with self.subTest(invalid_id=invalid_id):
                raw = {"findings": [{"id": invalid_id, "title": "Claim"}]}
                with self.assertRaisesRegex(ValueError, "id must match"):
                    builder.normalize_payload(raw, Path("findings.json"), None)

    def test_digit_bearing_unknown_template_token_is_rejected(self):
        raw = {"findings": [{"id": "F001", "title": "First"}]}
        payload = builder.normalize_payload(raw, Path("findings.json"), None)
        with tempfile.TemporaryDirectory() as directory:
            broken_template = Path(directory) / "template.html"
            broken_template.write_text(
                TEMPLATE_FILE.read_text(encoding="utf-8") + "\n@@EXTRA_TOKEN_2@@\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "unknown: @@EXTRA_TOKEN_2@@"):
                builder.render_board(payload, broken_template)

    def test_duplicate_ids_are_rejected(self):
        raw = {
            "findings": [
                {"id": "F001", "title": "First"},
                {"id": "F001", "title": "Second"},
            ]
        }
        with self.assertRaisesRegex(ValueError, "duplicate finding id F001"):
            builder.normalize_payload(raw, Path("findings.json"), None)

    def test_csv_cells_are_quoted_with_inner_quotes_doubled(self):
        template = TEMPLATE_FILE.read_text(encoding="utf-8")
        csv_function = re.search(
            r"function csvCell\(value\) \{.*?\n    \}",
            template,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(csv_function)
        self.assertIn("""`"${String(value ?? '').replaceAll('"', '""')}"`""", csv_function.group(0))

    def test_import_undo_is_invalidated_by_manual_edits(self):
        template = TEMPLATE_FILE.read_text(encoding="utf-8")
        decision_handler = re.search(
            r"document\.addEventListener\('change'.*?\n    \}\);",
            template,
            flags=re.DOTALL,
        )
        note_handler = re.search(
            r"document\.addEventListener\('input'.*?\n    \}\);",
            template,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(decision_handler)
        self.assertIsNotNone(note_handler)
        self.assertIn("invalidateImportRollback();", decision_handler.group(0))
        self.assertIn("invalidateImportRollback();", note_handler.group(0))

    def test_import_missing_count_tracks_presence_not_validity(self):
        template = TEMPLATE_FILE.read_text(encoding="utf-8")
        self.assertIn(
            "if (FINDING_BY_ID.has(candidate.id)) presentFindingIds.add(candidate.id);",
            template,
        )
        self.assertIn(
            "missingCount: FINDINGS.filter((finding) => !presentFindingIds.has(finding.id)).length",
            template,
        )
        self.assertNotIn("missingCount: FINDINGS.length - entries.length", template)

    def test_script_end_tag_in_finding_text_stays_inside_the_json(self):
        title = "Inline </script> in index.html ends the bootstrap block early"
        raw = {"findings": [{"id": "F001", "title": title}]}
        payload = builder.normalize_payload(raw, Path("findings.json"), None)
        rendered = builder.render_board(payload, TEMPLATE_FILE)
        embedded_match = re.search(
            r'<script id="findings-data" type="application/json">(.*?)</script>',
            rendered,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(embedded_match)
        self.assertNotIn("</script>", embedded_match.group(1))
        self.assertEqual(json.loads(embedded_match.group(1))["findings"][0]["title"], title)


    def test_figures_are_embedded_beside_the_finding(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / "shots").mkdir()
            (folder / "shots" / "page.png").write_bytes(PNG_1X1)
            raw = {
                "findings": [
                    {
                        "id": "F001",
                        "title": "Gap between boxes",
                        "figures": [{"path": "shots/page.png", "caption": "Page 12 <before> & after"}],
                    }
                ]
            }
            payload = builder.normalize_payload(raw, folder / "findings.json", None)
            rendered = builder.render_board(payload, TEMPLATE_FILE)

        self.assertIn('class="figure-strip"', rendered)
        self.assertIn('<img src="data:image/png;base64,', rendered)
        self.assertIn("<figcaption>Page 12 &lt;before&gt; &amp; after</figcaption>", rendered)
        self.assertIn('alt="Page 12 &lt;before&gt; &amp; after"', rendered)
        embedded_text = embedded_data(rendered)
        embedded = json.loads(embedded_text)
        figure = embedded["findings"][0]["figures"][0]
        self.assertEqual(figure["media_type"], "image/png")
        self.assertEqual(figure["caption"], "Page 12 <before> & after")
        self.assertEqual(len(figure["sha256"]), 64)
        # The image is in the row only; the JSON that search and export use stays small.
        self.assertNotIn("figure_data", embedded)
        self.assertNotIn(base64.b64encode(PNG_1X1).decode("ascii")[:24], embedded_text)

    def test_a_changed_image_gives_a_new_dataset_signature(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            image = folder / "page.png"
            raw = {"findings": [{"id": "F001", "title": "T", "figures": ["page.png"]}]}
            image.write_bytes(PNG_1X1)
            first = builder.normalize_payload(raw, folder / "findings.json", None)["dataset_signature"]
            again = builder.normalize_payload(raw, folder / "findings.json", None)["dataset_signature"]
            image.write_bytes(PNG_1X1 + b"\0")
            changed = builder.normalize_payload(raw, folder / "findings.json", None)["dataset_signature"]
        self.assertEqual(first, again)
        self.assertNotEqual(first, changed)

    def test_svg_figures_are_embedded(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / "diagram.svg").write_text(
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<!-- Created with Inkscape -->\n'
                '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">'
                '<rect width="10" height="10"/></svg>\n',
                encoding="utf-8",
            )
            raw = {"findings": [{"id": "F001", "title": "T", "figures": ["diagram.svg"]}]}
            payload = builder.normalize_payload(raw, folder / "findings.json", None)
            rendered = builder.render_board(payload, TEMPLATE_FILE)

        self.assertIn('<img src="data:image/svg+xml;base64,', rendered)
        figure = json.loads(embedded_data(rendered))["findings"][0]["figures"][0]
        self.assertEqual(figure["media_type"], "image/svg+xml")

    def test_figures_reject_non_images_unreadable_files_and_missing_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / "notes.txt").write_text("Page 12 has a wide gap.\n", encoding="utf-8")
            for figure in ({"path": "notes.txt"}, {"path": "absent.png"}, {"caption": "no path"}, 7):
                with self.subTest(figure=figure):
                    raw = {"findings": [{"id": "F001", "title": "T", "figures": [figure]}]}
                    with self.assertRaises(ValueError):
                        builder.normalize_payload(raw, folder / "findings.json", None)

    def test_findings_without_figures_render_no_strip(self):
        raw = json.loads(EXAMPLE_FILE.read_text(encoding="utf-8"))
        payload = builder.normalize_payload(raw, EXAMPLE_FILE, None)
        rendered = builder.render_board(payload, TEMPLATE_FILE)
        self.assertNotIn('class="figure-strip"', rendered)
        self.assertNotIn('"figures"', embedded_data(rendered))
        self.assertIn('id="figure-dialog"', rendered)


    OPTION_DECISIONS = ["current:CURRENT", "a:A", "b:B", "discuss:Discuss"]

    def test_options_are_shown_and_limit_the_offered_decisions(self):
        raw = {
            "decisions": self.OPTION_DECISIONS,
            "findings": [
                {
                    "id": "F001",
                    "title": "Gap between boxes",
                    "options": [
                        {"value": "current", "text": "One gap <for> all & every box"},
                        {"value": "a", "text": "A larger gap"},
                    ],
                },
                {
                    "id": "F002",
                    "title": "Line spacing",
                    "options": [
                        {"value": "current", "text": "14.5 pt"},
                        {"value": "a", "text": "12 pt"},
                        {"value": "b", "label": "B (pravopis)", "text": "12 pt in the lists only"},
                    ],
                },
                {"id": "F003", "title": "A finding without options"},
            ],
        }
        payload = builder.normalize_payload(raw, Path("findings.json"), None)
        rendered = builder.render_board(payload, TEMPLATE_FILE)

        self.assertIn('<dl class="option-list" aria-label="Choices">', rendered)
        self.assertIn("<dt>CURRENT</dt><dd>One gap &lt;for&gt; all &amp; every box</dd>", rendered)
        self.assertIn("<dt>B (pravopis)</dt><dd>12 pt in the lists only</dd>", rendered)

        def offered(finding_id):
            row = re.search(rf'<tr class="finding-row" id="finding-{finding_id}".*?</tr>', rendered, flags=re.DOTALL)
            return re.findall(rf'name="decision-{finding_id}" value="([a-z]+)"', row.group(0))

        # F001 has no option b, so it does not offer b; Discuss is not an option anywhere.
        self.assertEqual(offered("F001"), ["undecided", "current", "a", "discuss"])
        self.assertEqual(offered("F002"), ["undecided", "current", "a", "b", "discuss"])
        # A finding without options offers every decision.
        self.assertEqual(offered("F003"), ["undecided", "current", "a", "b", "discuss"])
        embedded = json.loads(embedded_data(rendered))
        self.assertEqual(embedded["option_values"], ["a", "b", "current"])
        self.assertEqual(embedded["findings"][1]["options"][2]["label"], "B (pravopis)")

    def test_options_reject_unknown_duplicate_empty_and_unoffered_decisions(self):
        bad = [
            [{"value": "z", "text": "not a decision of the board"}],
            [{"value": "a", "text": "x"}, {"value": "a", "text": "y"}],
            [{"value": "a", "text": "  "}],
            [{"value": "undecided", "text": "x"}],
        ]
        for options in bad:
            with self.subTest(options=options):
                raw = {"decisions": self.OPTION_DECISIONS,
                       "findings": [{"id": "F001", "title": "T", "options": options}]}
                with self.assertRaises(ValueError):
                    builder.normalize_payload(raw, Path("findings.json"), None)
        raw = {
            "decisions": self.OPTION_DECISIONS,
            "findings": [
                {"id": "F001", "title": "T", "options": [{"value": "current", "text": "x"}, {"value": "a", "text": "y"}]},
                {"id": "F002", "title": "U", "options": [{"value": "current", "text": "x"}, {"value": "b", "text": "z"}]},
                {"id": "F003", "title": "V", "decision": "b",
                 "options": [{"value": "current", "text": "x"}, {"value": "a", "text": "y"}]},
            ],
        }
        with self.assertRaises(ValueError):
            builder.normalize_payload(raw, Path("findings.json"), None)

    def test_template_keeps_each_finding_to_its_own_options(self):
        template = TEMPLATE_FILE.read_text(encoding="utf-8")
        self.assertIn("const OPTION_VALUES = new Set(DATA.option_values || []);", template)
        self.assertIn("return finding && offersDecision(finding, value) ? value : 'undecided';", template)
        self.assertIn("!offersDecision(FINDING_BY_ID.get(candidate.id), candidate.decision)", template)
        self.assertIn("decision_text: chosenOptionText(finding),", template)
        self.assertIn("'decision', 'note', 'decision_text']", template)
        self.assertIn("markChosenOption(rowById.get(id), radio.value);", template)


    def write_pdf(self, folder, name="figures.pdf"):
        path = Path(folder) / name
        path.write_bytes(b"%PDF-1.7\n% test\n")
        return path

    def test_aids_are_drawn_from_numbers_and_can_be_hidden(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / "page.png").write_bytes(PNG_1X1)
            raw = {"findings": [{"id": "F001", "title": "T", "figures": [{
                "path": "page.png",
                "aids": {"width": 100, "height": 50, "rects": [[1, 2, 30, 40]], "lines": [[0, 100, 25]]},
            }]}]}
            rendered = builder.render_board(builder.normalize_payload(raw, folder / "findings.json", None), TEMPLATE_FILE)
        self.assertIn('<svg class="aid-layer" viewBox="0 0 100.0 50.0"', rendered)
        self.assertIn('<rect x="1.0" y="2.0" width="29.0" height="38.0"/>', rendered)
        self.assertIn('<line class="aid-guide" x1="0.0" y1="25.0" x2="100.0" y2="25.0"/>', rendered)
        self.assertIn('id="aids-toggle"', rendered)
        self.assertIn('body[data-aids="off"] .aid-layer { display: none; }', rendered)
        for bad in ({"width": 1, "height": 1, "rects": [["<script>", 0, 1, 1]]},
                    {"width": 1, "height": 1, "lines": [[0, 1]]}, {"width": 0, "height": 1}):
            with self.subTest(aids=bad):
                raw = {"findings": [{"id": "F001", "title": "T", "figures": [{"path": "page.png", "aids": bad}]}]}
                with tempfile.TemporaryDirectory() as directory:
                    (Path(directory) / "page.png").write_bytes(PNG_1X1)
                    with self.assertRaises(ValueError):
                        builder.normalize_payload(raw, Path(directory) / "findings.json", None)

    def test_vector_files_are_embedded_once_with_their_variants(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / "page.png").write_bytes(PNG_1X1)
            self.write_pdf(folder)
            pdfjs = folder / "pdfjs"
            pdfjs.mkdir()
            (pdfjs / "pdf.min.mjs").write_text("export const x = 1;", encoding="utf-8")
            (pdfjs / "pdf.worker.min.mjs").write_text("// worker", encoding="utf-8")
            vector = {"path": "figures.pdf", "page": 1, "plain_page": 2}
            raw = {"findings": [
                {"id": "F001", "title": "T", "figures": [{"path": "page.png", "vector": vector,
                    "variants": [{"label": "CURRENT", "vector": {"path": "figures.pdf", "page": 3}},
                                 {"label": "A", "vector": {"path": "figures.pdf", "page": 4, "plain_page": 5}}]}]},
                {"id": "F002", "title": "U", "figures": [{"path": "page.png", "vector": vector}]},
            ]}
            payload = builder.normalize_payload(raw, folder / "findings.json", None)
            rendered = builder.render_board(payload, TEMPLATE_FILE, pdfjs)
        self.assertEqual(rendered.count('<script type="application/octet-stream" id="vector-file-'), 1)
        self.assertIn('id="pdfjs-module"', rendered)
        self.assertIn('id="pdfjs-worker"', rendered)
        embedded = json.loads(embedded_data(rendered))
        figure = embedded["findings"][0]["figures"][0]
        self.assertEqual(figure["vector"], {"file": 0, "page": 1, "plain_page": 2})
        self.assertEqual([v["label"] for v in figure["variants"]], ["CURRENT", "A"])
        self.assertEqual(figure["variants"][0]["vector"], {"file": 0, "page": 3, "plain_page": 3})
        self.assertNotIn(str(folder), embedded_data(rendered))
        self.assertIn('data-finding-id="F001" data-figure-index="0"', rendered)

    def test_vector_input_is_checked(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / "page.png").write_bytes(PNG_1X1)
            (folder / "not.pdf").write_text("plain text", encoding="utf-8")
            self.write_pdf(folder)
            for vector in ({"path": "not.pdf", "page": 1}, {"path": "figures.pdf", "page": 0},
                           {"path": "figures.pdf", "page": "1"}, {"page": 1}):
                with self.subTest(vector=vector):
                    raw = {"findings": [{"id": "F001", "title": "T", "figures": [{"path": "page.png", "vector": vector}]}]}
                    with self.assertRaises(ValueError):
                        builder.normalize_payload(raw, folder / "findings.json", None)

    def test_main_script_is_not_ended_early_by_a_closing_tag_in_its_own_text(self):
        # The HTML parser ends a script element at the first "</script", even inside a JavaScript
        # comment or string. Everything after it, such as the start-up calls at the end of the
        # script, would never run. Python's parser treats script content the same way.
        raw = json.loads(EXAMPLE_FILE.read_text(encoding="utf-8"))
        payload = builder.normalize_payload(raw, EXAMPLE_FILE, None)
        rendered = builder.render_board(payload, TEMPLATE_FILE)

        class Scripts(HTMLParser):
            def __init__(self):
                super().__init__()
                self.scripts, self.current = [], None

            def handle_starttag(self, tag, attrs):
                if tag == "script":
                    self.current = [dict(attrs), ""]

            def handle_data(self, data):
                if self.current is not None:
                    self.current[1] += data

            def handle_endtag(self, tag):
                if tag == "script" and self.current is not None:
                    self.scripts.append(self.current)
                    self.current = None

        parser = Scripts()
        parser.feed(rendered)
        main = [data for attrs, data in parser.scripts if not attrs]
        self.assertEqual(len(main), 1)
        self.assertIn("loadState();", main[0])
        self.assertIn("refreshAll();", main[0])

    def test_viewer_keys_work_wherever_the_focus_is(self):
        # A click on the picture leaves the focus on the page body, outside the dialog, so a key
        # listener on the dialog would not hear the arrow keys. The listener is on the document and
        # acts only while the viewer is open.
        template = TEMPLATE_FILE.read_text(encoding="utf-8")
        self.assertNotIn("figureDialog.addEventListener('keydown'", template)
        self.assertRegex(
            template,
            r"document\.addEventListener\('keydown', \(event\) => \{\s*if \(!figureDialog\.open\) return;",
        )

    def test_template_viewer_uses_pdfjs_arrow_keys_and_the_aids_switch(self):
        template = TEMPLATE_FILE.read_text(encoding="utf-8")
        self.assertIn("ArrowLeft: () => stepViewer(-1), ArrowRight: () => stepViewer(1),", template)
        self.assertIn("library.GlobalWorkerOptions.workerSrc", template)
        self.assertIn("pdf.getPage(state.aids ? vector.page : vector.plain_page)", template)
        self.assertIn("aids: DATA.initial_aids !== false", template)
        self.assertIn("initial_aids: state.aids,", template)

    def test_two_tabs_of_a_board_keep_each_others_rulings(self):
        # Each tab saved its whole state, so a ruling made in one tab was lost when the other tab saved
        # next. A save now re-reads the stored state and writes only what this tab changed, and a tab takes
        # over another tab's saves through the browser's storage event. Runs in a headless Chrome.
        import shutil
        import subprocess
        import time
        import urllib.parse
        import urllib.request

        chrome = shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chromium-browser")
        try:
            import websocket
        except ImportError:
            websocket = None
        if not chrome or websocket is None:
            self.skipTest("needs Chrome and the websocket-client package")
        raw = json.loads(EXAMPLE_FILE.read_text(encoding="utf-8"))
        payload = builder.normalize_payload(raw, EXAMPLE_FILE, None)
        rendered = builder.render_board(payload, TEMPLATE_FILE)
        with tempfile.TemporaryDirectory() as folder:
            board = Path(folder) / "board.html"
            board.write_text(rendered, encoding="utf-8")
            profile = Path(folder) / "profile"
            process = subprocess.Popen(
                [chrome, "--headless=new", "--remote-debugging-port=0", f"--user-data-dir={profile}",
                 "--no-first-run", "--no-default-browser-check", "about:blank"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            try:
                port_file = profile / "DevToolsActivePort"
                for _ in range(200):
                    if port_file.exists() and port_file.read_text().strip():
                        break
                    time.sleep(0.05)
                base = f"http://127.0.0.1:{port_file.read_text().split()[0]}"

                class Tab:
                    def __init__(self):
                        request = urllib.request.Request(f"{base}/json/new?{urllib.parse.quote(board.as_uri(), safe=':/')}",
                                                         method="PUT")
                        info = json.loads(urllib.request.urlopen(request).read())
                        self.ws = websocket.create_connection(info["webSocketDebuggerUrl"], timeout=60, suppress_origin=True)
                        self.next = 0

                    def ev(self, expression):
                        self.next += 1
                        self.ws.send(json.dumps({"id": self.next, "method": "Runtime.evaluate",
                                                 "params": {"expression": expression, "returnByValue": True}}))
                        while True:
                            message = json.loads(self.ws.recv())
                            if message.get("id") == self.next:
                                return message.get("result", {}).get("result", {}).get("value")

                    def wait(self, expression):
                        for _ in range(200):
                            if self.ev(expression):
                                return
                            time.sleep(0.05)
                        raise AssertionError(f"timed out: {expression}")

                    def rule(self, finding_id, value):
                        self.ev(f"document.querySelector('input[type=\"radio\"][data-finding-id={json.dumps(finding_id)}]"
                                f"[value={json.dumps(value)}]').click()")
                        # a hidden tab runs the 180 ms save timer up to a second late
                        self.wait("saveTimer === null")

                first, second = Tab(), Tab()
                for tab in (first, second):
                    tab.wait("document.readyState === 'complete' && typeof saveTimer !== 'undefined'"
                             " && document.querySelectorAll('.finding-row').length > 0")
                choices = second.ev("""JSON.stringify([...document.querySelectorAll('.finding-row')].slice(0, 3).map((row) =>
                    [row.dataset.findingId, [...row.querySelectorAll('input[type="radio"]')].map((input) => input.value)
                      .find((value) => value !== 'undecided' && value !== row.dataset.decision)]))""")
                (a, va), (b, vb), (c, vc) = json.loads(choices)
                first.rule(a, va)
                second.rule(b, vb)
                first.rule(c, vc)
                stored = json.loads(first.ev("localStorage.getItem(STORAGE_KEY)"))["decisions"]
                self.assertEqual((stored.get(a), stored.get(b), stored.get(c)), (va, vb, vc))
                second.wait(f"state.decisions[{json.dumps(c)}] === {json.dumps(vc)}")
                first.ws.close()
                second.ws.close()
            finally:
                import os
                import signal
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=20)


if __name__ == "__main__":
    unittest.main()
