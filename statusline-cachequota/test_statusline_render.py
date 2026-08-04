import importlib.util
import os
import re
import unittest
from pathlib import Path


os.environ.pop("NO_COLOR", None)
os.environ["CLAUDE_STATUSLINE_COLOR"] = "always"
os.environ["CLAUDE_STATUSLINE_THEME"] = "dark"

MODULE_PATH = Path(__file__).with_name("statusline-render.py")
SPEC = importlib.util.spec_from_file_location("statusline_render", MODULE_PATH)
statusline = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(statusline)

ANSI_RE = re.compile(r"\x1b\[([0-9;]*)m")
MIN_TEXT_CONTRAST = 4.5


def ansi_cells(text):
    cells = []
    fg = bg = None
    pos = 0
    for match in ANSI_RE.finditer(text):
        cells.extend((char, fg, bg) for char in text[pos:match.start()])
        codes = [int(value or 0) for value in match.group(1).split(";")]
        i = 0
        while i < len(codes):
            code = codes[i]
            if code == 0:
                fg = bg = None
                i += 1
            elif code in (38, 48) and codes[i + 1] == 2:
                color = tuple(codes[i + 2:i + 5])
                if code == 38:
                    fg = color
                else:
                    bg = color
                i += 5
            else:
                i += 1
        pos = match.end()
    cells.extend((char, fg, bg) for char in text[pos:])
    return cells


def relative_luminance(rgb):
    def linear(channel):
        channel /= 255
        return (channel / 12.92 if channel <= 0.04045
                else ((channel + 0.055) / 1.055) ** 2.4)

    r, g, b = (linear(channel) for channel in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(a, b):
    lighter, darker = sorted((relative_luminance(a), relative_luminance(b)),
                             reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


class LabeledBarContrastTest(unittest.TestCase):
    def test_text_meets_wcag_aa_at_every_percentage(self):
        label = statusline.format_context_label(245_000, 372_000)
        self.assertEqual(len(label), statusline.BAR_LABEL_WIDTH)

        for pct in range(101):
            cells = ansi_cells(statusline.make_bar(pct, label=label))
            self.assertEqual(len(cells), statusline.LABELED_BAR_WIDTH)
            self.assertEqual("".join(char for char, _, _ in cells[1:-1]), label)

            for index, (char, fg, bg) in enumerate(cells[1:-1]):
                with self.subTest(pct=pct, index=index, char=char):
                    self.assertIsNotNone(fg)
                    self.assertIsNotNone(bg)
                    ratio = contrast_ratio(fg, bg)
                    self.assertGreaterEqual(
                        ratio,
                        MIN_TEXT_CONTRAST,
                        "%d%% cell %d (%r) contrast %.2f:1 for %r on %r"
                        % (pct, index, char, ratio, fg, bg),
                    )


if __name__ == "__main__":
    unittest.main()
