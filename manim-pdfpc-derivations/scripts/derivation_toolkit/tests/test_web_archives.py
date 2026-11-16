"""Distinct HTML outputs for one deck must retain their own downloadable archives."""
from contextlib import redirect_stdout
from concurrent.futures import ThreadPoolExecutor
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import zipfile

import fitz

from ..catalog import Catalog
from ..verify.browser_fixture import load_fixture
from ..web import build as web_build
from ..web.build import build_bundle


class HtmlArchives(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="HTML archives ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.project = self.root / "project"
        self.project.mkdir()
        (self.project / "presentations.json").write_text(json.dumps({
            "version": 2, "default": "article", "decks": [{
                "id": "article", "title": "Independent article",
                "source": "article.pdf", "build": {"kind": "external"},
            }],
        }))
        self.catalog = Catalog(self.project)
        self.vendor = self.root / "pdfjs"
        (self.vendor / "build").mkdir(parents=True)
        (self.vendor / "package.json").write_text('{"version":"5.1.91"}')
        # This test exercises real PDF packaging and archive identity. The
        # browser acceptance gate exercises the actual PDF.js implementation.
        for name in ("pdf.min.mjs", "pdf.worker.min.mjs"):
            (self.vendor / "build" / name).write_text("export {};\n")
        (self.vendor / "LICENSE").write_text("Fixture vendor license\n")
        self.complete = self.document("complete", 2)
        self.excerpt = self.document("excerpt", 1)

    def document(self, name, count):
        target = self.root / f"{name}.pdf"
        with fitz.open() as document:
            for number in range(count):
                page = document.new_page(width=200, height=300)
                page.insert_text((20, 30), f"{name} page {number + 1}")
            document.save(target)
        return target

    def build(self, pdf, output=None, **options):
        with patch.dict("os.environ", {"DERIVATION_BUILD_DIR": ""}), \
                redirect_stdout(io.StringIO()):
            return build_bundle(
                self.catalog, self.catalog.get(), output=output, pdf=pdf,
                pdfjs_dist=self.vendor, themes=("light",),
                cache_root=self.project / "build",
                **options,
            )

    def assert_archive(self, archive, name, html, page_count):
        with zipfile.ZipFile(archive) as bundle:
            self.assertEqual(bundle.read(f"{name}/index.html"), html.read_bytes())
            self.assertTrue(all(member.startswith(name + "/")
                                for member in bundle.namelist()))
        pages = load_fixture(html)["manifest"]["themes"]["light"]["pages"]
        self.assertEqual(len(pages), page_count)

    def test_explicit_outputs_for_one_deck_keep_both_archives(self):
        delivery = self.root / "delivery"
        complete = self.build(self.complete, delivery / "article-html5")
        complete_zip = delivery / "article-html5.zip"
        accepted_archive = complete_zip.read_bytes()
        excerpt = self.build(self.excerpt, delivery / "one-page-html5")
        self.assertEqual(complete_zip.read_bytes(), accepted_archive,
                         "Building an excerpt replaced the complete presentation ZIP")
        self.assert_archive(complete_zip, "article-html5", complete, 2)
        self.assert_archive(delivery / "one-page-html5.zip", "one-page-html5", excerpt, 1)

    def test_default_output_keeps_the_deck_archive_name(self):
        html = self.build(self.complete)
        self.assertEqual(html.parent.name, "html5")
        self.assert_archive(html.parent.parent / "article-html5.zip", "article-html5", html, 2)

    def accepted_delivery(self, name="article-html5"):
        output = self.root / "delivery" / name
        self.build(self.complete, output)
        (output / "lecturer-notes.txt").write_text("Keep these authored notes.\n")
        (output / "notes").mkdir()
        (output / "notes/next-course.txt").write_text("A separate author's file.\n")
        return output

    def delivery_bytes(self):
        root = self.root / "delivery"
        return {str(path.relative_to(root)): path.read_bytes()
                for path in root.rglob("*") if path.is_file()}

    def assert_delivery_preserved(self, previous):
        current = self.delivery_bytes()
        self.assertEqual(set(current), set(previous))
        for name, content in previous.items():
            self.assertTrue(current[name] == content, "Delivered bytes changed: " + name)

    def test_zip_construction_failure_preserves_the_previous_complete_delivery(self):
        output = self.accepted_delivery()
        before = self.delivery_bytes()
        with patch.object(zipfile.ZipFile, "write", side_effect=OSError("ZIP construction failed")):
            with self.assertRaisesRegex(OSError, "ZIP construction failed"):
                self.build(self.excerpt, output)
        self.assert_delivery_preserved(before)

    def test_zip_publication_failure_or_interrupt_restores_html_and_archive(self):
        for exception in (OSError, KeyboardInterrupt):
            with self.subTest(exception=exception.__name__):
                output = self.accepted_delivery(exception.__name__ + "-html5")
                archive = output.with_suffix(".zip")
                before = self.delivery_bytes()
                replace = os.replace
                injected = False

                def fail_zip(source, destination):
                    nonlocal injected
                    if Path(destination) == archive and not injected:
                        injected = True
                        raise exception("ZIP publication failed")
                    return replace(source, destination)

                with patch.object(web_build.os, "replace", side_effect=fail_zip):
                    with self.assertRaises(exception):
                        self.build(self.excerpt, output)
                self.assertTrue(injected)
                self.assert_delivery_preserved(before)

    def test_successful_update_preserves_unrelated_files_and_archives_only_the_bundle(self):
        output = self.accepted_delivery()
        outside = self.root / "private-notes.txt"
        outside.write_text("Do not include this file in a downloadable presentation.\n")
        (output / "private-link").symlink_to(outside)
        html = self.build(self.excerpt, output)
        self.assertEqual((output / "lecturer-notes.txt").read_text(), "Keep these authored notes.\n")
        self.assertEqual((output / "notes/next-course.txt").read_text(), "A separate author's file.\n")
        self.assertTrue((output / "private-link").is_symlink())
        archive = output.with_suffix(".zip")
        self.assert_archive(archive, output.name, html, 1)
        with zipfile.ZipFile(archive) as bundle:
            self.assertEqual(set(bundle.namelist()), {output.name + "/" + name for name in
                             ("index.html", "README.txt", "PDFJS-LICENSE.txt", ".derivation-html5")})

    def test_no_zip_update_preserves_an_existing_archive_and_unrelated_files(self):
        output = self.accepted_delivery()
        archive = output.with_suffix(".zip")
        before = archive.read_bytes()
        html = self.build(self.excerpt, output, archive=False)
        self.assertEqual(archive.read_bytes(), before)
        self.assertTrue((output / "lecturer-notes.txt").is_file())
        self.assertEqual(len(load_fixture(html)["manifest"]["themes"]["light"]["pages"]), 1)

    def test_failed_initial_archive_does_not_publish_a_partial_delivery(self):
        output = self.root / "delivery/new-html5"
        with patch.object(zipfile.ZipFile, "write", side_effect=OSError("ZIP construction failed")):
            with self.assertRaises(OSError):
                self.build(self.complete, output)
        self.assertFalse(output.exists())
        self.assertFalse(output.with_suffix(".zip").exists())
        self.assertFalse(any((self.root / "delivery").glob("*.part")))

    def test_failure_replacing_html_or_license_restores_the_previous_set(self):
        for name in ("index.html", "PDFJS-LICENSE.txt", "README.txt"):
            with self.subTest(file=name):
                output = self.accepted_delivery(name.replace(".", "-") + "-html5")
                before = self.delivery_bytes()
                replace = os.replace
                injected = False

                def fail_one(source, destination):
                    nonlocal injected
                    if Path(destination) == output / name and not injected:
                        injected = True
                        raise OSError("owned-file replacement failed")
                    return replace(source, destination)

                with patch.object(web_build.os, "replace", side_effect=fail_one):
                    with self.assertRaisesRegex(OSError, "owned-file replacement failed"):
                        self.build(self.excerpt, output)
                self.assertTrue(injected)
                self.assert_delivery_preserved(before)

    def test_failed_initial_final_publication_removes_the_partial_bundle(self):
        output = self.root / "delivery/new-html5"
        archive = output.with_suffix(".zip")
        replace = os.replace
        injected = False

        def fail_zip(source, destination):
            nonlocal injected
            if Path(destination) == archive and not injected:
                injected = True
                raise OSError("initial final publication failed")
            return replace(source, destination)

        with patch.object(web_build.os, "replace", side_effect=fail_zip):
            with self.assertRaises(OSError):
                self.build(self.complete, output)
        self.assertTrue(injected)
        self.assertFalse(output.exists())
        self.assertFalse(archive.exists())
        self.assertFalse(any((self.root / "delivery").glob(".derivation-publish-*")))

    def test_two_writers_serialize_the_complete_html_archive_set(self):
        output = self.root / "delivery/shared-html5"
        output.parent.mkdir()
        stages = []
        for number in (1, 2):
            stage = self.root / f"prepared-{number}" / output.name
            stage.mkdir(parents=True)
            for name in web_build.BUNDLE_FILES:
                (stage / name).write_text(f"Writer {number}: {name}\n")
            stages.append((stage, web_build.pack_source(stage, output.name)))
        first_inside, second_waiting, release = threading.Event(), threading.Event(), threading.Event()
        owner, guard = {}, threading.Lock()
        real_publish, real_flock = web_build.publish_files, web_build.fcntl.flock
        order = []

        def flock(descriptor, operation):
            if threading.get_ident() != owner.get("first"):
                second_waiting.set()
            return real_flock(descriptor, operation)

        def publish(replacements, *args, **kwargs):
            with guard:
                order.append(threading.get_ident())
            if threading.get_ident() == owner["first"]:
                first_inside.set()
                if not release.wait(10):
                    raise AssertionError("Test did not release the first publisher")
            return real_publish(replacements, *args, **kwargs)

        def first():
            owner["first"] = threading.get_ident()
            web_build.publish_bundle(stages[0][0], output, stages[0][1])

        with patch.object(web_build.fcntl, "flock", side_effect=flock), \
                patch.object(web_build, "publish_files", side_effect=publish), \
                ThreadPoolExecutor(max_workers=2) as workers:
            one = workers.submit(first)
            try:
                self.assertTrue(first_inside.wait(10))
                two = workers.submit(web_build.publish_bundle, stages[1][0], output, stages[1][1])
                self.assertTrue(second_waiting.wait(10))
                self.assertEqual(order, [owner["first"]])
            finally:
                release.set()
            one.result(timeout=10)
            two.result(timeout=10)
        self.assertEqual(len(order), 2)
        with zipfile.ZipFile(output.with_suffix(".zip")) as archive:
            for name in web_build.BUNDLE_FILES:
                self.assertEqual((output / name).read_bytes(), (stages[1][0] / name).read_bytes())
                self.assertEqual(archive.read(output.name + "/" + name), (output / name).read_bytes())


if __name__ == "__main__":
    unittest.main(verbosity=2)
