#!/usr/bin/env python3

import importlib.util
import json
import re
import tempfile
import unittest
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
        self.assertEqual(referenced_ids - declared_ids, set())

    def test_resolved_is_purple_and_fix_is_green(self):
        template = TEMPLATE_FILE.read_text(encoding="utf-8")
        self.assertIn("--good: #0ca30c;", template)
        self.assertIn("--resolved: #7c3aed;", template)
        self.assertIn(".stat-resolved .stat-mark { background: var(--resolved); }", template)
        self.assertIn(".stat-fix .stat-mark { background: var(--good); }", template)
        self.assertIn(".decision-resolved { --row-choice: var(--resolved); }", template)
        self.assertIn(".decision-fix { --row-choice: var(--good); }", template)
        self.assertIn('tbody tr[data-decision="resolved"] { --row-status: var(--resolved); }', template)
        self.assertIn('tbody tr[data-decision="fix"] { --row-status: var(--good); }', template)

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

    def test_csv_cells_neutralize_spreadsheet_formula_markers(self):
        template = TEMPLATE_FILE.read_text(encoding="utf-8")
        csv_function = re.search(
            r"function csvCell\(value\) \{.*?\n    \}",
            template,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(csv_function)
        self.assertIn("/^[=+\\-@\\t\\r]/.test(text)", csv_function.group(0))
        self.assertIn("? `'${text}` : text", csv_function.group(0))
        self.assertIn("safeText.replaceAll", csv_function.group(0))

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

    def test_script_breakout_text_is_escaped(self):
        raw = {
            "findings": [
                {
                    "id": "F001",
                    "title": "Close </script><script>alert(1)</script>",
                }
            ]
        }
        payload = builder.normalize_payload(raw, Path("findings.json"), None)
        rendered = builder.render_board(payload, TEMPLATE_FILE)
        embedded_match = re.search(
            r'<script id="findings-data" type="application/json">(.*?)</script>',
            rendered,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(embedded_match)
        self.assertNotIn("</script>", embedded_match.group(1))
        self.assertEqual(
            json.loads(embedded_match.group(1))["findings"][0]["title"],
            "Close </script><script>alert(1)</script>",
        )


if __name__ == "__main__":
    unittest.main()
