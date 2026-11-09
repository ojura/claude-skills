"""Composite geometric annotations for vector-authored derivations."""
from __future__ import annotations

import math

import numpy as np
from manim import Arc, Line, VGroup


class MarkedAngle(VGroup):
    """Keep an angle's zero direction, arc and semantic label in one moving group.

    The scene supplies typography, palette colours and label placement. The ray
    and arc share their origin and start direction, including after transforms.
    """

    def __init__(self, origin, start_angle, sweep, label, *, radius,
                 reference_length, label_offset, arc_color, reference_color,
                 arc_layer=5, reference_layer=2):
        if not 0 < radius < reference_length:
            raise ValueError("The zero-direction ray must extend beyond the angle arc")
        center = np.asarray(origin, dtype=float)
        direction = np.array([math.cos(start_angle), math.sin(start_angle), 0.0])
        self.reference = Line(center, center + reference_length * direction,
                              color=reference_color, stroke_width=1.5).set_z_index(reference_layer)
        self.arc = Arc(radius=radius, start_angle=start_angle, angle=sweep,
                       arc_center=center, color=arc_color, stroke_width=2).set_z_index(arc_layer)
        self.label = label.move_to(center + np.asarray(label_offset, dtype=float))
        super().__init__(self.reference, self.arc, self.label)
