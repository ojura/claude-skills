#!/usr/bin/env python3
"""A scripted stand-in for agent-resume's selftest.

probe.selftest copies this file into a private room under the tool's name and
runs probe.expect against it, so the verifier's rules are exercised on real
processes without running the real suite. The clean copy lists CATALOG,
reports every check as passing and exits 0. Each knob in the block below
changes one thing a verifier must notice, and a test mutation rewrites the
block: probe.knobs() renders it from the same defaults, so the block's text
here must stay exactly as that function writes it.

FAILING names the checks reported as failed. SUMMARY is exact, missing,
malformed or short. EXIT replaces the exit status. DROP leaves checks out of
the report, as removing a check with its declaration would. EXTRA adds (id,
description) rows the listing never had. DUPLICATE reports those ids twice.
DESCRIBE reports other descriptions for those ids. SLEEP stalls that many
seconds before answering. SIGNAL ends the process with that signal once the
report is printed. FILTER_LIES makes the failed-only report name a check that
passed. WRITE_BESIDE names a path under this file's directory that every
invocation writes to, as a unit leaking into the corpus beside the tool would.

With PROBE_SYNTHETIC_LOG set, every invocation appends its arguments to that
file as one JSON line, which is how a test sees what the verifier asked. With
PROBE_SYNTHETIC_PATHS set, every invocation appends the path it ran from, which
is how a test sees which copy the verifier ran.
"""

import json
import os
import signal
import sys
import time

CATALOG = (
    ("target-check", "the check a case names"),
    ("other-check", "a check a case may justify as a co-failure"),
    ("third-check", "a check nothing touches"),
    ("every-unit-reported", "no unit ended the run before it reported"),
)

FAILING = []
SUMMARY = 'exact'
EXIT = None
DROP = []
EXTRA = []
DUPLICATE = []
DESCRIBE = {}
SLEEP = 0
SIGNAL = None
FILTER_LIES = False
WRITE_BESIDE = None


def main(argv):
    log = os.environ.get("PROBE_SYNTHETIC_LOG")
    if log:
        with open(log, "a") as record:
            record.write(json.dumps(argv) + "\n")
    paths = os.environ.get("PROBE_SYNTHETIC_PATHS")
    if paths:
        with open(paths, "a") as record:
            record.write(os.path.abspath(__file__) + "\n")
    if WRITE_BESIDE:
        beside = os.path.join(os.path.dirname(os.path.abspath(__file__)), WRITE_BESIDE)
        os.makedirs(os.path.dirname(beside), exist_ok=True)
        with open(beside, "a") as record:
            record.write("written during the run\n")
    if "--selftest" not in argv:
        return 2
    rows = [(cid, DESCRIBE.get(cid, description))
            for cid, description in CATALOG if cid not in DROP]
    if "--list" in argv:
        width = max(len(cid) for cid, _description in rows)
        for cid, description in rows:
            print(f"{cid:<{width}}  {description}")
        return 0
    if SLEEP:
        time.sleep(SLEEP)
    verdicts = [(cid, description, cid not in FAILING,
                 "scripted failure" if cid in FAILING else "")
                for cid, description in rows]
    verdicts += [(cid, description, True, "") for cid, description in EXTRA]
    verdicts += [row for row in verdicts if row[0] in DUPLICATE]
    failed = sum(1 for _cid, _description, ok, _detail in verdicts if not ok)
    if "--failed" in argv:
        for cid, description, ok, detail in verdicts:
            if not ok or (FILTER_LIES and cid == "third-check"):
                print(f"{cid}  {description}" + (f"  ({detail})" if detail else ""))
        print(f"checks: {len(verdicts) - failed} ok, {failed} failed")
    else:
        idwidth = max(len(cid) for cid, _description, _ok, _detail in verdicts)
        width = max(len(description) for _cid, description, _ok, _detail in verdicts)
        for cid, description, ok, detail in verdicts:
            print(f"  {'PASS' if ok else 'FAIL'}  {cid:<{idwidth}}  {description:<{width}}"
                  + (f"  ({detail})" if detail else ""))
        print()
        passed = len(verdicts) - failed
        if SUMMARY == "exact":
            print(f"{passed}/{len(verdicts)} checks passed")
        elif SUMMARY == "malformed":
            print(f"{passed}/x checks passed")
        elif SUMMARY == "short":
            print(f"{passed - 1}/{len(verdicts) - 1} checks passed")
    sys.stdout.flush()
    if SIGNAL:
        os.kill(os.getpid(), getattr(signal, SIGNAL))
    if EXIT is not None:
        return EXIT
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
