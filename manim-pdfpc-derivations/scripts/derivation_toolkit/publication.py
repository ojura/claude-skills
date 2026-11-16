"""Publish related generated files and restore their previous versions on failure."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import stat
import tempfile
from typing import Iterable, Mapping


def publish_files(replacements: Mapping[Path, Path], removals: Iterable[Path] = ()) -> None:
    """Copy all inputs first, then replace destinations, rolling back failed changes.

    Keys are destination paths and values are prepared source files. Temporary
    files and backups share each destination's filesystem so replacement and
    restoration use rename. Callers serialize writers and validate the complete
    generated set before calling this function.
    """
    replacements = {Path(target): Path(source) for target, source in replacements.items()}
    targets = list(replacements)
    targets.extend(sorted(set(map(Path, removals)) - set(targets)))
    temporary: dict[Path, Path] = {}
    prepared: dict[Path, Path] = {}
    applied: list[tuple[Path, Path | None]] = []
    keep_backups = False
    try:
        for number, target in enumerate(targets):
            if target.is_dir():
                raise ValueError(f"Generated file destination is a directory: {target}")
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.parent not in temporary:
                temporary[target.parent] = Path(tempfile.mkdtemp(
                    prefix=".derivation-publish-", dir=target.parent))
            if target in replacements:
                source = replacements[target]
                staged = temporary[target.parent] / f"new-{number}"
                shutil.copyfile(source, staged)
                staged.chmod(stat.S_IMODE(source.stat().st_mode))
                prepared[target] = staged
        for number, target in enumerate(targets):
            backup = (temporary[target.parent] / f"old-{number}"
                      if os.path.lexists(target) else None)
            # Record recovery before moving the original. Its backup's presence
            # distinguishes a completed move from an interrupted/failed call.
            applied.append((target, backup))
            if backup is not None:
                os.replace(target, backup)
            if target in prepared:
                os.replace(prepared[target], target)
    except BaseException as error:
        # Retain backups even if restoring them is itself interrupted.
        keep_backups = True
        failures = []
        for target, backup in reversed(applied):
            try:
                if backup is not None:
                    if os.path.lexists(backup):
                        os.replace(backup, target)
                elif os.path.lexists(target):
                    target.unlink()
            except BaseException as restore_error:
                failures.append(f"{target}: {restore_error}")
        if failures:
            raise RuntimeError(
                "Publication failed and some previous files could not be restored: "
                + "; ".join(failures) + ". Backups remain in "
                + ", ".join(map(str, temporary.values()))) from error
        keep_backups = False
        raise
    finally:
        if not keep_backups:
            for directory in temporary.values():
                shutil.rmtree(directory)
