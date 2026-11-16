"""File publication preserves the prior generated set on preparation or rename failure."""
from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from .. import publication


def entries(root):
    result = {}
    for path in root.rglob("*"):
        name = str(path.relative_to(root))
        if path.is_symlink():
            result[name] = ("link", os.readlink(path))
        elif path.is_file():
            result[name] = ("file", path.read_bytes(), path.stat().st_mode & 0o777)
    return result


class FilePublication(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="derivation-publish-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.output = self.root / "output"
        self.source = self.root / "source"
        self.output.mkdir()
        self.source.mkdir()
        for name, content in (("first", b"old first"), ("second", b"old second"),
                              ("obsolete", b"obsolete"), ("unrelated", b"user file")):
            (self.output / name).write_bytes(content)
        (self.output / "dangling").symlink_to("absent-target")
        self.files = {}
        for name in ("first", "second", "new"):
            source = self.source / name
            source.write_bytes(("new " + name).encode())
            source.chmod(0o640)
            self.files[self.output / name] = source
        self.removals = [self.output / "obsolete", self.output / "dangling"]

    def test_success_replaces_generated_set_and_preserves_unrelated_files(self):
        publication.publish_files(self.files, self.removals)
        for target, source in self.files.items():
            self.assertEqual(target.read_bytes(), source.read_bytes())
            self.assertEqual(target.stat().st_mode & 0o777, 0o640)
        self.assertEqual((self.output / "unrelated").read_bytes(), b"user file")
        self.assertFalse(any(os.path.lexists(path) for path in self.removals))
        self.assertFalse(list(self.output.glob(".derivation-publish-*")))

    def test_failure_during_any_rename_restores_the_previous_set(self):
        before = entries(self.output)
        replace = publication.os.replace
        # Two replacements of old files, one new file and two deletions.
        for fail_at in range(1, 8):
            with self.subTest(rename=fail_at):
                count = 0

                def fail_once(source, destination):
                    nonlocal count
                    count += 1
                    if count == fail_at:
                        raise OSError("Injected rename failure")
                    return replace(source, destination)

                with patch.object(publication.os, "replace", side_effect=fail_once):
                    with self.assertRaisesRegex(OSError, "Injected rename failure"):
                        publication.publish_files(self.files, self.removals)
                self.assertEqual(entries(self.output), before)
                self.assertFalse(list(self.output.glob(".derivation-publish-*")))

    def test_missing_source_or_directory_destination_does_not_change_prior_files(self):
        before = entries(self.output)
        missing = {**self.files, self.output / "missing": self.source / "absent"}
        with self.assertRaises(FileNotFoundError):
            publication.publish_files(missing, self.removals)
        self.assertEqual(entries(self.output), before)
        directory = self.output / "folder"
        directory.mkdir()
        with self.assertRaisesRegex(ValueError, "destination is a directory"):
            publication.publish_files({**self.files, directory: self.source / "first"})
        self.assertEqual(entries(self.output), before)
        self.assertTrue(directory.is_dir())

    def test_interruption_immediately_after_backup_restores_the_previous_set(self):
        before = entries(self.output)
        replace = publication.os.replace
        interrupted = False

        def interrupt_after_move(source, destination):
            nonlocal interrupted
            result = replace(source, destination)
            if not interrupted and Path(destination).name.startswith("old-"):
                interrupted = True
                raise KeyboardInterrupt("After completed backup rename")
            return result

        with patch.object(publication.os, "replace", side_effect=interrupt_after_move):
            with self.assertRaisesRegex(KeyboardInterrupt, "completed backup rename"):
                publication.publish_files(self.files, self.removals)
        self.assertTrue(interrupted)
        self.assertEqual(entries(self.output), before)
        self.assertFalse(list(self.output.glob(".derivation-publish-*")))

    def test_failure_before_backup_leaves_the_original_destination_intact(self):
        before = entries(self.output)
        replace = publication.os.replace

        def fail_before_move(source, destination):
            if Path(destination).name.startswith("old-"):
                raise OSError("Before backup rename")
            return replace(source, destination)

        with patch.object(publication.os, "replace", side_effect=fail_before_move):
            with self.assertRaisesRegex(OSError, "Before backup rename"):
                publication.publish_files(self.files, self.removals)
        self.assertEqual(entries(self.output), before)
        self.assertFalse(list(self.output.glob(".derivation-publish-*")))

    def test_destination_symlink_is_replaced_without_touching_its_target(self):
        target = self.output / "external-link"
        outside = self.root / "external"
        outside.write_bytes(b"external source")
        target.symlink_to(outside)
        publication.publish_files({target: self.source / "first"})
        self.assertFalse(target.is_symlink())
        self.assertEqual(outside.read_bytes(), b"external source")
        self.assertEqual(target.read_bytes(), b"new first")

    def test_restoration_failure_retains_recoverable_backups(self):
        replace = publication.os.replace

        def fail_install_and_restore(source, destination):
            source, destination = Path(source), Path(destination)
            if ((source.name.startswith("new-") and destination.name == "second") or
                    (source.name.startswith("old-") and destination.name == "first")):
                raise OSError("Injected filesystem failure")
            return replace(source, destination)

        with patch.object(publication.os, "replace", side_effect=fail_install_and_restore):
            with self.assertRaisesRegex(RuntimeError, "Backups remain in"):
                publication.publish_files(self.files, self.removals)
        backups = list(self.output.glob(".derivation-publish-*/old-*"))
        self.assertTrue(any(path.read_bytes() == b"old first" for path in backups))
        self.assertEqual((self.output / "second").read_bytes(), b"old second")
        self.assertEqual((self.output / "unrelated").read_bytes(), b"user file")


if __name__ == "__main__":
    unittest.main(verbosity=2)
