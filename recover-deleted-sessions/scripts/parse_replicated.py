#!/usr/bin/env python3
# Scrubbed for public release. Set the CONFIG constants (RECOVERY_DIR, DEV, etc.) below or via env before running.
"""Extract message-shaped JSON records from ALL process memory dumps,
anchored on `"uuid":"<36-char-uuid>"` patterns rather than sessionId.

Reasoning:
- Each session record has a unique .uuid field (camelCase JSONL shape).
- Some records lack sessionId (file-history-snapshot, certain system meta).
- Records reach process memory via JSON.parse -- the V8 string pool retains
  the bytes verbatim. So `"uuid":"..."` matches mark every record's anchor.
- For each match, walk outward to find the enclosing JSON object, parse,
  classify by sessionId or by uuid graph membership.

Output: <RECOVERY_DIR>/recovered_replicated/<uuid>.jsonl,
keyed by record's sessionId. Records without sessionId are routed by
parentUuid -> known-uuid -> containing-session lookup.
"""
import os, re, json, hashlib, sys
from collections import defaultdict

# --- CONFIG ---

# Root directory for all recovery artifacts (find via df / mount)
RECOVERY_DIR = os.environ.get("RECOVERY_DIR", "./recovery")

# --- END CONFIG ---

DUMP_DIR = os.path.join(RECOVERY_DIR, 'process_dumps')
OUT_DIR = os.path.join(RECOVERY_DIR, 'recovered_replicated')
FINAL = os.path.join(RECOVERY_DIR, 'final')
os.makedirs(OUT_DIR, exist_ok=True)

# Build current uuid -> session mapping from final/
uuid_to_session = {}
for fn in sorted(os.listdir(FINAL)):
    if not fn.endswith('.jsonl'): continue
    sid = fn[:-6]
    for line in open(os.path.join(FINAL, fn), 'rb'):
        try:
            d = json.loads(line)
            u = d.get('uuid')
            if u: uuid_to_session[u] = sid
        except: pass
sys.stderr.write(f'known uuids in final/: {len(uuid_to_session)}\n')

deleted = set(open(os.path.join(RECOVERY_DIR, 'deleted_uuids.txt')).read().split())
known_sessions = set(open(os.path.join(RECOVERY_DIR, 'known_uuids.txt')).read().split())
known_sessions = {u.replace('.jsonl','') for u in known_sessions}

# Pattern: any uuid in JSON context. Conservative: require flanking quotes and the field name "uuid"
uuid_re = re.compile(rb'"uuid":"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"')

def find_obj_bounds(data, off, max_back=512*1024, max_fwd=512*1024):
    """Walk back to find `{` opening this record, then forward to balanced `}`."""
    lo = max(0, off - max_back)
    # Walk back for `{`
    start = off
    depth = 0
    while start > lo:
        c = data[start]
        if c == 0x7d: depth += 1
        elif c == 0x7b:
            if depth == 0: break
            depth -= 1
        start -= 1
    if start <= lo: return None
    # Forward brace-balance to closing }
    depth = 0; in_str = False; esc = False
    end = start
    hi = min(len(data), off + max_fwd)
    while end < hi:
        b = data[end]
        if esc: esc = False
        elif b == 0x5c: esc = True
        elif b == 0x22: in_str = not in_str
        elif not in_str:
            if b == 0x7b: depth += 1
            elif b == 0x7d:
                depth -= 1
                if depth == 0: return data[start:end+1]
        end += 1
    return None

per_session_new = defaultdict(dict)  # sid -> {uuid -> raw line bytes}
unrouted = {}  # uuid -> raw bytes (sessionId missing AND parent not known)

for fn in sorted(os.listdir(DUMP_DIR)):
    if not fn.endswith('.bin'): continue
    path = os.path.join(DUMP_DIR, fn)
    sys.stderr.write(f'\n[{fn}] {os.path.getsize(path)/1024/1024:.0f} MB\n')
    data = open(path,'rb').read()
    seen = set()
    matches = list(uuid_re.finditer(data))
    sys.stderr.write(f'  uuid anchors: {len(matches)}\n')
    new_in_file = 0
    for m in matches:
        u = m.group(1).decode()
        if u in seen: continue
        seen.add(u)
        # Skip if already in our final set
        if u in uuid_to_session: continue
        obj = find_obj_bounds(data, m.start())
        if obj is None: continue
        try:
            d = json.loads(obj)
        except: continue
        if not isinstance(d, dict): continue
        # Heuristic: must have type field to be a message record
        if 'type' not in d: continue
        sid = d.get('sessionId')
        if sid and sid in known_sessions:
            per_session_new[sid][u] = obj
            new_in_file += 1
        else:
            # Try to route via parentUuid
            pu = d.get('parentUuid')
            if pu and pu in uuid_to_session:
                target = uuid_to_session[pu]
                per_session_new[target][u] = obj
                new_in_file += 1
            else:
                unrouted[u] = obj
    sys.stderr.write(f'  new records routed: {new_in_file}, unrouted: {len(unrouted)}\n')

# Write per-session new records
for sid, recs in per_session_new.items():
    if sid not in known_sessions: continue
    out = os.path.join(OUT_DIR, f'{sid}.jsonl')
    rows = []
    for u, line in recs.items():
        try:
            d = json.loads(line)
            ts = str(d.get('timestamp',''))
        except: ts = ''
        rows.append((ts, line))
    rows.sort(key=lambda r: r[0])
    with open(out,'wb') as f:
        for _ts, line in rows:
            f.write(line + b'\n')

print()
print(f'{"UUID":<40} {"new_records":>12}')
for sid, recs in sorted(per_session_new.items(), key=lambda x: -len(x[1])):
    if sid in known_sessions:
        is_deleted = ' [DELETED]' if sid in deleted else ''
        print(f'  {sid}  {len(recs):>12}{is_deleted}')
print(f'\ntotal new records: {sum(len(r) for r in per_session_new.values())}')
print(f'unrouted (no sessionId, no known parent): {len(unrouted)}')
