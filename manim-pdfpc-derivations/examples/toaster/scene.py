"""The geometric mean in the fictional toaster report's statistical appendix."""
from __future__ import annotations

import json
import math
from pathlib import Path

from manim import DOWN, RIGHT, FadeIn, FadeOut, VGroup

from derivation_toolkit.animation.authoring import AuthoringScene, article_template, fit
from derivation_toolkit.animation.layout import Canvas, HandoutLayout


TRIAL_RATIOS = tuple(json.loads(Path(__file__).with_name("trials.json").read_text())["ratios"])
if len(TRIAL_RATIOS) != 2 or any(not math.isfinite(value) or value <= 0 for value in TRIAL_RATIOS):
    raise ValueError("This two-trial example needs two finite positive ratios")
PRODUCT = math.prod(TRIAL_RATIOS)
SPEEDUP = PRODUCT ** (1 / len(TRIAL_RATIOS))
RATIO = r"r_i=\frac{b_i}{o_i}"
GEOMETRIC_MEAN = r"S=\left(\prod_{i=1}^{n}\frac{b_i}{o_i}\right)^{1/n}"
MEAN_LOG = r"\log S=\frac{1}{n}\sum_{i=1}^{n}\log\frac{b_i}{o_i}"
COMPACT_MEAN = r"S=\left(\prod_{i=1}^{n}r_i\right)^{1/n}"
COMPACT_LOG = r"\log S=\frac{1}{n}\sum_{i=1}^{n}\log r_i"
PANEL_CROP = [360, 210, 560, 300]

FIGURE_TEMPLATE = article_template(
    r"\usepackage[T1]{fontenc}\usepackage{mathpazo}"
    r"\usepackage{tgpagella}\usepackage{tgheros}"
    r"\usepackage{amsmath}\pagestyle{empty}"
)


