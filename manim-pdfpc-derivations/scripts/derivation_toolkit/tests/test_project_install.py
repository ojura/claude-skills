"""Check offline installation ownership and explicit project/document contracts."""
from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import fitz

from ..catalog import Catalog
from ..export import inspect_interpreter, metadata_tex, palette_tex
from .. import install as installer
from ..package_assets import collect_assets
from ..web.build import page_mapping
from .test_compiled_inputs import timeline


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
        with fitz.open() as document:
            document.new_page(width=200, height=300)
            document.save(pdf)
        rows, files = collect_assets(Catalog(self.project), self.root / "absent",
                                    inputs={"sample": {"light": {"pdf": str(pdf)}}})
        self.assertEqual(len(files), 1)
        self.assertEqual(rows[0].split("\t")[5], "light")
        self.assertEqual(files[0][0], pdf)

    def test_package_input_paths_are_relative_to_the_project(self):
        documents = self.project / "documents"
        documents.mkdir()
        pdf = documents / "report.pdf"
        vector = documents / "report.vector.json"
        with fitz.open() as document:
            document.new_page(width=200, height=300)
            document.save(pdf)
        vector.write_text(json.dumps(timeline()))
        original_directory = Path.cwd()
        try:
            os.chdir(self.root)
            _, files = collect_assets(Catalog(self.project), self.root / "absent", inputs={
                "sample": {"light": {"pdf": "documents/report.pdf",
                                     "vectors": "documents/report.vector.json"}}})
        finally:
            os.chdir(original_directory)
        self.assertEqual([source for source, _ in files], [pdf, vector])

    def test_palette_export_preserves_authored_colour_names(self):
        self.catalog["palette"] = {"file": "palette.tex", "prefix": "sample"}
        self.write_catalog()
        names = [f"sample{theme}{role}" for theme in ("Light", "Dark")
                 for role in ("Background", "Foreground", "Accent", "Muted", "Guide", "Faint",
                              "AxisX", "AxisY", "AxisZ")]
        definitions = [rf"\definecolor{{{name}}}{{HTML}}{{a1b2c3}}" for name in names]
        definitions.append(r"\definecolor{unrelatedColour}{HTML}{FFFFFF}")
        (self.project / "palette.tex").write_text("\n".join(definitions) + "\n")
        exported = palette_tex(self.project)
        for name in names:
            self.assertIn(rf"\definecolor{{{name}}}{{HTML}}{{A1B2C3}}", exported)
        self.assertNotIn("Axisx", exported)
        self.assertNotIn("unrelatedColour", exported)

    def test_page_map_rejects_missing_and_disordered_logical_pages(self):
        for pages in ([{"number": 2, "logical": 1}],
                      [{"number": 1, "logical": 2}],
                      [{"number": 1, "logical": 1}, {"number": 2, "logical": 2},
                       {"number": 3, "logical": 1}]):
            with self.subTest(pages=pages), self.assertRaises(ValueError):
                page_mapping(Path("unused.pdf"), {"version": 1, "pages": pages}, len(pages))
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
        self.package = package
        package.mkdir(parents=True)
        for name in ("__init__.py", "cli.py"):
            (package / name).write_text("# committed package\n")
        (package / "install.py").write_bytes(Path(installer.__file__).read_bytes())
        self.entry_template = Path(installer.__file__).with_name("entrypoint.py.in").read_bytes()
        (package / "entrypoint.py.in").write_bytes(self.entry_template)
        (package / "ci").mkdir()
        (package / installer.WORKFLOW_SOURCE).write_text("name: Canonical workflow\non: workflow_call\n")
        self.revision = self.commit_source()

    def commit_source(self):
        subprocess.run(["git", "-C", str(self.source), "add", "."], check=True)
        tree = subprocess.check_output(["git", "-C", str(self.source), "write-tree"], text=True).strip()
        environment = dict(os.environ, GIT_AUTHOR_NAME="Fixture", GIT_AUTHOR_EMAIL="fixture@example.invalid",
                           GIT_COMMITTER_NAME="Fixture", GIT_COMMITTER_EMAIL="fixture@example.invalid")
        return subprocess.check_output(["git", "-C", str(self.source), "commit-tree",
                                        tree, "-m", "fixture"], text=True, env=environment).strip()

    def updater(self, script, project, revision=None, *, check=False, ci=False):
        command = [sys.executable, "-B", str(script), "--project", str(project),
                   "--source", str(self.source)]
        command.extend(["--check"] if check else ["--revision", revision or self.revision])
        if ci:
            command.append("--ci")
        return subprocess.run(command, capture_output=True, text=True,
                              env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})

    def assert_success(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def changed_entry(self, name):
        return self.entry_template.replace(b"update through its installer.",
                                           ("selected entry " + name + ".").encode())

    def literal_source(self, entry):
        """Construct the literal representation used by pre-template revisions."""
        text = Path(installer.__file__).read_text()
        lines = text.splitlines(keepends=True)
        for node in reversed(ast.parse(text).body):
            if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and
                                                    target.id == "BOOTSTRAP" for target in node.targets):
                del lines[node.lineno - 1:node.end_lineno]
        text = "".join(lines)
        if entry is not None:
            text = text.replace('if __name__ == "__main__":',
                                "BOOTSTRAP = " + repr(entry.decode()) + '\n\nif __name__ == "__main__":')
        return text

    def snapshot(self):
        return {path.relative_to(self.project).as_posix(): (path.read_bytes(), path.stat().st_mode & 0o777)
                for path in self.project.rglob("*") if path.is_file()}

    def test_installed_old_updater_selects_new_committed_entry(self):
        first = self.updater(self.package / "install.py", self.project, ci=True)
        self.assert_success(first)
        old_updater = self.project / installer.VENDOR / "install.py"
        updated_entry = self.changed_entry("two")
        (self.package / "entrypoint.py.in").write_bytes(updated_entry)
        (self.package / "cli.py").write_text("# second committed package\n")
        newer = self.commit_source()
        upgrade = self.updater(old_updater, self.project, newer)
        self.assert_success(upgrade)
        fresh = self.root / "fresh"
        fresh.mkdir()
        self.assert_success(self.updater(self.package / "install.py", fresh, newer, ci=True))
        self.assert_success(self.updater(old_updater, self.project, check=True))
        self.assert_success(self.updater(fresh / installer.VENDOR / "install.py", fresh, check=True))
        self.assertEqual((self.project / installer.ENTRY).read_bytes(), updated_entry)
        self.assertEqual((fresh / installer.ENTRY).read_bytes(), updated_entry)
        self.assertEqual((self.project / installer.LOCK).read_bytes(), (fresh / installer.LOCK).read_bytes())
        self.assertEqual(installer._inventory(self.project / installer.VENDOR),
                         installer._inventory(fresh / installer.VENDOR))

    def test_dirty_running_updater_cannot_replace_selected_entry(self):
        caller = self.root / "dirty-updater.py"
        caller.write_text(self.literal_source(self.changed_entry("dirty caller")))
        (self.root / "entrypoint.py.in").write_bytes(self.changed_entry("dirty adjacent file"))
        (self.package / "entrypoint.py.in").write_bytes(self.changed_entry("uncommitted source"))
        self.assert_success(self.updater(caller, self.project))
        fresh = self.root / "fresh"
        fresh.mkdir()
        self.assert_success(self.updater(self.package / "install.py", fresh))
        self.assertEqual((self.project / installer.ENTRY).read_bytes(), self.entry_template)
        self.assertEqual((fresh / installer.ENTRY).read_bytes(), self.entry_template)
        self.assertEqual((self.project / installer.LOCK).read_bytes(), (fresh / installer.LOCK).read_bytes())

    def test_newer_running_updater_can_select_older_entry_revision(self):
        (self.package / "entrypoint.py.in").write_bytes(self.changed_entry("newer"))
        (self.package / "install.py").write_text(self.literal_source(self.changed_entry("newer")))
        self.commit_source()
        self.assert_success(self.updater(self.package / "install.py", self.project, self.revision))
        self.assertEqual((self.project / installer.ENTRY).read_bytes(), self.entry_template)

    def test_canonical_check_rejects_a_self_consistent_wrong_entry_hash(self):
        installer.install(self.project, self.source, self.revision)
        wrong_entry = self.changed_entry("unrelated")
        (self.project / installer.ENTRY).write_bytes(wrong_entry)
        lock_file = self.project / installer.LOCK
        lock = json.loads(lock_file.read_text())
        lock["entry_sha256"] = installer.digest(wrong_entry)
        lock_file.write_text(json.dumps(lock))
        result = self.updater(self.package / "install.py", self.project, check=True)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("entry", result.stderr)

    def test_selected_legacy_literal_is_read_without_executing_source(self):
        legacy_entry = self.changed_entry("legacy")
        (self.package / "entrypoint.py.in").unlink()
        text = self.literal_source(legacy_entry)
        sentinel = self.root / "source-executed"
        text = text.replace('if __name__ == "__main__":',
                            f'Path({str(sentinel)!r}).write_text("executed")\n\nif __name__ == "__main__":')
        (self.package / "install.py").write_text(text)
        revision = self.commit_source()
        installer.install(self.project, self.source, revision)
        self.assertEqual((self.project / installer.ENTRY).read_bytes(), legacy_entry)
        installer.verify_installation(self.project)
        self.assertEqual(installer.main(["--project", str(self.project), "--source", str(self.source), "--check"]), 0)
        self.assertFalse(sentinel.exists(), "Selecting the template must not execute committed source")

    def test_missing_or_empty_selected_template_preserves_old_installation(self):
        installer.install(self.project, self.source, self.revision, ci=True)
        before = self.snapshot()
        for missing in (False, True):
            with self.subTest(missing=missing):
                (self.package / "install.py").write_text(self.literal_source(None))
                template = self.package / "entrypoint.py.in"
                if missing:
                    template.unlink()
                else:
                    template.write_bytes(b"")
                revision = self.commit_source()
                with self.assertRaisesRegex(ValueError, "entry.*template"):
                    installer.install(self.project, self.source, revision)
                self.assertEqual(self.snapshot(), before)

    def test_local_entry_edit_is_preserved_before_a_template_upgrade(self):
        installer.install(self.project, self.source, self.revision)
        entry = self.project / installer.ENTRY
        entry.write_bytes(self.changed_entry("consumer local work"))
        before = self.snapshot()
        (self.package / "entrypoint.py.in").write_bytes(self.changed_entry("next"))
        revision = self.commit_source()
        with self.assertRaisesRegex(ValueError, "entry point has local edits"):
            installer.install(self.project, self.source, revision)
        self.assertEqual(self.snapshot(), before)

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

    def test_existing_lock_symlink_preserves_its_target_and_installed_files(self):
        installer.install(self.project, self.source, self.revision)
        lock_path = self.project / installer.LOCK
        external = self.root / "external-lock.json"
        lock_path.rename(external)
        lock_path.symlink_to(external)
        before = external.read_bytes()
        entry_before = (self.project / installer.ENTRY).read_bytes()
        inventory_before = installer._inventory(self.project / installer.VENDOR)
        for operation in (lambda: installer.verify_installation(self.project),
                          lambda: installer.install(self.project, self.source, self.revision)):
            with self.assertRaisesRegex(ValueError, "ordinary toolkit lock"):
                operation()
        self.assertTrue(lock_path.is_symlink())
        self.assertEqual(external.read_bytes(), before)
        self.assertEqual((self.project / installer.ENTRY).read_bytes(), entry_before)
        self.assertEqual(installer._inventory(self.project / installer.VENDOR), inventory_before)

    def test_dangling_lock_symlink_is_rejected_before_installation(self):
        lock_path = self.project / installer.LOCK
        lock_path.parent.mkdir()
        external = self.root / "uncreated-lock.json"
        lock_path.symlink_to(external)
        for operation in (lambda: installer.verify_installation(self.project),
                          lambda: installer.install(self.project, self.source, self.revision)):
            with self.assertRaisesRegex(ValueError, "ordinary toolkit lock"):
                operation()
        self.assertTrue(lock_path.is_symlink())
        self.assertFalse(external.exists())
        self.assertFalse((self.project / installer.ENTRY).exists())
        self.assertFalse((self.project / installer.VENDOR).exists())

    def test_failed_update_restores_code_entry_and_lock(self):
        installer.install(self.project, self.source, self.revision, ci=True)
        before = self.snapshot()
        (self.package / "entrypoint.py.in").write_bytes(self.changed_entry("replacement"))
        (self.package / "cli.py").write_text("# replacement code\n")
        (self.package / installer.WORKFLOW_SOURCE).write_text("name: Replacement workflow\non: workflow_call\n")
        replacement = self.commit_source()
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
                installer.install(self.project, self.source, replacement)
        real_verify(self.project)
        self.assertEqual(self.snapshot(), before)

    def replacement_revision(self):
        (self.package / "entrypoint.py.in").write_bytes(self.changed_entry("replacement"))
        (self.package / "cli.py").write_text("# replacement code\n")
        (self.package / installer.WORKFLOW_SOURCE).write_text("name: Replacement workflow\non: workflow_call\n")
        return self.commit_source()

    def test_interrupting_the_backup_move_preserves_the_previous_installation(self):
        installer.install(self.project, self.source, self.revision, ci=True)
        before = self.snapshot()
        replacement = self.replacement_revision()
        target = self.project / installer.VENDOR
        real_replace = os.replace
        for after_move in (False, True):
            with self.subTest(after_move=after_move):
                def interrupted(source, destination):
                    if Path(source) == target and Path(destination).name == "previous":
                        if after_move:
                            real_replace(source, destination)
                        raise KeyboardInterrupt("interrupted backup move")
                    return real_replace(source, destination)
                with patch.object(installer.os, "replace", side_effect=interrupted):
                    with self.assertRaisesRegex(KeyboardInterrupt, "interrupted backup"):
                        installer.install(self.project, self.source, replacement)
                installer.verify_installation(self.project)
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(list(target.parent.glob(".toolkit-install-*")), [])

    def test_failed_restoration_retains_package_and_matching_metadata(self):
        original_project = self.project
        real_replace, real_write = os.replace, Path.write_bytes
        real_verify = installer.verify_installation
        for failed_part in ("package", "entry", "lock", "workflow"):
            with self.subTest(failed_part=failed_part):
                self.project = original_project / failed_part
                self.project.mkdir()
                unrelated = self.project / "notes.txt"
                unrelated.write_text("consumer work\n")
                installer.install(self.project, self.source, self.revision, ci=True)
                before = self.snapshot()
                old_inventory = installer._inventory(self.project / installer.VENDOR)
                replacement = self.replacement_revision()
                target = self.project / installer.VENDOR
                metadata = {"entry": self.project / installer.ENTRY,
                            "lock": self.project / installer.LOCK,
                            "workflow": self.project / installer.WORKFLOW}
                checks = 0
                def fail_publication(project):
                    nonlocal checks
                    checks += 1
                    if checks == 2:
                        raise RuntimeError("publication failure")
                    return real_verify(project)
                def fail_package_restore(source, destination):
                    if (failed_part == "package" and Path(source).name in ("previous", "restoring") and
                            Path(destination) == target):
                        raise OSError("package restoration denied")
                    return real_replace(source, destination)
                def fail_metadata_restore(path, contents):
                    if failed_part in metadata and path == metadata[failed_part]:
                        previous = before[path.relative_to(self.project).as_posix()][0]
                        if contents == previous:
                            raise OSError(f"{failed_part} restoration denied")
                    return real_write(path, contents)
                with patch.object(installer, "verify_installation", side_effect=fail_publication), \
                     patch.object(installer.os, "replace", side_effect=fail_package_restore), \
                     patch.object(Path, "write_bytes", new=fail_metadata_restore):
                    with self.assertRaises((RuntimeError, OSError)) as raised:
                        installer.install(self.project, self.source, replacement)
                recovery = list(target.parent.glob(".toolkit-install-*"))
                self.assertEqual(len(recovery), 1, "Failed restoration must not erase the old installation")
                recovery = recovery[0]
                self.assertIn(str(recovery), str(raised.exception))
                self.assertEqual(installer._inventory(recovery / "previous"), old_inventory)
                for path in metadata.values():
                    relative = path.relative_to(self.project)
                    saved = recovery / "metadata" / relative
                    self.assertEqual((saved.read_bytes(), saved.stat().st_mode & 0o777), before[relative.as_posix()])
                self.assertEqual(unrelated.read_text(), "consumer work\n")
                # The retained files are sufficient to recover the exact old
                # installation without its source checkout or the failed updater.
                if target.exists():
                    shutil.rmtree(target)
                shutil.copytree(recovery / "previous", target)
                for path in metadata.values():
                    shutil.copy2(recovery / "metadata" / path.relative_to(self.project), path)
                shutil.rmtree(recovery)
                real_verify(self.project)
                self.assertEqual(self.snapshot(), before)
        self.project = original_project

    def test_failed_first_install_removes_only_its_published_files(self):
        (self.project / "notes.txt").write_text("unrelated content\n")
        before = self.snapshot()
        with patch.object(installer, "verify_installation", side_effect=RuntimeError("publication failure")):
            with self.assertRaisesRegex(RuntimeError, "publication failure"):
                installer.install(self.project, self.source, self.revision, ci=True)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.project / installer.VENDOR).exists())
        self.assertEqual(list((self.project / installer.VENDOR).parent.glob(".toolkit-install-*")), [])

    def test_optional_ci_is_exact_owned_source_and_survives_an_ordinary_update(self):
        installer.install(self.project, self.source, self.revision)
        self.assertFalse((self.project / installer.WORKFLOW).exists())
        lock = installer.install(self.project, self.source, self.revision, ci=True)
        workflow = self.project / installer.WORKFLOW
        self.assertEqual(workflow.read_bytes(),
                         (self.project / installer.VENDOR / installer.WORKFLOW_SOURCE).read_bytes())
        self.assertEqual(installer.install(self.project, self.source, self.revision), lock)
        workflow.write_text("name: Consumer edit\n")
        with self.assertRaisesRegex(ValueError, "workflow has local edits"):
            installer.install(self.project, self.source, self.revision, ci=True)
        self.assertEqual(workflow.read_text(), "name: Consumer edit\n")

    def test_optional_ci_preserves_an_unowned_file_or_dangling_symlink(self):
        workflow = self.project / installer.WORKFLOW
        workflow.parent.mkdir(parents=True)
        for symlink in (False, True):
            with self.subTest(symlink=symlink):
                outside = self.root / "uncreated-workflow.yml"
                if symlink:
                    workflow.symlink_to(outside)
                else:
                    workflow.write_text("name: Unrelated workflow\n")
                with self.assertRaisesRegex(ValueError, "unowned CI workflow"):
                    installer.install(self.project, self.source, self.revision, ci=True)
                self.assertFalse((self.project / installer.VENDOR).exists())
                self.assertFalse(outside.exists())
                self.assertEqual(workflow.is_symlink(), symlink)
                if not symlink:
                    self.assertEqual(workflow.read_text(), "name: Unrelated workflow\n")
                workflow.unlink()

    def test_optional_ci_rejects_a_symlinked_workflow_directory(self):
        outside = self.root / "outside-workflows"
        outside.mkdir()
        (self.project / ".github").mkdir()
        (self.project / ".github/workflows").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "ordinary directory"):
            installer.install(self.project, self.source, self.revision, ci=True)
        self.assertEqual(list(outside.iterdir()), [])
        self.assertFalse((self.project / installer.VENDOR).exists())

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
