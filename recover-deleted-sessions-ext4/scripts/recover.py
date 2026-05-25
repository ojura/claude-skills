#!/usr/bin/env python3
# Scrubbed for public release. Set the CONFIG constants (RECOVERY_DIR, DEV, etc.) below or via env before running.
"""Reconstruct deleted JSONL files from raw block matches.

Reads ripgrep --null-data -aob output (NUL-separated <offset>:sessionId":"<UUID>")
for each (matches_file, device, outdir) source, clusters nearby offsets, dd-reads
windows, extracts JSONL lines per session UUID. Dedupes by .uuid, sorts by timestamp.

Sources are processed independently so we can carve from main fs + swapfile + swap
partition. The merge step (merge.py) combines all carved outputs with archive
backups into the final canonical jsonl.
"""
import os, sys, subprocess, json, re

# --- CONFIG ---

# Root directory for all recovery artifacts (find via df / mount)
RECOVERY_DIR = os.environ.get("RECOVERY_DIR", "./recovery")

# The affected main filesystem partition (find via `lsblk` / `df -T`)
DEV = os.environ.get("DEV", "/dev/CHANGEME")

# Swap file path (find via `swapon --show`)
SWAP = os.environ.get("SWAP", "/swapfile")

# Swap partition device (find via `swapon --show` or `lsblk`)
SWAP_DEV = os.environ.get("SWAP_DEV", "/dev/CHANGEME_SWAP")

# --- END CONFIG ---

DELETED_LIST = os.path.join(RECOVERY_DIR, 'deleted_uuids.txt')

# (matches_file, device, outdir, label)
SOURCES = [
    (os.path.join(RECOVERY_DIR, 'raw_matches.txt'),     DEV,      os.path.join(RECOVERY_DIR, 'recovered'),          'main'),
    (os.path.join(RECOVERY_DIR, 'swap_matches.txt'),    SWAP,     os.path.join(RECOVERY_DIR, 'recovered_swap'),     'swap'),
    (os.path.join(RECOVERY_DIR, 'swappart_matches.txt'),SWAP_DEV, os.path.join(RECOVERY_DIR, 'recovered_swappart'), 'swappart'),
]

deleted = set(l.strip() for l in open(DELETED_LIST) if l.strip())

def cluster(offs, gap=1024*1024):
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
        proc = subprocess.run(cmd, stdin=pwd_f, capture_output=True, timeout=120)
    return proc.stdout[pad:pad+length]

def carve(matches_file, device, outdir, label):
    if not os.path.exists(matches_file) or os.path.getsize(matches_file) == 0:
        print(f'[{label}] no matches at {matches_file}', file=sys.stderr)
        return 0
    print(f'[{label}] parsing {matches_file}...', file=sys.stderr)
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
                uuid = m.group(1).decode()
                if uuid in deleted:
                    uuid_offsets.setdefault(uuid, []).append(off)
        except Exception: pass
    print(f'[{label}] deleted UUIDs with matches: {len(uuid_offsets)} / {len(deleted)}', file=sys.stderr)
    os.makedirs(outdir, exist_ok=True)
    total_lines = 0
    for uuid in sorted(uuid_offsets, key=lambda u: -len(uuid_offsets[u])):
        offs = uuid_offsets[uuid]
        clusters = cluster(offs)
        needle = f'"sessionId":"{uuid}"'.encode()
        seen = {}
        for c in clusters:
            start = max(0, c[0] - 512*1024)
            end = c[-1] + 512*1024
            win = read_window(device, start, end - start)
            for line in win.split(b'\n'):
                if needle not in line: continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                lu = d.get('uuid') or d.get('promptId') or d.get('leafUuid') or ''
                if lu and lu not in seen:
                    seen[lu] = line
                elif not lu:
                    import hashlib
                    h = hashlib.md5(line).hexdigest()
                    if h not in seen: seen[h] = line
        if not seen: continue
        rows = []
        for k, line in seen.items():
            try:
                d = json.loads(line)
                ts = str(d.get('timestamp', ''))
            except: ts = ''
            rows.append((ts, line))
        rows.sort(key=lambda r: r[0])
        outp = os.path.join(outdir, uuid + '.jsonl')
        with open(outp, 'wb') as f:
            for _, line in rows:
                f.write(line.rstrip() + b'\n')
        total_lines += len(rows)
        print(f'[{label}] {uuid}: {len(offs)} matches in {len(clusters)} clusters -> {len(rows)} lines', file=sys.stderr)
    print(f'[{label}] total recovered lines: {total_lines}', file=sys.stderr)
    return total_lines

if __name__ == '__main__':
    for matches_file, device, outdir, label in SOURCES:
        carve(matches_file, device, outdir, label)
