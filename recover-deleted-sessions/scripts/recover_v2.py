#!/usr/bin/env python3
# Scrubbed for public release. Set the CONFIG constants (RECOVERY_DIR, DEV, etc.) below or via env before running.
"""V2 carve: wider window + parent-UUID-based inclusion of sessionId-less records.

Improvements over v1:

1. **Wider window** (4 MB instead of 512 KB on each side).
   Catches JSONL lines longer than ~1 MB that v1 truncated mid-line.

2. **Two-pass per session:**
   a. First pass: collect lines whose sessionId == target UUID (anchor set).
      Build the set of known .uuid values seen in those lines.
   b. Second pass: in the same windows, also include valid-JSON lines that
      have NO sessionId field but whose .parentUuid or .logicalParentUuid
      is in the anchor set's uuid pool. This recovers compact_boundary,
      system, and meta records that lack the sessionId anchor.

Output goes to recovered_v2/<UUID>.jsonl under RECOVERY_DIR.
The merge step combines this with archive backups for the final.
"""
import os, sys, subprocess, json, re, hashlib

# --- CONFIG ---

# Root directory for all recovery artifacts (find via df / mount)
RECOVERY_DIR = os.environ.get("RECOVERY_DIR", "./recovery")

# The affected main filesystem partition (find via `lsblk` / `df -T`)
DEV = os.environ.get("DEV", "/dev/CHANGEME")

# --- END CONFIG ---

DELETED_LIST = os.path.join(RECOVERY_DIR, 'deleted_uuids.txt')
WINDOW_PAD = 4 * 1024 * 1024
CLUSTER_GAP = 4 * 1024 * 1024

SOURCES = [
    (os.path.join(RECOVERY_DIR, 'raw_matches.txt'), DEV, os.path.join(RECOVERY_DIR, 'recovered_v2'), 'main'),
]

deleted = set(l.strip() for l in open(DELETED_LIST) if l.strip())

def cluster(offs, gap):
    offs = sorted(set(offs))
    out = []
    cur = [offs[0]]
    for o in offs[1:]:
        if o - cur[-1] < gap:
            cur.append(o)
        else:
            out.append(cur); cur = [o]
    out.append(cur)
    return out

def read_window(device, start_byte, length):
    aligned_start = (start_byte // 4096) * 4096
    pad = start_byte - aligned_start
    blocks = (pad + length + 4095) // 4096
    cmd = ['sudo', '-S', '-p', '', 'dd', f'if={device}',
           'bs=4096', f'skip={aligned_start//4096}', f'count={blocks}',
           'status=none']
    with open(os.path.expanduser('~/password'),'rb') as pwd_f:
        proc = subprocess.run(cmd, stdin=pwd_f, capture_output=True, timeout=300)
    return proc.stdout[pad:pad+length]

def parse_line(line):
    try:
        d = json.loads(line)
        if isinstance(d, dict): return d
    except Exception: pass
    return None

def line_key(line, d):
    """Stable key for dedupe: prefer .uuid, fall back to other ids, last-resort md5."""
    return (d.get('uuid') or d.get('promptId') or d.get('leafUuid')
            or hashlib.md5(line).hexdigest())

def carve(matches_file, device, outdir, label):
    if not os.path.exists(matches_file) or os.path.getsize(matches_file) == 0:
        sys.stderr.write(f'[{label}] no matches at {matches_file}\n')
        return
    sys.stderr.write(f'[{label}] parsing {matches_file}...\n')
    uuid_offsets = {}
    raw = open(matches_file, 'rb').read()
    for s in raw.split(b'\x00'):
        s = s.strip()
        if not s: continue
        try:
            colon = s.index(b':')
            off = int(s[:colon])
            m = re.search(rb'sessionId":"([0-9a-f-]{36})', s[colon+1:])
            if m:
                u = m.group(1).decode()
                if u in deleted:
                    uuid_offsets.setdefault(u, []).append(off)
        except Exception: pass
    sys.stderr.write(f'[{label}] deleted UUIDs with matches: {len(uuid_offsets)} / {len(deleted)}\n')

    os.makedirs(outdir, exist_ok=True)
    total_lines = 0
    for uuid in sorted(uuid_offsets, key=lambda u: -len(uuid_offsets[u])):
        offs = uuid_offsets[uuid]
        clusters = cluster(offs, CLUSTER_GAP)
        needle = f'"sessionId":"{uuid}"'.encode()

        # First pass: collect sessionId-positive lines per cluster + cache windows
        windows = []
        anchor_lines = {}
        anchor_uuids = set()
        for c in clusters:
            start = max(0, c[0] - WINDOW_PAD)
            end = c[-1] + WINDOW_PAD
            win = read_window(device, start, end - start)
            windows.append(win)
            for line in win.split(b'\n'):
                if needle not in line: continue
                d = parse_line(line)
                if d is None: continue
                k = line_key(line, d)
                if k not in anchor_lines:
                    anchor_lines[k] = (d.get('timestamp', ''), line)
                    if d.get('uuid'): anchor_uuids.add(d['uuid'])

        # Second pass: include sessionId-less lines whose parent links into anchor_uuids
        extra_lines = {}
        for win in windows:
            for line in win.split(b'\n'):
                if needle in line: continue
                d = parse_line(line)
                if d is None: continue
                if d.get('sessionId') and d['sessionId'] != uuid: continue
                pu = d.get('parentUuid')
                lpu = d.get('logicalParentUuid')
                if (pu and pu in anchor_uuids) or (lpu and lpu in anchor_uuids):
                    k = line_key(line, d)
                    if k not in anchor_lines and k not in extra_lines:
                        extra_lines[k] = (d.get('timestamp', ''), line, d.get('type', '?'))

        if not anchor_lines and not extra_lines: continue

        all_rows = []
        for k, (ts, line) in anchor_lines.items():
            all_rows.append((str(ts), line))
        for k, (ts, line, _typ) in extra_lines.items():
            all_rows.append((str(ts), line))
        all_rows.sort(key=lambda r: r[0])

        outp = os.path.join(outdir, uuid + '.jsonl')
        with open(outp, 'wb') as f:
            for _ts, line in all_rows:
                f.write(line.rstrip() + b'\n')
        total_lines += len(all_rows)
        sys.stderr.write(f'[{label}] {uuid}: {len(offs)} matches, {len(clusters)} clusters -> {len(anchor_lines)} anchor + {len(extra_lines)} parent-linked = {len(all_rows)} lines\n')
    sys.stderr.write(f'[{label}] total recovered lines: {total_lines}\n')

if __name__ == '__main__':
    for matches_file, device, outdir, label in SOURCES:
        carve(matches_file, device, outdir, label)
