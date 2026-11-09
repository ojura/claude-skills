#!/usr/bin/env python3
"""Check native-to-PDF region alignment at GDK scale 2, before and after resize."""

import argparse
from dataclasses import dataclass
import hashlib
import json
import math
import os
import select
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter
from pypdf import PdfReader


@dataclass(frozen=True)
class Binding:
    page: int
    label: str
    scene: str
    time: float
    region: tuple
    page_size: tuple
    scene_size: tuple
    background: tuple


def select_binding(pdf, vectors, scene_id=None, physical_page=None):
    """Resolve one actual link and its time-zero poster in the selected inputs."""
    timeline = json.loads(Path(vectors).read_text())
    if timeline.get("version") != 1:
        raise ValueError("Unsupported vector timeline version")
    scenes = {item["id"]: item for item in timeline["scenes"]}
    if len(scenes) != len(timeline["scenes"]):
        raise ValueError("Duplicate scene IDs in the vector timeline")
    reader = PdfReader(pdf)
    if physical_page is not None and not 1 <= physical_page <= len(reader.pages):
        raise ValueError("--page must name a physical page in the PDF")
    for index, page in enumerate(reader.pages, 1):
        if physical_page is not None and index != physical_page:
            continue
        links = []
        for reference in page.get("/Annots", []):
            annotation = reference.get_object()
            action = annotation.get("/A")
            uri = action.get_object().get("/URI", "") if action is not None else ""
            if str(uri).startswith("pdfpc-vector:"):
                links.append((str(uri).split(":", 1)[1], annotation["/Rect"]))
        matches = [link for link in links if scene_id is None or link[0] == scene_id]
        if not matches:
            continue
        if len(links) != 1:
            raise ValueError(f"Physical page {index} must have exactly one vector region")
        identifier, rectangle = matches[0]
        if identifier not in scenes:
            raise ValueError(f"PDF scene {identifier!r} is absent from the selected timeline")
        scene = scenes[identifier]
        size = tuple(float(scene[key]) for key in ("width", "height"))
        if not all(math.isfinite(value) and value > 0 for value in size):
            raise ValueError(f"Scene {identifier!r} has invalid dimensions")
        checkpoints = scene.get("checkpoints", [])
        if not checkpoints or checkpoints[0].get("time") != 0:
            raise ValueError(f"Scene {identifier!r} needs its first poster checkpoint at time zero")
        background = tuple(float(value) for value in scene["background"])
        if len(background) != 4 or not all(math.isfinite(v) and 0 <= v <= 1 for v in background):
            raise ValueError(f"Scene {identifier!r} has an invalid RGBA background")
        left, bottom, right, top = map(float, page.cropbox)
        width, height = right - left, top - bottom
        rotation = page.rotation % 360
        if width <= 0 or height <= 0 or rotation not in (0, 90, 180, 270):
            raise ValueError(f"Physical page {index} has invalid display geometry")

        def display_point(x, y):
            x, y = float(x) - left, float(y) - bottom
            return {0: (x, height-y), 90: (y, x),
                    180: (width-x, y), 270: (height-y, width-x)}[rotation]

        x0, y0, x1, y1 = map(float, rectangle)
        points = [display_point(x, y) for x in (x0, x1) for y in (y0, y1)]
        region = (min(p[0] for p in points), min(p[1] for p in points),
                  max(p[0] for p in points), max(p[1] for p in points))
        page_size = (height, width) if rotation in (90, 270) else (width, height)
        rw, rh = region[2] - region[0], region[3] - region[1]
        if (not all(math.isfinite(v) for v in (*region, *page_size)) or rw <= 0 or rh <= 0
                or region[0] < 0 or region[1] < 0
                or region[2] > page_size[0] or region[3] > page_size[1]):
            raise ValueError(f"Physical page {index} has a vector region outside its display bounds")
        if abs(rw/rh - size[0]/size[1]) > 0.0002:
            raise ValueError(f"Scene {identifier!r} and its PDF region have different aspect ratios")
        return Binding(index, str(reader.page_labels[index-1]), identifier,
                       float(checkpoints[0]["time"]), region, page_size, size, background)
    raise ValueError("No matching pdfpc-vector annotation in the selected PDF/page")


def window_sizes(binding):
    width, height = binding.page_size
    sizes = []
    for max_width, max_height in ((1000, 750), (1200, 900)):
        scale = min(max_width/width, max_height/height)
        # GDK scale 2 uses integral logical pixels; extreme aspect ratios letterbox.
        sizes.append(tuple(max(320, 2*round(value*scale/2)) for value in (width, height)))
    return sizes


def viewport_geometry(binding, size):
    width, height = size
    page_width, page_height = binding.page_size
    scale = min(width/page_width, height/page_height)
    offset = ((width-page_width*scale)/2, (height-page_height*scale)/2)
    box = tuple(round(offset[i % 2] + value*scale) for i, value in enumerate(binding.region))
    if box[2] <= box[0] or box[3] <= box[1]:
        raise ValueError("Selected vector region is too small for the verification viewport")
    return scale, offset, box


