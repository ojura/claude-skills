"""Check copied math, graphic backing and overlays in actual compiled PDFs."""
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


@unittest.skipUnless(shutil.which("pdflatex"), "The graphics regression requires pdfTeX")
class TexThemeGraphics(unittest.TestCase):
    def check_graphics(self, document_class: str):
        with tempfile.TemporaryDirectory(prefix="derivation-tex-graphics-") as directory:
            root = Path(directory)
            backgrounds = {"light": (221, 229, 238), "dark": (23, 32, 42)}
            (root / "palette.tex").write_text("\n".join(
                rf"\definecolor{{derivation{theme.title()}{role}}}{{HTML}}{{{value}}}"
                for theme, background in backgrounds.items()
                for role, value in (("Background", "".join(f"{x:02X}" for x in background)),
                                    ("Foreground", "000000" if theme == "light" else "FFFFFF"),
                                    ("Accent", "B72126"), ("Muted", "777777"),
                                    ("Guide", "999999"), ("Faint", "DDDDDD"))))
            # The empty part of this PDF is transparent, not painted white.
            with fitz.open() as graphic:
                page = graphic.new_page(width=20, height=20)
                page.draw_rect(fitz.Rect(8, 8, 12, 12), color=None, fill=(0, 0, 0))
                graphic.save(root / "graphic.pdf")
            beamer = document_class == "beamer"
            overlay = "<2->" if beamer else ""
            picture = (r"\begin{tikzpicture}[remember picture,overlay]" + "\n" +
                       r"\node[inner sep=0,outer sep=0,anchor=north west] at "
                       r"([xshift=20bp,yshift=-20bp]current page.north west) {" +
                       rf"\DerivationWhiteBackgroundGraphics{overlay}[width=40bp]{{graphic.pdf}}" + "};\n" +
                       r"\node[inner sep=0,outer sep=0,anchor=north west] at "
                       r"([xshift=80bp,yshift=-20bp]current page.north west) {" +
                       rf"\DerivationUncoverGraphics{overlay}[width=40bp]{{graphic.pdf}}" + "};\n" +
                       r"\end{tikzpicture}\mbox{}")
            for theme, background in backgrounds.items():
                with self.subTest(document_class=document_class, theme=theme):
                    source = (rf"\documentclass{{{document_class}}}" + "\n" +
                              rf"\def\DerivationTheme{{{theme}}}" + "\n" +
                              rf"\def\DerivationRepositoryRoot{{{root.as_posix()}}}" + "\n" +
                              rf"\input{{{THEME.as_posix()}}}" + "\n" +
                              r"\pagestyle{empty}\pagecolor{derivationBackground}" + "\n" +
                              r"\begin{document}" + "\n" +
                              (r"\begin{frame}[plain]" if beamer else "") + picture +
                              (r"\end{frame}" if beamer else "") +
                              r"\end{document}" + "\n")
                    (root / "fixture.tex").write_text(source)
                    for _ in range(2):
                        result = subprocess.run(
                            ["pdflatex", "-interaction=nonstopmode", "-halt-on-error", "fixture.tex"],
                            cwd=root, capture_output=True, text=True)
                        self.assertEqual(result.returncode, 0, result.stdout[-5000:])
                    with fitz.open(root / "fixture.pdf") as document:
                        self.assertEqual(len(document), 2 if beamer else 1)
                        for number, page in enumerate(document):
                            image = page.get_pixmap(dpi=144, alpha=False)

                            def pixel(x, y):
                                return image.pixel(round(x * 2), round(y * 2))[:3]

                            def near(actual, expected):
                                self.assertTrue(all(abs(a - e) <= 3 for a, e in zip(actual, expected)),
                                                (document_class, theme, number, actual, expected))

                            backing = (255, 255, 255) if theme == "dark" else background
                            near(pixel(15, 15), background)
                            if beamer and number == 0:
                                near(pixel(24, 24), background)
                                near(pixel(40, 40), background)
                                near(pixel(84, 24), tuple(.16 * a + .84 * b
                                                        for a, b in zip(backing, background)))
                                near(pixel(100, 40), tuple(.84 * x for x in background))
                            else:
                                near(pixel(24, 24), backing)
                                near(pixel(84, 24), backing)
                                near(pixel(40, 40), (0, 0, 0))
                                near(pixel(100, 40), (0, 0, 0))
                                # The backing has no padding and graphics keep their requested size.
                                near(pixel(19, 24), background)
                                near(pixel(61, 24), background)

    def test_article_graphics_backing_in_both_themes(self):
        self.check_graphics("article")

    def test_beamer_graphics_reveal_and_fade_in_both_themes(self):
        self.check_graphics("beamer")


if __name__ == "__main__":
    unittest.main(verbosity=2)
