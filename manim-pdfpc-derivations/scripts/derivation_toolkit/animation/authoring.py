"""Semantic text and vector-layout factories shared by authored derivations."""
from __future__ import annotations

from dataclasses import dataclass
import math
import re

import numpy as np
from manim import Arrow, Line, ORIGIN, TexTemplate, VGroup, VMobject

from .geometry import MarkedAngle
from .tex_semantics import SemanticMathTex, SemanticTex
from .vector_scene import VectorScene


def article_template(preamble: str | None = None) -> TexTemplate:
    """Use an explicit DVI-compatible figure font configuration."""
    return TexTemplate(
        documentclass=r"\documentclass{article}",
        preamble=preamble or r"\usepackage{amsmath}\usepackage{amssymb}\usepackage{lmodern}\pagestyle{empty}",
    )


@dataclass(frozen=True)
class SceneLayout:
    title_position: tuple[float, float, float]
    divider_start: tuple[float, float, float]
    divider_end: tuple[float, float, float]
    compact_size: tuple[float, float]
    angle_reference_length: float = 0.95

    @classmethod
    def for_canvas(cls, canvas):
        width, height = canvas.width / canvas.scale, canvas.height / canvas.scale
        return cls((0, height / 2 - .55, 0), (0, -.3 * height, 0),
                   (0, .3 * height, 0), (.45 * width, .45 * height))


def rotate_vector(point, angle):
    cosine, sine = math.cos(angle), math.sin(angle)
    return np.array([cosine * point[0] - sine * point[1],
                     sine * point[0] + cosine * point[1], point[2] if len(point) > 2 else 0.0])


def fit(mobject, width, height):
    """Fit existing vector geometry into a box without changing its proportions."""
    if width <= 0 or height <= 0:
        raise ValueError("A layout box must have positive width and height")
    if mobject.width > width:
        mobject.scale(width / mobject.width)
    if mobject.height > height:
        mobject.scale(height / mobject.height)
    return mobject


class AuthoringScene(VectorScene):
    """Supply typography and layout while keeping mathematics in the provider."""

    tex_template = None
    prose_command = "textsf"
    layout = None

    def __init__(self, palette, sample_rate=30):
        self.tex_template = self.tex_template or article_template()
        self.layout = self.layout or SceneLayout.for_canvas(self.canvas)
        super().__init__(palette, sample_rate)

    def math(self, value, size=32, color="foreground", position=None):
        item = SemanticMathTex(value, font_size=size, color=self.palette[color],
                               tex_template=self.tex_template).set_z_index(20)
        if position is not None:
            item.move_to(position)
        return item

    def prose(self, value, size=30, color="foreground", position=None, *, alert_terms=()):
        parts = ([part for part in re.split("(" + "|".join(map(re.escape, alert_terms)) + ")", value)
                  if part] if alert_terms else [value])
        if not re.fullmatch(r"[A-Za-z]+", self.prose_command):
            raise ValueError("prose_command must name a TeX text-family command")
        item = SemanticTex(*(f"\\{self.prose_command}{{{part}}}" for part in parts),
                           arg_separator="", font_size=size, color=self.palette[color],
                           tex_template=self.tex_template).set_z_index(25)
        for part, fragment in zip(parts, item):
            if part in alert_terms:
                fragment.set_color(self.palette["accent"])
        if position is not None:
            item.move_to(position)
        return item

    def arrow(self, start, end, color="foreground", width=3.5, opacity=1, layer=5,
              *, tip_length=.13, max_tip_ratio=.18, max_width_ratio=12):
        return Arrow(start, end, buff=0, color=self.palette[color], tip_length=tip_length,
                     stroke_width=width, max_tip_length_to_length_ratio=max_tip_ratio,
                     max_stroke_width_to_length_ratio=max_width_ratio).set_opacity(opacity).set_z_index(layer)

    def line(self, start, end, color="guide", width=2, opacity=1, layer=2):
        return Line(start, end, color=self.palette[color],
                    stroke_width=width).set_opacity(opacity).set_z_index(layer)

    def frame(self, origin, angle=0, length=1.0, color="muted", opacity=1, layer=3):
        origin = np.asarray(origin)
        return VGroup(
            self.arrow(origin, origin + rotate_vector([length, 0., 0.], angle),
                       color, 2.2, opacity, layer),
            self.arrow(origin, origin + rotate_vector([0., length, 0.], angle),
                       color, 2.2, opacity, layer),
        )

    def marked_angle(self, origin, start_angle, sweep, label, *, radius,
                     label_offset, color="foreground", layer=5, reference_length=None):
        return MarkedAngle(
            origin, start_angle, sweep, self.math(label, 27, color),
            radius=radius,
            reference_length=self.layout.angle_reference_length if reference_length is None else reference_length,
            label_offset=label_offset, arc_color=self.palette[color],
            reference_color=self.palette["guide"], arc_layer=layer,
        )

    def backdrop(self, question, *, title_position=None, divider=None):
        self.add(self.prose(question, size=31, position=(
            self.layout.title_position if title_position is None else title_position)))
        endpoints = divider or (self.layout.divider_start, self.layout.divider_end)
        self.divider = self.line(*endpoints, width=1, color="faint")
        self.add(self.divider)

    def compact(self, *mobjects, size=None, position=ORIGIN):
        group = VGroup(*(item.copy().clear_updaters() for item in mobjects))
        fit(group, *(size or self.layout.compact_size))
        return [group.move_to(position)]

    def brackets(self, left, right, center_y=0, height=1.5, *, inward=.14,
                 color="foreground", width=2.5, layer=10):
        half = height / 2
        result = VGroup()
        for x, direction in ((left, inward), (right, -inward)):
            item = VMobject(color=self.palette[color], stroke_width=width)
            item.set_points_as_corners([
                [x + direction, center_y + half, 0], [x, center_y + half, 0],
                [x, center_y - half, 0], [x + direction, center_y - half, 0],
            ])
            result.add(item)
        return result.set_z_index(layer)
