#!/usr/bin/env python3
"""A scripted stand-in for agent-resume's selftest.

probe.selftest copies this file into a private room under the tool's name and
runs probe.expect against it, so the verifier's rules are exercised on real
processes without running the real suite. The clean copy lists CATALOG,
reports every check as passing and exits 0. Each knob in the block below
changes one thing a verifier must notice. The block is the only place the
knobs are written: probe.knob_block() reads their names and defaults from
it, and a test mutation rewrites it with probe.knobs(). So each knob stays a
line of its own, NAME = value as repr writes the value, the knob lines
follow each other, and no other top-level assignment of a literal to a name
in capitals fits on one line.

FAILING names the checks reported as failed. SUMMARY is exact, missing,
malformed or short. EXIT replaces the exit status. DROP leaves checks out of
the report, as removing a check with its declaration would. EXTRA adds (id,
description) rows the listing never had. DUPLICATE reports those ids twice.
DESCRIBE reports other descriptions for those ids. SLEEP stalls that many
seconds before answering. SIGNAL ends the process with that signal once the
report is printed. FILTER_LIES makes the failed-only report name a check that
passed. WRITE_BESIDE names a path under this file's directory that every
invocation writes to, as a unit leaking into the corpus beside the tool would.
BYTES_NOT_UTF8 puts a byte that is not UTF-8, 0xff, in each failing row's
detail and on stderr, as a tool that prints a path in any bytes would.

The next five start processes that outlive their part of the run, as the
real suite's fixtures can. OWNED_DRIVER starts a driver in a session of its
own with a terminal child that ignores the hangup its pty sends, as the real
suite's completion drive does; with "stays" the driver keeps running, and with
"exits" it ends at once, so its terminal child is handed to whichever process
adopts orphans. STUBBORN starts a process that ignores SIGTERM and answers it
by starting another process, so a cleanup that read the process table once
and never again misses the newer one. NAMED_SYSTEMD starts a process that
calls itself systemd, which no cleanup may signal. NAME_NOT_UTF8 starts a
process that gives itself a command name in bytes that are not UTF-8, with a
sleeping child, so a cleanup that decodes names loses both. LISTING_LEAVES
makes a --list run start a driver that stays, as OWNED_DRIVER="stays" does
for a report. LOCKED_TMPDIR names a directory: a run given a TMPDIR inside it
makes a directory in that TMPDIR, puts a file in it and takes away its write
permission, so removing the TMPDIR leaves both; a run given any other TMPDIR
does nothing.

With PROBE_SYNTHETIC_LOG set, every invocation appends its arguments to that
file as one JSON line, which is how a test sees what the verifier asked. With
PROBE_SYNTHETIC_PATHS set, every invocation appends the path it ran from, which
is how a test sees which copy the verifier ran. With PROBE_SYNTHETIC_ENVIRON
set, every invocation appends its TMPDIR, TMUX_TMPDIR and XDG_CACHE_HOME and
the command line of the process that started it, its supervisor, as one JSON
line, which is how a test sees what the suite was given and which verifier file
its supervisor loaded. With PROBE_SYNTHETIC_STARTED set, every process those
five knobs start is appended there as its pid and start time, which is how a
test sees whether each was ended.
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
BYTES_NOT_UTF8 = False
OWNED_DRIVER = None
STUBBORN = False
NAMED_SYSTEMD = False
NAME_NOT_UTF8 = False
LISTING_LEAVES = False
LOCKED_TMPDIR = None

# The driver OWNED_DRIVER starts. Its child is a terminal's: pty.fork puts it in
# a session of its own, and it ignores the hangup the pty sends when the driver
# ends, so only an owner that ends it by identity ends it. The child says ready
# on the terminal once it ignores the hangup, and the driver waits for that
# before it says who its child is, so a driver that ends at once cannot take
# the child with it. Given "exits", the driver then ends.
DRIVER = r"""
import os, pty, signal, sys, time
child, terminal = pty.fork()
if child == 0:
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    os.write(1, b"ready\n")
    time.sleep(600)
    os._exit(0)
said = b""
while b"ready" not in said:
    said += os.read(terminal, 64)
print(os.getpid(), child, flush=True)
if sys.argv[1] == "exits":
    os._exit(0)
time.sleep(600)
"""

# The code that reads a process's start time and records a process the runner
# started in the witness file the test reads, the one PROBE_SYNTHETIC_STARTED
# names, kept as source text: the runner executes it below, and
# STUBBORN_PROCESS, which runs as a program of its own, begins with it.
WITNESS = r'''
import os


def started_at(pid):
    """The kernel's start time for pid, field 22 of its stat.

    Read as bytes, since the command name before the fields can be any bytes.
    """
    with open(f"/proc/{pid}/stat", "rb") as stat:
        raw = stat.read()
    return raw[raw.rindex(b")") + 2:].split()[19].decode()


def witnessed(*pids):
    """Append pid and start time of each process to PROBE_SYNTHETIC_STARTED, when it is set."""
    witness = os.environ.get("PROBE_SYNTHETIC_STARTED")
    if witness:
        with open(witness, "a") as started:
            for pid in pids:
                started.write(f"{pid} {started_at(pid)}\n")
'''

# The process STUBBORN starts. SIGTERM does not end it: it starts a sleeper in
# a session of its own instead, records the sleeper in the witness file, and
# carries on until SIGKILL.
STUBBORN_PROCESS = WITNESS + r"""
import signal, subprocess, time
def answer(_number, _frame):
    sleeper = subprocess.Popen(["sleep", "600"], start_new_session=True)
    witnessed(sleeper.pid)
