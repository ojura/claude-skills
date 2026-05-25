#!/usr/bin/env python3
# Scrubbed for public release. Set the CONFIG constants (RECOVERY_DIR, DEV, etc.) below or via env before running.
"""Parse the dumped ext4 journal and find pre-deletion inode snapshots for our
deleted session jsonls.

Strategy:
1. The journal is 1 GB of 4 KB blocks. Some blocks are JBD2 metadata
   (descriptor / commit / superblock); the rest are data blocks holding
   snapshots of FS metadata blocks (inode tables, bitmaps, dir blocks, etc).
2. We don't bother parsing JBD2 framing fully -- instead we scan every 4 KB
   block and check if it LOOKS like an ext4 inode table.
3. An inode table block holds 16 inodes (each 256 B). For each candidate
   inode slot, we check the fingerprint of a session jsonl inode:
   - i_mode = 0x81a4 (regular file 0644)
   - i_uid_lo = your UID (set via UID_FILTER env or `id -u`)
   - i_links_count = 1
   - i_size_lo > 0 (we want non-deleted snapshot, so size > 0)
   - i_flags has EXTENTS bit (0x80000)
   - i_block[0:4] == ext4 extent header magic 0xf30a
4. For each match, dump: extent header + extent entries (which point at
   the original data blocks of the deleted file).

Block ranges these extents point at can then be dd-read to recover the
original file content.
"""
import struct, sys, os, json

# --- CONFIG ---

# Root directory for all recovery artifacts (find via df / mount)
RECOVERY_DIR = os.environ.get("RECOVERY_DIR", "./recovery")

# Your uid (`id -u`); session files are owned by you
EXPECTED_UID = int(os.environ.get("UID_FILTER", os.getuid()))

# --- END CONFIG ---

JOURNAL = os.path.join(RECOVERY_DIR, 'journal.bin')
INODE_SIZE = 256
BLOCK_SIZE = 4096

# Patterns that identify a "session jsonl inode" in its pre-deletion state
EXPECTED_MODE = 0x81a4       # regular file, 0644
EXTENTS_FLAG = 0x80000       # EXT4_EXTENTS_FL
EXT_MAGIC = 0xf30a           # extent header magic (eh_magic), little-endian short

def parse_inode(raw, base_block, slot_idx):
    if len(raw) < INODE_SIZE: return None
    mode, uid_lo, size_lo, atime, ctime, mtime, dtime, gid_lo, nlink, blocks_lo, flags = \
        struct.unpack_from('<HHIIIIIHHII', raw, 0)
    if mode != EXPECTED_MODE: return None
    if uid_lo != EXPECTED_UID: return None
    if size_lo == 0 and dtime != 0: return None  # already deleted-state snapshot
    if not (flags & EXTENTS_FLAG): return None
    # Extent header at offset 40
    eh_magic, eh_entries, eh_max, eh_depth, eh_generation = struct.unpack_from('<HHHHI', raw, 40)
    if eh_magic != EXT_MAGIC: return None
    if eh_entries == 0 or eh_entries > 4: return None
    size_hi = struct.unpack_from('<I', raw, 108)[0]
    size = (size_hi << 32) | size_lo
    if size < 1024 or size > 1024*1024*1024: return None  # filter to reasonable session sizes
    # Parse extent entries (eh_depth=0 means leaf with eh_entries Extent records)
    extents = []
    if eh_depth == 0:
        for i in range(eh_entries):
            o = 40 + 12 + i * 12
            ee_block, ee_len, ee_start_hi, ee_start_lo = struct.unpack_from('<IHHI', raw, o)
            phys = (ee_start_hi << 32) | ee_start_lo
            extents.append({'logical_blk': ee_block, 'len': ee_len, 'phys_blk': phys})
    else:
        # internal node: indexes pointing at extent-tree blocks
        for i in range(eh_entries):
            o = 40 + 12 + i * 12
            ei_block, ei_leaf_lo, ei_leaf_hi, _ = struct.unpack_from('<IIHH', raw, o)
            phys = (ei_leaf_hi << 32) | ei_leaf_lo
            extents.append({'idx_block': ei_block, 'idx_leaf_phys': phys, 'depth': eh_depth})
    return {
        'journal_block': base_block,
        'slot_idx': slot_idx,
        'mode': oct(mode), 'uid': uid_lo, 'size': size,
        'mtime': mtime, 'dtime': dtime, 'nlink': nlink, 'flags': hex(flags),
        'eh_depth': eh_depth, 'eh_entries': eh_entries,
        'extents': extents,
    }

def main():
    fsize = os.path.getsize(JOURNAL)
    nblocks = fsize // BLOCK_SIZE
    print(f'journal: {fsize} bytes = {nblocks} blocks')
    found = []
    with open(JOURNAL,'rb') as f:
        for blk in range(nblocks):
            f.seek(blk * BLOCK_SIZE)
            data = f.read(BLOCK_SIZE)
            for slot in range(BLOCK_SIZE // INODE_SIZE):
                ino = parse_inode(data[slot*INODE_SIZE:(slot+1)*INODE_SIZE], blk, slot)
                if ino: found.append(ino)
            if blk % 50000 == 0 and blk > 0:
                print(f'  scanned {blk}/{nblocks}, found {len(found)} candidate inodes', file=sys.stderr)

    print(f'\ntotal candidate session-like inodes in journal: {len(found)}')
    # Sort by size descending, show top 30
    found.sort(key=lambda x: -x['size'])
    print(f'\ntop 30 by size:')
    for i, ino in enumerate(found[:30]):
        n_ext = len(ino['extents'])
        ext_summary = ''
        if ino['extents'] and 'phys_blk' in ino['extents'][0]:
            blk_ranges = [(e['phys_blk'], e['phys_blk']+e['len']-1) for e in ino['extents']]
            ext_summary = ' '.join(f"{a}-{b}" for a,b in blk_ranges[:3])
        elif ino['extents']:
            ext_summary = f"depth={ino['eh_depth']}"
        print(f"  size={ino['size']:>10} mtime={ino['mtime']} ext={n_ext}({ext_summary}) at jblk={ino['journal_block']}/slot{ino['slot_idx']}")

    # Save full result
    out = os.path.join(RECOVERY_DIR, 'journal_inode_candidates.json')
    with open(out,'w') as f:
        json.dump(found, f, indent=1, default=str)
    print(f'\nfull list -> {out}')

if __name__ == '__main__':
    main()
