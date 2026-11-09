"""Compile cataloged Beamer presentations into the selected build directory."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from .catalog import Catalog, Deck, PACKAGE_ROOT
from .paths import CachePaths, get_paths


TEX_PACKAGES = ("beamer.cls", "graphicx.sty", "etoolbox.sty", "hyperref.sty")
TEX_BINDINGS = {
    "theme": "DerivationTheme", "repository_root": "DerivationRepositoryRoot",
    "minted_output_dir": "DerivationMintedOutputDir", "assets": "DerivationAssets",
    "live": "DerivationLive", "handout": "DerivationCompactHandout",
}


def tex_environment(paths: CachePaths | None = None) -> dict[str, str]:
    paths = paths or get_paths()
    catalog = Catalog(paths.repository_root)
    env = os.environ.copy()
    env["DERIVATION_CACHE_ROOT"] = str(paths.cache_root)
    env["DERIVATION_BUILD_DIR"] = str(paths.build_dir)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    local_share = paths.texlive_dir / "usr/share"
    local_tex = local_share / "texlive/texmf-dist/tex"
    roots = [local_tex / "latex", local_tex / "generic", local_share / "texmf/tex/latex"]
    search = [str(root) + "//" for root in roots if root.is_dir()]
    search += [str(PACKAGE_ROOT / "tex") + "//", str(paths.repository_root)]
    search += [str(paths.repository_root / value.rstrip("/")) + ("//" if value.endswith("//") else "")
               for value in catalog.tex.get("search_paths", [])]
    env["TEXINPUTS"] = ":".join(search) + ":" + env.get("TEXINPUTS", "")
    return env


def missing_tex_packages(env: dict[str, str], packages=TEX_PACKAGES) -> list[str]:
    return [name for name in packages if subprocess.run(
        ["kpsewhich", name], env=env, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, check=False,
    ).returncode]


def fetch_tex_packages(paths: CachePaths) -> None:
    """Extract explicitly selected Debian archives; dependencies stay project-owned."""
    packages = Catalog(paths.repository_root).tex.get("debian_packages")
    selector = re.compile(r"[a-z0-9][a-z0-9+.-]*(?::[a-z0-9][a-z0-9-]*)?(?:=[A-Za-z0-9.+:~_-]+)?")
    if not isinstance(packages, list) or not packages or any(
            not isinstance(package, str) or selector.fullmatch(package) is None for package in packages):
        raise ValueError("Configure tex.debian_packages as a nonempty list of Debian package selectors "
                         "for --fetch-tex, or use an environment that already supplies tex.packages.")
    for command in ("apt-get", "dpkg-deb"):
        if not shutil.which(command):
            raise RuntimeError(f"{command} is unavailable; --fetch-tex requires Debian archive tools.")
    cache, destination = paths.tex_packages_dir, paths.texlive_dir
    cache.mkdir(parents=True, exist_ok=True)
    destination.mkdir(parents=True, exist_ok=True)
    with (cache / "download.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        for package in packages:
            with tempfile.TemporaryDirectory(prefix="download-", dir=cache) as temporary:
                staging = Path(temporary)
                subprocess.run(["apt-get", "download", package], cwd=staging, check=True)
                archives = list(staging.glob("*.deb"))
                if len(archives) != 1:
                    raise RuntimeError(f"Expected one downloaded archive for {package}; found {len(archives)}.")
                archive = cache / archives[0].name
                shutil.copyfile(archives[0], archive)
                subprocess.run(["dpkg-deb", "-x", str(archive), str(destination)], check=True)


def require_assets(deck: Deck, theme: str, modes: list[str], assets: Path) -> None:
    if deck.animation is None:
        return
    metadata_file = assets / "steps.json"
    required = {metadata_file, assets / "steps.tex"}
    if metadata_file.is_file():
        try:
            metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
            scenes = metadata["scenes"]
            identifiers = [scene["id"] for scene in scenes]
            if metadata["version"] != 1 or not identifiers or len(set(identifiers)) != len(identifiers):
                raise ValueError("Expected version 1 metadata with unique scene IDs")
            for scene in scenes:
                figures = []
                if "live" in modes:
                    figures.append(scene["poster"])
                if "handout" in modes:
                    figures.extend(step["figure"] for step in scene["steps"])
                if "steps" in modes:
                    figures.extend(step["full_figure"] for step in scene["steps"] if step["full_figure"] is not None)
                for figure in figures:
                    if Path(figure).name != figure:
                        raise ValueError(f"Figure must be a filename: {figure}")
                    required.add(assets / theme / figure)
        except (ValueError, KeyError, TypeError) as error:
            raise RuntimeError(f"Invalid scene assets for {deck.id}: {error}") from error
    if "live" in modes:
        required.add(assets / theme / deck.animation.manifest)
    missing = sorted(str(path) for path in required if not path.is_file())
    if missing:
        raise RuntimeError("Missing scene exports:\n  " + "\n  ".join(missing) +
                           f"\nBuild {deck.id} with --regenerate, or supply --assets-dir.")


@contextmanager
def source_workspace(deck: Deck, auxiliary: Path, assets: Path | None, *, copy_workspace=False):
    # The ASCII aliases keep minted 2.x shell commands independent of source
    # and cache directory names. Tools that write to cwd use this private view.
    with tempfile.TemporaryDirectory(prefix="derivation-latex-") as temporary:
        workspace = Path(temporary)
        output_alias = workspace / "output"
        if copy_workspace:
            output_alias.mkdir()
        else:
            output_alias.symlink_to(auxiliary, target_is_directory=True)

        def expose(source, target):
            if copy_workspace:
                copy_workspace_files(source, target)
            else:
                target.symlink_to(source, target_is_directory=source.is_dir())

        repository_view = workspace / "source"
        original, view = deck.repository_root, repository_view
        for component in deck.source_dir.relative_to(deck.repository_root).parts:
            view.mkdir(parents=True, exist_ok=True)
            for entry in original.iterdir():
                if entry.name not in (component, "build") and not entry.name.startswith("."):
                    expose(entry, view / entry.name)
            original, view = original / component, view / component
        view.mkdir(parents=True, exist_ok=True)
        for entry in original.iterdir():
            if not entry.name.startswith(".") and entry.name not in ("build", "svg-inkscape"):
                expose(entry, view / entry.name)
        assets_alias = None
        if assets is not None:
            assets_alias = workspace / "assets"
            expose(assets, assets_alias)
        try:
            yield view, repository_view, output_alias, assets_alias
        finally:
            if copy_workspace:
                copy_workspace_files(output_alias, auxiliary)


def copy_workspace_files(source: Path, destination: Path) -> None:
    """Copy regular source/output bytes without creating links or copying xattrs."""
    if source.is_dir():
        destination.mkdir(parents=True, exist_ok=True)
        for entry in source.iterdir():
            if entry.name not in {"build", ".git"}:
                copy_workspace_files(entry, destination / entry.name)
    else:
        shutil.copyfile(source, destination)


def build_one(deck: Deck, theme: str, mode: str, env: dict[str, str],
              paths: CachePaths, assets: Path | None = None, *, copy_workspace=False) -> Path:
    catalog = Catalog(deck.repository_root)
    stem = f"{deck.document}-{theme}-{mode}"
    auxiliary = paths.latex_dir / stem
    auxiliary.mkdir(parents=True, exist_ok=True)
    with source_workspace(deck, auxiliary, assets, copy_workspace=copy_workspace) as (
            working, repository, output, assets_alias):
        compile_env = env.copy()
        compile_env["TEXINPUTS"] = str(working) + ":" + str(deck.source_dir) + ":" + env.get("TEXINPUTS", "")
        definitions = []
        values = {"theme": theme, "repository_root": repository.as_posix(),
                  "minted_output_dir": output.as_posix()}
        bindings = catalog.tex.get("bindings", {})
        if mode == "handout":
            definitions.append(r"\PassOptionsToClass{handout}{beamer}")
            values["handout"] = "1"
        if mode == "live":
            values["live"] = "1"
        if assets_alias is not None:
            values["assets"] = assets_alias.as_posix()
        for role, value in values.items():
            names = {TEX_BINDINGS[role], bindings.get(role, TEX_BINDINGS[role])}
            for name in sorted(names):
                if not re.fullmatch(r"[A-Za-z]+", name):
                    raise ValueError(f"Invalid TeX macro binding: {name}")
                definitions.append(rf"\def\{name}{{{value}}}")
        palette = catalog.data.get("palette")
        if palette:
            definitions += [
                rf"\def\DerivationPaletteFile{{{(repository / palette['file']).as_posix()}}}",
                rf"\def\DerivationPalettePrefix{{{palette.get('prefix', 'derivation')}}}",
            ]
        definitions.append(
            rf"\AtBeginDocument{{\ifdefined\svgsetup\svgsetup{{inkscapepath={output.as_posix()}/svg-inkscape/}}\fi}}")
        definitions.append(rf"\input{{{deck.source.name}}}")
        engine = deck.build.get("engine", catalog.tex.get("engine", "pdflatex"))
        command = [engine, "-interaction=nonstopmode", "-halt-on-error",
                   "-file-line-error", "-shell-escape",
                   f"-output-directory={output}", f"-jobname={stem}", " ".join(definitions)]
        for number in (1, 2):
            terminal_log = auxiliary / f"pass-{number}.txt"
            with terminal_log.open("w", encoding="utf-8") as output_log:
                result = subprocess.run(command, cwd=working, env=compile_env, stdout=output_log,
                                        stderr=subprocess.STDOUT, check=False)
            if result.returncode:
                tail = "\n".join(terminal_log.read_text(errors="replace").splitlines()[-45:])
                raise RuntimeError(f"LaTeX failed for {deck.id}/{theme}/{mode}:\n{tail}\nFull log: {terminal_log}")
    navigation = (auxiliary / f"{stem}.nav").read_text(encoding="utf-8")
    frames = re.findall(r"\\beamer@framepages\s*\{(\d+)\}\{(\d+)\}", navigation)
    page_count = re.search(r"\\beamer@documentpages\s*\{(\d+)\}", navigation)
    if not frames or page_count is None:
        raise RuntimeError(f"{deck.id}: LaTeX did not produce Beamer navigation metadata")
    if mode == "handout" and any(first != last for first, last in frames):
        raise RuntimeError(f"{stem} has multiple physical pages for a logical frame")
    destination = paths.pdf(theme, mode)
    destination.parent.mkdir(parents=True, exist_ok=True)
    copy_file = shutil.copyfile if copy_workspace else shutil.copy2
    copy_file(auxiliary / f"{stem}.pdf", destination)
    page_map = [None] * int(page_count.group(1))
    for logical, (first, last) in enumerate(frames, 1):
        for page in range(int(first), int(last) + 1):
            if not 1 <= page <= len(page_map) or page_map[page - 1] is not None:
                raise RuntimeError(f"{deck.id}: invalid overlapping Beamer page ranges")
            page_map[page - 1] = logical
    if any(number is None for number in page_map):
        raise RuntimeError(f"{deck.id}: Beamer navigation does not cover every PDF page")
    destination.with_suffix(".presentation.json").write_text(json.dumps({
        "version": 1, "deck": deck.id, "title": deck.title, "theme": theme,
        "logicalSlides": len(frames), "pages": [
            {"number": index, "logical": logical}
            for index, logical in enumerate(page_map, 1)
        ],
    }, indent=2) + "\n", encoding="utf-8")
    sidecar = destination.with_suffix(".vector.json")
    if mode == "live" and deck.animation is not None:
        copy_file(assets / theme / deck.animation.manifest, sidecar)
    elif sidecar.exists():
        sidecar.unlink()
    print(f"Built {destination} ({len(frames)} frames, {int(page_count.group(1))} pages)", flush=True)
    return destination


def build_deck(deck: Deck, themes: list[str], modes: list[str], paths: CachePaths, *,
               regenerate: bool = False, fetch_tex: bool = False,
               assets_dir: Path | None = None, copy_workspace: bool = False,
               isolated_cache: bool = False) -> list[Path]:
    catalog = Catalog(deck.repository_root)
    if deck.build.get("kind", "beamer") != "beamer":
        raise ValueError(f"{deck.id}: build the source with its declared project tool, then supply its compiled PDF")
    engine = deck.build.get("engine", catalog.tex.get("engine", "pdflatex"))
    if engine not in ("pdflatex", "lualatex", "xelatex"):
        raise ValueError(f"Unsupported TeX engine: {engine}")
    packages = tuple(catalog.tex.get("packages", TEX_PACKAGES))
    commands = [engine, "kpsewhich", *catalog.tex.get("commands", [])]
    for command in commands:
        if not shutil.which(command):
            raise RuntimeError(f"Required build command {command} is unavailable. "
                               "Install the project's TeX/tools dependencies or use its authoring image.")
    if isolated_cache:
        if regenerate or fetch_tex:
            raise ValueError("A new isolated TeX cache uses existing figure assets and installed TeX dependencies")
        try:
            paths.cache_root.mkdir(parents=True, exist_ok=False)
        except FileExistsError as error:
            raise ValueError(f"An isolated build requires a new cache directory: {paths.cache_root}") from error
    paths.source_cache_dir.mkdir(parents=True, exist_ok=True)
    with (paths.source_cache_dir / "build.lock").open("a") as lock:
        if not isolated_cache:
            fcntl.flock(lock, fcntl.LOCK_EX)
        env = tex_environment(paths)
        missing = missing_tex_packages(env, packages)
        if missing and fetch_tex:
            fetch_tex_packages(paths)
            env = tex_environment(paths)
            missing = missing_tex_packages(env, packages)
        if missing:
            hint = ("The configured Debian archives were extracted, but these resources are still missing. "
                    "Archive extraction does not install dependency packages or configure fonts."
                    if fetch_tex else
                    "Install the project's TeX dependencies, or configure tex.debian_packages "
                    "for opt-in --fetch-tex archive extraction.")
            raise RuntimeError("Missing TeX resources: " + ", ".join(missing) + ". " + hint)
        assets = assets_dir.resolve() if assets_dir else (deck.animation.assets if deck.animation else None)
        if regenerate and deck.animation is not None:
            from .export import run_export
            assets = assets_dir.resolve() if assets_dir else paths.assets_dir
            run_export(deck, themes, paths, assets, environment=env)
        outputs = []
        for theme in themes:
            if deck.animation is not None:
                require_assets(deck, theme, modes, assets)
            for mode in modes:
                outputs.append(build_one(deck, theme, mode, env, paths, assets,
                                         copy_workspace=copy_workspace))
        return outputs
