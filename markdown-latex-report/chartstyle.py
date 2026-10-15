"""House style for the charts of a report built with markdown-latex-report.

Use it from the report's plot script:

    import sys; sys.path.insert(0, "/path/to/markdown-latex-report")   # or copy this file beside the script
    import chartstyle as cs
    cs.apply()
    fig, ax = cs.figure(height=3.2)
    ax.plot(x, y, color=cs.PALETTE[0], label="raw serving")
    cs.style(ax, grid_axis="y")
    ax.set_xlabel("time after the stamp, s")
    cs.save(fig, "lag_over_time", outdir)          # writes lag_over_time.png and .pdf

Why these sizes: the report sets a figure at 90 percent of its text width. A4
with the preamble's margins gives a text width of 6.47 in, so a figure drawn
6.3 in wide prints at a scale of 0.92; 9 pt text then prints at 8.3 pt and
9.5 pt at 8.8 pt, close to the footnote size of the report. Smaller sizes
print too small to read beside 10 pt body text; a wider figure shrinks more.
Draw every figure WIDTH wide and choose the height; do not scale a wider
figure down.

Rules that the review of real reports produced:
  * every text in the figure is at least FONT (9 pt); panel titles and axis
    labels at LABEL (9.5 pt); nothing smaller;
  * legend and axis labels use the words of the report, not the names of the
    data columns (write "served-pose age at the poll, ms", not "a_ms");
  * one palette for one meaning across all figures (the first color for the
    same series everywhere);
  * a scatter with thousands of points is rasterized at 300 dpi
    (ax.scatter(..., rasterized=True)) so that the PDF stays small and
    searchable text stays vector; everything else stays vector;
  * save PNG and PDF with the same basename, PNG at 150 dpi for the Markdown
    and the PDF for the report (filter.lua swaps the .png for the .pdf);
  * the data a figure draws lives beside the script in a committed file with
    a header that names its source, so that the figure can be redrawn later.
"""
import os

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

WIDTH = 6.3          # inches; the report scales it to 90 percent of a 6.47 in text width
FONT = 9             # tick labels, legends, annotations
LABEL = 9.5          # axis labels and panel titles

SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK2 = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
BASE = "#c3c2b7"
PALETTE = ["#2a78d6", "#eb6834", "#1baf7a", "#8a5cd6", "#c99a1e", "#5d7a8c"]


def apply():
    """Set matplotlib's defaults to the house style. Call once, before drawing."""
    plt.rcParams.update({
        "font.family": "DejaVu Sans",     # TrueType; the OpenType TeX Gyre fonts embed as CID fonts that poppler flags
        "font.size": FONT,
        "axes.titlesize": LABEL,
        "axes.labelsize": LABEL,
        "legend.fontsize": FONT,
        "xtick.labelsize": FONT,
        "ytick.labelsize": FONT,
        "axes.edgecolor": BASE,
        "axes.labelcolor": INK2,
        "axes.facecolor": SURFACE,
        "figure.facecolor": SURFACE,
        "savefig.facecolor": SURFACE,
        "xtick.color": MUTED,
        "ytick.color": MUTED,
        "xtick.labelcolor": INK2,
        "ytick.labelcolor": INK2,
        "axes.grid": True,
        "grid.color": GRID,
        "grid.linewidth": 0.6,
        "grid.linestyle": "-",
        "legend.frameon": False,
        "axes.prop_cycle": matplotlib.cycler(color=PALETTE),
        "pdf.fonttype": 42,
        "svg.fonttype": "none",
        "figure.dpi": 100,
    })


def figure(height=3.4, nrows=1, ncols=1, **kw):
    """A figure WIDTH wide; returns (fig, ax) or (fig, axes) as plt.subplots does."""
    kw.setdefault("figsize", (WIDTH, height))
    fig, ax = plt.subplots(nrows, ncols, **kw)
    if nrows * ncols == 1:
        style(ax)
    else:
        for a in ax.ravel() if hasattr(ax, "ravel") else ax:
            style(a)
    return fig, ax


def style(ax, grid_axis="y"):
    """Two spines, a light grid on one axis, no tick marks."""
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.spines["left"].set_color(BASE)
    ax.spines["bottom"].set_color(BASE)
    ax.grid(True, axis=grid_axis)
    ax.grid(False, axis="y" if grid_axis == "x" else "x")
    ax.set_axisbelow(True)
    ax.tick_params(length=0)


def save(fig, name, outdir=".", dpi=150):
    """Write NAME.png (raster, for the Markdown) and NAME.pdf (vector, for the PDF) and close the figure."""
    png = os.path.join(outdir, name + ".png")
    pdf = os.path.join(outdir, name + ".pdf")
    fig.savefig(png, dpi=dpi, facecolor=SURFACE)
    fig.savefig(pdf, facecolor=SURFACE)
    plt.close(fig)
    return png, pdf
