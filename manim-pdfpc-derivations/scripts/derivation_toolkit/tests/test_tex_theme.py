"""Check copied math against the actual PDF glyphs and unchanged page pixels."""
from __future__ import annotations

from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

import fitz


THEME = Path(__file__).resolve().parents[1] / "tex" / "theme.tex"
FORMULAS = r"""
\noindent\(\bigl(x\bigr)\quad\Bigl(x\Bigr)\quad
  \biggl(x\biggr)\quad\Biggl(x\Biggr)\)\par
\noindent\(\bigl[x\bigr]\quad\Bigl[x\Bigr]\quad
  \biggl[x\biggr]\quad\Biggl[x\Biggr]\)\par
\[S^n=\prod_{i=1}^n r_i,\qquad
  S=\left(\prod_{i=1}^n r_i\right)^{1/n}.\]
"""


def copied_text(path: Path) -> dict[str, str]:
    with fitz.open(path) as document:
        mupdf = "".join(page.get_text() for page in document)
    poppler = subprocess.check_output(["pdftotext", "-layout", str(path), "-"], text=True)
    # The upstream CMEX map records glyph size with Unicode variation selectors.
    return {name: re.sub(r"[\ufe00-\ufe0f]", "", text)
            for name, text in (("MuPDF", mupdf), ("Poppler", poppler))}


@unittest.skipUnless(shutil.which("pdflatex") and shutil.which("pdftotext"),
                     "The PDF text regression requires pdfTeX and Poppler")
class TexThemeText(unittest.TestCase):
    def test_sized_math_delimiters_copy_with_unchanged_geometry(self):
        with tempfile.TemporaryDirectory(prefix="derivation-tex-text-") as directory:
            root = Path(directory)
            baseline_theme = root / "theme-without-cmex-map.tex"
            baseline_theme.write_text(THEME.read_text().replace(
                r"\input{glyphtounicode-cmex.tex}", "", 1))
            (root / "palette.tex").write_text("\n".join(
                rf"\definecolor{{derivation{variant}{role}}}{{HTML}}{{{value}}}"
                for variant in ("Light", "Dark")
                for role, value in (("Background", "FFFFFF"), ("Foreground", "000000"),
                                    ("Accent", "224466"), ("Muted", "777777"),
                                    ("Guide", "999999"), ("Faint", "DDDDDD"))))
            for font, preamble in (("cm", ""), ("pagella", r"\usepackage{mathpazo,tgpagella}")):
                with self.subTest(font=font):
                    outputs = {}
                    for mapped in (False, True):
                        name = font + ("-mapped" if mapped else "-baseline")
                        theme_path = THEME if mapped else baseline_theme
                        theme = (rf"\def\DerivationRepositoryRoot{{{root.as_posix()}}}" + "\n" +
                                 rf"\input{{{theme_path.as_posix()}}}")
                        source = (r"\documentclass[a4paper]{article}" + "\n" +
                                  r"\usepackage[T1]{fontenc}\usepackage{amsmath}" + "\n" +
                                  preamble + "\n" + theme + "\n" +
                                  r"\pagestyle{empty}\begin{document}" + "\n" +
                                  FORMULAS + r"\end{document}" + "\n")
                        (root / (name + ".tex")).write_text(source)
                        result = subprocess.run(
                            ["pdflatex", "-interaction=nonstopmode", "-halt-on-error", name + ".tex"],
                            cwd=root, capture_output=True, text=True)
                        self.assertEqual(result.returncode, 0, result.stdout[-5000:])
                        outputs[mapped] = root / (name + ".pdf")
                    baseline = copied_text(outputs[False])
                    observed = copied_text(outputs[True])
                    for reader, text in observed.items():
                        with self.subTest(reader=reader):
                            self.assertEqual(text.count("("), 5, text)
                            self.assertEqual(text.count(")"), 5, text)
                            self.assertEqual(text.count("["), 4, text)
                            self.assertEqual(text.count("]"), 4, text)
                            self.assertNotIn("\ufffd", text)
                            self.assertEqual(text.count("∏"), 2, text)
                    self.assertTrue(any(text.count("(") != 5 or text.count(")") != 5
                                        for text in baseline.values()), baseline)
                    with fitz.open(outputs[False]) as before, fitz.open(outputs[True]) as after:
                        self.assertEqual(len(before), len(after))
                        for old, new in zip(before, after):
                            self.assertEqual(old.rect, new.rect)
                            self.assertEqual(old.get_pixmap(dpi=144).samples,
                                             new.get_pixmap(dpi=144).samples)


if __name__ == "__main__":
    unittest.main(verbosity=2)
