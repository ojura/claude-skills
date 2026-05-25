#!/usr/bin/env python3
# Scrubbed for public release. Set the CONFIG constants (RECOVERY_DIR, DEV, etc.) below or via env before running.
"""SAFE merge for survivors: only proc-memory records (already JSONL-shape).

Skips webview live records entirely -- those use a different schema
(content[i].content nested wrapper) that the renderer chokes on if reshape
is imperfect. Conservative path.
"""
import json, os, hashlib, sys

# --- CONFIG ---

# Root directory for all recovery artifacts (find via df / mount)
RECOVERY_DIR = os.environ.get("RECOVERY_DIR", "./recovery")

# Your project dir: ~/.claude/projects/<slug>
# Find it via: ls ~/.claude/projects/
DST = os.environ.get("PROJECT_DIR", os.path.expanduser("~/.claude/projects/CHANGEME"))

# Session UUIDs for surviving (truncated) sessions to be patched.
# Replace with your own surviving session UUIDs (find via `ls <DST>`).
TARGETS = [
    # '<uuid-of-truncated-session-1>',
    # '<uuid-of-truncated-session-2>',
]

# --- END CONFIG ---

OUT = os.path.join(RECOVERY_DIR, 'final_survivors_safe')
os.makedirs(OUT, exist_ok=True)

for uid in TARGETS:
    cur_path = f'{DST}/{uid}.jsonl'
    by_uuid = {}
    cur_uuids = set()

    # Current file -- verbatim
    for line in open(cur_path, 'rb'):
        line = line.rstrip(b'\n')
        if not line: continue
        try:
            d = json.loads(line)
            u = d.get('uuid')
            ts = str(d.get('timestamp',''))
        except: continue
        k = u or ('M:'+hashlib.md5(line).hexdigest())
        by_uuid[k] = (ts, line)
        if u: cur_uuids.add(u)
    initial = len(by_uuid)

    # Proc memory -- JSONL-shape, safe drop-in
    added = 0
    for src in [os.path.join(RECOVERY_DIR, 'recovered_proc', f'{uid}.jsonl'),
                os.path.join(RECOVERY_DIR, 'recovered_replicated', f'{uid}.jsonl')]:
        if not os.path.exists(src): continue
        for line in open(src, 'rb'):
            line = line.rstrip(b'\n')
            if not line: continue
            try:
                d = json.loads(line)
                u = d.get('uuid')
                ts = str(d.get('timestamp',''))
            except: continue
            k = u or ('M:'+hashlib.md5(line).hexdigest())
            if k not in by_uuid:
                by_uuid[k] = (ts, line)
                added += 1
                if u: cur_uuids.add(u)

    rows = sorted(by_uuid.values(), key=lambda r: r[0])
    out_path = f'{OUT}/{uid}.jsonl'
    with open(out_path, 'wb') as f:
        for _ts, line in rows:
            f.write(line + b'\n')
    print(f'{uid}: {initial} -> {len(rows)} (+{added})')
