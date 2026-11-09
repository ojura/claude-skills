#!/usr/bin/env python3
"""Check the packaged presentation CLI without a GUI or authoring tools."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

sys.dont_write_bytecode = True
APPRUN = Path(__file__).resolve().parents[1] / "native/AppRun"
BASENAME = "example-motion"
SOURCE_REVISION = "0123456789abcdef0123456789abcdef01234567"
BUILD_TIMESTAMP = "2026-11-09T01:02:03Z"


class AppRunCLI(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="derivation-cli-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.bundle = self.root / "bundle path # test"
        self.assets = self.bundle / "usr/share/derivation-presentations"
        self.assets.mkdir(parents=True)
        self.deck_assets = self.assets / "decks/example-motion"
        self.deck_assets.mkdir(parents=True)
        self.static_assets = self.assets / "decks/example-static"
        self.static_assets.mkdir(parents=True)
        (self.assets / "default-deck").write_text("example-motion\n")
        (self.assets / "decks.tsv").write_text(
            f"example-motion\t{BASENAME}\tde-DE\t0\t1\tlight,dark\tExample motion\n"
            "example-static\tstatic-example\tfr\t1\t0\tlight,dark\tArchived example\n")
        font_config = self.bundle / "etc/fonts/fonts.conf.in"
        font_config.parent.mkdir(parents=True)
        shutil.copyfile(APPRUN.with_name("fonts.conf"), font_config)
        (self.bundle / "usr/share/fonts").mkdir(parents=True)
        (self.assets / "bundle-id").write_text("a" * 64 + "\n")
        (self.assets / "source-revision").write_text(SOURCE_REVISION + "\n")
        (self.assets / "build-timestamp").write_text(BUILD_TIMESTAMP + "\n")
        (self.assets / "project-title").write_text("Example project\n")
        for theme in ("light", "dark"):
            (self.deck_assets / f"{BASENAME}-{theme}-live.pdf").write_bytes(f"PDF {theme}".encode())
            (self.deck_assets / f"{BASENAME}-{theme}-live.vector.json").write_text(json.dumps({"theme": theme}))
            (self.static_assets / f"static-example-{theme}-live.pdf").write_bytes(f"Static PDF {theme}".encode())
        viewer = self.bundle / "usr/bin/pdfpc"
        viewer.parent.mkdir(parents=True)
        viewer.write_text("#!/bin/bash\nprintf '%s\\0' \"$PDFPC_DATA_DIR\" \"$PDFPC_CONFIG_DIR\" \"${HOOK_SEEN:-}\" \"$PDFPC_TITLE_SUFFIX\" \"$@\"\n")
        viewer.chmod(0o755)
        hooks = self.bundle / "apprun-hooks"
        hooks.mkdir()
        (hooks / "gtk.sh").write_text('export HOOK_SEEN=gtk\nexport LD_LIBRARY_PATH="$APPDIR/usr/lib:$LD_LIBRARY_PATH"\n')
        self.cache = self.root / "xdg cache # test"
        self.env = os.environ.copy()
        for key in ("DERIVATION_CACHE_ROOT", "DERIVATION_BUILD_DIR", "LD_LIBRARY_PATH"):
            self.env.pop(key, None)
        self.env.update(APPDIR=str(self.bundle), XDG_CACHE_HOME=str(self.cache), HOME=str(self.root / "home"))
        # Runtime has only basic OS utilities: no TeX, Manim, Python, Git or downloaders.
        runtime_bin = self.root / "runtime-bin"
        runtime_bin.mkdir()
        for name in ("cat", "realpath", "getent", "dirname", "mkdir", "flock", "cmp",
                     "mktemp", "cp", "chmod", "mv", "rm", "sha256sum"):
            command = shutil.which(name)
            self.assertIsNotNone(command, name)
            (runtime_bin / name).symlink_to(command)
        self.env["PATH"] = str(runtime_bin)

    def invoke(self, *arguments, env=None):
        return subprocess.run(["/bin/bash", str(APPRUN), *map(str, arguments)],
                              cwd=self.root, env=env or self.env, capture_output=True)

    def expected(self, theme="light", identity="a" * 64):
        return self.cache / "derivation-presentations/bundles" / identity / "example-motion/pdf" / f"{BASENAME}-{theme}-live.pdf"

    def test_help_needs_no_payload_and_creates_nothing(self):
        (self.assets / "bundle-id").unlink()
        result = self.invoke("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(b"does not require TeX, Manim, downloads or compilation", result.stdout)
        self.assertFalse(self.cache.exists())

    def test_queries_are_pure_and_theme_selection_matches_cli(self):
        for arguments, theme in [((), "light"), (("--dark",), "dark"), (("--dark", "--light"), "light")]:
            result = self.invoke(*arguments, "--print-pdf")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.decode().strip(), str(self.expected(theme)))
        self.assertFalse(self.cache.exists())

    def test_environment_and_cli_paths_handle_spaces_and_hash(self):
        env = self.env | {"DERIVATION_CACHE_ROOT": str(self.root / "environment root #"),
                          "DERIVATION_BUILD_DIR": str(self.root / "environment output #")}
        result = self.invoke("--print-pdf", env=env)
        self.assertEqual(result.stdout.decode().strip(), str(self.root / "environment output #" / f"{BASENAME}-light-live.pdf"))
        result = self.invoke("--cache-root", "~/chosen root #", "--output-dir", "relative output #", "--print-pdf", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.decode().strip(), str(self.root / "relative output #" / f"{BASENAME}-light-live.pdf"))
        result = self.invoke("--cache-root", "~/chosen root #", "--print-pdf")
        self.assertEqual(result.stdout.decode().strip(), str(self.root / "home/chosen root #/bundles" / ("a" * 64) / "example-motion/pdf" / f"{BASENAME}-light-live.pdf"))
        self.assertFalse(self.cache.exists())
        self.assertFalse((self.root / "relative output #").exists())
        self.assertFalse((self.root / "home").exists())

    def test_launch_copies_assets_and_preserves_all_viewer_arguments(self):
        arguments = ["--no-install", "--windowed=both", "-Z", "1000:750", "-P", "6"]
        result = self.invoke("--dark", arguments[0], "--", *arguments[1:])
        self.assertEqual(result.returncode, 0, result.stderr)
        actual = [part.decode() for part in result.stdout.split(b"\0")[:-1]]
        self.assertEqual(actual, [str(self.bundle / "usr/share/pdfpc"), str(self.bundle / "etc"), "gtk",
                                 f"Example project {SOURCE_REVISION[:8]} | built {BUILD_TIMESTAMP}",
                                 *arguments, str(self.expected("dark"))])
        for suffix in (".pdf", ".vector.json"):
            destination = self.expected("dark").with_suffix(suffix)
            self.assertEqual(destination.read_bytes(), (self.deck_assets / destination.name).read_bytes())

    def test_repeated_run_reuses_identical_assets_and_repairs_changed_copy(self):
        self.assertEqual(self.invoke().returncode, 0)
        pdf = self.expected()
        sidecar = pdf.with_suffix(".vector.json")
        before = [path.stat().st_mtime_ns for path in (pdf, sidecar)]
        self.assertEqual(self.invoke().returncode, 0)
        self.assertEqual(before, [path.stat().st_mtime_ns for path in (pdf, sidecar)])
        pdf.write_bytes(b"interrupted or altered copy")
        self.assertEqual(self.invoke().returncode, 0)
        self.assertEqual(pdf.read_bytes(), (self.deck_assets / pdf.name).read_bytes())
        for path in (pdf, sidecar):
            self.assertFalse(list(path.parent.glob(f".{path.name}.*")))

    def test_gio_module_paths_are_isolated_before_hooks_and_viewer(self):
        module_dir = self.bundle / "usr/lib/gio/modules"
        module_dir.mkdir(parents=True)
        (self.bundle / "apprun-hooks/gtk.sh").write_text(
            'export HOOK_GIO_MODULE_DIR="$GIO_MODULE_DIR"\n'
            'export HOOK_GIO_EXTRA_MODULES="${GIO_EXTRA_MODULES-unset}"\n')
        (self.bundle / "usr/bin/pdfpc").write_text(
            '#!/bin/bash\nprintf \'%s\\0\' "$GIO_MODULE_DIR" '
            '"${GIO_EXTRA_MODULES-unset}" "$HOOK_GIO_MODULE_DIR" '
            '"$HOOK_GIO_EXTRA_MODULES"\n')
        env = self.env | {"GIO_MODULE_DIR": "/host/gio/modules",
                          "GIO_EXTRA_MODULES": "/host/extra:/another/host/modules"}
        result = self.invoke(env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        actual = [part.decode() for part in result.stdout.split(b"\0")[:-1]]
        self.assertEqual(actual, [str(module_dir), "unset", str(module_dir), "unset"])

    def font_probe(self):
        (self.bundle / "apprun-hooks/gtk.sh").write_text(
            'export HOOK_FONTCONFIG_FILE="$FONTCONFIG_FILE"\n')
        (self.bundle / "usr/bin/pdfpc").write_text(
            '#!/bin/bash\nprintf \'%s\\0\' "$PWD" "$FONTCONFIG_FILE" '
            '"$FONTCONFIG_PATH" "$HOOK_FONTCONFIG_FILE"\n')

    def test_font_config_uses_absolute_xml_path_and_preserves_cwd(self):
        relocated = self.root / 'bundle & <fonts> "quoted" \'path\' #'
        self.bundle.rename(relocated)
        self.bundle = relocated
        self.env["APPDIR"] = str(relocated)
        self.font_probe()
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        cwd, filename, directory, hook_filename = result.stdout.decode().split("\0")[:-1]
        self.assertEqual(cwd, str(self.root))
        config = Path(filename)
        self.assertEqual(directory, str(config.parent))
        self.assertEqual(hook_filename, filename)
        self.assertTrue(config.is_relative_to(self.cache))
        self.assertEqual(ET.parse(config).getroot().findtext("dir"),
                         str(relocated / "usr/share/fonts"))
        before = config.stat().st_mtime_ns
        self.assertEqual(self.invoke().returncode, 0)
        self.assertEqual(config.stat().st_mtime_ns, before)

    def test_relocated_appdirs_keep_independent_font_configs(self):
        self.font_probe()
        first = self.invoke()
        self.assertEqual(first.returncode, 0, first.stderr)
        first_config = Path(first.stdout.decode().split("\0")[1])
        first_contents = first_config.read_bytes()
        relocated = self.root / "second mounted AppDir"
        shutil.copytree(self.bundle, relocated)
        second = self.invoke(env=self.env | {"APPDIR": str(relocated)})
        self.assertEqual(second.returncode, 0, second.stderr)
        second_config = Path(second.stdout.decode().split("\0")[1])
        self.assertNotEqual(first_config, second_config)
        self.assertEqual(first_config.read_bytes(), first_contents)
        self.assertEqual(ET.parse(second_config).getroot().findtext("dir"),
                         str(relocated / "usr/share/fonts"))

    def test_new_bundle_identity_keeps_previous_cached_payload(self):
        self.assertEqual(self.invoke().returncode, 0)
        old = self.expected()
        (self.assets / "bundle-id").write_text("b" * 64 + "\n")
        self.assertEqual(self.invoke("--dark").returncode, 0)
        self.assertTrue(old.is_file())
        self.assertTrue(self.expected("dark", "b" * 64).is_file())

    def test_invalid_bundle_and_missing_assets_fail_before_cache_writes(self):
        (self.assets / "bundle-id").write_text("../invalid\n")
        self.assertNotEqual(self.invoke("--print-pdf").returncode, 0)
        (self.assets / "bundle-id").write_text("a" * 64 + "\n")
        (self.deck_assets / f"{BASENAME}-light-live.vector.json").unlink()
        self.assertNotEqual(self.invoke().returncode, 0)
        self.assertFalse(self.cache.exists())

    def test_missing_path_argument_is_reported(self):
        for option in ("--cache-root", "--output-dir"):
            result = self.invoke(option)
            self.assertEqual(result.returncode, 2)
            self.assertIn(b"Missing directory", result.stderr)
        self.assertFalse(self.cache.exists())

    def test_catalog_query_is_pure_and_lists_language_and_history(self):
        result = self.invoke("--list")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(b"example-static\tfr\t1\tlight,dark\tArchived example", result.stdout)
        self.assertFalse(self.cache.exists())

    def test_static_selection_uses_only_its_pdf_and_the_catalog_default(self):
        for arguments in (("--deck", "example-static"), ()):
            if not arguments:
                (self.assets / "default-deck").write_text("example-static\n")
            result = self.invoke(*arguments, "--dark")
            self.assertEqual(result.returncode, 0, result.stderr)
            pdf = Path(result.stdout.decode().split("\0")[-2])
            self.assertEqual(pdf.name, "static-example-dark-live.pdf")
            self.assertEqual(pdf.read_bytes(), b"Static PDF dark")
            self.assertFalse(pdf.with_suffix(".vector.json").exists())

    def test_unknown_or_missing_deck_fails_before_writing_cache(self):
        for arguments in (("--deck",), ("--deck", "unknown"), ("--deck", "../example-motion")):
            result = self.invoke(*arguments)
            self.assertEqual(result.returncode, 2, result.stderr)
        self.assertFalse(self.cache.exists())

    def test_packaged_title_metadata_overrides_inherited_suffix(self):
        result = self.invoke(env=self.env | {"PDFPC_TITLE_SUFFIX": "old unrelated build"})
        self.assertEqual(result.returncode, 0, result.stderr)
        actual = result.stdout.decode().split("\0")
        self.assertEqual(actual[3], f"Example project {SOURCE_REVISION[:8]} | built {BUILD_TIMESTAMP}")

    def test_invalid_build_metadata_fails_before_cache_writes(self):
        for name, invalid in (("source-revision", "invalid revision with spaces"),
                              ("build-timestamp", "2026-11-09 01:02:03 local")):
            filename = self.assets / name
            original = filename.read_text()
            filename.write_text(invalid + "\n")
            result = self.invoke()
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(b"Invalid bundled source revision or UTC build timestamp", result.stderr)
            self.assertFalse(self.cache.exists())
            filename.write_text(original)


if __name__ == "__main__":
    unittest.main(verbosity=2)
