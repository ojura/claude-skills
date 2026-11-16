"""Native documentation must survive ordinary and data-only package creation."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ..native import payload_template
from ..verify import runtime


class NativeDocumentation(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="viewer documentation ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "native source"
        self.appdir = self.root / "AppDir"
        self.documents = self.appdir / "usr/share/doc/pdfpc"
        for relative, original in runtime.NATIVE_DOCUMENTS.items():
            content = (original + "\nFixture document contents.\n").encode()
            for filename in (self.source / original, self.documents / relative):
                filename.parent.mkdir(parents=True, exist_ok=True)
                filename.write_bytes(content)

    def test_installed_documents_match_source(self):
        paths = runtime.verify_native_documentation(self.appdir, self.source)
        self.assertEqual(paths, ["usr/share/doc/pdfpc/" + name for name in runtime.NATIVE_DOCUMENTS])
        self.assertEqual(runtime.verify_native_documentation(self.appdir), paths)

    def test_each_missing_or_empty_document_is_rejected(self):
        for relative in runtime.NATIVE_DOCUMENTS:
            filename = self.documents / relative
            contents = filename.read_bytes()
            for missing in (True, False):
                with self.subTest(document=relative, missing=missing):
                    if missing:
                        filename.unlink()
                    else:
                        filename.write_bytes(b"")
                    with self.assertRaisesRegex(ValueError, "Missing or empty installed viewer document"):
                        runtime.verify_native_documentation(self.appdir)
                    filename.write_bytes(contents)

    def test_changed_document_bytes_are_rejected(self):
        for relative in runtime.NATIVE_DOCUMENTS:
            filename = self.documents / relative
            contents = filename.read_bytes()
            with self.subTest(document=relative):
                filename.write_bytes(b"X" + contents[1:])
                with self.assertRaisesRegex(ValueError, "differs from native source"):
                    runtime.verify_native_documentation(self.appdir, self.source)
                filename.write_bytes(contents)

    def test_package_identity_directory_does_not_replace_the_native_install(self):
        moved = self.appdir / "usr/share/doc/example-project"
        self.documents.rename(moved)
        with self.assertRaisesRegex(ValueError, "usr/share/doc/pdfpc/LICENSE.txt"):
            runtime.verify_native_documentation(self.appdir)

    def test_command_works_without_site_packages_and_checks_the_source(self):
        command = [sys.executable, "-I", "-S", "-B", runtime.__file__, "documentation",
                   "--appdir", str(self.appdir), "--source", str(self.source)]
        checked = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(checked.returncode, 0, checked.stderr)
        self.assertEqual(json.loads(checked.stdout), runtime.verify_native_documentation(self.appdir))
        (self.documents / "LICENSE.txt").unlink()
        missing = subprocess.run(command, capture_output=True, text=True)
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("Missing or empty installed viewer document", missing.stderr)

    def test_template_rejects_missing_documents_before_copying(self):
        (self.documents / "LICENSE.txt").unlink()
        release = Path.read_text

        def read_text(filename, *args, **kwargs):
            if filename == Path("/etc/os-release"):
                return "ID=ubuntu\nVERSION_ID=20.04\n"
            return release(filename, *args, **kwargs)

        with patch.object(Path, "read_text", read_text), \
                patch.object(payload_template.os, "uname", return_value=SimpleNamespace(machine="x86_64")), \
                patch.object(payload_template.shutil, "copytree") as copytree:
            with self.assertRaisesRegex(ValueError, "Missing or empty installed viewer document"):
                payload_template.build_template(None, self.appdir, self.root / "tools")
            copytree.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
