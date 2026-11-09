"""Explicit test typography, colours and output paths; no project discovery."""
from __future__ import annotations

import os
from pathlib import Path

from manim import config
from ..animation.authoring import article_template
from ..animation.layout import DEFAULT_CANVAS

TEMPLATE = article_template()
PALETTES = {
    "light": {"background": "#FFFFFF", "foreground": "#202124", "accent": "#B72126",
              "muted": "#72777F", "guide": "#A6ADB7", "faint": "#E1E4E8"},
    "dark": {"background": "#17191D", "foreground": "#F0F1F3", "accent": "#EF6C70",
             "muted": "#A3A9B3", "guide": "#737D8D", "faint": "#3D444F"},
}


def configure(theme, *, canvas=DEFAULT_CANVAS):
    folder = Path(os.environ.get("DERIVATION_TEST_BUILD", Path.cwd() / "build/test-toolkit"))
    (folder / "media/tex").mkdir(parents=True, exist_ok=True)
    config.media_dir = str(folder / "media")
    config.tex_dir = str(folder / "media/tex")
    canvas.configure()
    config.background_color = PALETTES[theme]["background"]
    config.write_to_movie = False
    config.save_last_frame = False
    config.dry_run = True
    config.verbosity = "ERROR"
