#!/usr/bin/env python3
"""Exercise the actual native data reader with valid and adversarial payloads."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import shutil
import shlex
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from ..native import payload_bundle as bundle
from ..verify import runtime

HERE = Path(__file__).resolve().parents[1] / "native"
POLICY = HERE / "payload-policy.json"
RULES = json.loads(POLICY.read_text())
PREFIX = "usr/share/derivation-presentations/"


class PayloadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        selected = os.environ.get("PAYLOAD_READER")
        if selected:
            cls.reader_path = Path(selected).resolve()
            if not cls.reader_path.is_file():
                raise RuntimeError(f"Selected payload reader is missing: {cls.reader_path}")
            return
        if not shutil.which("cc") or not shutil.which("pkg-config"):
            raise unittest.SkipTest("Native payload tests need cc and GLib/JSON-GLib/zlib development files")
        flags = subprocess.run(["pkg-config", "--cflags", "--libs", "glib-2.0", "json-glib-1.0", "zlib"],
                               text=True, capture_output=True)
        if flags.returncode:
            raise unittest.SkipTest("Native payload tests need GLib/JSON-GLib/zlib development files")
        cls.compiler_directory = tempfile.TemporaryDirectory(prefix="payload-reader-tests-")
        cls.addClassCleanup(cls.compiler_directory.cleanup)
        cls.reader_path = Path(cls.compiler_directory.name) / "reader"
        subprocess.run(["cc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
                        str(HERE / "payload_reader.c"), "-o", str(cls.reader_path),
                        *shlex.split(flags.stdout)], check=True)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="payload-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.template = self.root / "template.appimage"
        self.template.write_bytes(b"\x7fELF" + b"template-only\n" * 50)
        self.input = self.root / "source.pdf"
        self.input.write_bytes(b"%PDF-1.7\n" + bytes(range(256)) * 1024)
        self.image = self.root / "complete.appimage"
        self.cache = self.root / "cache"
        self.name = PREFIX + "decks/article/paper-light-live.pdf"
        self.receipt = bundle.append(self.template, {self.name: self.input}, self.image,
                                     RULES, {"source": "fixture"})

    def reader(self, mode="extract", *arguments, expected=0, image=None):
        command = [str(self.reader_path), str(POLICY), str(image or self.image), mode]
        command += list(map(str, arguments)) or ([] if mode == "inspect" else [str(self.cache)])
        result = subprocess.run(command, text=True, capture_output=True, timeout=15)
        if expected == 0:
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout)
        return result

    def rewrite(self, mutate=None, data=None):
        raw = self.image.read_bytes()
        magic, base, length, manifest_length, *_ = bundle.FOOTER.unpack(raw[-bundle.FOOTER.size:])
        manifest = json.loads(raw[base + length:base + length + manifest_length])
        payload = raw[base:base + length]
        if data is not None:
            payload = data(payload, manifest)
        if mutate:
            mutate(manifest)
        encoded = bundle.canonical_json(manifest)
        self.image.write_bytes(raw[:base] + payload + encoded + bundle.FOOTER.pack(
            magic, base, len(payload), len(encoded), hashlib.sha256(raw[:base]).digest(),
            hashlib.sha256(payload).digest(), hashlib.sha256(encoded).digest()))

    def test_actual_reader_roundtrip_and_cache_reuse(self):
        self.assertEqual(self.image.read_bytes()[:self.template.stat().st_size], self.template.read_bytes())
        manifest = json.loads(self.reader("inspect").stdout)
        self.assertEqual(manifest["files"], self.receipt["files"])
        directory = Path(self.reader().stdout.strip())
        target = directory / self.name
        self.assertEqual(target.read_bytes(), self.input.read_bytes())
        self.assertEqual(target.stat().st_mode & 0o777, 0o444)
        before = target.stat().st_mtime_ns
        self.assertEqual(Path(self.reader().stdout.strip()), directory)
        self.assertEqual(target.stat().st_mtime_ns, before)

    def test_python_inspection_and_existing_payload_template_rejection(self):
        self.assertEqual(len(bundle.MAGIC), 16)
        inspection = bundle.inspect(self.image)
        self.assertEqual(inspection["manifest"]["files"], self.receipt["files"])
        self.assertEqual(inspection["manifest"], json.loads(self.reader("inspect").stdout))
        with self.assertRaisesRegex(ValueError, "already contains a data payload"):
            bundle.append(self.image, {self.name: self.input}, self.root / "nested.appimage", RULES, {})
        self.assertFalse((self.root / "nested.appimage").exists())

    def test_generic_target_side_launch_and_transient_cleanup(self):
        result = self.reader("run", self.cache, "1", "/usr/bin/printenv", "APPIMAGE_PAYLOAD_DIR")
        self.assertFalse(Path(result.stdout.strip()).exists())
        self.assertFalse(self.cache.exists())
        result = self.reader("run", self.cache, "0", "/usr/bin/printenv", "APPIMAGE_PAYLOAD_DIR")
        self.assertTrue((Path(result.stdout.strip()) / self.name).is_file())

    def test_generic_application_queries_leave_durable_pdf_path_and_no_transient_data(self):
        appdir = self.root / "generic AppDir"
        launcher = appdir / "usr/libexec/payload-application"
        launcher.parent.mkdir(parents=True)
        shutil.copyfile(HERE / "AppRun", launcher)
        launcher.chmod(0o755)
        reader = appdir / "usr/bin/appimage-payload-reader"
        reader.parent.mkdir(parents=True)
        shutil.copyfile(self.reader_path, reader)
        reader.chmod(0o755)
        viewer = reader.with_name("pdfpc")
        viewer.write_text('#!/bin/bash\nprintf "%s\\n" "$@"\n')
        viewer.chmod(0o755)
        font_config = appdir / "etc/fonts/fonts.conf.in"
        font_config.parent.mkdir(parents=True)
        shutil.copyfile(HERE / "fonts.conf", font_config)
        (appdir / "usr/share/fonts").mkdir(parents=True)
        policy = appdir / "usr/share/appimage-payload/policy.json"
        policy.parent.mkdir(parents=True)
        shutil.copyfile(POLICY, policy)
        metadata = {"decks.tsv": "article\tpaper\tes\t0\t0\tlight\tIndependent paper\n",
                    "default-deck": "article\n", "project-title": "Example project\n",
                    "bundle-id": "a" * 64 + "\n", "source-revision": "fixture\n",
                    "build-timestamp": "2026-11-09T01:02:03Z\n"}
        files = {self.name: self.input}
        for name, contents in metadata.items():
            file = self.root / name
            file.write_text(contents)
            files[PREFIX + name] = file
        image = self.root / "application.appimage"
        bundle.append(self.template, files, image, RULES, {})
        environment = {**os.environ, "APPDIR": str(appdir), "APPIMAGE": str(image),
                       "DERIVATION_CACHE_ROOT": str(self.cache), "TMPDIR": str(self.root)}
        environment.pop("DERIVATION_BUILD_DIR", None)
        environment.pop("APPIMAGE_PAYLOAD_DIR", None)
        def query(*arguments):
            return subprocess.run(["/bin/bash", str(HERE / "PayloadAppRun"), *arguments],
                                  env=environment, text=True, capture_output=True, timeout=15)
        expected = self.cache / "bundles" / ("a" * 64) / "article/pdf/paper-light-live.pdf"
        result = query("--print-pdf")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(Path(result.stdout.strip()), expected)
        self.assertFalse(self.cache.exists())
        self.assertFalse(list(self.root.glob("appimage-payload-*")))
        result = query("--list")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("article\tes\t0\tlight\tIndependent paper", result.stdout)
        self.assertFalse(self.cache.exists())
        self.assertNotEqual(query("--dark", "--print-pdf").returncode, 0)
        self.assertFalse(self.cache.exists())
        result = query()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(expected.read_bytes(), self.input.read_bytes())
        self.assertFalse(expected.with_suffix(".vector.json").exists())
        inspected = runtime.packaged_payload(appdir, image, self.root / "inspection", environment)
        self.assertEqual((inspected / "decks/article/paper-light-live.pdf").read_bytes(), self.input.read_bytes())
        ordinary = appdir / "usr/share/derivation-presentations"
        ordinary.mkdir()
        with patch.object(runtime, "command", side_effect=AssertionError("ordinary payload needs no extraction")):
            self.assertEqual(runtime.packaged_payload(appdir, image, self.root / "unused", environment), ordinary)

    def test_parallel_cache_publication(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            outputs = list(pool.map(lambda _: self.reader().stdout.strip(), range(4)))
        self.assertEqual(len(set(outputs)), 1)
        self.assertFalse(list((self.cache / "payloads").glob(".payload-stage-*")))

    def test_template_data_manifest_and_footer_corruption(self):
        original = self.image.read_bytes()
        base = self.template.stat().st_size
        points = [20, base + 2, len(original) - bundle.FOOTER.size - 3, len(original) - 1]
        for position in points:
            with self.subTest(position=position):
                raw = bytearray(original)
                raw[position] ^= 1
                self.image.write_bytes(raw)
                marker = self.root / "untrusted-launch"
                self.reader("run", self.cache, "0", "/usr/bin/touch", marker, expected=2)
                self.assertFalse(marker.exists())
                self.assertFalse(self.cache.exists())
        self.image.write_bytes(original)

    def test_truncated_and_forged_footer_lengths(self):
        original = self.image.read_bytes()
        for cut in [1, 135, len(original) - 10]:
            with self.subTest(cut=cut):
                self.image.write_bytes(original[:-cut])
                self.reader(expected=2)
        footer = list(bundle.FOOTER.unpack(original[-bundle.FOOTER.size:]))
        footer[2] = (1 << 64) - 1
        self.image.write_bytes(original[:-bundle.FOOTER.size] + bundle.FOOTER.pack(*footer))
        self.reader(expected=2)

    def test_traversal_absolute_empty_component_and_executable_type(self):
        original = self.image.read_bytes()
        for name in ["../escaped.pdf", "/tmp/escaped.pdf", PREFIX + "../escaped.pdf",
                     PREFIX + "a//b.pdf", PREFIX + "a\\b.pdf", PREFIX + "script.so"]:
            with self.subTest(name=name):
                self.image.write_bytes(original)
                self.rewrite(lambda m: m["files"][0].update(path=name))
                self.reader(expected=2)
                self.assertFalse(self.cache.exists())

    def test_duplicate_entry_and_conflicting_parent(self):
        original = self.image.read_bytes()
        self.rewrite(lambda m: m["files"].append(dict(m["files"][0])))
        self.reader(expected=2)
        self.image.write_bytes(original)
        def conflict(data, manifest):
            entry = dict(manifest["files"][0])
            entry.update(path=entry["path"] + "/child.pdf", offset=len(data))
            manifest["files"].append(entry)
            return data + data
        self.rewrite(data=conflict)
        self.reader(expected=2)

    def test_escaped_nul_fields_are_not_silently_truncated(self):
        original = self.image.read_bytes()
        for field in ("path", "sha256", "format"):
            with self.subTest(field=field):
                self.image.write_bytes(original)
                def mutate(manifest):
                    target = manifest if field == "format" else manifest["files"][0]
                    target[field] += "\0suffix"
                self.rewrite(mutate)
                self.reader(expected=2)
                self.assertFalse(self.cache.exists())

    def test_metadata_rejects_nul_but_preserves_literal_escape_text(self):
        for metadata in ({"bad\0key": "value"}, {"list": ["bad\0value"]}):
            with self.assertRaises(ValueError):
                bundle.append(self.template, {self.name: self.input}, self.root / "bad.appimage",
                              RULES, metadata)
            self.assertFalse((self.root / "bad.appimage").exists())
        literal = {"text": r"literal \u0000 sequence"}
        self.rewrite(lambda manifest: manifest.update(metadata=literal))
        self.assertEqual(json.loads(self.reader("inspect").stdout)["metadata"], literal)

    def test_decompression_size_and_global_bounds(self):
        original = self.image.read_bytes()
        for size in [1, bundle.MAX_FILE + 1]:
            self.image.write_bytes(original)
            self.rewrite(lambda m: m["files"][0].update(bytes=size))
            self.reader(expected=2)
            self.assertFalse(list(self.cache.glob("payloads/.payload-stage-*")))

    def test_truncated_compression_and_extra_stream_data(self):
        original = self.image.read_bytes()
        def shorten(data, manifest):
            manifest["files"][0]["compressed_bytes"] -= 1
            return data[:-1]
        def extend(data, manifest):
            manifest["files"][0]["compressed_bytes"] += 1
            return data + b"x"
        for mutate in [shorten, extend]:
            self.image.write_bytes(original)
            self.rewrite(data=mutate)
            self.reader(expected=2)
            self.assertFalse(list(self.cache.glob("payloads/.payload-stage-*")))

    def test_file_digest_cannot_be_replaced_by_rehashing_footer(self):
        self.rewrite(lambda m: m["files"][0].update(sha256="0" * 64))
        self.reader(expected=2)

    def test_cache_corruption_and_symlink_are_rejected(self):
        directory = Path(self.reader().stdout.strip())
        target = directory / self.name
        target.chmod(0o600)
        target.write_bytes(b"changed")
        self.reader(expected=2)
        target.unlink()
        outside = self.root / "outside.pdf"
        outside.write_bytes(b"preserve this file")
        target.symlink_to(outside)
        self.reader(expected=2)
        self.assertEqual(outside.read_bytes(), b"preserve this file")

    def test_cache_directory_symlink_is_rejected(self):
        directory = Path(self.reader().stdout.strip())
        shutil.rmtree(directory)
        outside = self.root / "unrelated"
        outside.mkdir()
        directory.symlink_to(outside, target_is_directory=True)
        self.reader(expected=2)
        self.assertEqual(list(outside.iterdir()), [])

    def test_writer_rejects_unsafe_inputs_and_preserves_existing_outputs(self):
        before = self.image.read_bytes()
        with self.assertRaises(ValueError):
            bundle.append(self.template, {self.name: self.input}, self.image, RULES, {})
        self.assertEqual(self.image.read_bytes(), before)
        link = self.root / "link.pdf"
        link.symlink_to(self.input)
        with self.assertRaises(ValueError):
            bundle.append(self.template, {self.name: link}, self.root / "bad.appimage", RULES, {})
        self.assertFalse((self.root / "bad.appimage").exists())
        with self.assertRaises(ValueError):
            bundle.append(self.template, {"../escape.pdf": self.input}, self.root / "bad.appimage", RULES, {})

    def test_dangling_output_symlink_is_preserved(self):
        output = self.root / "dangling.appimage"
        missing = self.root / "preserve-missing-target"
        output.symlink_to(missing)
        with self.assertRaises(ValueError):
            bundle.append(self.template, {self.name: self.input}, output, RULES, {})
        self.assertTrue(output.is_symlink())
        self.assertEqual(Path(os.readlink(output)), missing)
        self.assertFalse(missing.exists())

    def test_competing_producers_publish_one_complete_winner(self):
        output = self.root / "contended.appimage"
        other = self.root / "other.pdf"
        other.write_bytes(b"different complete source\n" * 10000)
        barrier = threading.Barrier(2)
        thread = threading.local()
        fsync = os.fsync
        def both_inputs_staged(fd):
            fsync(fd)
            if not getattr(thread, "staged", False):
                thread.staged = True
                barrier.wait(timeout=5)
        def produce(path):
            try:
                receipt = bundle.append(self.template, {self.name: path}, output, RULES, {"input": path.name})
                return (path, receipt)
            except ValueError:
                return None
        with patch.object(bundle.os, "fsync", both_inputs_staged), ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(produce, [self.input, other]))
        winners = [result for result in results if result is not None]
        self.assertEqual(len(winners), 1)
        path, receipt = winners[0]
        self.assertEqual(bundle.digest(output), receipt["output_sha256"])
        directory = Path(self.reader(image=output).stdout.strip())
        self.assertEqual((directory / self.name).read_bytes(), path.read_bytes())

    def test_writer_and_reader_enforce_component_byte_limits(self):
        original = self.image.read_bytes()
        for component in ["a" * 256 + ".pdf", "é" * 128 + ".pdf"]:
            name = PREFIX + component
            with self.assertRaises(ValueError):
                bundle.append(self.template, {name: self.input}, self.root / "bad.appimage", RULES, {})
            self.image.write_bytes(original)
            self.rewrite(lambda m: m["files"][0].update(path=name))
            self.reader(expected=2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
