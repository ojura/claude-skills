"""Read the same project-owned colour definitions used by its TeX theme."""
from __future__ import annotations

from pathlib import Path
import re

from .catalog import Catalog

ROLES = ("background", "foreground", "accent", "muted", "guide", "faint")
DEFINITION = re.compile(r"\\definecolor\{([A-Za-z][A-Za-z0-9]*)\}\{HTML\}\{([0-9A-Fa-f]{6})\}")


def load_palettes(repository_root: str | Path | None = None) -> dict[str, dict[str, str]]:
    catalog = Catalog(repository_root)
    definition = catalog.data.get("palette")
    if not isinstance(definition, dict) or "file" not in definition:
        raise ValueError("Configure palette.file and palette.prefix in presentations.json")
    source = catalog.path(definition["file"])
    prefix = definition.get("prefix", "derivation")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", prefix):
        raise ValueError("The palette prefix must be a TeX colour-name prefix")
    palettes: dict[str, dict[str, str]] = {"light": {}, "dark": {}}
    for line in source.read_text(encoding="utf-8").splitlines():
        match = DEFINITION.fullmatch(line.split("%", 1)[0].strip())
        if match is None:
            continue
        name, value = match.groups()
        for theme, colours in palettes.items():
            beginning = prefix + theme.title()
            if not name.startswith(beginning):
                continue
            role = name[len(beginning):].lower()
            if role in colours:
                raise ValueError(f"Duplicate {theme} {role} colour in {source}")
            colours[role] = f"#{value.upper()}"
    for theme, colours in palettes.items():
        missing = set(ROLES) - colours.keys()
        if missing:
            raise ValueError(f"Missing {theme} colours in {source}: {', '.join(sorted(missing))}")
    return palettes
