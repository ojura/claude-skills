"""Check source isolation and the opt-in new-cache contract for constrained hosts."""
from __future__ import annotations

from contextlib import ExitStack
import fcntl
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from .. import build
from ..catalog import Catalog
from ..paths import get_paths


class PrivateBuild(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="private-tex-build-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "project with spaces"
        (self.root / "slides").mkdir(parents=True)
        (self.root / "shared").mkdir()
        (self.root / "shared/common.tex").write_text(r"\newcommand{\ExampleWord}{Copied input}")
        (self.root / "slides/example.tex").write_text(
            r"\documentclass{beamer}\input{../shared/common.tex}\begin{document}"
            r"\begin{frame}{Private build}\ExampleWord\end{frame}\end{document}" + "\n")
        (self.root / "presentations.json").write_text(json.dumps({
            "version": 2, "default": "example", "tex": {"packages": ["beamer.cls"]},
            "decks": [{"id": "example", "source": "slides/example.tex", "title": "Private example"}],
        }))
        self.deck = Catalog(self.root).get()
        self.paths = get_paths(self.deck, self.root / "build/new-cache", self.root / "build/documents")

    def test_copy_workspace_preserves_relative_inputs_and_failure_outputs_without_links_or_xattrs(self):
        (self.root / "shared/build").mkdir()
        (self.root / "shared/build/omit.txt").write_text("build products")
        (self.root / ".git").mkdir()
        (self.root / ".git/omit").write_text("repository internals")
        auxiliary = self.root / "build/aux"
        auxiliary.mkdir(parents=True)
        with ExitStack() as blocked:
            for target in ("os.symlink", "os.link", "shutil.copystat", "os.setxattr"):
                blocked.enter_context(patch(target, side_effect=AssertionError("forbidden hosted operation")))
            with self.assertRaisesRegex(RuntimeError, "compile failed"):
                with build.source_workspace(self.deck, auxiliary, self.root / "shared", copy_workspace=True) as (
                        working, repository, output, assets):
                    self.assertEqual((working / "../shared/common.tex").read_bytes(),
                                     (self.root / "shared/common.tex").read_bytes())
                    self.assertEqual((assets / "common.tex").read_bytes(),
                                     (self.root / "shared/common.tex").read_bytes())
                    self.assertFalse((repository / ".git").exists())
                    self.assertFalse((repository / "shared/build").exists())
                    self.assertFalse(any(path.is_symlink() for path in repository.rglob("*")))
                    (output / "failure.log").write_text("saved compiler diagnostic")
                    raise RuntimeError("compile failed")
        self.assertEqual((auxiliary / "failure.log").read_text(), "saved compiler diagnostic")
        self.assertFalse(working.exists())
        self.assertFalse((self.root / "slides/failure.log").exists())

    def test_normal_cache_remains_locked_and_new_cache_is_exclusive(self):
        with patch.object(build.shutil, "which", return_value="/installed/tool"), \
             patch.object(build, "missing_tex_packages", return_value=[]), \
             patch.object(build, "build_one", return_value=self.paths.pdf("light", "live")), \
             patch.object(build.fcntl, "flock", wraps=fcntl.flock) as lock:
            build.build_deck(self.deck, ["light"], ["live"], self.paths)
            self.assertEqual(lock.call_count, 1)
            self.assertEqual(lock.call_args.args[1], fcntl.LOCK_EX)
            marker = self.paths.cache_root / "preserve.txt"
            marker.write_text("existing cache")
            with self.assertRaisesRegex(ValueError, "requires a new cache"):
                build.build_deck(self.deck, ["light"], ["live"], self.paths, isolated_cache=True)
            self.assertEqual(marker.read_text(), "existing cache")
            private = get_paths(self.deck, self.root / "build/other-new-cache")
            with patch.object(build.fcntl, "flock", side_effect=AssertionError("lock not available")):
                build.build_deck(self.deck, ["light"], ["live"], private, isolated_cache=True)

    def test_new_cache_uses_existing_assets_and_installed_dependencies(self):
        with patch.object(build.shutil, "which", return_value="/installed/tool"):
            for option in ("regenerate", "fetch_tex"):
                with self.subTest(option=option), self.assertRaisesRegex(ValueError, "existing figure assets"):
                    build.build_deck(self.deck, ["light"], ["live"], self.paths,
                                     isolated_cache=True, **{option: True})
                self.assertFalse(self.paths.cache_root.exists())

    @unittest.skipUnless(shutil.which("pdflatex") and shutil.which("kpsewhich"), "Installed TeX is required")
    def test_actual_tex_build_succeeds_with_links_locks_and_xattrs_denied(self):
        if build.missing_tex_packages(build.tex_environment(self.paths), ("beamer.cls",)):
            self.skipTest("Installed Beamer is required")
        with ExitStack() as blocked:
            for target in ("os.symlink", "os.link", "fcntl.flock", "shutil.copystat", "os.setxattr"):
                blocked.enter_context(patch(target, side_effect=AssertionError("forbidden hosted operation")))
            outputs = build.build_deck(self.deck, ["light", "dark"], ["live"], self.paths,
                                       copy_workspace=True, isolated_cache=True)
        self.assertEqual(len(outputs), 2)
        for output in outputs:
            self.assertTrue(output.read_bytes().startswith(b"%PDF-"))
            metadata = json.loads(output.with_suffix(".presentation.json").read_text())
            self.assertEqual(metadata["pages"], [{"number": 1, "logical": 1}])


if __name__ == "__main__":
    unittest.main(verbosity=2)
