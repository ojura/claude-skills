"""Keep a visible sequence of teaching descriptions with its checkpoint labels."""
from __future__ import annotations

from manim import FadeIn, FadeOut


class StepDescriptions:
    """Reveal each declared description, retain earlier ones in gray, then clear them.

    The caller supplies a text factory so typography and placement remain with
    the scene. Checkpoint labels come from the same strings as the painted text.
    A missing, transparent or skipped description fails during authoring.
    """

    def __init__(self, scene, labels, make_text):
        self.scene = scene
        self.labels = tuple(labels)
        if not self.labels or any(not label.strip() for label in self.labels):
            raise ValueError("A description sequence needs nonempty step labels")
        self.items = tuple(make_text(label, index) for index, label in enumerate(self.labels))
        self.index = -1
        self.finished = False

    @property
    def label(self):
        if self.index < 0:
            raise ValueError("Begin the visible descriptions before using their label")
        return self.labels[self.index]

    def require_visible(self):
        if self.index < 0 or self.finished:
            raise ValueError("No visible description sequence is active")
        active = {id(leaf) for item in self.scene.mobjects
                  for leaf in item.family_members_with_points()}
        for index, item in enumerate(self.items[:self.index + 1]):
            leaves = item.family_members_with_points()
            if not leaves or any(
                    id(leaf) not in active or
                    (float(leaf.get_fill_opacity()) < 0.99 and
                     (float(leaf.get_stroke_opacity()) < 0.99 or leaf.get_stroke_width() <= 0))
                    for leaf in leaves):
                raise ValueError("Teaching description is missing or transparent: " + self.labels[index])

    def begin(self):
        if self.index != -1:
            raise ValueError("A description sequence can begin only once")
        self.index = 0
        self.scene.add(self.items[0])
        self.require_visible()

    def advance(self, *animations, **options):
        self.require_visible()
        if self.index + 1 >= len(self.items):
            raise ValueError("Every teaching description has already appeared")
        next_index = self.index + 1
        self.scene.play(
            self.items[self.index].animate.set_color(self.scene.palette["muted"]),
            FadeIn(self.items[next_index]), *animations, **options)
        self.index = next_index
        self.require_visible()

    def checkpoint(self, caption=None, **options):
        self.require_visible()
        self.scene.checkpoint(self.label, self.label if caption is None else caption, **options)

    def finish(self, *animations, **options):
        self.require_visible()
        if self.index != len(self.items) - 1:
            raise ValueError("Every declared teaching description must appear before the result")
        self.scene.play(*(FadeOut(item) for item in self.items), *animations, **options)
        self.finished = True
