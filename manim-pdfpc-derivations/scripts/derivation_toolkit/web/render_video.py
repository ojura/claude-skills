#!/usr/bin/env python3
"""Render an explicitly requested MP4 from a compiled PDF and its original timeline."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import io
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

import cairo
import fitz

from .build import pdf_manifest
from ..catalog import Catalog
from ..paths import get_paths, output_path
from ..animation.vector_scene import draw_paths, scene_paths_at


@dataclass(frozen=True)
class Section:
    page: int
    title: str
    start: float
    end: float
    frames: int
    moving: bool


def clip_sections(pages: list[dict], scenes: dict, fps: int,
                  static_hold: float, checkpoint_hold: float) -> list[Section]:
    """Keep authored motion duration and insert explicit reading pauses."""
    result = []
    for page in pages:
        binding = page["scene"]
        if binding is None:
            result.append(Section(page["number"], page["title"], 0, 0,
                                  max(1, round(static_hold * fps)), False))
            continue
        scene = scenes[binding["id"]]
        checkpoints = [float(point["time"]) for point in scene["checkpoints"]]
        if not checkpoints or checkpoints[0] != 0 or checkpoints[-1] != scene["duration"]:
            raise ValueError("Scene checkpoints must include the start and the end")
        result.append(Section(page["number"], page["title"], 0, 0, fps, False))
        for index, (start, end) in enumerate(zip(checkpoints, checkpoints[1:])):
            # Differences of rounded boundaries keep the entire scene's duration exact.
            count = round(end * fps) - round(start * fps)
            result.append(Section(page["number"], page["title"], start, end, count, True))
            hold = 2.0 if index == len(checkpoints) - 2 else checkpoint_hold
            result.append(Section(page["number"], page["title"], end, end,
                                  max(1, round(hold * fps)), False))
    return result


def chapters(sections: list[Section], fps: int, title: str) -> str:
    lines = [";FFMETADATA1", "title=" + title.replace("\n", " ").replace("=", r"\=")]
    current = 0
    for page in dict.fromkeys(section.page for section in sections):
        selected = [section for section in sections if section.page == page]
        end = current + sum(section.frames for section in selected)
        title = selected[0].title.replace("\\", "\\\\").replace("=", "\\=")
        title = title.replace(";", "\\;").replace("#", "\\#").replace("\n", " ")
        lines.extend(["[CHAPTER]", f"TIMEBASE=1/{fps}", f"START={current}",
                      f"END={end}", f"title={title}"])
        current = end
    return "\n".join(lines) + "\n"


def render(args: argparse.Namespace, catalog: Catalog) -> dict:
    if not 1 <= args.fps <= 240 or args.width < 320 or args.width % 2:
        raise ValueError("Use an even width of at least 320 and a frame rate from 1 to 240")
    if args.static_hold <= 0 or args.checkpoint_hold <= 0:
        raise ValueError("Reading pauses must be positive")
    encoder = shutil.which("ffmpeg")
    if not encoder:
        raise RuntimeError("FFmpeg is required to encode the video")
    pdf = args.pdf.expanduser().resolve()
    deck = catalog.get(args.deck)
    vector = (args.vectors or pdf.with_suffix(".vector.json")).expanduser().resolve()
    if args.vectors and not vector.is_file():
        raise ValueError(f"Missing explicit timeline: {vector}")
    output = output_path(args.output or get_paths(deck).build_dir / "video" /
                         f"{deck.document}-{args.theme}-{args.fps}fps.mp4", catalog.root)
    output_path(output.parent, catalog.root)
    snapshots = output_path(args.snapshots, catalog.root) if args.snapshots else None
    if output.exists():
        raise FileExistsError(f"Output already exists: {output}")
    metadata = pdf_manifest(pdf, vector if vector.is_file() else None, args.theme, args.page_map)
    timeline = json.loads(vector.read_text(encoding="utf-8")) if vector.is_file() else {"scenes": []}
    scenes = {scene["id"]: scene for scene in timeline["scenes"]}
    last_page = args.last_page or len(metadata["pages"])
    selected = [page for page in metadata["pages"] if args.first_page <= page["number"] <= last_page]
    if not selected or args.first_page < 1 or last_page > len(metadata["pages"]):
        raise ValueError("The requested page interval is outside the PDF")
    ratios = {round(page["width"] / page["height"], 5) for page in selected}
    if len(ratios) != 1:
        raise ValueError("All clip pages must have the same aspect ratio")
    height = 2 * round(args.width / next(iter(ratios)) / 2)
    sections = clip_sections(selected, scenes, args.fps, args.static_hold, args.checkpoint_hold)
    total_frames = sum(section.frames for section in sections)
    output.parent.mkdir(parents=True, exist_ok=True)
    if snapshots:
        snapshots.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".derivation-video-", dir=output.parent) as scratch:
        scratch = Path(scratch)
        movie = scratch / "render.mp4"
        tags = scratch / "chapters.txt"
        tags.write_text(chapters(sections, args.fps, deck.title), encoding="utf-8")
        command = [encoder, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                   "-f", "rawvideo", "-pixel_format", "bgra" if sys.byteorder == "little" else "argb",
                   "-video_size", f"{args.width}x{height}", "-framerate", str(args.fps), "-i", "pipe:0",
                   "-f", "ffmetadata", "-i", str(tags), "-map", "0:v:0", "-map_metadata", "1",
                   "-map_chapters", "1", "-an", "-c:v", "libx264", "-preset", "medium", "-crf", "18",
                   "-threads", str(args.threads), "-pix_fmt", "yuv420p", "-movflags", "+faststart",
                   str(movie)]
        with (scratch / "ffmpeg.log").open("w+") as log, fitz.open(pdf) as document:
            encoder_process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=log)
            surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, args.width, height)
            context = cairo.Context(surface)
            current_page = None
            completed = 0
            try:
                for section in sections:
                    meta = metadata["pages"][section.page - 1]
                    scene = scenes[meta["scene"]["id"]] if meta["scene"] else None
                    if current_page != section.page:
                        pixmap = document[section.page - 1].get_pixmap(
                            matrix=fitz.Matrix(args.width / meta["width"], height / meta["height"]),
                            alpha=False, annots=False)
                        background = cairo.ImageSurface.create_from_png(io.BytesIO(pixmap.tobytes("png")))
                        current_page = section.page
                        print(f"Rendering PDF page {section.page}: {section.title}", flush=True)
                    for index in range(section.frames):
                        scene_time = (section.start + (section.end - section.start) * index / section.frames
                                      if section.moving else section.start)
                        # Static holds reuse the same finished frame.
                        if section.moving or index == 0:
                            context.save()
                            context.scale(args.width / background.get_width(), height / background.get_height())
                            context.set_source_surface(background, 0, 0)
                            context.paint()
                            context.restore()
                            if scene:
                                x0, y0, x1, y1 = meta["scene"]["rect"]
                                context.save()
                                context.scale(args.width / meta["width"], height / meta["height"])
                                context.rectangle(x0, y0, x1 - x0, y1 - y0)
                                context.clip()
                                context.translate(x0, y0)
                                context.scale((x1 - x0) / scene["width"], (y1 - y0) / scene["height"])
                                context.set_source_rgba(*scene["background"])
                                context.paint()
                                draw_paths(context, scene_paths_at(scene, scene_time))
                                context.restore()
                            surface.flush()
                            if snapshots and (index == 0 or section.moving and index == section.frames // 2):
                                name = f"page-{section.page:02d}-time-{scene_time:06.2f}.png"
                                surface.write_to_png(str(snapshots / name))
                        encoder_process.stdin.write(surface.get_data())
                        completed += 1
                    print(f"Frames {completed}/{total_frames}", flush=True)
                encoder_process.stdin.close()
                returncode = encoder_process.wait()
                if returncode:
                    log.seek(0)
                    raise RuntimeError("FFmpeg failed: " + log.read())
            except BaseException:
                if encoder_process.stdin and not encoder_process.stdin.closed:
                    encoder_process.stdin.close()
                if encoder_process.poll() is None:
                    encoder_process.terminate()
                    try:
                        encoder_process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        encoder_process.kill()
                        encoder_process.wait()
                raise
            finally:
                surface.finish()
        os.replace(movie, output)
    return {"output": str(output), "bytes": output.stat().st_size, "frames": completed,
            "fps": args.fps, "seconds": completed / args.fps, "width": args.width, "height": height,
            "pages": [page["number"] for page in selected], "theme": args.theme}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path)
    parser.add_argument("deck", nargs="?")
    parser.add_argument("--pdf", type=Path, required=True, help="The accepted live PDF")
    parser.add_argument("--vectors", type=Path, help="Matching vector JSON; defaults to the PDF sidecar")
    parser.add_argument("--page-map", type=Path)
    parser.add_argument("--output", type=Path, help="New MP4 path; defaults below the deck build directory")
    parser.add_argument("--theme", choices=["light", "dark"], default="light")
    parser.add_argument("--first-page", type=int, default=1)
    parser.add_argument("--last-page", type=int)
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--fps", type=int, default=60)
    parser.add_argument("--static-hold", type=float, default=3)
    parser.add_argument("--checkpoint-hold", type=float, default=1)
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--snapshots", type=Path, help="Optional checkpoint/midpoint PNGs for visual review")
    args = parser.parse_args()
    print(json.dumps(render(args, Catalog(args.project)), indent=2))


if __name__ == "__main__":
    main()
