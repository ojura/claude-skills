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

The default leaf is the end of the chain that continues from the file's last
user or assistant entry, not the trailing last-prompt's leafUuid. A
last-prompt is written only when the operator submits a prompt, so turns
driven by teammate messages append entries without one and that pointer
falls behind. Grafting onto the stale pointer would leave every turn after it
out of the new boundary's ancestry, where no logicalParentUuid walk can reach
them again. The records that follow the last turn (its turn_duration, a
local command and its output, a queued message nobody answered) are its
descendants, so the default leaf follows them and they stay ancestors of the
boundary. It follows parent links, not file order, because the harness can
write a record above its parent. When the last-prompt names a different
entry the tool says so, with that entry's type, line and distance from the
leaf.

Optionally trims a dead tail first (--leaf): keeps everything up to and
including the target entry, plus an immediately-following last-prompt line
if it already points at the target.

A physical line holding two records with no newline between them (the
harness writes fresh-spawned teammate transcripts that way: the founding
user entry followed by an agent-name or cost-state record) is read as two
entries and written back unchanged; the tool says which lines it found.

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


_DECODER = json.JSONDecoder()


def parse_records(line):
    """The records on one physical line, each paired with the exact text it
    occupies, so that the pieces written back in order reproduce the line.

    Needed because at spawn the harness writes a teammate's root user entry
    (the brief) and a metadata record (agent-name, cost-state) with no newline
    between them; a strict json.loads refuses that line whole. Each record is
    decoded from where the previous one ended. The first piece keeps the
    line's leading whitespace and the last keeps its newline. A remainder that
    is not a record raises JSONDecodeError."""
    text_end = len(line.rstrip("\r\n"))
    records, starts, pos = [], [], 0
    while True:
        while pos < text_end and line[pos] in " \t":
            pos += 1
        if pos >= text_end:
            break
        obj, end = _DECODER.raw_decode(line, pos)
        records.append(obj)
        starts.append(pos)
        pos = end
    bounds = [0] + starts[1:] + [len(line)]
    return [(records[i], line[bounds[i]:bounds[i + 1]])
            for i in range(len(records))]


def load(path):
    """Entries as (line number, dict, raw text), and the lines that hold more
    than one record. A strict per-line parse refuses such a line whole, and
    the harness's own loader drops it, root entry included."""
    entries, glued = [], []
    with open(path) as fh:
        for n, line in enumerate(fh, 1):
            try:
                entries.append((n, json.loads(line), line))
                continue
            except json.JSONDecodeError:
                pass
            try:
                records = parse_records(line)
            except json.JSONDecodeError as e:
                die(f"line {n} is not valid JSON ({e}); refusing to touch the file")
            if not records:
                die(f"line {n} holds no record; refusing to touch the file")
            glued.append((n, [d.get("type") for d, _ in records]))
            entries.extend((n, d, raw) for d, raw in records)
    return entries, glued


