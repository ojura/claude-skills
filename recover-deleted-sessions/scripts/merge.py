#!/usr/bin/env python3
# Scrubbed for public release. Set the CONFIG constants (RECOVERY_DIR, DEV, etc.) below or via env before running.
"""Merge archive backups + carved sessions into final per-UUID jsonls.

For each deleted UUID:
1. If ~/claude_archive/<UUID>.jsonl exists, use it as canonical (it's a complete
   pre-deletion backup; the redact script copied originals here verbatim).
   But also bring in any carved lines (by .uuid) not already present in the
   archive -- covers writes that happened between backup and deletion.
2. Otherwise use <RECOVERY_DIR>/recovered/<UUID>.jsonl (carved from raw blocks).
3. Sort merged content by timestamp.
4. Write to <RECOVERY_DIR>/final/<UUID>.jsonl

Reports per-UUID source/lines/size and summary.
"""
import os, json, hashlib

# --- CONFIG ---

# Root directory for all recovery artifacts (find via df / mount)
RECOVERY_DIR = os.environ.get("RECOVERY_DIR", "./recovery")

# --- END CONFIG ---

ARCHIVE = os.path.expanduser('~/claude_archive')
CARVED_DIRS = [
    (os.path.join(RECOVERY_DIR, 'recovered_v4'),       'J'),
    (os.path.join(RECOVERY_DIR, 'recovered'),          'M'),
    (os.path.join(RECOVERY_DIR, 'recovered_swap'),     'S'),
    (os.path.join(RECOVERY_DIR, 'recovered_swappart'), 'P'),
]
DELETED = os.path.join(RECOVERY_DIR, 'deleted_uuids.txt')
OUT = os.path.join(RECOVERY_DIR, 'final')

os.makedirs(OUT, exist_ok=True)
deleted = [l.strip() for l in open(DELETED) if l.strip()]

def load(path):
    """Return list of (key, ts, raw_line_bytes). key is .uuid, fallback md5."""
    rows = []
    if not os.path.exists(path):
        return rows
    with open(path, 'rb') as f:
        for line in f:
            line = line.rstrip(b'\n')
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            key = d.get('uuid') or d.get('promptId') or d.get('leafUuid') or hashlib.md5(line).hexdigest()
            ts = str(d.get('timestamp', ''))
            rows.append((key, ts, line))
    return rows

report = []
for uuid in deleted:
    arch_path = os.path.join(ARCHIVE, f'{uuid}.jsonl')
    has_arch = os.path.exists(arch_path)
    carved_sources = []
    for cdir, tag in CARVED_DIRS:
        cp = os.path.join(cdir, f'{uuid}.jsonl')
        if os.path.exists(cp):
            carved_sources.append((cp, tag))
    if not has_arch and not carved_sources:
        report.append((uuid, 'MISSING', 0, 0))
        continue
    seen = {}
    if has_arch:
        for key, ts, line in load(arch_path):
            seen[key] = (ts, line, 'A')
    add_counts = {}
    for cp, tag in carved_sources:
        added = 0
        for key, ts, line in load(cp):
            if key not in seen:
                seen[key] = (ts, line, tag)
                added += 1
        add_counts[tag] = added
    rows = [(ts, line) for (ts, line, _src) in seen.values()]
    rows.sort(key=lambda r: r[0])
    out_path = os.path.join(OUT, f'{uuid}.jsonl')
    with open(out_path, 'wb') as f:
        for _ts, line in rows:
            f.write(line + b'\n')
    parts = []
    if has_arch: parts.append('A')
    parts += [f'{tag}(+{add_counts[tag]})' for _cp, tag in carved_sources]
    src = '+'.join(parts) if parts else 'carved'
    report.append((uuid, src, len(rows), os.path.getsize(out_path)))

# Print sorted by status (missing last) then by line count desc
def sort_key(r):
    return (r[1] == 'MISSING', -r[2])
report.sort(key=sort_key)
print(f"{'UUID':40} {'SOURCE':18} {'LINES':>7} {'SIZE':>10}")
for uuid, src, lines, size in report:
    print(f"{uuid:40} {src:18} {lines:>7} {size:>10}")

n_full = sum(1 for r in report if r[1].startswith('A'))
n_missing = sum(1 for r in report if r[1] == 'MISSING')
n_carved = len(report) - n_full - n_missing
print(f"\nTotal: {len(report)} deleted UUIDs")
print(f"  full from archive (canonical): {n_full}")
print(f"  carved-only (partial):         {n_carved}")
print(f"  missing (no data found):       {n_missing}")
