#!/usr/bin/env python3
"""Pseudocompact a Claude Code session JSONL: graft a synthetic compaction
boundary onto a chosen leaf so the next resume starts with an empty context
window while lpu-aware tooling keeps the full ancestry.

What it appends (validated empirically 2026-08-03):
  1. A rootless user entry: parentUuid=null, logicalParentUuid=<leaf>,
     isCompactSummary=true, content "The conversation was compacted."
  2. A trailing last-prompt line with leafUuid=<new entry uuid>.
     THIS LINE IS WHAT MAKES IT WORK: on resume the harness picks its leaf
     from the file's final last-prompt line, not from the last chain entry.
     Without it the new entry is silently ignored.

The default leaf is the file's LAST conversation entry, not the trailing
last-prompt's leafUuid. A last-prompt is written only when the operator
submits a prompt, so turns driven by teammate messages append entries
without one and that pointer falls behind. Grafting onto the stale pointer
orphans every turn after it, since they stop being ancestors of the new
boundary and no logicalParentUuid walk can reach them again. When the two
disagree the tool says so and grafts at the real tip.

Optionally trims a dead tail first (--leaf): keeps everything up to and
including the target entry, plus an immediately-following last-prompt line
if it already points at the target.

Caveats the tool cannot fix for you:
  - A live `claude --resume <session>` process keeps its old in-memory leaf.
    Kill and re-resume after running this; prompting the stale process
    appends entries parented on uuids that may no longer exist.
  - A cancelled /resume in the fresh process appends a cruft turn and a
    last-prompt pointing at it; re-run with --leaf <pseudocompaction-uuid>
    is NOT the fix: just delete the cruft lines and re-append the
    last-prompt (or restore the .bak and re-run this tool).

Usage:
  pseudocompact.py <session.jsonl | session-id> [--leaf UUID]
                   [--message TEXT] [--dry-run]
"""

import argparse
import datetime
import glob
import json
import os
import shutil
import subprocess
import sys
import uuid as uuidlib

PROJECTS_DIR = os.path.expanduser("~/.claude/projects")
DEFAULT_MESSAGE = "The conversation was compacted."

# Context fields copied from the donor entry onto the synthetic one, so it
# matches the file's convention (shape mirrored from a real v2.1.x
# compact-summary entry).
CONTEXT_FIELDS = [
    "userType", "entrypoint", "cwd", "sessionId", "version",
    "gitBranch", "slug", "teamName", "agentName",
]


def die(msg):
    sys.exit(f"error: {msg}")


def resolve_file(arg):
    if os.path.isfile(arg):
        return arg
    hits = glob.glob(os.path.join(PROJECTS_DIR, "*", f"{arg}.jsonl"))
    if not hits:
        die(f"no file and no {arg}.jsonl anywhere under {PROJECTS_DIR}")
    if len(hits) > 1:
        die("session id matches multiple files:\n  " + "\n  ".join(hits))
    return hits[0]


def load(path):
    entries = []
    with open(path) as fh:
        for n, line in enumerate(fh, 1):
            try:
                entries.append((n, json.loads(line), line))
            except json.JSONDecodeError as e:
                die(f"line {n} is not valid JSON ({e}); refusing to touch the file")
    return entries


def ref_check(entries):
    """Return (field, uuid) refs that do not resolve in-file.
    Pre-existing danglers are legal (cross-file logicalParentUuid from
    resumes/phantom-lpu backfill); callers compare before vs after and only
    complain about new ones."""
    uuids = {d["uuid"] for _, d, _ in entries if d.get("uuid")}
    dangling = set()
    for n, d, _ in entries:
        for field in ("parentUuid", "logicalParentUuid", "leafUuid"):
            ref = d.get(field)
            if ref and ref not in uuids:
                dangling.add((field, ref))
    return dangling