class ToastSpeedup(AuthoringScene):
    scene_id = "geometric-mean"
    canvas = Canvas(width=1280, height=720, scale=100)
    handout_layout = HandoutLayout(columns=3, max_height_fraction=0.68, row_gap_mm=2)
    tex_template = FIGURE_TEMPLATE

    def panel(self, *formulae):
        group = VGroup(*(self.math(formula, 52) for formula in formulae))
        group.arrange(DOWN, buff=0.45)
        return self.compact(group, size=(5.1, 2.5))

    def construct(self):
        self.add(self.prose("A typical speedup for two trials", 36,
                            position=[0, 3.0, 0]))
        example_note = self.prose("Two illustrative trial ratios", 25, "muted",
                                  position=[0, -3.0, 0])
        self.add(example_note)

        values = [self.math(str(value), 54, "accent") for value in TRIAL_RATIOS]
        cards = []
        for index, (value, x) in enumerate(zip(values, (-3.1, 3.1)), 1):
            ratio = self.math(rf"r_{index}=\frac{{b_{index}}}{{o_{index}}}=", 40)
            row = VGroup(ratio, value).arrange(RIGHT, buff=0.13)
            label = self.prose(f"Trial {index}", 28, "muted")
            card = VGroup(label, row).arrange(DOWN, buff=0.3).move_to([x, 0.7, 0])
            cards.append(card)
        explanation = self.prose(
            "Divide each baseline pop time by its optimised pop time.", 28,
            position=[0, -1.4, 0])
        fit(explanation, 11.4, 0.7)
        self.add(*cards, explanation)
        self.checkpoint("Compare the trial ratios", "Compute each ratio", crop=PANEL_CROP,
                        handout=self.panel(*(rf"r_{i}={value}" for i, value in enumerate(TRIAL_RATIOS, 1))))

        self.wait(0.4)
        moving = [value.copy() for value in values]
        dot = self.math(r"\cdot", 48)
        equals = self.math("=", 48)
        total = self.math(str(PRODUCT), 54, "accent")
        target = VGroup(moving[0].copy(), dot, moving[1].copy(), equals, total)
        target.arrange(RIGHT, buff=0.18).move_to([0, 0.5, 0])
        self.add(*moving)
        self.play(*(value.animate.move_to(target[position])
                    for value, position in zip(moving, (0, 2))),
                  *(card.animate.set_opacity(0.2) for card in cards),
                  FadeOut(explanation), run_time=1.6)
        multiply_note = self.prose("Multiply the trial ratios.", 30,
                                   position=[0, -1.3, 0])
        self.play(FadeIn(dot), FadeIn(equals), FadeIn(total), FadeIn(multiply_note), run_time=0.8)
        self.wait(0.4)
        product_tex = rf"{TRIAL_RATIOS[0]}\cdot{TRIAL_RATIOS[1]}={PRODUCT}"
        self.checkpoint("Multiply the ratios", "Multiply the ratios", crop=PANEL_CROP,
                        handout=self.panel(product_tex), pause=False)

        product = VGroup(*moving, dot, equals, total)
        power = self.math(rf"S^2={PRODUCT}", 52, position=[0, 0.7, 0])
        equal_note = self.prose("One equal factor per trial must give the same product.",
                                28, position=[0, -1.7, 0])
        fit(equal_note, 11.4, 0.7)
        self.play(*(FadeOut(card) for card in cards), FadeOut(product), FadeOut(multiply_note),
                  FadeIn(power), FadeIn(equal_note), run_time=0.9)
        root_tex = rf"S=\sqrt{{{PRODUCT}}}={SPEEDUP:g}"
        root = self.math(root_tex, 56, "accent", [0, -0.4, 0])
        self.play(FadeIn(root), run_time=1.1)
        self.wait(0.6)
        self.checkpoint("Find the equal per-trial factor", "Take one factor per trial",
                        crop=PANEL_CROP, handout=self.panel(rf"S^2={PRODUCT}", root_tex))

        log_tex = (rf"\log S=\frac{{\log {TRIAL_RATIOS[0]}+\log {TRIAL_RATIOS[1]}}}{{2}}"
                   rf"=\log {SPEEDUP:g}")
        logarithms = self.math(log_tex, 48, position=[0, 0.4, 0])
        fit(logarithms, 11.3, 1.5)
        log_note = self.prose("Products become sums on a log scale.", 30,
                              position=[0, -1.3, 0])
        self.play(FadeOut(power), FadeOut(root), FadeOut(equal_note),
                  FadeIn(logarithms), FadeIn(log_note), run_time=1.25)
        self.wait(0.4)
        self.checkpoint("Average the log ratios", "Average on a log scale", crop=PANEL_CROP,
                        handout=self.panel(COMPACT_LOG), pause=False)

        general = self.math(GEOMETRIC_MEAN, 48, "accent", [0, 0.9, 0])
        mean_log = self.math(MEAN_LOG, 43, position=[0, -0.45, 0])
        for formula in (general, mean_log):
            fit(formula, 11.4, 1.5)
        definitions = self.prose(r"$b_i$: baseline pop time; $o_i$: optimised pop time.",
                                 27, "muted", [0, -2.0, 0])
        fit(definitions, 11.4, 0.7)
        condition = self.prose("Use positive trial times.", 25, "muted", [0, -3.0, 0])
        self.play(FadeOut(logarithms), FadeOut(log_note), FadeOut(example_note),
                  FadeIn(general), run_time=0.9)
        self.play(FadeIn(mean_log), FadeIn(definitions), FadeIn(condition), run_time=1.25)
        self.wait(0.65)
        self.checkpoint("Generalise to any positive trial ratios", "Generalise to n trials",
                        crop=PANEL_CROP, handout=self.panel(COMPACT_MEAN, RATIO))


SCENES = [ToastSpeedup]
REFERENCE_TIMES = {"geometric-mean": 3.5}
RESULTS = {"geometric-mean": [GEOMETRIC_MEAN.replace("=", " &= ", 1),
                             MEAN_LOG.replace("=", " &= ", 1)],
           "toaster-example": [rf"S=\sqrt{{{TRIAL_RATIOS[0]}\cdot{TRIAL_RATIOS[1]}}}={SPEEDUP:g}"]}
