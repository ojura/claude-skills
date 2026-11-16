"""Compare real browser vector drawing and PDF composition with native-format inputs.

Uses the maintained player, bundler and owned-browser harness. Cairo and browser
edge antialiasing are measured separately from exact interior colour, geometry,
layer order, clipping and coverage of the replaced PDF poster.
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import math
from pathlib import Path
import shutil
import subprocess
import tempfile

import fitz
import numpy as np
from PIL import Image

from ..animation.vector_scene import scene_paths_at
from ..catalog import Catalog
from ..web.build import build_bundle


FIXTURE = Path(__file__).resolve().parents[1] / "tests/fixtures/format-options.vector.json"
STAGE_SIZE = (400, 300)


def rgba(encoded):
    return np.asarray(Image.open(io.BytesIO(base64.b64decode(encoded))).convert("RGBA")).copy()


def neighbours(image):
    padded = np.pad(image, ((1, 1), (1, 1), (0, 0)), mode="edge")
    return [padded[y:y + image.shape[0], x:x + image.shape[1]]
            for y in range(3) for x in range(3)]


def neighbourhood(image):
    samples = neighbours(image)
    return np.minimum.reduce(samples), np.maximum.reduce(samples)


def compare_rasters(actual, native):
    if actual.shape != native.shape:
        raise AssertionError(f"Raster sizes differ: {actual.shape} / {native.shape}")
    difference = np.abs(actual.astype(np.int16) - native.astype(np.int16))
    interiors = []
    for reference in (native, actual):
        low, high = neighbourhood(reference)
        interior = difference[np.all(low == high, axis=2)]
        maximum = int(interior.max()) if interior.size else 0
        if maximum > 1:
            raise AssertionError(f"Constant-colour interior differs by {maximum}/255")
        interiors.append(maximum)
    # Compare accumulated coverage as well as constant interiors. A narrow path
    # can have no constant 3x3 interior even when its opaque core disappears.
    # Premultiplication measures painted colour independently of transparent RGB.
    painted = []
    for image in (actual, native):
        value = image.astype(np.float64)
        value[:, :, :3] *= value[:, :, 3:] / 255
        painted.append(value)
    delta = np.abs(np.add.reduce(neighbours(painted[0] - painted[1])))
    low_a, high_a = neighbourhood(painted[0])
    low_n, high_n = neighbourhood(painted[1])
    contrast = np.maximum(high_a, high_n) - np.minimum(low_a, low_n)
    # Allow one pixel-equivalent of local contrast over each 3x3 neighbourhood,
    # plus one channel quantization level per sample. This permits boundary
    # sampling differences while rejecting missing cores and one-pixel shifts.
    allowance = contrast.astype(np.float64) + 9
    coverage = float(np.max(delta / allowance))
    if coverage > 1:
        raise AssertionError(f"Local stroke coverage differs by {coverage:.6f} times its allowance")
    return {"differentChannels": int(np.count_nonzero(difference)),
            "maximumDifference": int(difference.max()),
            "constantInteriorMaximum": max(interiors),
            "maximumCoverageRatio": coverage}


def colour_rect(identifier, rectangle, colour):
    left, top, right, bottom = rectangle
    return {"id": identifier, "commands": "MLLLZ", "keyframes": [{
        "time": 0, "coords": [left, top, right, top, right, bottom, left, bottom],
        "fill": colour, "stroke": [0, 0, 0, 0], "stroke_width": 0,
    }]}


def make_documents(root, vendor):
    root.mkdir()
    (root / "presentations.json").write_text(json.dumps({
        "version": 2, "default": "format", "decks": [
            {"id": "format", "title": "Vector format and PDF composition", "source": "fixture.pdf",
             "build": {"kind": "external"}},
        ],
    }))
    catalog = Catalog(root)
    rows, inputs = [], {"format": {}}
    for theme, background in (("light", [1, 1, 1, 1]), ("dark", [.2, .2, .2, 1])):
        document = fitz.open()
        scenes = []
        for aspect, (width, height) in enumerate(((100, 60), (100, 100), (200, 60))):
            for offset, (dx, dy) in enumerate(((0, 0), (.25, .125), (.625, .375))):
                identifier = f"edge-{aspect}-{offset}"
                region = [50 + dx, 60 + dy, 350 + dx, 240 + dy]
                scale = min(300 / width, 180 / height)
                fitted = [region[0] + (300 - width * scale) / 2,
                          region[1] + (180 - height * scale) / 2]
                fitted += [fitted[0] + width * scale, fitted[1] + height * scale]
                page = document.new_page(width=400, height=300)
                page.draw_rect(page.rect, color=None, fill=(0, .5, 0))
                page.draw_rect(fitz.Rect(fitted), color=None, fill=(1, 0, 1))
                page.insert_link({"kind": fitz.LINK_URI, "from": fitz.Rect(region),
                                  "uri": "pdfpc-vector:" + identifier})
                scenes.append({"id": identifier, "width": width, "height": height,
                               "duration": 1, "background": background,
                               "checkpoints": [{"time": 0, "label": "Start"}, {"time": 1, "label": "End"}],
                               "objects": [
                                   colour_rect("landmark", [.4 * width, .4 * height, .6 * width, .6 * height], [1, 0, 0, 1]),
                                   colour_rect("right-clipped", [1.1 * width, 0, 1.2 * width, height], [0, 0, 1, 1]),
                                   colour_rect("top-clipped", [0, -.2 * height, width, -.1 * height], [0, 0, 1, 1]),
                               ]})
                if theme == "light":
                    rows.append({"page": page.number + 1, "fitted": fitted, "scene": identifier})
        pdf = root / f"{theme}.pdf"
        vectors = root / f"{theme}.vector.json"
        document.save(pdf)
        document.close()
        vectors.write_text(json.dumps({"version": 1, "scenes": scenes}))
        inputs["format"][theme] = {"pdf": str(pdf), "vectors": str(vectors)}
    html = build_bundle(catalog, catalog.get(), inputs=inputs, pdfjs_dist=vendor,
                        output=root / "build/animated", archive=False)
    with fitz.open() as document:
        page = document.new_page(width=300, height=400)
        page.insert_text((25, 40), "Static document without a timeline")
        document.save(root / "static.pdf")
    static = build_bundle(catalog, catalog.get(), pdf=root / "static.pdf", themes=("light",),
                          pdfjs_dist=vendor, output=root / "build/static", archive=False)
    return html, static, rows


def canvas_cases(view, probe, scratch):
    source = json.loads(FIXTURE.read_text())
    view.eval("(async()=>{window.formatRenderer=await import('derivation-vector');"
              "window.formatScenes=window.formatRenderer.prepareTimeline(" + json.dumps(source) + ");return true;})()")
    reports = []
    for scene in source["scenes"]:
        for width, height in ((120, 80), (120, 120), (240, 80), (240, 160)):
            for time in (0, .5, 1, 2):
                result = view.eval("(()=>{const renderer=window.formatRenderer, scene=window.formatScenes.get(" +
                                   json.dumps(scene["id"]) + ");const canvas=document.createElement('canvas');"
                                   f"canvas.width={width};canvas.height={height};"
                                   f"renderer.renderSceneFitted(canvas.getContext('2d'),scene,{time},{width},{height});"
                                   "return {png:canvas.toDataURL().split(',')[1],states:scene.objects.map(object=>({"
                                   f"id:object.id,z:object.z,...renderer.stateAt(object,{time}),"
                                   f"coords:Array.from(renderer.stateAt(object,{time}).coords)"
                                   "}))};})()")
                expected = scene_paths_at(scene, time)
                for actual, wanted in zip(result["states"], expected, strict=True):
                    assert actual["id"] == wanted["id"] and actual["z"] == wanted.get("z", 0)
                    for key in ("coords", "fill", "stroke"):
                        np.testing.assert_allclose(actual[key], wanted[key], rtol=0, atol=1e-12)
                    assert abs(actual["strokeWidth"] - wanted["stroke_width"]) < 1e-12
                native_file = scratch / "native.png"
                subprocess.run([str(probe), "render", str(FIXTURE), scene["id"], str(time),
                                str(native_file), str(width), str(height)], check=True, capture_output=True)
                native = np.asarray(Image.open(native_file).convert("RGBA"))
                actual = rgba(result["png"])
                report = {"scene": scene["id"], "time": time, "size": [width, height]}
                try:
                    report.update(compare_rasters(actual, native))
                except AssertionError as error:
                    Image.fromarray(actual).save(scratch / "canvas-failure.png")
                    report["error"] = str(error)
                reports.append(report)
    return reports


def composed_case(view, row, theme, scale, output):
    from .browser_protocol import ready
    width, height = (value * scale for value in STAGE_SIZE)
    previous = view.eval("document.getElementById('slide').dataset.renderedRevision")
    view.eval("(()=>{const p=document.querySelector('.projection');"
              f"p.style.cssText='position:fixed;left:0;top:0;width:{width}px;height:{height}px;padding:0;';"
              "document.getElementById('controls').style.display='none';"
              "document.getElementById('slide').style.boxShadow='none';return true;})()")
    view.wait("document.getElementById('slide').dataset.renderedRevision !== " + json.dumps(previous))
    ready(view, row["page"], theme)
    metrics = view.eval("(()=>{const s=document.getElementById('slide').getBoundingClientRect();"
                        "return {left:s.left,top:s.top,width:s.width,height:s.height,"
                        "innerWidth,innerHeight,dpr:devicePixelRatio};})()")
    image_path = output / "composed.png"
    baseline_path = output / "poster.png"
    view.capture(image_path)
    view.eval("(document.getElementById('vector').style.visibility='hidden',true)")
    view.capture(baseline_path)
    view.eval("(document.getElementById('vector').style.visibility='',true)")
    actual = np.asarray(Image.open(image_path).convert("RGB"))
    baseline = np.asarray(Image.open(baseline_path).convert("RGB"))
    ratio = actual.shape[1] / metrics["innerWidth"]
    assert abs(metrics["width"] - width) < .1 and abs(metrics["height"] - height) < .1, metrics
    assert metrics["top"] + height <= metrics["innerHeight"], metrics
    left, top, right, bottom = row["fitted"]
    mapped = [metrics["left"] + left * scale, metrics["top"] + top * scale,
              metrics["left"] + right * scale, metrics["top"] + bottom * scale]
    x0, y0 = (math.floor(value * ratio + 1e-9) for value in mapped[:2])
    x1, y1 = (math.ceil(value * ratio - 1e-9) for value in mapped[2:])
    background = np.array([255, 255, 255] if theme == "light" else [51, 51, 51])
    covered = np.zeros(actual.shape[:2], dtype=bool)
    covered[y0:y1, x0:x1] = True
    landmark = np.zeros_like(covered)
    lx0 = math.floor((mapped[0] + .4 * (mapped[2] - mapped[0])) * ratio) - 1
    lx1 = math.ceil((mapped[0] + .6 * (mapped[2] - mapped[0])) * ratio) + 1
    ly0 = math.floor((mapped[1] + .4 * (mapped[3] - mapped[1])) * ratio) - 1
    ly1 = math.ceil((mapped[1] + .6 * (mapped[3] - mapped[1])) * ratio) + 1
    landmark[ly0:ly1, lx0:lx1] = True
    damaged = covered & ~landmark & np.any(actual != background, axis=2)
    outside = ~covered & np.any(actual != baseline, axis=2)
    centre = actual[(ly0 + ly1) // 2, (lx0 + lx1) // 2]
    red = covered & (actual[:, :, 0] > 220) & (actual[:, :, 1] < 35) & (actual[:, :, 2] < 35)
    ys, xs = np.nonzero(red)
    red_bounds = [int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1] if xs.size else None
    expected_bounds = [lx0 + 1, ly0 + 1, lx1 - 1, ly1 - 1]
    misplaced = red_bounds is None or any(abs(actual_edge - expected_edge) > 1
                                        for actual_edge, expected_edge in zip(red_bounds, expected_bounds))
    result = {"scene": row["scene"], "theme": theme, "scale": scale,
              "posterEdgePixels": int(np.count_nonzero(damaged)),
              "changedSurroundingPixels": int(np.count_nonzero(outside)),
              "redCentre": centre.tolist(), "deviceRatio": ratio,
              "redBounds": red_bounds, "expectedRedBounds": expected_bounds}
    if damaged.any() or outside.any() or centre.tolist() != [255, 0, 0] or misplaced:
        result["error"] = "Composed vector does not exactly replace its fitted PDF poster"
        shutil.copyfile(image_path, output / f"{theme}-{row['scene']}-{scale}-failure.png")
    return result


def main():
    from .browser_protocol import browser, ready
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--pdfjs-dist", type=Path, required=True)
    parser.add_argument("--vector-probe", type=Path, required=True)
    parser.add_argument("--browser", choices=("both", "chrome", "firefox"), default="both")
    args = parser.parse_args()
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    results = []
    with tempfile.TemporaryDirectory(prefix="browser-format-", dir=output) as temporary:
        scratch = Path(temporary)
        html, static, rows = make_documents(scratch / "consumer", args.pdfjs_dist.resolve())
        for kind in (("chrome", "firefox") if args.browser == "both" else (args.browser,)):
            executable = shutil.which("google-chrome" if kind == "chrome" else "firefox")
            if not executable:
                raise RuntimeError("Missing browser: " + kind)
            owned = scratch / kind
            owned.mkdir()
            captures = output / kind
            captures.mkdir(exist_ok=True)
            with browser(kind, executable, owned) as view:
                view.navigate(html.as_uri())
                ready(view, 1, "light")
                direct = canvas_cases(view, args.vector_probe.resolve(), captures)
                print(kind, "direct", len(direct), "failures", sum("error" in item for item in direct), flush=True)
                composed = []
                for theme in ("light", "dark"):
                    for row in rows:
                        for scale in (1, 2):
                            view.navigate(html.as_uri() + f"?theme={theme}#p={row['page']}")
                            ready(view, row["page"], theme)
                            composed.append(composed_case(view, row, theme, scale, captures))
                view.navigate(static.as_uri())
                ready(view, 1, "light")
                assert view.eval("document.getElementById('play').disabled") is True
                results.append({"browser": kind, "canvas": direct, "composed": composed, "static": "passed"})
                (output / "results.json").write_text(json.dumps(results, indent=2) + "\n")
                print(kind, "composed", len(composed), "failures", sum("error" in item for item in composed), flush=True)
    failures = sum("error" in case for result in results for group in ("canvas", "composed") for case in result[group])
    if failures:
        raise SystemExit(f"{failures} browser rendering cases failed; see {output / 'results.json'}")
    print("Browser/native format and composed-PDF checks passed", flush=True)


if __name__ == "__main__":
    main()
