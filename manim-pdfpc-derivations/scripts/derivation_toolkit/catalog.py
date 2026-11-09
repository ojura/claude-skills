"""Read project-owned presentation sources and configuration."""
from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import re
from typing import Any

THEMES = ("light", "dark")
MODES = ("live", "steps", "handout")
PACKAGE_ROOT = Path(__file__).resolve().parent


def project_root(start: str | Path | None = None) -> Path:
    """Find an explicit project, or the nearest catalog above the current directory."""
    location = Path(start or Path.cwd()).expanduser().resolve()
    if location.is_file():
        location = location.parent
    if start is not None:
        if not (location / "presentations.json").is_file():
            raise ValueError(f"No presentations.json in project: {location}")
        return location
    for candidate in (location, *location.parents):
        if (candidate / "presentations.json").is_file():
            return candidate
    raise ValueError("Select a project with --project, or run inside a directory containing presentations.json")


@dataclass(frozen=True)
class Animation:
    provider: Path
    assets: Path
    manifest: str


@dataclass(frozen=True)
class Deck:
    repository_root: Path
    id: str
    title: str
    source: Path
    language: str = "und"
    archived: bool = False
    animation: Animation | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    build: dict[str, Any] = field(default_factory=dict)

    @property
    def source_dir(self) -> Path:
        return self.source.parent

    @property
    def document(self) -> str:
        return self.source.stem


class Catalog:
    def __init__(self, repository_root: str | Path | None = None):
        self.root = project_root(repository_root)
        self.data = json.loads((self.root / "presentations.json").read_text(encoding="utf-8"))
        if self.data.get("version") not in (1, 2):
            raise ValueError("Unsupported presentation catalog version")
        self.project = self.data.get("project", {})
        self.tex = self.data.get("tex", {})
        self.native = self.data.get("native", {})
        self.package = self.data.get("package", {})
        self.decks: dict[str, Deck] = {}
        sources: set[Path] = set()
        for item in self.data["decks"]:
            identifier = item["id"]
            if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", identifier) or identifier in self.decks:
                raise ValueError(f"Invalid or duplicate deck ID: {identifier}")
            source = self.path(item["source"])
            if source in sources:
                raise ValueError(f"Duplicate deck source: {source}")
            sources.add(source)
            building = dict(item.get("build", {}))
            if building.get("kind", "beamer") == "beamer" and source.suffix != ".tex":
                raise ValueError(f"A Beamer source must be a TeX file: {source}")
            language = item.get("language", "und")
            if not isinstance(language, str) or not re.fullmatch(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*", language):
                raise ValueError(f"Invalid language tag: {identifier}")
            archived = item.get("archived", False)
            if type(archived) is not bool:
                raise ValueError(f"Deck archive flag must be boolean: {identifier}")
            animation = None
            if "animation" in item:
                definition = item["animation"]
                manifest = definition.get("manifest", "timeline.vector.json")
                if Path(manifest).name != manifest or not manifest.endswith(".vector.json"):
                    raise ValueError(f"Invalid timeline filename: {manifest}")
                animation = Animation(self.path(definition["provider"]),
                                      self.path(definition["assets"]), manifest)
            known = {"id", "title", "source", "language", "archived", "animation", "metadata", "build"}
            metadata = {**{key: value for key, value in item.items() if key not in known},
                        **item.get("metadata", {})}
            self.decks[identifier] = Deck(
                self.root, identifier, item["title"], source, language,
                archived, animation, metadata, building,
            )
        self.default = self.data["default"]
        if self.default not in self.decks:
            raise ValueError("The default presentation is absent from the catalog")
        self.native_helper = self.path(self.data["native_helper"]) if "native_helper" in self.data else None

    @property
    def title(self) -> str:
        return self.project.get("title", self.root.name)

    def path(self, relative: str) -> Path:
        path = (self.root / relative).resolve()
        try:
            path.relative_to(self.root)
        except ValueError as error:
            raise ValueError(f"Catalog path leaves the project: {relative}") from error
        return path

    def get(self, identifier: str | None = None) -> Deck:
        key = identifier or self.default
        try:
            return self.decks[key]
        except KeyError as error:
            raise ValueError(f"Unknown presentation '{key}'; use list to see the catalog") from error

    def select(self, identifier: str | None = None) -> list[Deck]:
        return list(self.decks.values()) if identifier == "all" else [self.get(identifier)]
