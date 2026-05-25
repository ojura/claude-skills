#!/usr/bin/env python3
"""Install the static ChatGPT archive browser into a dump directory."""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dump_dir", help="ChatGPT archive/dump directory")
    parser.add_argument(
        "--force",
        action="store_true",
        help="overwrite existing browser template files",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    skill_root = Path(__file__).resolve().parents[1]
    source = skill_root / "assets" / "browser-template"
    dump_dir = Path(args.dump_dir).expanduser().resolve()
    target = dump_dir / "browser"

    if not source.is_dir():
        print(f"missing browser template: {source}", file=sys.stderr)
        return 2

    dump_dir.mkdir(parents=True, exist_ok=True)
    target.mkdir(parents=True, exist_ok=True)

    conflicts = []
    for src in source.rglob("*"):
        if not src.is_file():
            continue
        rel = src.relative_to(source)
        dst = target / rel
        if dst.exists() and not args.force:
            conflicts.append(str(dst))

    if conflicts:
        print("browser files already exist; rerun with --force to overwrite:", file=sys.stderr)
        for path in conflicts[:20]:
            print(f"  {path}", file=sys.stderr)
        if len(conflicts) > 20:
            print(f"  ... {len(conflicts) - 20} more", file=sys.stderr)
        return 1

    copied = 0
    for src in source.rglob("*"):
        if not src.is_file():
            continue
        rel = src.relative_to(source)
        dst = target / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        copied += 1

    print(f"installed {copied} browser files into {target}")
    print(f"open with a local server at: /browser/index.html")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
