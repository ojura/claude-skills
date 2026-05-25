#!/usr/bin/env python3
# Scrubbed for public release. Set the CONFIG constants (RECOVERY_DIR, DEV, etc.) below or via env before running.
"""Extract session-shaped JSON records from claude subprocess memory dumps.

Each dump is <RECOVERY_DIR>/process_dumps/<uuid>.bin -- the concatenated
rw-p anonymous memory regions of one running claude subprocess.

Strategy:
1. Locate every byte sequence matching `"sessionId":"<UUID>"` for ANY of the
   known UUIDs.
2. For each match, walk backward to find the JSON line start (`{"...`)
   and forward to the line end (matching `}`).
3. Parse as JSON; if it's a session-shaped record, dedupe by .uuid (or
   md5 fallback for sessionId-less records like file-history-snapshot).
4. Emit per-UUID jsonl to <RECOVERY_DIR>/recovered_proc/<uuid>.jsonl.
"""
import os, re, json, hashlib, sys
from collections import defaultdict

# --- CONFIG ---

# Root directory for all recovery artifacts (find via df / mount)
RECOVERY_DIR = os.environ.get("RECOVERY_DIR", "./recovery")

# --- END CONFIG ---

DUMP_DIR = os.path.join(RECOVERY_DIR, 'process_dumps')
OUT_DIR = os.path.join(RECOVERY_DIR, 'recovered_proc')
KNOWN = os.path.join(RECOVERY_DIR, 'known_uuids.txt')

os.makedirs(OUT_DIR, exist_ok=True)
known = set()
for line in open(KNOWN):
    u = line.strip().rstrip('.jsonl')
    if u: known.add(u)

# Build pattern: any of the known UUIDs as sessionId
uuid_re = re.compile(rb'"sessionId":"([0-9a-f-]{36})"')

def find_line_bounds(data, off, max_back=2*1024*1024, max_fwd=2*1024*1024):
    """Find the JSON object enclosing position `off`. Walks backward to find
    a `{` at the start of a JSON object (preceded by newline or doc start),
    forward to the matching closing `}`."""
    # Walk back to find a `{"` that looks like a record start
    start = off
    lo = max(0, off - max_back)
    while start > lo:
        if data[start:start+2] == b'{"' and (start == 0 or data[start-1] in (0x0a, 0x00, 0x0d)):
            break
        start -= 1
    if start <= lo: return None
    # Walk forward and brace-count
    depth = 0
    in_str = False
    esc = False
    end = start
    hi = min(len(data), off + max_fwd)
    while end < hi:
        b = data[end]
        if esc:
            esc = False
        elif b == 0x5c:  # backslash
            esc = True
        elif b == 0x22:  # "
            in_str = not in_str
        elif not in_str:
            if b == 0x7b: depth += 1   # {
            elif b == 0x7d:            # }
                depth -= 1
                if depth == 0:
                    return data[start:end+1]
        end += 1
    return None

per_uuid = defaultdict(dict)  # uuid -> {key -> line bytes}

for fn in sorted(os.listdir(DUMP_DIR)):
    if not fn.endswith('.bin'): continue
    src_uuid = fn[:-4]
    path = os.path.join(DUMP_DIR, fn)
    sys.stderr.write(f'\n[{src_uuid}] {os.path.getsize(path)/1024/1024:.0f} MB\n')
    with open(path,'rb') as f:
        data = f.read()
    matches = list(uuid_re.finditer(data))
    sys.stderr.write(f'  sessionId matches: {len(matches)}\n')
    seen_starts = set()  # avoid extracting same line twice
    extracted = 0
    for m in matches:
        u = m.group(1).decode()
        if u not in known: continue
        line = find_line_bounds(data, m.start())
        if line is None: continue
        # Avoid duplicate parses
        h = hashlib.md5(line).hexdigest()
        try:
            d = json.loads(line)
            if not isinstance(d, dict): continue
        except: continue
        # Use uuid as primary key, md5 fallback
        key = d.get('uuid') or d.get('messageId') or ('M:' + h)
        if key not in per_uuid[u]:
            per_uuid[u][key] = line
            extracted += 1
    sys.stderr.write(f'  extracted: {extracted} new unique records\n')

# Also look for sessionId-less file-history-snapshot lines (anchor on type field)
fhs_re = re.compile(rb'"type":"file-history-snapshot"')
for fn in sorted(os.listdir(DUMP_DIR)):
    if not fn.endswith('.bin'): continue
    src_uuid = fn[:-4]
    if src_uuid not in known: continue
    path = os.path.join(DUMP_DIR, fn)
    with open(path,'rb') as f:
        data = f.read()
    fhs_count = 0
    for m in fhs_re.finditer(data):
        line = find_line_bounds(data, m.start())
        if line is None: continue
        try:
            d = json.loads(line)
        except: continue
        if d.get('type') != 'file-history-snapshot': continue
        # Attribute to the source process's session (it loaded that session)
        key = d.get('messageId') or ('M:' + hashlib.md5(line).hexdigest())
        if key not in per_uuid[src_uuid]:
            per_uuid[src_uuid][key] = line
            fhs_count += 1
    sys.stderr.write(f'  [{src_uuid}] +{fhs_count} file-history-snapshot lines\n')

# Write per-UUID outputs, sorted by timestamp
for u, lines in per_uuid.items():
    rows = []
    for k, line in lines.items():
        try:
            d = json.loads(line)
            ts = str(d.get('timestamp', ''))
        except: ts = ''
        rows.append((ts, line))
    rows.sort(key=lambda r: r[0])
    out = os.path.join(OUT_DIR, f'{u}.jsonl')
    with open(out, 'wb') as f:
        for _ts, line in rows:
            f.write(line + b'\n')

print()
print(f'{"UUID":<40} {"lines":>6}')
for u, lines in sorted(per_uuid.items(), key=lambda x: -len(x[1])):
    print(f'  {u}  {len(lines):>6}')
print(f'\ntotal unique records: {sum(len(l) for l in per_uuid.values())}')
