"""Exercise the managed workflow's real HTML build and its advertised output paths."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest
import zipfile

import fitz


class HtmlWorkflow(unittest.TestCase):
    def test_managed_build_produces_the_browser_input_and_upload_archive(self):
        toolkit = Path(__file__).resolve().parents[1]
        workflow = (toolkit / "ci/github-actions.yml").read_text()
        step = workflow.split("      - name: Build and exercise the exact standalone HTML\n", 1)[1]
        script = textwrap.dedent(step.split("        run: |\n", 1)[1].split("      - name:", 1)[0])
        build_script = script.split("node --test", 1)[0]
        browser_path = re.search(r'--html ("[^\n]+")\s*\\', script).group(1)
        archive_check = next(line for line in script.splitlines() if line.startswith("test -s "))
        upload_path = workflow.split("      - name: Upload the standalone browser presentation\n", 1)[1]
        upload_path = re.search(r"(?m)^          path: (.+)$", upload_path).group(1)

        with tempfile.TemporaryDirectory(prefix="managed HTML workflow ") as temporary:
            root = Path(temporary)
            project = root / "consumer"
            assets = root / "runner/derivation-assets"
            project.mkdir()
            assets.mkdir(parents=True)
            (project / "presentations.json").write_text(json.dumps({
                "version": 2, "default": "article", "decks": [{
                    "id": "article", "title": "Article fixture", "source": "article.pdf",
                    "build": {"kind": "external"},
                }],
            }))
            installed = project / "tools/vendor/manim_pdfpc/derivation_toolkit"
            installed.parent.mkdir(parents=True)
            installed.symlink_to(toolkit, target_is_directory=True)
            vendor = project / "build/tools/html5/node_modules/pdfjs-dist"
            (vendor / "build").mkdir(parents=True)
            (vendor / "package.json").write_text('{"version":"5.1.91"}')
            for name in ("pdf.min.mjs", "pdf.worker.min.mjs"):
                (vendor / "build" / name).write_text("export {};\n")
            (vendor / "LICENSE").write_text("Fixture vendor license\n")
            pdf = assets / "article.pdf"
            with fitz.open() as document:
                page = document.new_page(width=200, height=300)
                page.insert_text((20, 30), "Real PDF in the managed workflow")
                document.save(pdf)
            (assets / "inputs.json").write_text(json.dumps({
                "article": {"light": {"pdf": "/assets/article.pdf"}},
            }))
            environment = dict(os.environ, GITHUB_WORKSPACE=str(root), PROJECT="consumer",
                               RUNNER_TEMP=str(root / "runner"), HTML_DECK="article",
                               PACKAGE_INPUTS="inputs.json", DERIVATION_CACHE_ROOT="",
                               DERIVATION_BUILD_DIR="", PYTHONDONTWRITEBYTECODE="1")
            environment["PATH"] = str(Path(sys.executable).parent) + os.pathsep + environment["PATH"]
            result = subprocess.run(["bash", "-euc", build_script + "\n" +
                                     "test -s " + browser_path + "\n" + archive_check],
                                    cwd=root, env=environment, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            pattern = upload_path.replace("${{ runner.temp }}", str(root / "runner"))
            archives = list(Path(pattern).parent.glob(Path(pattern).name))
            self.assertEqual(len(archives), 1, "The upload glob must match the built archive")
            with zipfile.ZipFile(archives[0]) as archive:
                indexes = [name for name in archive.namelist() if name.endswith("/index.html")]
                self.assertEqual(len(indexes), 1)
                self.assertEqual(archive.read(indexes[0]),
                                 (archives[0].with_suffix("") / "index.html").read_bytes())


if __name__ == "__main__":
    unittest.main(verbosity=2)
