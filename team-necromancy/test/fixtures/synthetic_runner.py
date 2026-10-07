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

OWNED_DRIVER starts a driver in a session of its own whose terminal child
ignores the hangup its pty sends, as the real suite's completion drive does,
and writes the driver down where the suite writes the groups it owns: in the
file the verifier names ("direct") or in a worker's file that one names
("nested"). Ending the runner's group reaches neither process. RECORD_LINES
are lines written as they are into that same file, for a test that names a
process of its own there.

With PROBE_SYNTHETIC_LOG set, every invocation appends its arguments to that
file as one JSON line, which is how a test sees what the verifier asked. With
PROBE_SYNTHETIC_PATHS set, every invocation appends the path it ran from, which
is how a test sees which copy the verifier ran. With PROBE_SYNTHETIC_GROUPS
set, OWNED_DRIVER appends the pid and start time of the driver and of its
terminal child, which is how a test sees whether both were ended.
"""

import json
import os
import signal
import subprocess
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
OWNED_DRIVER = None
RECORD_LINES = []

# The driver OWNED_DRIVER starts. Its child is a terminal's: pty.fork puts it in
# a session of its own, and it ignores the hangup the pty sends when the driver
# ends, so only an owner that ends it by identity ends it.
DRIVER = r"""
import os, pty, signal, time
child, _terminal = pty.fork()
if child == 0:
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    time.sleep(600)
    os._exit(0)
print(os.getpid(), child, flush=True)
time.sleep(600)
"""


def started_at(pid):
    """The kernel's start time for pid, field 22 of its stat, as the suite writes it."""
    with open(f"/proc/{pid}/stat") as stat:
        text = stat.read()
    return text[text.rindex(")") + 2:].split()[19]


def owned_groups_record():
    """The file the verifier named for the groups this run owns, or None."""
    name = os.path.basename(os.path.realpath(__file__))
    return os.environ.get(f"{name.upper().replace('-', '_')}_SELFTEST_OWNED_GROUPS")


def start_owned_driver(where):
    """Start the driver and write it down as the suite would, directly or in a worker's file."""
    driver = subprocess.Popen([sys.executable, "-c", DRIVER], stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              text=True, start_new_session=True)
    leader, child = (int(word) for word in driver.stdout.readline().split())
    entry = f"{leader} {started_at(leader)}\n"
    record = owned_groups_record()
    if record and where == "nested":
        worker = record + ".worker"
        with open(worker, "a") as owned:
            owned.write(entry)
        with open(record, "a") as owned:
            owned.write(f"record {worker}\n")
    elif record:
        with open(record, "a") as owned:
            owned.write(entry)
    witness = os.environ.get("PROBE_SYNTHETIC_GROUPS")
    if witness:
        with open(witness, "a") as named:
            for pid in (leader, child):
                named.write(f"{pid} {started_at(pid)}\n")


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
    if OWNED_DRIVER:
        start_owned_driver(OWNED_DRIVER)
    if RECORD_LINES and owned_groups_record():
        with open(owned_groups_record(), "a") as owned:
            owned.write("".join(f"{line}\n" for line in RECORD_LINES))
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