def redraw_size(records, binding, requested):
    """Use the actual frame size; GTK may round its aspect by one logical pixel."""
    scale = min(value/page for value, page in zip(requested, binding.page_size))
    fitted = tuple(page*scale for page in binding.page_size)
    for event in reversed(records):
        if (event.get("event") == "frame" and event.get("view") == "presentation"
                and event.get("scene") == binding.scene and event.get("page") == binding.page
                and event.get("displayed_time") == binding.time):
            actual = (event["width"], event["height"])
            if all(0 < value <= limit and abs(value-target) <= 2
                   for value, target, limit in zip(actual, fitted, requested)):
                return actual
    return None


def drawing_box(window_size, viewport_size, gdk_scale=2):
    """Locate pdfpc's centered AspectFrame child in physical screenshot pixels."""
    if (gdk_scale <= 0 or any(value <= 0 or value % gdk_scale
                             for value in (*window_size, *viewport_size))
            or any(view > window for view, window in zip(viewport_size, window_size))):
        raise ValueError("Drawing allocation must fit the window in whole GDK logical pixels")
    # Gtk.AspectFrame(0.5, 0.5) truncates its centering offset in logical pixels.
    # A one-logical-pixel remainder therefore stays at the right/bottom edge.
    x, y = ((window-view)//(2*gdk_scale)*gdk_scale
            for window, view in zip(window_size, viewport_size))
    return x, y, x+viewport_size[0], y+viewport_size[1]


def render_reference(pdf, binding, size, output):
    """Render the PDF at uniform scale, preserving its crop, rotation and margins."""
    width, height = size
    page_width, page_height = binding.page_size
    xscale, yscale = ((width, -1) if width/page_width <= height/page_height else (-1, height))
    subprocess.run(["pdftocairo", "-png", "-singlefile", "-cropbox", "-f", str(binding.page),
                    "-l", str(binding.page), "-scale-to-x", str(xscale), "-scale-to-y", str(yscale),
                    str(pdf), str(output)], check=True, capture_output=True)
    with Image.open(Path(output).with_suffix(".png")) as raster:
        raster = raster.convert("RGB")
    scale, offset, _ = viewport_geometry(binding, size)
    assert all(abs(actual-expected*scale) <= 1 for actual, expected in zip(raster.size, binding.page_size))
    reference = Image.new("RGB", size, "black")
    reference.paste(raster, tuple(round(value) for value in offset))
    reference.save(Path(output).with_suffix(".png"))
    return reference


