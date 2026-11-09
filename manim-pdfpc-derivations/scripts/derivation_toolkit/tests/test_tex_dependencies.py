"""Exercise the opt-in Debian archive boundary without downloading packages."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from ..build import fetch_tex_packages
from ..catalog import Catalog
from ..paths import get_paths


class TexArchives(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="derivation-tex-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.catalog = {"version": 2, "default": "example", "tex": {},
                        "decks": [{"id": "example", "title": "Example", "source": "example.tex"}]}
        self.write_catalog()
        self.paths = get_paths(Catalog(self.root).get(), self.root / "build")

    def write_catalog(self):
        (self.root / "presentations.json").write_text(json.dumps(self.catalog))

    def test_archive_fetch_requires_explicit_valid_project_selectors(self):
        for packages in (None, [], "texlive-custom", ["--option"]):
            self.catalog["tex"]["debian_packages"] = packages
            self.write_catalog()
            with self.subTest(packages=packages):
                with patch("derivation_toolkit.build.shutil.which",
                           side_effect=AssertionError("No tool lookup before validating the project")):
                    with self.assertRaisesRegex(ValueError, "tex.debian_packages"):
                        fetch_tex_packages(self.paths)
        self.assertFalse(self.paths.tex_packages_dir.exists())

    def test_each_selector_extracts_its_own_download_instead_of_a_stale_cached_archive(self):
        packages = ["texlive-custom=1.0", "texlive-extra:all=2.0"]
        self.catalog["tex"]["debian_packages"] = packages
        self.write_catalog()
        cache = self.paths.tex_packages_dir
        cache.mkdir(parents=True)
        stale = cache / "texlive-custom_99_all.deb"
        stale.write_bytes(b"stale archive")
        os.utime(stale, (4_000_000_000, 4_000_000_000))
        downloaded, extracted = [], []

        def run(argv, **kwargs):
            if argv[:2] == ["apt-get", "download"]:
                selected = argv[2]
                downloaded.append(selected)
                name = selected.split("=", 1)[0].split(":", 1)[0]
                version = selected.split("=", 1)[1]
                (Path(kwargs["cwd"]) / f"{name}_{version}_all.deb").write_text(selected)
            elif argv[:2] == ["dpkg-deb", "-x"]:
                extracted.append(Path(argv[2]).read_text())
                self.assertEqual(Path(argv[3]), self.paths.texlive_dir)
            else:
                self.fail(f"Unexpected external command: {argv}")
            return subprocess.CompletedProcess(argv, 0)

        with patch("derivation_toolkit.build.shutil.which", return_value="/configured/tool"):
            with patch("derivation_toolkit.build.subprocess.run", side_effect=run):
                fetch_tex_packages(self.paths)
        self.assertEqual(downloaded, packages)
        self.assertEqual(extracted, packages)
        self.assertEqual(stale.read_bytes(), b"stale archive")
        self.assertEqual(list(cache.glob("download-*")), [])
