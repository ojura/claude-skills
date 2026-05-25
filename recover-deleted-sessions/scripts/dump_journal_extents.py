#!/usr/bin/env python3
# Scrubbed for public release. Set the CONFIG constants (RECOVERY_DIR, DEV, etc.) below or via env before running.
"""Dump every journal-attributed inode's extent contents to disk once.

Output: <RECOVERY_DIR>/journal_extents_raw/<uuid>.bin
        (extent regions concatenated in extent order, truncated to inode size)
        <RECOVERY_DIR>/journal_extents_raw/<uuid>.meta.json
        (attribution + extent map for reference)

Then any analysis can read those .bin files directly without sudo dd.
"""
import json, subprocess, os, sys

# --- CONFIG ---

# Root directory for all recovery artifacts (find via df / mount)
RECOVERY_DIR = os.environ.get("RECOVERY_DIR", "./recovery")

# The affected main filesystem partition (find via `lsblk` / `df -T`)
DEV = os.environ.get("DEV", "/dev/CHANGEME")

# --- END CONFIG ---

ATTRIB = os.path.join(RECOVERY_DIR, 'journal_attribution_v2.json')
OUT = os.path.join(RECOVERY_DIR, 'journal_extents_raw')
PWD = os.path.expanduser('~/password')

os.makedirs(OUT, exist_ok=True)
attrib = json.load(open(ATTRIB))

def dd_read(start_blk, length_blocks):
    cmd = ['sudo','-S','-p','','dd', f'if={DEV}',
           'bs=4096', f'skip={start_blk}', f'count={length_blocks}', 'status=none']
    with open(PWD,'rb') as f:
        p = subprocess.run(cmd, stdin=f, capture_output=True, timeout=600)
    return p.stdout

total_bytes = 0
for entry in attrib:
    u = entry['uuid']
    size = entry['size']
    extents = entry['extents']
    out_bin = os.path.join(OUT, f'{u}.bin')
    out_meta = os.path.join(OUT, f'{u}.meta.json')
    if os.path.exists(out_bin) and os.path.getsize(out_bin) == size:
        sys.stderr.write(f'[skip] {u} already dumped ({size} B)\n')
        continue
    chunks = []
    for phys, length in extents:
        chunks.append(dd_read(phys, length))
    data = b''.join(chunks)[:size]
    with open(out_bin,'wb') as f:
        f.write(data)
    with open(out_meta,'w') as f:
        json.dump(entry, f, indent=1)
    total_bytes += len(data)
    sys.stderr.write(f'[ok] {u} -> {len(data)} B ({len(data)/1024/1024:.1f} MB) across {len(extents)} extents\n')

sys.stderr.write(f'\ntotal dumped: {total_bytes / 1024/1024:.1f} MB across {len(attrib)} sessions\n')