def compare_region(observed, reference, binding, size):
    if observed.size != size or reference.size != size:
        raise AssertionError("Native and PDF images must have the requested viewport dimensions")
    _, _, box = viewport_geometry(binding, size)
    a = np.asarray(observed.convert("RGB").crop(box)).astype(np.int16)
    b = np.asarray(reference.convert("RGB").crop(box)).astype(np.int16)
    if binding.background[3] == 1:
        background = np.rint(np.asarray(binding.background[:3])*255).astype(np.int16)
    else:
        # The PDF already composites transparent canvases onto the document.
        colors, counts = np.unique(b.reshape(-1, 3), axis=0, return_counts=True)
        background = colors[np.argmax(counts)]
    contrast_a = np.max(np.abs(a-background), axis=2)
    contrast_b = np.max(np.abs(b-background), axis=2)
    ink_a, ink_b = contrast_a > 30, contrast_b > 30
    if not np.any(ink_b):
        raise AssertionError("Selected poster has no visible foreground to verify")
    expand = lambda ink: np.asarray(Image.fromarray(ink.astype(np.uint8)*255)
                                    .filter(ImageFilter.MaxFilter(3))) > 0
    # Thin antialiased strokes can fall below the core mask in one renderer.
    # Compare definite foreground against lower-contrast visible support.
    mismatch = (np.count_nonzero(ink_a & ~expand(contrast_b > 10))
                + np.count_nonzero(ink_b & ~expand(contrast_a > 10)))
    fraction = mismatch / max(1, np.count_nonzero(ink_a) + np.count_nonzero(ink_b))
    return {"size": list(size), "region_pixels": list(box), "background_rgb": background.tolist(),
            "foreground_contrast": {"core": 30, "antialias_support": 10},
            "mean_channel_error": float(np.abs(a-b).mean()),
            "ink_pixels_outside_one_pixel_tolerance": int(mismatch),
            "unmatched_ink_fraction": float(fraction)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("pdfpc", type=Path)
    parser.add_argument("pdf", type=Path, help="PDF containing a live vector region")
    parser.add_argument("--vectors", type=Path, help="Timeline (default: PDF basename.vector.json)")
    parser.add_argument("--scene", help="Scene ID (default: first vector link in PDF order)")
    parser.add_argument("--page", type=int, help="Physical PDF page, starting at 1")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not os.environ.get("PDFPC_VECTOR_HIDPI_BUS"):
        return subprocess.run(["dbus-run-session", "--", "env", "PDFPC_VECTOR_HIDPI_BUS=1",
            sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]]).returncode

    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    pdf = args.pdf.resolve()
    vectors = (args.vectors or pdf.with_suffix(".vector.json")).resolve()
    binding = select_binding(pdf, vectors, args.scene, args.page)
    # A private copy avoids reading or writing the lecturer's adjacent .pdfpc state.
    input_pdf = output / "input.pdf"
    shutil.copyfile(pdf, input_pdf)
    trace = output / "trace.jsonl"
    trace.write_text("")
    read_fd, write_fd = os.pipe()
    xvfb_log = (output / "xvfb.log").open("w")
    xvfb = subprocess.Popen(["Xvfb", "-displayfd", str(write_fd), "-screen", "0", "3840x2160x24",
                             "-nolisten", "tcp", "-noreset"], pass_fds=(write_fd,),
                            stdout=xvfb_log, stderr=xvfb_log)
    os.close(write_fd)
    with os.fdopen(read_fd) as stream:
        display = stream.readline().strip() if select.select([stream], [], [], 15)[0] else ""
    if not display:
        xvfb.terminate()
        xvfb.wait(timeout=5)
        xvfb_log.close()
        raise RuntimeError("Xvfb did not start")
    environment = {**os.environ, "DISPLAY": f":{display}", "GDK_BACKEND": "x11", "GDK_SCALE": "2",
                   "PDFPC_VECTOR_TRACE": str(trace), "PDFPC_VECTOR_MANIFEST": str(vectors),
                   "GIO_USE_VFS": "local",
                   "XDG_CONFIG_HOME": str(output / "config"), "XDG_CACHE_HOME": str(output / "cache")}
    app_log = (output / "pdfpc.log").open("w")
    app = None

    def events():
        return [json.loads(line) for line in trace.read_text().splitlines() if line.endswith("}")]

    def wait_for(condition):
        end = time.monotonic() + 15
        while time.monotonic() < end:
            if app.poll() is not None:
                raise RuntimeError(f"pdfpc exited: {app.returncode}")
            value = condition()
            if value:
                return value
            time.sleep(0.025)
        raise AssertionError("Timed out waiting for native high-DPI redraw")

    def window(name):
        return subprocess.check_output(["xdotool", "search", "--all", "--pid", str(app.pid),
                                        "--name", name], env=environment, text=True).split()[-1]

    report = {"gdk_scale": 2, "scene": binding.scene, "time": binding.time,
              "physical_page": binding.page, "page_label": binding.label,
              "page_size": binding.page_size, "scene_size": binding.scene_size,
              "pdf": str(pdf), "vectors": str(vectors),
              "pdf_sha256": hashlib.sha256(pdf.read_bytes()).hexdigest(),
              "vectors_sha256": hashlib.sha256(vectors.read_bytes()).hexdigest(), "checks": []}
    try:
        app = subprocess.Popen([str(args.pdfpc.resolve()), "-N", "--disable-auto-grouping",
                                "-w", "both", "-Z", "800:600", "-P", str(binding.page), str(input_pdf)],
                               env=environment, stdout=app_log, stderr=app_log, start_new_session=True)
        report["owned_pdfpc_pid"] = app.pid
        wait_for(lambda: any(e.get("event") == "frame" and e.get("view") == "presentation"
                             and e.get("scene") == binding.scene and e.get("page") == binding.page
                             for e in events()))
        audience = window("pdfpc - presentation")
        presenter = window("pdfpc - presenter")
        assert audience != presenter
        subprocess.run(["xdotool", "windowsize", presenter, "1280", "900", "windowmove", presenter,
                        "2000", "20", "windowmove", audience, "20", "20", "mousemove", "3800", "2100"],
                       env=environment, check=True)
        for requested in window_sizes(binding):
            width, height = requested
            before_resize = len(events())
            subprocess.run(["xdotool", "windowsize", audience, str(width), str(height)],
                           env=environment, check=True)
            width, height = wait_for(lambda: redraw_size(events()[before_resize:], binding, requested))
            time.sleep(0.15)
            actual = output / f"native-{width}.png"
            expected = output / f"poster-{width}"
            subprocess.run(["import", "-window", audience, str(actual)], env=environment, check=True)
            reference = render_reference(input_pdf, binding, (width, height), expected)
            with Image.open(actual) as observed:
                assert observed.size == requested, (observed.size, requested)
                box = drawing_box(observed.size, (width, height))
                result = compare_region(observed.crop(box), reference, binding, (width, height))
            result["requested_size"] = list(requested)
            result["drawing_box_in_window"] = list(box)
            report["checks"].append(result)
            assert result["unmatched_ink_fraction"] < 0.005 and result["mean_channel_error"] < 2, result
    finally:
        if app is not None and app.poll() is None:
            os.killpg(app.pid, signal.SIGTERM)
            try:
                app.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(app.pid, signal.SIGKILL)
                app.wait()
        xvfb.terminate()
        xvfb.wait(timeout=5)
        report["owned_pdfpc_exit"] = app.returncode if app is not None else None
        app_log.close()
        xvfb_log.close()
        (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
