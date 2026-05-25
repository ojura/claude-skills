#!/usr/bin/env python3
# Scrubbed for public release. Set the CONFIG constants (RECOVERY_DIR, DEV, etc.) below or via env before running.
"""Merge recovery sources into post-restart-truncated surviving session files.

For each target session:
1. Load current on-disk content (truncated, post-restart) -- KEEP all of it.
2. Add JSONL-shaped records from recovered_proc + recovered_replicated
   that aren't already present (by .uuid).
3. Add webview live records (camelCase reshaped per playbook line 811).
   Reshape into JSONL-compatible shape:
     - inject sessionId
     - move .content into .message.content + .message.role
     - tag with _source: "live_webview" so the recovery origin is legible
4. Sort by timestamp, write to <RECOVERY_DIR>/final_survivors/<uid>.jsonl
   for inspection BEFORE replacing the live file.
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

OUT = os.path.join(RECOVERY_DIR, 'final_survivors')
os.makedirs(OUT, exist_ok=True)
live = json.load(open(os.path.join(RECOVERY_DIR, 'exthost_loaded', 'all_live.json')))

def reshape_live(rec, sid, prev_uuid):
    """Reshape a webview-assembler record into JSONL message format.

    Webview shape (per playbook line 811): top-level .content is an array of
    "content wrappers" {.content: {type, text/...}, .partial, .progressSignal,
    .hash, .endTime, .startTime, .lastModifiedTime}.

    JSONL shape: .message.content is an array of flat content items
    {type, text} or {type, thinking, signature} or {type, tool_use, ...}.

    Reshape: flatten each wrapper.content to be the array element directly.
    """
    typ = rec.get('type')
    out = {
        '_source': 'live_webview',
        'sessionId': sid,
        'type': typ,
        'uuid': rec.get('uuid'),
        'parentUuid': prev_uuid,
        'isSidechain': False,
    }
    # Timestamp: webview uses numeric ms; JSONL prefers ISO. Keep numeric if
    # already string, else convert.
    ts = rec.get('timestamp')
    if isinstance(ts, (int, float)):
        from datetime import datetime, timezone
        out['timestamp'] = datetime.fromtimestamp(ts/1000, tz=timezone.utc).isoformat().replace('+00:00','Z')
    else:
        out['timestamp'] = ts

    # Flatten content[i].content into message.content
    flat_content = []
    for wrap in rec.get('content', []) or []:
        inner = wrap.get('content') if isinstance(wrap, dict) else None
        if inner and isinstance(inner, dict) and 'type' in inner:
            flat_content.append(inner)
        elif isinstance(wrap, dict) and 'type' in wrap:
            flat_content.append(wrap)  # already flat

    if typ in ('user', 'assistant'):
        msg = {'role': typ, 'content': flat_content}
        if rec.get('betaMessageId'):
            msg['id'] = rec['betaMessageId']
        out['message'] = msg
    else:
        # system / attachment / etc -- keep flat content if present
        if flat_content: out['content'] = flat_content

    return out

for uid in TARGETS:
    cur_path = f'{DST}/{uid}.jsonl'
    cur_uuids = set()
    by_uuid = {}  # uuid -> (timestamp_str, raw_line_bytes)

    # 1. Current on-disk content -- keep verbatim
    with open(cur_path, 'rb') as f:
        for line in f:
            line = line.rstrip(b'\n')
            if not line: continue
            try:
                d = json.loads(line)
                u = d.get('uuid')
                ts = str(d.get('timestamp',''))
            except: continue
            if u:
                by_uuid[u] = (ts, line)
                cur_uuids.add(u)
            else:
                # No uuid: keep by md5
                by_uuid['M:'+hashlib.md5(line).hexdigest()] = (ts, line)

    initial = len(by_uuid)

    # 2. Proc memory records (JSONL-shape) -- STRICT FILTER: must look like a session record
    VALID_TYPES = {'user','assistant','system','attachment','file-history-snapshot',
                   'custom-title','last-prompt','queue-operation','ai-title',
                   'permission-mode','tool_use','tool_result'}
    def is_session_record(d):
        if not isinstance(d, dict): return False
        t = d.get('type')
        if t not in VALID_TYPES: return False
        # Must have either uuid OR messageId (file-history-snapshot)
        if not (d.get('uuid') or d.get('messageId')): return False
        # Must have sessionId (or be valid orphan like file-history-snapshot)
        return True

    added_proc = 0
    skipped_garbage = 0
    for src in [os.path.join(RECOVERY_DIR, 'recovered_proc', f'{uid}.jsonl'),
                os.path.join(RECOVERY_DIR, 'recovered_replicated', f'{uid}.jsonl')]:
        if not os.path.exists(src): continue
        with open(src, 'rb') as f:
            for line in f:
                line = line.rstrip(b'\n')
                if not line: continue
                try:
                    d = json.loads(line)
                except: continue
                if not is_session_record(d):
                    skipped_garbage += 1
                    continue
                u = d.get('uuid') or d.get('messageId')
                ts = str(d.get('timestamp',''))
                if u and u in by_uuid: continue
                k = u or ('M:'+hashlib.md5(line).hexdigest())
                if k not in by_uuid:
                    by_uuid[k] = (ts, line)
                    if u: cur_uuids.add(u)
                    added_proc += 1
    if skipped_garbage:
        sys.stderr.write(f'  filtered out {skipped_garbage} non-session records (debug/PID/error logs)\n')

    # 3. Webview live records (reshaped) -- chain parentUuid in original order
    added_live = 0
    prev_uuid = None
    for rec in live.get(uid, []):
        u = rec.get('uuid')
        if u and u in by_uuid:
            prev_uuid = u
            continue
        reshaped = reshape_live(rec, uid, prev_uuid)
        line = json.dumps(reshaped, default=str).encode()
        ts = str(reshaped.get('timestamp',''))
        k = u or ('M:'+hashlib.md5(line).hexdigest())
        if k not in by_uuid:
            by_uuid[k] = (ts, line)
            added_live += 1
            prev_uuid = u

    # 4. Sort + write
    rows = sorted(by_uuid.values(), key=lambda r: r[0])
    out_path = f'{OUT}/{uid}.jsonl'
    with open(out_path, 'wb') as f:
        for _ts, line in rows:
            f.write(line + b'\n')

    print(f'{uid}:')
    print(f'  current: {initial} records')
    print(f'  +proc:    {added_proc}')
    print(f'  +live:    {added_live}')
    print(f'  total:    {len(rows)} -> {out_path}')
