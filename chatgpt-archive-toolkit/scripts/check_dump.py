#!/usr/bin/env python3
"""Validate a ChatGPT archive directory and report counts."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


INDEX_FILES = {
    "conversations": "indexes/conversations.json",
    "conversation_summaries": "indexes/conversation_summaries.json",
    "library_nodes": "indexes/library_nodes.json",
    "artifact_references": "indexes/artifact_references.json",
    "media_downloads": "indexes/media_downloads.json",
    "file_downloads": "indexes/file_downloads.json",
    "endpoint_snapshots": "indexes/endpoint_snapshots.json",
}

BROWSER_FILES = ("browser/index.html", "browser/styles.css", "browser/app.js")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dump_dir", help="ChatGPT archive/dump directory")
    parser.add_argument("--strict", action="store_true", help="return non-zero on warnings")
    return parser.parse_args()


def load_json(path: Path, errors: list[str]) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        errors.append(f"missing JSON file: {path}")
    except json.JSONDecodeError as exc:
        errors.append(f"invalid JSON in {path}: {exc}")
    return None


def as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for key in ("items", "conversations", "nodes", "data", "results"):
            candidate = value.get(key)
            if isinstance(candidate, list):
                return candidate
    return []


def existing_path(root: Path, value: Any) -> bool:
    if not value:
        return False
    path = Path(str(value))
    if path.is_absolute():
        return path.exists()
    return (root / path).exists()


def count_downloads(root: Path, rows: list[Any]) -> dict[str, int]:
    total = len(rows)
    ok = 0
    missing_payloads = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("ok") is True:
            ok += 1
            if not existing_path(root, row.get("path")):
                missing_payloads += 1
    return {"indexed": total, "ok": ok, "missing_ok_payloads": missing_payloads}


def check_referenced_paths(root: Path, rows: list[Any], key: str) -> int:
    missing = 0
    for row in rows:
        if isinstance(row, dict) and row.get(key) and not existing_path(root, row.get(key)):
            missing += 1
    return missing


def main() -> int:
    args = parse_args()
    root = Path(args.dump_dir).expanduser().resolve()
    errors: list[str] = []
    warnings: list[str] = []

    if not root.is_dir():
        print(json.dumps({"root": str(root), "errors": ["dump directory does not exist"]}, indent=2))
        return 2

    manifest = load_json(root / "manifest.json", errors)
    indexes: dict[str, list[Any]] = {}
    for name, rel in INDEX_FILES.items():
        path = root / rel
        if not path.exists():
            warnings.append(f"missing index: {rel}")
            indexes[name] = []
            continue
        loaded = load_json(path, errors)
        indexes[name] = as_list(loaded)

    conversations = indexes["conversations"]
    missing_conversation_json = check_referenced_paths(root, conversations, "path")
    if conversations and missing_conversation_json:
        warnings.append(f"{missing_conversation_json} conversation index entries point to missing JSON files")

    raw_endpoint_missing = check_referenced_paths(root, indexes["endpoint_snapshots"], "path")
    if raw_endpoint_missing:
        warnings.append(f"{raw_endpoint_missing} endpoint snapshots point to missing raw API files")

    missing_browser = [rel for rel in BROWSER_FILES if not (root / rel).exists()]
    if missing_browser:
        warnings.append("missing browser files: " + ", ".join(missing_browser))

    counts = manifest.get("counts", {}) if isinstance(manifest, dict) else {}
    report = {
        "root": str(root),
        "manifest": {
            "present": manifest is not None,
            "created_at": manifest.get("created_at") if isinstance(manifest, dict) else None,
            "declared_counts": counts,
        },
        "conversations": {
            "indexed": len(conversations),
            "missing_json": missing_conversation_json,
            "conversation_files_on_disk": len(list((root / "conversations").glob("*.json")))
            if (root / "conversations").is_dir()
            else 0,
        },
        "files": count_downloads(root, indexes["file_downloads"]),
        "media": count_downloads(root, indexes["media_downloads"]),
        "library_nodes": {"indexed": len(indexes["library_nodes"])},
        "artifact_references": {"indexed": len(indexes["artifact_references"])},
        "raw_api": {
            "endpoint_snapshots_indexed": len(indexes["endpoint_snapshots"]),
            "missing_snapshot_files": raw_endpoint_missing,
            "raw_api_files_on_disk": len(list((root / "raw_api").glob("*.json"))) if (root / "raw_api").is_dir() else 0,
        },
        "browser": {
            "present": not missing_browser,
            "missing": missing_browser,
        },
        "warnings": warnings,
        "errors": errors,
    }

    print(json.dumps(report, indent=2))
    if errors:
        return 2
    if args.strict and warnings:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
