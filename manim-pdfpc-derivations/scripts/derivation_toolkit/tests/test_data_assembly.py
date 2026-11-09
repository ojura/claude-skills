"""Exercise generic transport/data assembly without running a template or packer."""
from __future__ import annotations

from contextlib import ExitStack
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from ..catalog import Catalog
from ..native import data_assembly as assembly
from ..native.build import native_pin
from ..native.payload_bundle import digest, inspect
from ..native.payload_template import HERE, PAYLOAD, TEMPLATE_FORMAT


class DataAssembly(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="generic-data-assembly-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = self.root / "independent project"
        self.project.mkdir()
        self.catalog_file = self.project / "presentations.json"
        self.catalog_file.write_text(json.dumps({
            "version": 2, "project": {"title": "Independent paper", "audience": "reviewers"},
            "package": {"id": "example-paper"}, "default": "paper",
            "decks": [{"id": "paper", "source": "paper.md", "title": "An article", "language": "fr",
                       "build": {"kind": "external"}, "metadata": {"arbitrary": [1, "data"]}},
                      {"id": "motion", "source": "motion.tex", "title": "Animated companion", "language": "en",
                       "build": {"kind": "external"}}],
        }))
        self.catalog = Catalog(self.project)
        self.template = self.root / "template.appimage"
        self.template.write_bytes(b"\x7fELF" + b"immutable template bytes\n" * 100)
        repository, revision = native_pin(self.catalog)
        self.info = {"format": TEMPLATE_FORMAT, "version": 1,
                     "native_repository": repository, "native_revision": revision,
                     "template_sha256": digest(self.template), "template_bytes": self.template.stat().st_size,
                     "runtime_build_os": "Ubuntu 20.04 fixture", "runtime_source_revision": "runtime-fixture",
                     "highest_required_glibc": "2.31", "native_binary_sha256": "a" * 64}
        for field, name in (("policy_sha256", "payload-policy.json"),
                            ("application_launcher_sha256", "AppRun"),
                            ("payload_launcher_sha256", "PayloadAppRun"),
                            ("payload_reader_source_sha256", "payload_reader.c")):
            self.info[field] = digest(HERE / name)
        self.receipt = self.root / "template.json"
        self.receipt.write_text(json.dumps(self.info))
        self.transport = self.project / "build/transport"
        self.output = self.project / "build/assembled"
        documents = self.project / "build/documents"
        documents.mkdir(parents=True)
        self.inputs = self.root / "inputs.json"
        mapping = {"paper": {"light": {"pdf": "build/documents/paper.pdf"}}, "motion": {}}
        (documents / "paper.pdf").write_bytes(b"%PDF-1.7\nindependent light-only article\n")
        for theme in ("light", "dark"):
            pdf = documents / f"motion-{theme}.pdf"
            vectors = pdf.with_suffix(".vector.json")
            pdf.write_bytes(f"%PDF-1.7\n{theme} companion\n".encode())
            vectors.write_text(json.dumps({"version": 1, "theme": theme, "scenes": []}))
            mapping["motion"][theme] = {"pdf": str(pdf.relative_to(self.project)),
                                         "vectors": str(vectors.relative_to(self.project))}
        self.inputs.write_text(json.dumps(mapping))

    def prepare(self, **kwargs):
        return assembly.prepare_transport(self.catalog, self.template, self.receipt,
                    destination=self.transport, source_revision="input-fixture", part_bytes=400, **kwargs)

    def test_transport_and_data_only_roundtrip_preserves_generic_catalog(self):
        config = self.prepare()
        self.assertGreater(len(config["template_parts"]), 1)
        with ExitStack() as blocked:
            for target in ("subprocess.run", "subprocess.Popen", "os.symlink", "os.link",
                           "fcntl.flock", "shutil.copystat", "os.setxattr"):
                blocked.enter_context(patch(target, side_effect=AssertionError("forbidden hosted operation")))
            report = assembly.assemble(self.catalog, self.transport, output_dir=self.output, inputs=self.inputs)
        self.assertTrue(report["ok"], report)
        image = self.output / "output.appimage"
        self.assertEqual(image.read_bytes()[:self.template.stat().st_size], self.template.read_bytes())
        manifest = inspect(image)["manifest"]
        self.assertEqual(manifest["metadata"]["package_id"], "example-paper")
        self.assertEqual(report["compiled_sources"], [])
        with zipfile.ZipFile(self.output / "output.assets.zip") as archive:
            rows = archive.read(str(PAYLOAD / "decks.tsv")).decode().splitlines()
            self.assertEqual(rows[0].split("\t"), ["paper", "paper", "fr", "0", "0", "light", "An article"])
            self.assertEqual(rows[1].split("\t")[4:6], ["1", "light,dark"])
            metadata = json.loads(archive.read(str(PAYLOAD / "catalog.json")))
            self.assertEqual(metadata["project"]["audience"], "reviewers")
            self.assertEqual(metadata["decks"][0]["metadata"], {"arbitrary": [1, "data"]})
            self.assertNotIn(str(PAYLOAD / "decks/paper/paper-dark-live.pdf"), archive.namelist())
            self.assertEqual(archive.read(str(PAYLOAD / "project-title")), b"Independent paper\n")
        self.assertEqual(json.loads((self.output / "output.assembly.json").read_text()), report)

    def test_corrupt_transport_and_changed_project_stop_before_compilation(self):
        self.prepare()
        part = self.transport / "template.appimage.part-000"
        before = part.read_bytes()
        part.write_bytes(b"X" + before[1:])
        with patch.object(assembly, "build_deck", side_effect=AssertionError("must validate before building")):
            report = assembly.assemble(self.catalog, self.transport, output_dir=self.output, inputs=self.inputs)
        self.assertFalse(report["ok"])
        self.assertIn("part digest mismatch", report["error"])
        self.assertFalse((self.output / "output.appimage").exists())
        part.write_bytes(before)
        self.catalog_file.write_text(self.catalog_file.read_text() + "\n")
        with self.assertRaisesRegex(ValueError, "different project metadata"):
            assembly.restore_template(self.catalog, self.transport, self.root / "restored.appimage")

    def test_symlink_duplicate_and_out_of_order_transport_parts_are_rejected(self):
        self.prepare()
        config_file = self.transport / "config.json"
        original = json.loads(config_file.read_text())
        for change in (lambda config: config["template_parts"].reverse(),
                       lambda config: config["template_parts"].append(config["template_parts"][0])):
            config = json.loads(json.dumps(original))
            change(config)
            config_file.write_text(json.dumps(config))
            target = self.root / "restored.appimage"
            with self.assertRaises(ValueError):
                assembly.restore_template(self.catalog, self.transport, target)
            target.unlink(missing_ok=True)
        config_file.write_text(json.dumps(original))
        first = self.transport / original["template_parts"][0]["name"]
        outside = self.root / "preserved-part"
        first.rename(outside)
        first.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "expected regular file"):
            assembly.restore_template(self.catalog, self.transport, self.root / "restored.appimage")

    def test_receipt_pin_policy_and_existing_destinations_are_checked(self):
        for field in ("native_revision", "policy_sha256", "application_launcher_sha256"):
            info = {**self.info, field: "0" * len(self.info[field])}
            with self.subTest(field=field), self.assertRaises(ValueError):
                assembly.validate_template(self.catalog, info)
        self.transport.parent.mkdir(parents=True, exist_ok=True)
        missing = self.root / "preserved-missing"
        self.transport.symlink_to(missing)
        with self.assertRaisesRegex(ValueError, "Preserving existing"):
            self.prepare()
        self.assertTrue(self.transport.is_symlink())
        self.assertFalse(missing.exists())

    def test_artifacts_are_preserved_and_source_output_requires_explicit_host_option(self):
        self.prepare()
        self.output.mkdir()
        marker = self.output / "output.appimage"
        marker.symlink_to(self.root / "preserved-missing")
        with self.assertRaisesRegex(ValueError, "Preserving existing"):
            assembly.assemble(self.catalog, self.transport, output_dir=self.output, inputs=self.inputs)
        self.assertTrue(marker.is_symlink())
        with self.assertRaisesRegex(ValueError, "repository build"):
            assembly.assemble(self.catalog, self.transport, output_dir=self.project, inputs=self.inputs)
        report = assembly.assemble(self.catalog, self.transport, host_output_dir=self.project, inputs=self.inputs)
        self.assertTrue(report["ok"], report)
        self.assertTrue((self.project / "output.appimage").is_file())

    def test_external_documents_require_their_explicit_compiled_inputs(self):
        self.prepare(selection="paper")
        report = assembly.assemble(self.catalog, self.transport, output_dir=self.output)
        self.assertFalse(report["ok"])
        self.assertIn("declared project tool", report["error"])
        self.assertFalse((self.output / "output.appimage").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
