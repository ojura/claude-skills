#!/usr/bin/env python3
# Scrubbed for public release. Set the CONFIG constants (RECOVERY_DIR, DEV, etc.) below or via env before running.
"""Cross-reference journal-found inode candidates with carved session match offsets.

For each candidate inode in journal_inode_candidates.json:
1. Compute the disk byte ranges its extents cover.
2. For each deleted session UUID, count how many of its raw matches fall
   inside those byte ranges.
3. If a candidate inode has many matches for ONE session UUID and few for
   others, that candidate IS that session's pre-deletion inode snapshot.

Once attributed, we have:
- The session's original total file size
- Its full extent layout (so we can dd-read every block, including ones
  that didn't contain a sessionId match in our carve, to recover lines we'd
  otherwise miss).
"""
import json, re, os
from collections import defaultdict

# --- CONFIG ---

# Root directory for all recovery artifacts (find via df / mount)
RECOVERY_DIR = os.environ.get("RECOVERY_DIR", "./recovery")

# --- END CONFIG ---

CAND = os.path.join(RECOVERY_DIR, 'journal_inode_candidates.json')
MATCHES = os.path.join(RECOVERY_DIR, 'raw_matches.txt')
DELETED = os.path.join(RECOVERY_DIR, 'deleted_uuids.txt')

deleted = set(l.strip() for l in open(DELETED) if l.strip())

# Parse all matches: byte offset per UUID
matches_by_uuid = {}
raw = open(MATCHES,'rb').read()
for s in raw.split(b'\x00'):
    s = s.strip()
    if not s: continue
    try:
        c = s.index(b':')
        off = int(s[:c])
        m = re.search(rb'sessionId":"([0-9a-f-]{36})', s[c+1:])
        if m:
            u = m.group(1).decode()
            if u in deleted:
                matches_by_uuid.setdefault(u, []).append(off)
    except: pass
print(f'sessions with at least 1 match: {len(matches_by_uuid)}')

# Index match BLOCKS per UUID (block = byte_offset // 4096)
blocks_by_uuid = {u: set(o // 4096 for o in offs) for u, offs in matches_by_uuid.items()}

candidates = json.load(open(CAND))
# Filter to leaf-extent candidates (depth==0) so we have phys_blk extents directly
leaves = [c for c in candidates if c['eh_depth'] == 0 and c['extents']]
print(f'leaf-extent candidates: {len(leaves)}/{len(candidates)}')

# For each candidate, compute total covered block set and score against each session
attribution = []  # list of (best_uuid, score, candidate)
for c in leaves:
    covered_blocks = set()
    for e in c['extents']:
        a = e['phys_blk']
        b = a + e['len'] - 1
        # Full enumeration could be too big; instead, intersect on-the-fly with each UUID's blocks
        # Use range-based intersect: for each UUID, count blocks within [a,b]
        for blk in range(a, b + 1):
            covered_blocks.add(blk)
    if len(covered_blocks) < 1:
        continue

    best_uuid = None
    best_score = 0
    second = 0
    for u, blkset in blocks_by_uuid.items():
        score = len(covered_blocks & blkset)
        if score > best_score:
            second = best_score
            best_score = score
            best_uuid = u
        elif score > second:
            second = score
    if best_score >= 1:  # any match
        attribution.append((best_uuid, best_score, second, c))

# Sort by score
attribution.sort(key=lambda x: -x[1])
print(f'\ncandidates with >=1 session-match-block overlap: {len(attribution)}')

# Show distinct UUIDs by best candidate
seen_uuids = {}
print('\n=== best journal-inode-snapshot per session UUID (only sessions where attribution found) ===')
print('UUID                                  size       ext  matched_blks  next_best   total_blks')
for u, score, second, c in attribution:
    if u in seen_uuids: continue  # only show top per uuid
    if score < 5: continue  # filter noise; require >=5 block overlap to attribute
    seen_uuids[u] = c
    n_blocks = sum(e['len'] for e in c['extents'])
    print(f"{u}  {c['size']:>10}  {len(c['extents'])}     {score:>4}/{n_blocks}      {second:>4}      jblk={c['journal_block']}")

print(f'\nattributed sessions: {len(seen_uuids)}')

# Save mapping
out_path = os.path.join(RECOVERY_DIR, 'journal_session_attribution.json')
with open(out_path,'w') as f:
    out = {u: c for u, c in seen_uuids.items()}
    json.dump(out, f, indent=1, default=str)
print(f'saved -> {out_path}')
