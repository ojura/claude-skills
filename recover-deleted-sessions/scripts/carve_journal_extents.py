#!/usr/bin/env python3
# Scrubbed for public release. Set the CONFIG constants (RECOVERY_DIR, DEV, etc.) below or via env before running.
"""V4 carve: read journal-attributed inode extents directly.

For each deleted-session UUID with a journal attribution:
1. Read all blocks listed in the inode's extent tree.
2. Concatenate (in extent order -- same as logical file order).
3. Split on \\n, JSON-parse each line.
4. Keep lines whose sessionId matches the UUID OR whose sessionId is null
   (file-history-snapshot, system meta) -- those are the lines that lack
   the grep anchor. Lines whose sessionId is a DIFFERENT UUID are dropped
   (block reused for another file post-deletion).
5. Merge with existing recovered/<uuid>.jsonl, dedupe by line content md5.
6. Write to <RECOVERY_DIR>/recovered_v4/<uuid>.jsonl.
"""
import json, subprocess, os, hashlib, sys

# --- CONFIG ---

# Root directory for all recovery artifacts (find via df / mount)
RECOVERY_DIR = os.environ.get("RECOVERY_DIR", "./recovery")

# The affected main filesystem partition (find via `lsblk` / `df -T`)
DEV = os.environ.get("DEV", "/dev/CHANGEME")

# --- END CONFIG ---

ATTRIB = os.path.join(RECOVERY_DIR, 'journal_attribution_v2.json')
OUT = os.path.join(RECOVERY_DIR, 'recovered_v4')
EXISTING = os.path.join(RECOVERY_DIR, 'recovered')
PWD_PATH = os.path.expanduser('~/password')

def dd_read_blocks(start_blk, length):
    """Read `length` 4KB blocks starting from physical block `start_blk`."""
    cmd = ['sudo','-S','-p','','dd', f'if={DEV}',
           'bs=4096', f'skip={start_blk}', f'count={length}', 'status=none']
    with open(PWD_PATH,'rb') as pwd_f:
        p = subprocess.run(cmd, stdin=pwd_f, capture_output=True, timeout=600)
    return p.stdout

def line_md5(line):
    return hashlib.md5(line).hexdigest()

def main():
    attrib = json.load(open(ATTRIB))
    os.makedirs(OUT, exist_ok=True)
    summary = []
    for entry in attrib:
        u = entry['uuid']
        size = entry['size']
        extents = entry['extents']
        n_blocks = sum(l for _, l in extents)
        sys.stderr.write(f'[{u}] reading {n_blocks} blocks ({n_blocks*4/1024:.1f} MB) across {len(extents)} extents\n')
        # Read all extents in order
        chunks = []
        for phys, length in extents:
            chunks.append(dd_read_blocks(phys, length))
        data = b''.join(chunks)
        # Truncate to inode-recorded size (extent blocks may extend past file end)
        data = data[:size]

        # Split on \n, parse, filter
        kept = {}  # md5 -> (timestamp_str, line_bytes, type)
        skipped_other_session = 0
        parse_fails = 0
        for line in data.split(b'\n'):
            if not line.strip(): continue
            try:
                d = json.loads(line)
            except Exception:
                parse_fails += 1
                continue
            if not isinstance(d, dict): continue
            sid = d.get('sessionId')
            if sid is not None and sid != u:
                skipped_other_session += 1
                continue
            ts = str(d.get('timestamp',''))
            h = line_md5(line)
            kept[h] = (ts, line, d.get('type','?'))

        # Also load existing carve and merge by md5
        existing_path = os.path.join(EXISTING, f'{u}.jsonl')
        existing_count = 0
        if os.path.exists(existing_path):
            for line in open(existing_path,'rb'):
                line = line.rstrip(b'\n')
                if not line: continue
                h = line_md5(line)
                if h not in kept:
                    try: d = json.loads(line); ts = str(d.get('timestamp','')); typ = d.get('type','?')
                    except: ts = ''; typ = '?'
                    kept[h] = (ts, line, typ)
                existing_count += 1

        # Sort and write
        rows = [(ts, line, typ) for ts, line, typ in kept.values()]
        rows.sort(key=lambda r: r[0])
        outp = os.path.join(OUT, f'{u}.jsonl')
        with open(outp, 'wb') as f:
            for _ts, line, _typ in rows:
                f.write(line + b'\n')
        # Count types
        type_counts = {}
        for _ts, _ln, t in rows:
            type_counts[t] = type_counts.get(t, 0) + 1
        gain = len(rows) - existing_count
        sys.stderr.write(f'  -> {len(rows)} lines (was {existing_count}, gain {gain}); types: {type_counts}; skipped_other={skipped_other_session} parse_fails={parse_fails}\n')
        summary.append((u, len(rows), existing_count, gain, type_counts))

    # Final summary
    print('\n=== SUMMARY ===')
    print(f'{"UUID":<40} {"lines":>6} {"prev":>6} {"gain":>6}  types')
    for u, lines, prev, gain, types in sorted(summary, key=lambda x: -x[3]):
        types_str = ' '.join(f'{k}={v}' for k, v in sorted(types.items(), key=lambda kv: -kv[1]))
        print(f'  {u}  {lines:>6}  {prev:>6}  {gain:>+6}  {types_str}')

if __name__ == '__main__':
    main()
