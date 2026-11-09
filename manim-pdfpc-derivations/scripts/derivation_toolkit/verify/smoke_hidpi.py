#!/usr/bin/env python3
"""Check native-to-PDF region alignment at GDK scale 2, before and after resize."""

import argparse
import json
import os
import signal
import subprocess
import time
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter
from pypdf import PdfReader


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("pdfpc", type=Path)
    parser.add_argument("pdf", type=Path, help="The light live deck")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not os.environ.get("PDFPC_VECTOR_HIDPI_BUS"):
        return subprocess.run(["dbus-run-session", "--", "env", "PDFPC_VECTOR_HIDPI_BUS=1",
            "python3", str(Path(__file__).resolve()), str(args.pdfpc.resolve()),
            str(args.pdf.resolve()), "--output", str(args.output.resolve())]).returncode

    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    reader = PdfReader(args.pdf)
    binding = None
    for index, page in enumerate(reader.pages):
        for reference in page.get("/Annots", []):
            annotation = reference.get_object()
            if annotation.get("/A", {}).get("/URI") == "pdfpc-vector:rotation":
                binding = (index + 1, int(reader.page_labels[index]),
                           tuple(float(v) for v in annotation["/Rect"]),
                           float(page.mediabox.width), float(page.mediabox.height))
    if binding is None:
        raise ValueError("No rotation annotation in the live PDF")
    physical_page, logical_page, region, page_width, page_height = binding
    trace = output / "trace.jsonl"
    trace.write_text("")
    read_fd, write_fd = os.pipe()
    xvfb_log = (output / "xvfb.log").open("w")
    xvfb = subprocess.Popen(["Xvfb", "-displayfd", str(write_fd), "-screen", "0", "3840x2160x24",
                             "-nolisten", "tcp", "-noreset"], pass_fds=(write_fd,),
                            stdout=xvfb_log, stderr=xvfb_log)
    os.close(write_fd)
    with os.fdopen(read_fd) as stream:
        display = stream.readline().strip()
    if not display:
        raise RuntimeError("Xvfb did not start")
    environment = {**os.environ, "DISPLAY": f":{display}", "GDK_BACKEND": "x11", "GDK_SCALE": "2",
                   "PDFPC_VECTOR_TRACE": str(trace), "GIO_USE_VFS": "local",
                   "XDG_CONFIG_HOME": str(output / "config"), "XDG_CACHE_HOME": str(output / "cache")}
    app_log = (output / "pdfpc.log").open("w")
    app = subprocess.Popen([str(args.pdfpc.resolve()), "-N", "-w", "both", "-Z", "800:600",
                            "-P", str(logical_page), str(args.pdf.resolve())],
                           env=environment, stdout=app_log, stderr=app_log, start_new_session=True)

    def events():
        return [json.loads(line) for line in trace.read_text().splitlines() if line.endswith("}")]

    def wait_for(condition):
        end = time.monotonic() + 15
        while time.monotonic() < end:
            if app.poll() is not None:
                raise RuntimeError(f"pdfpc exited: {app.returncode}")
            if condition():
                return
            time.sleep(0.025)
        raise AssertionError("Timed out waiting for native high-DPI redraw")

    def window(name):
        return subprocess.check_output(["xdotool", "search", "--all", "--pid", str(app.pid),
                                        "--name", name], env=environment, text=True).split()[-1]

    report = {"gdk_scale": 2, "scene": "rotation", "time": 0, "checks": [], "owned_pdfpc_pid": app.pid}
    try:
        wait_for(lambda: any(e.get("event") == "frame" and e.get("view") == "presentation"
                             and e.get("scene") == "rotation" for e in events()))
        audience = window("pdfpc - presentation")
        presenter = window("pdfpc - presenter")
        assert audience != presenter
        subprocess.run(["xdotool", "windowsize", presenter, "1280", "900", "windowmove", presenter,
                        "2000", "20", "windowmove", audience, "20", "20", "mousemove", "3800", "2100"],
                       env=environment, check=True)
        for width, height in ((1000, 750), (1200, 900)):
            subprocess.run(["xdotool", "windowsize", audience, str(width), str(height)],
                           env=environment, check=True)
            wait_for(lambda: any(e.get("view") == "presentation" and e.get("width") == width and
                                 e.get("height") == height and e.get("displayed_time") == 0
                                 for e in events() if e["event"] == "frame"))
            time.sleep(0.15)
            actual = output / f"native-{width}.png"
            expected = output / f"poster-{width}"
            subprocess.run(["import", "-window", audience, str(actual)], env=environment, check=True)
            subprocess.run(["pdftocairo", "-png", "-singlefile", "-f", str(physical_page),
                            "-l", str(physical_page), "-scale-to-x", str(width), "-scale-to-y", str(height),
                            str(args.pdf.resolve()), str(expected)], check=True, capture_output=True)
            reference = Image.open(expected.with_suffix(".png")).convert("RGB")
            observed = Image.open(actual).convert("RGB")
            assert observed.size == reference.size == (width, height)
            scale = min(width / page_width, height / page_height)
            xoff, yoff = (width - page_width * scale) / 2, (height - page_height * scale) / 2
            box = (round(xoff + region[0] * scale), round(yoff + (page_height - region[3]) * scale),
                   round(xoff + region[2] * scale), round(yoff + (page_height - region[1]) * scale))
            a = np.asarray(observed.crop(box)).astype(np.int16)
            b = np.asarray(reference.crop(box)).astype(np.int16)
            ink_a, ink_b = np.min(a, axis=2) < 180, np.min(b, axis=2) < 180
            expand = lambda ink: np.asarray(Image.fromarray(ink.astype(np.uint8)*255)
                                            .filter(ImageFilter.MaxFilter(3))) > 0
            mismatch = np.count_nonzero(ink_a & ~expand(ink_b)) + np.count_nonzero(ink_b & ~expand(ink_a))
            fraction = mismatch / max(1, np.count_nonzero(ink_a) + np.count_nonzero(ink_b))
            result = {"size": [width, height], "region_pixels": list(box),
                      "mean_channel_error": float(np.abs(a-b).mean()),
                      "ink_pixels_outside_one_pixel_tolerance": int(mismatch),
                      "unmatched_ink_fraction": float(fraction)}
            report["checks"].append(result)
            assert fraction < 0.005 and result["mean_channel_error"] < 2, result
    finally:
        if app.poll() is None:
            os.killpg(app.pid, signal.SIGTERM)
            try:
                app.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(app.pid, signal.SIGKILL)
                app.wait()
        xvfb.terminate()
        xvfb.wait(timeout=5)
        report["owned_pdfpc_exit"] = app.returncode
        app_log.close()
        xvfb_log.close()
        (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
