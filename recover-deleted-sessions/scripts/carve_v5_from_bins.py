#!/usr/bin/env python3
# Scrubbed for public release. Set the CONFIG constants (RECOVERY_DIR, DEV, etc.) below or via env before running.
"""V5 carve: read pre-dumped journal_extents_raw/<uuid>.bin files.

Improvements over v4 (which was identical logic but read sudo dd live):
- All input now from RECOVERY_DIR, no sudo, repeatable.
- When multiple byte-different copies of the same logical record (same .uuid
  or messageId) exist on disk, keep the LONGEST byte version. This preserves
  the most-complete copy when some replicas were partially overwritten.
- Keep file-history-snapshot records (no sessionId) by md5 dedupe of full line.

Output -> <RECOVERY_DIR>/recovered_v5/<uuid>.jsonl
"""
import os, json, hashlib, sys

# --- CONFIG ---

# Root directory for all recovery artifacts (find via df / mount)
RECOVERY_DIR = os.environ.get("RECOVERY_DIR", "./recovery")

# --- END CONFIG ---

BIN_DIR = os.path.join(RECOVERY_DIR, 'journal_extents_raw')
PREV_DIR = os.path.join(RECOVERY_DIR, 'recovered_v4')
OUT = os.path.join(RECOVERY_DIR, 'recovered_v5')

os.makedirs(OUT, exist_ok=True)

def session_key(d, raw_line):
    """Logical key. Records with .uuid: that's the key. file-history-snapshot:
    use messageId. Otherwise md5 of full line."""
    return d.get('uuid') or d.get('messageId') or d.get('promptId') or d.get('leafUuid') or ('M:' + hashlib.md5(raw_line).hexdigest())

def parse_session_json(line):
    if not line.strip(): return None
    try: d = json.loads(line)
    except: return None
    if not isinstance(d, dict): return None
    return d

def collect(uuid, source_label, source_bytes, target_uuid):
    """Yield (key, raw_line, ts, type) for each session-shaped line."""
    for line in source_bytes.split(b'\n'):
        line = line.rstrip()
        if not line: continue
        d = parse_session_json(line)
        if d is None: continue
        sid = d.get('sessionId')
        if sid is not None and sid != target_uuid:
            continue
        # filter to clearly session-shaped: must have at least one well-known field
        if not any(k in d for k in ('uuid','messageId','type','parentUuid','timestamp','sessionId')):
            continue
        k = session_key(d, line)
        ts = str(d.get('timestamp', ''))
        yield (k, line, ts, d.get('type','?'), source_label)

summary = []
for fn in sorted(os.listdir(BIN_DIR)):
    if not fn.endswith('.bin'): continue
    u = fn[:-4]
    bin_path = os.path.join(BIN_DIR, fn)
    bin_data = open(bin_path,'rb').read()
    prev_path = os.path.join(PREV_DIR, f'{u}.jsonl')
    prev_data = open(prev_path,'rb').read() if os.path.exists(prev_path) else b''

    by_key = {}  # key -> (ts, line, type, source)
    for key, line, ts, typ, src in collect(u, 'BIN', bin_data, u):
        cur = by_key.get(key)
        if cur is None or len(line) > len(cur[1]):
            by_key[key] = (ts, line, typ, src)
    for key, line, ts, typ, src in collect(u, 'PREV', prev_data, u):
        cur = by_key.get(key)
        if cur is None or len(line) > len(cur[1]):
            by_key[key] = (ts, line, typ, src)

    rows = list(by_key.values())
    rows.sort(key=lambda r: r[0])
    out_path = os.path.join(OUT, f'{u}.jsonl')
    with open(out_path,'wb') as f:
        for ts, line, _typ, _src in rows:
            f.write(line + b'\n')

    prev_lines = prev_data.count(b'\n')
    types = {}
    for _ts, _line, t, _src in rows:
        types[t] = types.get(t, 0) + 1
    gain = len(rows) - prev_lines
    sys.stderr.write(f'{u}: prev={prev_lines} v5={len(rows)} (gain {gain:+d}); types={types}\n')
    summary.append((u, len(rows), prev_lines, gain, types))

print('\n=== V5 RESULT ===')
print(f'{"UUID":<40} {"v5":>6} {"v4":>6} {"gain":>5}')
for u, lines, prev, gain, _ in sorted(summary, key=lambda x: -x[3]):
    print(f'  {u}  {lines:>6}  {prev:>6}  {gain:>+5}')
print(f'\ntotal: {sum(l for _,l,_,_,_ in summary)} lines (was {sum(p for _,_,p,_,_ in summary)})')