def kind(d):
    """An entry's type with its subtype or attachment type, as in
    system/local_command or attachment/queued_command."""
    sub = d.get("subtype")
    if not sub and isinstance(d.get("attachment"), dict):
        sub = d["attachment"].get("type")
    return f"{d.get('type')}/{sub}" if sub else str(d.get("type"))


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
    entries, glued = load(path)
    for n, types in glued:
        print(f"note: line {n} holds {len(types)} records with no newline between "
              f"them ({', '.join(str(t) for t in types)}); read as separate entries, "
              "bytes kept as written")
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
        # The graft leaf is the end of the chain that continues from the file's
        # last user or assistant entry, NOT the trailing last-prompt's leafUuid.
        # A last-prompt is written only when the operator submits a prompt, so on
        # a session whose final turns were driven by teammate messages (which
        # append entries but no last-prompt) that pointer lags behind the real
        # tip. Grafting onto it would leave every turn after it out of the new
        # boundary's ancestry, where no walk that follows logicalParentUuid
        # reaches them.
        main_entries = [(n, d) for n, d, _ in kept
                if d.get("uuid") and not d.get("isSidechain")]
        turns = [t for t in main_entries if t[1].get("type") in ("user", "assistant")]
        if not turns:
            die("no conversation entry with a uuid found; nothing to hang the leaf on")
        # The records that follow the last turn (turn_duration, a local command
        # and its output, a queued message nobody answered) are its descendants;
        # follow them so they stay on the boundary's ancestry. Follow the links,
        # not the file order: the harness can write a record before its parent
        # (a turn_duration one line above the reply it times, a "Not logged in"
        # reply above the prompt it answers). File order only picks among
        # siblings, taking the branch written last.
        children = {}
        for t in main_entries:
            children.setdefault(t[1].get("parentUuid"), []).append(t)
        last_turn = tip = turns[-1]
        steps, seen = 0, {tip[1]["uuid"]}
        while children.get(tip[1]["uuid"]):
            nxt = children[tip[1]["uuid"]][-1]
            if nxt[1]["uuid"] in seen:
                print(f"note: the parent links after {last_turn[1]['uuid']} loop back to "
                      f"{nxt[1]['uuid']} (line {nxt[0]}); stopping at the entry before it")
                break
            seen.add(nxt[1]["uuid"])
            tip = nxt
            steps += 1
        leaf_uuid = tip[1]["uuid"]
        last_prompts = [d for _, d, _ in kept
                        if d.get("type") == "last-prompt" and d.get("leafUuid")]
        if last_prompts and last_prompts[-1]["leafUuid"] != leaf_uuid:
            cand = last_prompts[-1]["leafUuid"]
            anywhere = {d["uuid"]: (n, d) for n, d, _ in kept if d.get("uuid")}
            by_uuid = {t[1]["uuid"]: t for t in main_entries}
            # the leaf's ancestors, newest first, until cand or the root
            ancestors, cur, seen = [], tip, set()
            while cur is not None and cur[1]["uuid"] != cand and cur[1]["uuid"] not in seen:
                seen.add(cur[1]["uuid"])
                ancestors.append(cur)
                cur = by_uuid.get(cur[1].get("parentUuid"))
            if cand not in anywhere:
                print(f"note: the trailing last-prompt names {cand}, which is not an "
                      "entry in this file; grafting at the leaf below instead")
            elif cur is None or cur[1]["uuid"] != cand:
                n, d = anywhere[cand]
                print(f"note: the trailing last-prompt names {cand} ({kind(d)}, line {n}), "
                      "which is not an ancestor of the leaf below; grafting at the leaf instead")
            else:
                n, d = anywhere[cand]
                k = len(ancestors)
                c = sum(1 for t in ancestors if t[1].get("type") in ("user", "assistant"))
                where = ("the leaf below is the next entry on its chain. Grafting at the "
                         "leaf keeps that entry" if k == 1 else
                         f"the leaf below is {k} entries further along its chain, {c} of "
                         "them user or assistant entries. Grafting at the leaf keeps them")
                print(f"note: the trailing last-prompt names {cand} ({kind(d)}, line {n}); "
                      f"{where} on the boundary's ancestry")

    leaf_entries = [(n, d) for n, d, _ in kept if d.get("uuid") == leaf_uuid]
    if not leaf_entries:
        die(f"leaf {leaf_uuid} is not among the kept lines")
    leaf_line, donor = leaf_entries[-1]

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
    print(f"file:            {path} ({entries[-1][0]} lines)")
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
    print(f"logical parent:  {leaf_uuid} ({kind(donor)}, line {leaf_line})")
    if not args.leaf and steps:
        print(f"                   {steps} entr{'y' if steps == 1 else 'ies'} further along the "
              f"chain from {last_turn[1]['uuid']} ({kind(last_turn[1])}, line {last_turn[0]}), "
              "the last user or assistant entry in the file")
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
            # a --leaf cut inside a glued line, or a file whose last line has
            # no newline, would otherwise glue the new entry onto the last record
            if kept and not kept[-1][2].endswith("\n"):
                fh.write("\n")
            fh.write(json.dumps(entry, separators=(",", ":")) + "\n")
            fh.write(json.dumps(last_prompt, separators=(",", ":")) + "\n")
        shutil.copymode(path, tmp)
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass

    new_danglers = ref_check(load(path)[0]) - dangling_before
    if new_danglers:
        shutil.copy2(backup, path)
        die("verification failed, restored backup; newly dangling refs: "
            + ", ".join(f"{f}->{r}" for f, r in sorted(new_danglers)))

    print(f"\nwritten: {kept[-1][0] + 2} lines; backup at {backup}")


if __name__ == "__main__":
    main()