signal.signal(signal.SIGTERM, answer)
print("ready", flush=True)
while True:
    time.sleep(600)
"""

# The process NAMED_SYSTEMD starts: prctl(PR_SET_NAME) gives it the command
# name systemd, the name the service manager that adopts orphans goes by.
NAMED_SYSTEMD_PROCESS = r"""
import ctypes, time
ctypes.CDLL(None).prctl(15, b"systemd", 0, 0, 0)
print("named", flush=True)
time.sleep(600)
"""

# The process NAME_NOT_UTF8 starts: it names itself \xff, left, \xfe, which
# is not UTF-8, then starts a sleeping child and says who that is.
NAME_NOT_UTF8_PROCESS = r"""
import ctypes, subprocess, time
ctypes.CDLL(None).prctl(15, b"\xffleft\xfe", 0, 0, 0)
child = subprocess.Popen(["sleep", "600"])
print("named", child.pid, flush=True)
time.sleep(600)
"""


def _witness_functions():
    """started_at and witnessed, as WITNESS defines them."""
    namespace = {}
    exec(WITNESS, namespace)
    return namespace["started_at"], namespace["witnessed"]


started_at, witnessed = _witness_functions()


def start(code, *args, own_session=True):
    """Start code under this interpreter; returns the process and the first line it printed."""
    process = subprocess.Popen([sys.executable, "-c", code, *args], stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               text=True, start_new_session=own_session)
    return process, process.stdout.readline().split()


def start_owned_driver(how):
    """Start the driver and its terminal child; with "exits" the driver ends straight after."""
    driver, said = start(DRIVER, how)
    leader, child = (int(word) for word in said)
    if how == "exits":
        # The driver ends by itself; what is left to end is its terminal child,
        # which has to outlive it for a cleanup to be what ends it.
        born = started_at(child)
        witnessed(child)
        driver.wait()
        if not os.path.exists(f"/proc/{child}") or started_at(child) != born:
            raise SystemExit("the driver's terminal child ended with the driver")
    else:
        witnessed(leader, child)


def main(argv):
    log = os.environ.get("PROBE_SYNTHETIC_LOG")
    if log:
        with open(log, "a") as record:
            record.write(json.dumps(argv) + "\n")
    environ = os.environ.get("PROBE_SYNTHETIC_ENVIRON")
    if environ:
        given = {name: os.environ.get(name) for name in ("TMPDIR", "TMUX_TMPDIR", "XDG_CACHE_HOME")}
        with open(f"/proc/{os.getppid()}/cmdline", "rb") as parent:
            given["supervisor"] = [part.decode(errors="replace")
                                   for part in parent.read().split(b"\0") if part]
        with open(environ, "a") as record:
            record.write(json.dumps(given) + "\n")
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
    if BYTES_NOT_UTF8:
        # A lone surrogate written under surrogateescape is the byte itself.
        for stream in (sys.stdout, sys.stderr):
            stream.reconfigure(errors="surrogateescape")
        print("a byte that is not UTF-8: \udcff", file=sys.stderr, flush=True)
    if LOCKED_TMPDIR and os.environ.get("TMPDIR", "").startswith(LOCKED_TMPDIR + os.sep):
        locked = os.path.join(os.environ["TMPDIR"], "locked")
        os.mkdir(locked)
        with open(os.path.join(locked, "kept"), "w") as kept:
            kept.write("kept\n")
        os.chmod(locked, 0o500)
    rows = [(cid, DESCRIBE.get(cid, description))
            for cid, description in CATALOG if cid not in DROP]
    if "--list" in argv:
        if LISTING_LEAVES:
            start_owned_driver("stays")
        width = max(len(cid) for cid, _description in rows)
        for cid, description in rows:
            print(f"{cid:<{width}}  {description}")
        return 0
    if OWNED_DRIVER:
        start_owned_driver(OWNED_DRIVER)
    if STUBBORN:
        stubborn, _ready = start(STUBBORN_PROCESS)
        witnessed(stubborn.pid)
    if NAMED_SYSTEMD:
        named, _said = start(NAMED_SYSTEMD_PROCESS)
        witnessed(named.pid)
    if NAME_NOT_UTF8:
        not_utf8, said = start(NAME_NOT_UTF8_PROCESS)
        witnessed(not_utf8.pid, int(said[1]))
    if SLEEP:
        time.sleep(SLEEP)
    verdicts = [(cid, description, cid not in FAILING,
                 ("scripted failure \udcff" if BYTES_NOT_UTF8 else "scripted failure")
                 if cid in FAILING else "")
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