def live_processes(session_id):
    if not session_id:
        return []
    try:
        out = subprocess.run(
            ["pgrep", "-af", session_id], capture_output=True, text=True
        ).stdout
    except FileNotFoundError:
        return []
    return [l for l in out.splitlines()
            if "claude" in l and "pgrep" not in l and " -c " not in l]


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("session", help="path to session JSONL, or bare session id")
    ap.add_argument("--leaf", metavar="UUID",
                    help="entry to pseudocompact at; everything after it is "
                         "trimmed (default: current leaf, no trimming)")
    ap.add_argument("--message", default=DEFAULT_MESSAGE,
                    help=f"summary text (default: {DEFAULT_MESSAGE!r})")
    ap.add_argument("--dry-run", action="store_true",
                    help="report the plan, write nothing")
    args = ap.parse_args()

    path = resolve_file(args.session)
    entries = load(path)
    dangling_before = ref_check(entries)

    # --- pick the cut point and logical parent ------------------------------
    if args.leaf:
        matches = [i for i, (_, d, _) in enumerate(entries)
                   if d.get("uuid") == args.leaf]
        if not matches:
            die(f"uuid {args.leaf} not found in {path}")
        cut = matches[-1]
        leaf_uuid = args.leaf
        # keep an adjacent last-prompt that already points at the kept leaf,
        # its lastPrompt text stays accurate for the session picker
        if (cut + 1 < len(entries)
                and entries[cut + 1][1].get("type") == "last-prompt"
                and entries[cut + 1][1].get("leafUuid") == leaf_uuid):
            cut += 1
        kept = entries[:cut + 1]
        trimmed = entries[cut + 1:]
    else:
        kept = entries
        trimmed = []
        # The graft leaf is the file's last conversation entry, NOT the trailing
        # last-prompt's leafUuid. A last-prompt is written only when the operator
        # submits a prompt, so on a session whose final turns were driven by
        # teammate messages (which append entries but no last-prompt) that pointer
        # lags behind the real tip. Grafting onto it orphans every turn after it:
        # they stop being ancestors of the new boundary, so no walk that follows
        # logicalParentUuid can reach them again, and they are silently gone.
        chain = [d for _, d, _ in kept
                 if d.get("uuid") and d.get("type") in ("user", "assistant")
                 and not d.get("isSidechain")]
        if not chain:
            die("no conversation entry with a uuid found; nothing to hang the leaf on")
        leaf_uuid = chain[-1]["uuid"]
        last_prompts = [d for _, d, _ in kept
                        if d.get("type") == "last-prompt" and d.get("leafUuid")]
        if last_prompts and last_prompts[-1]["leafUuid"] != leaf_uuid:
            cand = last_prompts[-1]["leafUuid"]
            idx = next((i for i, d in enumerate(chain) if d.get("uuid") == cand), None)
            if idx is None:
                print(f"note: the trailing last-prompt names {cand}, which is not a "
                      "conversation entry in this file; grafting at the last entry instead")
            else:
                n = len(chain) - 1 - idx
                print(f"note: the trailing last-prompt names {cand}, which is {n} "
                      f"conversation entr{'y' if n == 1 else 'ies'} short of the end "
                      f"of the file; grafting at the last entry instead so those {n} "
                      "are not orphaned")

    leaf_entries = [d for _, d, _ in kept if d.get("uuid") == leaf_uuid]
    if not leaf_entries:
        die(f"leaf {leaf_uuid} is not among the kept lines")
    donor = leaf_entries[-1]

    # --- build the two appended lines ---------------------------------------
    new_uuid = str(uuidlib.uuid4())
    entry = {"parentUuid": None, "logicalParentUuid": leaf_uuid,
             "isSidechain": donor.get("isSidechain", False)}
    for f in CONTEXT_FIELDS:
        if f in donor:
            entry[f] = donor[f]
    entry.update({
        "type": "user",
        "message": {"role": "user", "content": args.message},
        "isCompactSummary": True,
        "uuid": new_uuid,
        "timestamp": datetime.datetime.now(datetime.timezone.utc)
                     .strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
    })
    kept_prompts = [d for _, d, _ in kept if d.get("type") == "last-prompt"
                    and d.get("lastPrompt")]
    last_prompt = {
        "type": "last-prompt",
        "lastPrompt": kept_prompts[-1]["lastPrompt"] if kept_prompts
                      else args.message,
        "leafUuid": new_uuid,
        "sessionId": donor.get("sessionId", ""),
    }

    # --- report --------------------------------------------------------------
    print(f"file:            {path} ({len(entries)} lines)")
    if trimmed:
        first, last = trimmed[0][0], trimmed[-1][0]
        print(f"trim:            lines {first}-{last} ({len(trimmed)} lines)")
        for n, d, _ in trimmed[:8]:
            m = d.get("message") or {}
            c = m.get("content", "") if isinstance(m, dict) else ""
            if isinstance(c, list):
                c = " ".join(b.get("text") or b.get("name") or ""
                             for b in c if isinstance(b, dict))
            label = d.get("subtype") or str(c)[:60].replace("\n", " ")
            print(f"                   {n}: {d.get('type')} {label}")
        if len(trimmed) > 8:
            print(f"                   ... and {len(trimmed) - 8} more")
    else:
        print("trim:            nothing (appending at current leaf)")
    print(f"logical parent:  {leaf_uuid} ({donor.get('type')}"
          f"{'/' + donor['subtype'] if donor.get('subtype') else ''})")
    print(f"new leaf uuid:   {new_uuid}")

    procs = live_processes(donor.get("sessionId"))
    if procs:
        print("\nWARNING: live process(es) reference this session; the edit "
              "only takes effect on a fresh resume; prompting a stale process "
              "appends dangling-parented entries:")
        for p in procs:
            print(f"  {p}")

    if args.dry_run:
        print("\ndry run, nothing written")
        return

    # --- write ---------------------------------------------------------------
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup = f"{path}.bak-pseudocompact-{stamp}"
    shutil.copy2(path, backup)
    tmp = path + ".pseudocompact.tmp"
    try:
        with open(tmp, "w") as fh:
            fh.writelines(raw for _, _, raw in kept)
            fh.write(json.dumps(entry, separators=(",", ":")) + "\n")
            fh.write(json.dumps(last_prompt, separators=(",", ":")) + "\n")
        shutil.copymode(path, tmp)
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass

    new_danglers = ref_check(load(path)) - dangling_before
    if new_danglers:
        shutil.copy2(backup, path)
        die("verification failed, restored backup; newly dangling refs: "
            + ", ".join(f"{f}->{r}" for f, r in sorted(new_danglers)))

    print(f"\nwritten: {len(kept) + 2} lines; backup at {backup}")


if __name__ == "__main__":
    main()
