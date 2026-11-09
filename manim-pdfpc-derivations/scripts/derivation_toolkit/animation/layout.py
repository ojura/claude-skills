"""Authored canvas coordinates and printable-panel layout."""
from __future__ import annotations

from dataclasses import dataclass
import math

import numpy as np


@dataclass(frozen=True)
class Canvas:
    width: int = 1200
    height: int = 650
    scale: float = 100.0

    def __post_init__(self):
        if (type(self.width) is not int or type(self.height) is not int
                or min(self.width, self.height) <= 0
                or not math.isfinite(self.scale) or self.scale <= 0):
            raise ValueError("Canvas dimensions must be positive integers with a finite positive scale")

    @property
    def crop(self) -> list[float]:
        return [0, 0, self.width, self.height]

    def points(self, points):
        """Map both visible paths and semantic glyph metrics to this canvas."""
        result = np.asarray(points, dtype=float).copy()
        result[..., 0] = self.width / 2 + self.scale * result[..., 0]
        result[..., 1] = self.height / 2 - self.scale * result[..., 1]
        return result

    def configure(self):
        """Give Manim the same dimensions used by the vector and text recorder."""
        from manim import config
        config.frame_width = self.width / self.scale
        config.frame_height = self.height / self.scale
        config.pixel_width = self.width
        config.pixel_height = self.height


@dataclass(frozen=True)
class HandoutLayout:
    columns: int = 2
    max_height_fraction: float = 0.65
    row_gap_mm: float = 1.0

    def __post_init__(self):
        if type(self.columns) is not int or not 1 <= self.columns <= 10:
            raise ValueError("A handout needs between one and ten columns")
        if not math.isfinite(self.max_height_fraction) or not 0 < self.max_height_fraction <= 1:
            raise ValueError("Handout height fraction must be in (0, 1]")
        if not math.isfinite(self.row_gap_mm) or self.row_gap_mm < 0:
            raise ValueError("Handout row gap must be finite and nonnegative")


DEFAULT_CANVAS = Canvas()
