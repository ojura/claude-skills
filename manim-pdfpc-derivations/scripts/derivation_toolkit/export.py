"""Export registered Manim scenes as native timelines and matching selectable PDFs."""
from __future__ import annotations

import fcntl
from dataclasses import asdict
import hashlib
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from types import ModuleType

from .catalog import Catalog, Deck, PACKAGE_ROOT, THEMES
from .palette import load_palettes, palette_tex
from .paths import CachePaths, get_paths
from .publication import publish_files


REQUIREMENTS = Path(__file__).parent / "animation" / "requirements.lock"


def authoring_python(paths: CachePaths) -> Path:
    """Reuse the locked container interpreter or prepare the shared cached venv."""
    expected = {}
    for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        name, version = line.split("==", 1)
        expected[name] = version
    matches = True
    for name, version in expected.items():
        try:
            matches &= importlib.metadata.version(name) == version
        except importlib.metadata.PackageNotFoundError:
            matches = False
    if matches:
        return Path(sys.executable)
    environment = paths.manim_venv_dir
    environment.parent.mkdir(parents=True, exist_ok=True)
    with environment.with_suffix(".setup.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        python = environment / "bin/python"
        observed = inspect_interpreter(python, expected)
        if observed is None:
            subprocess.run([sys.executable, "-m", "venv", "--clear", str(environment)], check=True)
            subprocess.run([str(python), "-m", "pip", "install", "-r", str(REQUIREMENTS)], check=True)
            observed = inspect_interpreter(python, expected)
            if observed is None:
                raise RuntimeError(f"The authoring interpreter does not satisfy the locked environment: {python}")
        observed["requirements_sha256"] = hashlib.sha256(REQUIREMENTS.read_bytes()).hexdigest()
        observed["executable_sha256"] = hashlib.sha256(python.resolve().read_bytes()).hexdigest()
        (environment / ".derivation-runtime.json").write_text(json.dumps(observed, indent=2) + "\n")
    return python


def inspect_interpreter(python: Path, expected: dict[str, str]) -> dict | None:
    """Validate the selected executable and packages, including an existing venv."""
    if not python.is_file() or not os.access(python, os.X_OK):
        return None
    program = (
        "import importlib.metadata,json,sys; "
        "names=json.loads(sys.argv[1]); "
        "print(json.dumps({'implementation':sys.implementation.name,"
        "'cache_tag':sys.implementation.cache_tag,"
        "'versions':{name:importlib.metadata.version(name) for name in names}}))"
    )
    try:
        result = subprocess.run([str(python), "-I", "-c", program, json.dumps(list(expected))],
                                capture_output=True, text=True, timeout=30, check=False)
        observed = json.loads(result.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    if (result.returncode or observed.get("implementation") != sys.implementation.name
            or observed.get("cache_tag") != sys.implementation.cache_tag
            or observed.get("versions") != expected):
        return None
    return observed


def load_provider(deck: Deck) -> ModuleType:
    if deck.animation is None:
        raise ValueError(f"{deck.id} has no registered animation provider")
    name = "derivation_provider_" + hashlib.sha256(str(deck.animation.provider).encode()).hexdigest()[:16]
    spec = importlib.util.spec_from_file_location(name, deck.animation.provider)
    if spec is None or spec.loader is None:
        raise ValueError(f"Cannot load scene provider: {deck.animation.provider}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    scenes = list(module.SCENES)
    identifiers = [scene.scene_id for scene in scenes]
    if (not scenes or len(set(identifiers)) != len(identifiers)
            or any(not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", name) for name in identifiers)):
        raise ValueError(f"{deck.id}: scene registration must contain unique IDs")
    return module


def configure(theme: str, paths: CachePaths | None = None, *, canvas=None) -> None:
    from manim import config
    from .animation.layout import DEFAULT_CANVAS

    paths = paths or get_paths()
    media = paths.manim_media_dir
    (media / "tex").mkdir(parents=True, exist_ok=True)
    config.media_dir = str(media)
    config.tex_dir = str(media / "tex")
    (canvas or DEFAULT_CANVAS).configure()
    config.background_color = load_palettes(paths.repository_root)[theme]["background"]
    config.write_to_movie = False
    config.save_last_frame = False
    config.dry_run = True
    config.verbosity = "ERROR"


def compare_checkpoint(scene, state, destination: Path) -> None:
    import numpy as np
    from PIL import Image
    from .animation.vector_scene import scene_paths_at, write_snapshot

    rendered = scene_paths_at(scene, state["time"])
    expected = destination / ".checkpoint-manim.png"
    actual = destination / ".checkpoint-timeline.png"
    crop = [0, 0, scene["width"], scene["height"]]
    write_snapshot(expected, state["full_paths"], scene["background"], crop, png_scale=1)
    write_snapshot(actual, rendered, scene["background"], crop, png_scale=1)
    reference = np.asarray(Image.open(expected).convert("RGBA")).astype(np.int16)
    output = np.asarray(Image.open(actual).convert("RGBA")).astype(np.int16)
    difference = np.abs(reference - output)
    expected.unlink()
    actual.unlink()
    if np.max(difference) != 0:
        raise AssertionError(
            f'{scene["id"]} t={state["time"]}: Manim and timeline differ at '
            f'{np.count_nonzero(np.any(difference, axis=2))} pixels'
        )


def build_scene(cls, theme: str, output: Path, sample_rate: int, preview_output: Path | None,
                *, paths: CachePaths | None = None, reference_time: float | None = None):
    from .animation.vector_scene import scene_paths_at, write_snapshot

    paths = paths or get_paths()
    configure(theme, paths, canvas=cls.canvas)
    authoring = cls(load_palettes(paths.repository_root)[theme], sample_rate=sample_rate)
    authoring.construct()
    scene = authoring.export()
    folder = output / theme
    folder.mkdir(parents=True, exist_ok=True)
    preview_folder = preview_output / theme if preview_output is not None else None
    checkpoint_times = {point["time"] for point in scene["checkpoints"]}
    paths.manim_work_dir.mkdir(parents=True, exist_ok=True)
    steps = []
    with tempfile.TemporaryDirectory(prefix="checkpoint-", dir=paths.manim_work_dir) as temporary:
        for number, state in enumerate(authoring.handout_states, 1):
            compare_checkpoint(scene, state, Path(temporary))
            stem = f"{scene['id']}-step-{number:02d}"
            full_stem = f"{scene['id']}-full-step-{number:02d}"
            full_figure = f"{full_stem}.pdf" if state["time"] in checkpoint_times else None
            full_paths = state["full_paths"]
            write_snapshot(folder / f"{stem}.pdf", state["paths"], scene["background"], state["crop"])
            if full_figure is not None:
                write_snapshot(folder / full_figure, full_paths, scene["background"], canvas=authoring.canvas)
            if preview_folder is not None:
                write_snapshot(preview_folder / f"{stem}.png", state["paths"], scene["background"],
                               state["crop"], png_scale=1.5)
                write_snapshot(preview_folder / f"{full_stem}.png", full_paths,
                               scene["background"], png_scale=1, canvas=authoring.canvas)
            steps.append({
                "index": number, "time": state["time"], "caption": state["caption"],
                "figure": f"{stem}.pdf", "full_figure": full_figure,
                "width": state["crop"][2], "height": state["crop"][3],
                "full_preview": f"{full_stem}.png",
            })
    if not steps or steps[0]["time"] != 0 or steps[0]["full_figure"] is None:
        raise ValueError(f"{scene['id']}: the initial handout state must be a teaching checkpoint")
    if {step["time"] for step in steps if step["full_figure"] is not None} != checkpoint_times:
        raise ValueError(f"{scene['id']}: every teaching checkpoint needs a full-frame snapshot")
    reference_time = scene["duration"] / 2 if reference_time is None else reference_time
    if preview_folder is not None:
        write_snapshot(preview_folder / f"{scene['id']}-midpoint.png",
                       scene_paths_at(scene, reference_time), scene["background"], png_scale=1,
                       canvas=authoring.canvas)
    metadata = {
        "id": scene["id"], "duration": scene["duration"],
        "width": scene["width"], "height": scene["height"],
        "poster": steps[0]["full_figure"], "steps": steps, "reference_time": reference_time,
        "handout_layout": asdict(authoring.handout_layout),
    }
    count = sum(len(item["keyframes"]) for item in scene["objects"])
    print(f"{theme} {scene['id']}: {len(scene['objects'])} paths, {count} keyframes, "
          f"{scene['duration']:.2f} s, {len(steps)} matching snapshots, "
          f"{len(scene['checkpoints'])} live checkpoints", flush=True)
    return scene, metadata


def metadata_tex(metadata: dict, results: dict | None = None) -> str:
    lines = ["% Generated by derivation_toolkit.export from authored scenes and captions."]
    for scene in metadata["scenes"]:
        name = scene["id"]
        lines.append(r"\expandafter\def\csname DerivationPoster@" + name
                     + r"\endcsname{" + scene["poster"] + "}")
        for step in scene["steps"]:
            suffix = name + "@" + str(step["index"])
            lines.append(r"\expandafter\def\csname DerivationFigure@" + suffix
                         + r"\endcsname{" + step["figure"] + "}")
            lines.append(r"\expandafter\def\csname DerivationCaption@" + suffix
                         + r"\endcsname{" + step["caption"] + "}")
        full_steps = [step for step in scene["steps"] if step["full_figure"] is not None]
        overlays = "".join(r"\only<" + str(number) + r">{\DerivationGraphic{"
                           + step["full_figure"] + "}}"
                           for number, step in enumerate(full_steps, 1))
        lines.append(r"\expandafter\def\csname DerivationFullSteps@" + name
                     + r"\endcsname{" + overlays + "}")
        layout = scene.get("handout_layout", {
            "columns": 2, "max_height_fraction": .65, "row_gap_mm": 1.0,
        })
        columns = layout["columns"]
        panels = []
        for index, step in enumerate(scene["steps"], 1):
            panels.append(r"\DerivationPanel{" + name + "}{" + str(step["index"]) + "}")
            if index < len(scene["steps"]):
                panels.append(r"\par\vspace{" + str(layout["row_gap_mm"]) + "mm}"
                              if index % columns == 0 else r"\hspace{\DerivationPanelGap}")
        for field, value in {
            "Columns": columns, "Rows": (len(scene["steps"]) + columns - 1) // columns,
            "HeightFraction": layout["max_height_fraction"], "RowGap": layout["row_gap_mm"],
        }.items():
            lines.append(r"\expandafter\def\csname Derivation" + field + "@" + name
                         + r"\endcsname{" + str(value) + "}")
        lines.append(r"\expandafter\def\csname DerivationHandout@" + name
                     + r"\endcsname{" + "".join(panels) + "}")
    for scene, equations in (results or {}).items():
        lines.append(r"\expandafter\def\csname DerivationResult@" + scene
                     + r"\endcsname{" + r"\\".join(equations) + "}")
    return "\n".join(lines) + "\n"


def _filename(value: str) -> str:
    if not isinstance(value, str) or not value or Path(value).name != value or value in (".", ".."):
        raise ValueError(f"Invalid generated filename in steps metadata: {value!r}")
    return value


def _generated_files(metadata: dict, *, previews: bool = False) -> set[Path]:
    """Identify generated files using the existing public steps metadata."""
    result = set()
    for theme in metadata.get("themes", []):
        if theme not in THEMES:
            raise ValueError(f"Invalid theme in steps metadata: {theme!r}")
        if not previews:
            result.add(Path(theme) / _filename(metadata["manifest"]))
        for scene in metadata["scenes"]:
            if previews:
                result.add(Path(theme) / _filename(scene["id"] + "-midpoint.png"))
            for step in scene["steps"]:
                figure = _filename(step["figure"])
                names = ((str(Path(figure).with_suffix(".png")), step["full_preview"])
                         if previews else (figure, step["full_figure"]))
                for name in names:
                    if name is not None:
                        result.add(Path(theme) / _filename(name))
    return result


def _validate_export(metadata: dict, timelines: dict, staged: Path, output: Path) -> None:
    definitions = {item["id"]: item for item in metadata["scenes"]}
    for theme, scenes in timelines.items():
        if set(scenes) != set(definitions):
            raise ValueError(
                f"{theme}: scene metadata and timeline IDs differ; export all affected scenes in both themes")
        for identifier, scene in scenes.items():
            definition = definitions[identifier]
            if any(scene[key] != definition[key] for key in ("duration", "width", "height")):
                raise ValueError(f"{theme}/{identifier}: timeline dimensions or duration differ from shared metadata")
            stops = [step["time"] for step in definition["steps"] if step["full_figure"] is not None]
            if [point["time"] for point in scene["checkpoints"]] != stops:
                raise ValueError(f"{theme}/{identifier}: timeline checkpoints differ from shared metadata")
    for relative in _generated_files(metadata):
        candidate = staged / relative
        source = candidate if candidate.is_file() else output / relative
        if not source.is_file() or source.stat().st_size == 0:
            raise ValueError(f"Missing generated file {relative}; regenerate the affected scenes in both themes")


def _stage_export(deck, themes, paths, output, staged, previews, provider, selected, scene_id, sample_rate):
    from .animation.vector_scene import write_json

    metadata_file = output / "steps.json"
    previous = (json.loads(metadata_file.read_text(encoding="utf-8"))
                if metadata_file.is_file() else {"version": 1, "scenes": []})
    previous_manifest = previous.get("manifest", deck.animation.manifest)
    previous_themes = previous.get("themes", [
        theme for theme in THEMES if (output / theme / previous_manifest).is_file()])
    previous = {**previous, "manifest": previous_manifest, "themes": previous_themes}
    _generated_files(previous)
    old_metadata = {item["id"]: item for item in previous["scenes"]}
    registered = [cls.scene_id for cls in provider.SCENES]
    reference_times = getattr(provider, "REFERENCE_TIMES", {})
    all_themes = sorted(set(previous_themes) | set(themes))
    timelines, definitions = {}, {}
    for theme in all_themes:
        source = output / theme / previous_manifest
        retain = theme not in themes or scene_id is not None
        existing = json.loads(source.read_text(encoding="utf-8"))["scenes"] if retain and source.is_file() else []
        scenes = {item["id"]: item for item in existing if item["id"] in registered}
        theme_metadata = {identifier: old_metadata[identifier] for identifier in scenes
                          if identifier in old_metadata}
        if theme in themes:
            for cls in selected:
                scene, scene_metadata = build_scene(
                    cls, theme, staged, sample_rate, previews, paths=paths,
                    reference_time=reference_times.get(cls.scene_id))
                scenes[scene["id"]] = scene
                theme_metadata[scene["id"]] = scene_metadata
        timelines[theme] = scenes
        definitions[theme] = theme_metadata
    first_theme = all_themes[0]
    common = definitions[first_theme]
    for theme, theme_metadata in definitions.items():
        if theme_metadata != common:
            differing = sorted(identifier for identifier in set(common) | set(theme_metadata)
                               if common.get(identifier) != theme_metadata.get(identifier))
            hint = ("regenerate the affected scenes in both themes" if set(all_themes) - set(themes)
                    else "author matching captions, checkpoints, dimensions and handout layouts for both themes")
            raise ValueError(f"Shared metadata differs between {first_theme} and {theme} for "
                             f"{', '.join(differing)}; {hint}")
    metadata = {**previous, "manifest": deck.animation.manifest, "themes": all_themes,
                "scenes": [common[identifier] for identifier in registered if identifier in common]}
    for theme, scenes in timelines.items():
        manifest_file = staged / theme / deck.animation.manifest
        write_json(manifest_file, {"version": 1, "scenes": [
            scenes[identifier] for identifier in registered if identifier in scenes]})
        print(f"{theme} manifest: {manifest_file.stat().st_size / 1024 / 1024:.2f} MiB", flush=True)
    _validate_export(metadata, timelines, staged, output)
    (staged / "palettes.tex").write_text(palette_tex(deck.repository_root), encoding="utf-8")
    write_json(staged / "steps.json", metadata)
    (staged / "steps.tex").write_text(metadata_tex(metadata, getattr(provider, "RESULTS", {})), encoding="utf-8")
    return previous, metadata


def export_deck(deck: Deck, themes: list[str], paths: CachePaths, output: Path, *,
                scene_id: str | None = None, sample_rate: int = 30,
                preview_output: Path | None = None) -> None:
    if not 10 <= sample_rate <= 120:
        raise ValueError("The sample rate must be from 10 to 120")
    if not themes or set(themes) - set(THEMES):
        raise ValueError("Select light, dark or both themes")
    themes = list(dict.fromkeys(themes))
    provider = load_provider(deck)
    selected = [cls for cls in provider.SCENES if scene_id is None or cls.scene_id == scene_id]
    if not selected:
        raise ValueError(f"{deck.id}: unknown scene '{scene_id}'")
    output.mkdir(parents=True, exist_ok=True)
    paths.manim_work_dir.mkdir(parents=True, exist_ok=True)
    # Lock the directory itself, whose identity stays fixed during publication.
    # Exporters using a different cache still serialize updates to this output.
    descriptor = os.open(output, os.O_RDONLY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        with tempfile.TemporaryDirectory(prefix="export-", dir=paths.manim_work_dir) as temporary:
            staged = Path(temporary) / "figures"
            staged.mkdir()
            previews = Path(temporary) / "previews" if preview_output is not None else None
            previous, metadata = _stage_export(
                deck, themes, paths, output, staged, previews, provider, selected, scene_id, sample_rate)
            replacements = {output / source.relative_to(staged): source
                            for source in sorted(staged.rglob("*")) if source.is_file()}
            removals = {output / relative for relative in
                        _generated_files(previous) - _generated_files(metadata)}
            if previews is not None:
                replacements.update({preview_output / source.relative_to(previews): source
                                     for source in sorted(previews.rglob("*")) if source.is_file()})
                removals.update(preview_output / relative for relative in
                                _generated_files(previous, previews=True) - _generated_files(metadata, previews=True))
            publish_files(replacements, removals)
    finally:
        os.close(descriptor)
    if preview_output is not None:
        print(f"PNG previews: {preview_output.resolve()}", flush=True)


def run_export(deck: Deck, themes: list[str], paths: CachePaths, output: Path, *,
               scene_id: str | None = None, sample_rate: int = 30,
               previews: bool = False, preview_output: Path | None = None,
               environment: dict[str, str] | None = None) -> None:
    python = authoring_python(paths)
    command = [str(python), "-B", "-m", "derivation_toolkit",
               "--project", str(deck.repository_root),
               "export", deck.id, "--theme", "both" if len(themes) == 2 else themes[0],
               "--output-dir", str(output), "--cache-root", str(paths.cache_root),
               "--sample-rate", str(sample_rate), "--in-authoring-environment"]
    if scene_id:
        command += ["--scene", scene_id]
    if previews:
        command += ["--previews", "--preview-output", str(preview_output or paths.preview_dir)]
    env = (environment or os.environ).copy()
    env["PYTHONPATH"] = str(PACKAGE_ROOT.parent) + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    subprocess.run(command, cwd=deck.repository_root, env=env, check=True)
