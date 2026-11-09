"""Check offline installation ownership and explicit project/document contracts."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from ..catalog import Catalog
from ..export import inspect_interpreter, metadata_tex
from .. import install as installer
from ..package_assets import collect_assets
from ..web.build import page_mapping


class ProjectContracts(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="derivation-project-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = self.root / "consumer"
        self.project.mkdir()
        self.catalog = {"version": 2, "default": "sample", "project": {"title": "Independent document"},
                        "decks": [{"id": "sample", "title": "Example", "source": "sample.tex",
                                   "language": "de-AT", "metadata": {"subject": "arithmetic"}}]}
        self.write_catalog()

    def write_catalog(self):
        (self.project / "presentations.json").write_text(json.dumps(self.catalog))

    def test_arbitrary_language_and_metadata_are_project_owned(self):
        catalog = Catalog(self.project)
        self.assertEqual(catalog.get().language, "de-AT")
        self.assertEqual(catalog.get().metadata, {"subject": "arithmetic"})
        self.assertEqual(catalog.title, "Independent document")

    def test_external_light_only_article_keeps_one_actual_pdf(self):
        self.catalog["decks"][0].update(source="report.pdf", build={"kind": "external"})
        self.write_catalog()
        pdf = self.root / "actual-portrait.pdf"
        pdf.write_bytes(b"owned input")
        rows, files = collect_assets(Catalog(self.project), self.root / "absent",
                                    inputs={"sample": {"light": {"pdf": str(pdf)}}})
        self.assertEqual(len(files), 1)
        self.assertEqual(rows[0].split("\t")[5], "light")
        self.assertEqual(files[0][0], pdf)

    def test_page_map_rejects_missing_and_disordered_logical_pages(self):
        for pages in ([{"number": 2, "logical": 1}],
                      [{"number": 1, "logical": 2}]):
            with self.assertRaises(ValueError):
                page_mapping(Path("unused.pdf"), {"version": 1, "pages": pages}, 1)
        pages, count = page_mapping(Path("unused.pdf"), {"version": 1, "pages": [
            {"number": 1, "logical": 1}, {"number": 2, "logical": 1},
            {"number": 3, "logical": 2}]}, 3)
        self.assertEqual((len(pages), count), (3, 2))

    def test_odd_handout_layout_uses_its_authored_columns_and_gap(self):
        steps = [{"index": i, "figure": f"panel-{i}.pdf", "full_figure": f"full-{i}.pdf",
                  "caption": f"Stage {i}"} for i in range(1, 6)]
        metadata = {"scenes": [{"id": "sample", "poster": "poster.pdf", "steps": steps,
                                "handout_layout": {"columns": 3, "max_height_fraction": .4,
                                                   "row_gap_mm": 2}}]}
        tex = metadata_tex(metadata)
        self.assertIn(r"DerivationColumns@sample\endcsname{3}", tex)
        self.assertIn(r"DerivationRows@sample\endcsname{2}", tex)
        self.assertIn(r"DerivationRowGap@sample\endcsname{2}", tex)
        self.assertEqual(tex.count(r"\hspace{\DerivationPanelGap}"), 3)

    def test_selected_interpreter_is_checked_even_if_a_ready_marker_exists(self):
        folder = self.root / "environment"
        folder.mkdir()
        (folder / ".ready").write_text("obsolete readiness marker")
        missing = folder / "python"
        self.assertIsNone(inspect_interpreter(missing, {}))
        self.assertIsNotNone(inspect_interpreter(Path(sys.executable), {}))
        missing.write_text("#!/bin/sh\nexit 0\n")
        missing.chmod(0o755)
        self.assertIsNone(inspect_interpreter(missing, {}))
        self.assertIsNone(inspect_interpreter(Path(sys.executable), {"package-that-is-absent": "1"}))


class OfflineInstaller(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="derivation-install-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.project = self.root / "project"
        self.source.mkdir()
        self.project.mkdir()
        subprocess.run(["git", "init", "--quiet", str(self.source)], check=True)
        package = self.source / installer.SOURCE_PREFIX
        package.mkdir(parents=True)
        for name in ("__init__.py", "cli.py", "install.py"):
            (package / name).write_text("# committed package\n")
        subprocess.run(["git", "-C", str(self.source), "add", "."], check=True)
        tree = subprocess.check_output(["git", "-C", str(self.source), "write-tree"], text=True).strip()
        environment = dict(os.environ, GIT_AUTHOR_NAME="Fixture", GIT_AUTHOR_EMAIL="fixture@example.invalid",
                           GIT_COMMITTER_NAME="Fixture", GIT_COMMITTER_EMAIL="fixture@example.invalid")
        self.revision = subprocess.check_output(["git", "-C", str(self.source), "commit-tree",
                                                tree, "-m", "fixture"], text=True, env=environment).strip()

    def test_dirty_canonical_checkout_does_not_change_committed_installation(self):
        (self.source / installer.SOURCE_PREFIX / "cli.py").write_text("# unrelated local edit\n")
        lock = installer.install(self.project, self.source, self.revision)
        self.assertEqual(lock["revision"], self.revision)
        self.assertEqual((self.project / installer.VENDOR / "cli.py").read_text(), "# committed package\n")
        self.assertEqual(installer.verify_installation(self.project), lock)

    def test_local_vendor_edit_is_preserved_and_blocks_update(self):
        installer.install(self.project, self.source, self.revision)
        edited = self.project / installer.VENDOR / "cli.py"
        edited.write_text("# consumer local work\n")
        with self.assertRaisesRegex(ValueError, "local edits"):
            installer.install(self.project, self.source, self.revision)
        self.assertEqual(edited.read_text(), "# consumer local work\n")

    def test_failed_update_restores_code_entry_and_lock(self):
        installer.install(self.project, self.source, self.revision)
        before = {name: (self.project / name).read_bytes() for name in (installer.ENTRY, installer.LOCK)}
        real_verify = installer.verify_installation
        calls = 0
        def verify(project):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("publication failure")
            return real_verify(project)
        with patch.object(installer, "verify_installation", side_effect=verify):
            with self.assertRaisesRegex(RuntimeError, "publication failure"):
                installer.install(self.project, self.source, self.revision)
        real_verify(self.project)
        self.assertEqual(before, {name: (self.project / name).read_bytes() for name in before})

    def test_unowned_destination_and_extra_file_are_preserved(self):
        entry = self.project / installer.ENTRY
        entry.parent.mkdir()
        entry.write_text("# existing entry\n")
        with self.assertRaisesRegex(ValueError, "unowned"):
            installer.install(self.project, self.source, self.revision)
        self.assertEqual(entry.read_text(), "# existing entry\n")
        entry.unlink()
        installer.install(self.project, self.source, self.revision)
        extra = self.project / installer.VENDOR / "notes.txt"
        extra.write_text("retain")
        with self.assertRaisesRegex(ValueError, "extra"):
            installer.verify_installation(self.project)
        self.assertEqual(extra.read_text(), "retain")
