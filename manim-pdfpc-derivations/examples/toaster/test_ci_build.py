"""The report CI starts from committed sources, not an existing build directory."""
from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import unittest

import ci_build


class CleanReportInputs(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="toaster CI ")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_prepare_fetches_the_requested_commit_not_a_newer_working_tree(self):
        repository = self.root / "report source"
        report = repository / "markdown-latex-report/test/REPORT.md"
        report.parent.mkdir(parents=True)
        report.write_text("Committed report text.\n")
        subprocess.run(["git", "init", "--quiet", str(repository)], check=True)
        subprocess.run(["git", "-C", str(repository), "add", "."], check=True)
        subprocess.run(["git", "-C", str(repository), "-c", "user.name=Fixture",
                        "-c", "user.email=fixture@local", "commit", "--quiet", "-m", "report"], check=True)
        revision = ci_build.prepare_report.git(repository, "rev-parse", "HEAD").decode().strip()
        report.write_text("Uncommitted changes must not enter the workflow.\n")
        cache = self.root / "cache"
        fetched = ci_build.prepare(cache, str(repository), revision)
        self.assertEqual((fetched / "markdown-latex-report/test/REPORT.md").read_text(),
                         "Committed report text.\n")
        with self.assertRaises(FileExistsError):
            ci_build.prepare(cache, str(repository), revision)
        self.assertEqual(ci_build.prepare_report.git(fetched, "rev-parse", "HEAD").decode().strip(), revision)

    def test_prepare_requires_a_full_revision_before_creating_output(self):
        for revision in ("main", "1234567", "g" * 40):
            with self.subTest(revision=revision):
                cache = self.root / revision
                with self.assertRaisesRegex(ValueError, "full lowercase Git commit"):
                    ci_build.prepare(cache, "unused", revision)
                self.assertFalse(cache.exists())

    def test_staging_copies_sources_but_not_old_build_outputs_or_git_state(self):
        source = self.root / "consumer"
        for name in ("presentations.json", "tools/derivations.py", "scene.py", "trials.json",
                     "animations/generated/steps.json", "build/old.pdf", ".git/HEAD"):
            path = source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(name + "\n")
        staged = ci_build.stage_project(source, self.root / "cache/toaster-project")
        self.assertEqual((staged / "animations/generated/steps.json").read_bytes(),
                         (source / "animations/generated/steps.json").read_bytes())
        self.assertFalse((staged / "build").exists())
        self.assertFalse((staged / ".git").exists())
        self.assertTrue((source / "build/old.pdf").is_file())
        with self.assertRaises(FileExistsError):
            ci_build.stage_project(source, staged)
        with self.assertRaisesRegex(ValueError, "outside the source checkout"):
            ci_build.stage_project(source, source / "build/staged")
        dangling = self.root / "dangling"
        target = self.root / "not-created"
        dangling.symlink_to(target, target_is_directory=True)
        with self.assertRaises(FileExistsError):
            ci_build.stage_project(source, dangling)
        self.assertTrue(dangling.is_symlink())
        self.assertFalse(target.exists())

    def test_build_requires_prepared_report_before_copying_the_consumer(self):
        cache = self.root / "cache"
        with self.assertRaisesRegex(FileNotFoundError, "host prepare command"):
            ci_build.build(self.root / "consumer", cache, self.root / "assets")
        self.assertFalse((cache / "toaster-project").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
