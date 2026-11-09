#!/usr/bin/env python3
"""Compare native Cairo output with independently exported Manim timeline PNGs."""

import argparse
import hashlib
import json
import subprocess
from pathlib import Path

import numpy as np
from PIL import Image


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("probe", type=Path)
    parser.add_argument("reference", type=Path, help="Theme folder with PNG previews")
    parser.add_argument("--assets", type=Path,
                        help="Generated theme folder with the timeline (default: reference)")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--midpoints", action="store_true",
                        help="Also compare <scene>-midpoint.png at steps.json reference_time")
    parser.add_argument("--scene", action="append", help="Compare only the requested scene ID")
    args = parser.parse_args(argv)
    args.output.mkdir(parents=True, exist_ok=True)
    assets = args.assets or args.reference
    metadata = json.loads((assets.parent / "steps.json").read_text())
    manifest = assets / metadata.get("manifest", "derivations.vector.json")
    data = json.loads(manifest.read_text())
    recorded = {scene["id"]: scene for scene in metadata["scenes"]}
    results = []
    for scene in data["scenes"]:
        if args.scene and scene["id"] not in args.scene:
            continue
        checkpoints = [(step["full_preview"], step["time"])
                       for step in recorded[scene["id"]]["steps"]]
        if args.midpoints:
            checkpoints.append((f"{scene['id']}-midpoint.png", recorded[scene["id"]]["reference_time"]))
        for name, position in checkpoints:
            reference = args.reference / name
            actual = args.output / name
            with Image.open(reference) as source:
                width, height = source.size
                expected = np.asarray(source.convert("RGBA")).astype(np.int16)
            subprocess.run([str(args.probe.resolve()), "render", str(manifest.resolve()),
                            scene["id"], str(position), str(actual.resolve()),
                            str(width), str(height)], check=True, capture_output=True, text=True)
            with Image.open(actual) as image:
                observed = np.asarray(image.convert("RGBA")).astype(np.int16)
            difference = np.abs(observed - expected)
            results.append({"scene": scene["id"], "time": position, "image": name,
                            "changed_pixels": int(np.count_nonzero(np.any(difference, axis=2))),
                            "maximum_channel_error": int(difference.max()),
                            "mean_channel_error": float(difference.mean())})
    report = {"manifest": str(manifest.resolve()),
              "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
              "comparisons": results,
              "pixel_identical": bool(results) and all(item["changed_pixels"] == 0 for item in results)}
    (args.output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return 0 if report["pixel_identical"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
