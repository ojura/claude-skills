#!/usr/bin/env python3
"""Which selftest units read processes their run did not start.

Runs agent-resume's selftest with its process fence recording instead of
stopping: every refusal is written to a file, one JSON object a line, and the
run goes on, so one pass lists every unit that names a process the run did not
start, the reader it used and the pids it named. Rerun it after each package of
the consolidation is adapted; an empty result is the pass.

    proc_reader_audit.py [--live] [--tool PATH] [--static proc-readers.json]

--tool is the agent-resume to audit, by default the one beside this file; the
corpus beside it is the one its selftest copies. --live audits the live tier as
well, which starts real processes of its own (never a real tmux or claude).
--static compares the result with a list of units that a static trace found able
to reach a reader, such as the reviewer's proc-readers.json.

Each tier runs with TMPDIR set to a fresh private directory whose path is 15
characters long and with XDG_CACHE_HOME inside it. At this length a run's socket
directory plus its longest corpus socket name stays inside the 107 bytes a Unix
socket path may have, and the warning check promoted-starts-where-its-file-is
wraps where its phrase stays whole; both depend on the path's length.

Exit status: 0 when nothing was refused, 1 when something was, 2 when a tier
could not run.
"""
import argparse
import collections
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
TIERS = {"read-only": ["--selftest"], "live": ["--selftest", "--live"]}


def private_root():
    """A fresh 0700 directory whose path is exactly 15 characters long."""
    root = Path(tempfile.mkdtemp(prefix="fq", dir="/tmp"))
    if len(str(root)) != 15:
        raise SystemExit(f"{root} is not 15 characters long; see the module docstring")
    return root


def run_tier(tool, tier, record):
    """Run one tier with the fence recording into record; return its exit status."""
    root = private_root()
    (root / "c").mkdir(mode=0o700)
    env = dict(os.environ, TMPDIR=str(root), XDG_CACHE_HOME=str(root / "c"))
    env[f"{tool.name.upper().replace('-', '_')}_SELFTEST_FENCE"] = str(record)
    done = subprocess.run([sys.executable, str(tool), *TIERS[tier]], cwd=tool.parent,
                          env=env, capture_output=True, text=True, timeout=1800)
    kept = record.with_name(f"{record.stem}.{tier}.out")
    kept.write_text(done.stdout + ("\n--- stderr ---\n" + done.stderr if done.stderr else ""))
    last = (done.stdout.strip().splitlines() or ["(no output)"])[-1]
    failed = [line.split()[1] for line in done.stdout.splitlines()
              if line.strip().startswith("FAIL ")]
    print(f"{tier}: exit {done.returncode}, {last.strip()}  (TMPDIR {root}; output {kept})")
    for check_id in failed:
        print(f"  FAIL {check_id}")
    return done.returncode


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--tool", type=Path, default=HERE / "agent-resume")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--static", type=Path)
    parser.add_argument("--record", type=Path, help="keep the raw refusals here")
    args = parser.parse_args()
    tool = args.tool.resolve()
    # The suite runs from the tool's own directory, so the record is named by
    # its absolute path.
    record = (args.record or Path(tempfile.mkstemp(prefix="proc-audit-", suffix=".jsonl")[1])).resolve()
    record.write_text("")
    statuses = {tier: run_tier(tool, tier, record)
                for tier in (["read-only", "live"] if args.live else ["read-only"])}
    rows = [json.loads(line) for line in record.read_text().splitlines() if line.strip()]
    by_unit = collections.defaultdict(lambda: {"readers": collections.Counter(), "pids": set()})
    for row in rows:
        by_unit[row["unit"]]["readers"][row["reader"]] += 1
        by_unit[row["unit"]]["pids"].add(row["pid"])
    print(f"{len(rows)} refusal(s) in {len(by_unit)} unit(s); raw: {record}")
    for unit, seen in sorted(by_unit.items(), key=lambda kv: -sum(kv[1]["readers"].values())):
        pids = sorted(seen["pids"])
        shown = ", ".join(map(str, pids[:6])) + (" ..." if len(pids) > 6 else "")
        print(f"  {unit}: {dict(seen['readers'])}; {len(pids)} pid(s): {shown}")
    if args.static:
        listed = json.loads(args.static.read_text())
        units = {entry.get("unit") or entry.get("function") or entry.get("name")
                 for entry in (listed if isinstance(listed, list)
                               else listed.get("units") or listed.get("rows") or [])}
        units.discard(None)
        found = set(by_unit)
        print(f"static list: {len(units)} unit(s); refused at run time: {len(found & units)}; "
              f"only in the static list: {len(units - found)}; "
              f"only at run time: {len(found - units)}")
        for unit in sorted(found - units):
            print(f"  only at run time: {unit}")
    if any(status not in (0, 1) for status in statuses.values()):
        return 2
    return 1 if rows else 0


if __name__ == "__main__":
    sys.exit(main())
