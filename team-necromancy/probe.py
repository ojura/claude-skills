"""Mutation-test API for agent-resume.

Import this module from a mutation script and call ``expect``. The clean suite is
run once, every mutation receives its own copy of the tool and corpus, and the
mutants run with at most four suites active at once.

    import probe

    retained = probe.retained_expectation(mapping_path, live=True, jobs=2,
                                          adjudications=approved_path)
    probe.expect([
        ("check-id", "old text", "new text"),
        ("other-id", "old text", "new text", {"second-id": "why it fails too"}),
    ], live=True, jobs=2, expected=retained, timeout=600, certify=True)

A case succeeds only when its mutant fails exactly the checks it names: the
named check and each co-failure, with the reason the caller wrote down for it.
``expected`` is the independently retained {check id: description} mapping for
the selected tier and jobs value. The listing, the clean run and every mutant
must report that population with those descriptions and exit with the status
their rows call for. ``expect`` raises ``ProbeFailure`` when any of that does
not hold: a listing or report that misses, adds, repeats or misdescribes a
check, a run that ran out of time, died of a signal or stopped before its
summary, an exit status that disagrees with the rows, a mutant that fails more
or less than its case names. The report it prints and carries keeps the raw
stdout and stderr of every refused run. Success is an ordinary return, so
redirected output cannot be mistaken for it.

``retained_expectation`` reads that mapping for one selection from the frozen
expected-mapping-v1 file the reviewer keeps, with the file's hash, the source
it was frozen for and the approved adjudications it rests on, and ``expect``
writes all of them into its report so a verification record says which
expectation judged it.

Certification is asked for, not inferred: ``expect(..., certify=True)`` runs
only with a timeout of its own and such a retained expectation, adjudications
included, that still reads the same from its files, whose adjudications are all
decided and point where their decisions can, and whose frozen source is the
tool under test; only while the verifier on disk is the one this process loaded
and the code running was compiled from it, and only after the verifier's own
selftest passes. Its listing and clean suite run from a private copy of the
tool tree, hashed before and after the runs, and its result says it certified.
Every other call is a development run. Without ``expected`` the clean listing
stands in for the population, which checks that the listing, the clean run and
the mutants agree with each other and nothing more; a bare mapping has no
recorded origin. Both still judge every run strictly, and both return a result
that says it certifies nothing.
"""

import collections
import concurrent.futures
import contextlib
import dataclasses
import hashlib
import importlib.util
import inspect
import io
import json
import os
import platform
import py_compile
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import types
from collections.abc import Mapping
from pathlib import Path

TOOL = Path(__file__).resolve().parent / "agent-resume"
COMP = TOOL.parent / f"_{TOOL.name}"
MAX_PARALLEL_SUITES = 4
ENDED_CHECKS = {"every-unit-reported", "every-live-unit-reported"}
# What one suite may take before it is treated as having answered nothing. A
# clean run is seconds; the bound is here for the mutation that loops forever
# or blocks on a lock, because results are read only once every suite has
# finished and one hung suite would otherwise cost the whole run's report.
SUITE_TIMEOUT_S = 120.0
# How long a stopped suite gets to die before it is killed outright.
SUITE_GRACE_S = 5.0
# The last line of an unfiltered report and of a --failed report.
SUMMARY = re.compile(r"(\d+)/(\d+) checks passed")
FAILED_SUMMARY = re.compile(r"checks: (\d+) ok, (\d+) failed")
# The stand-in suite probe's own tests run against, beside the corpus.
SYNTHETIC_RUNNER = TOOL.parent / "test" / "fixtures" / "synthetic_runner.py"
VERIFIER = Path(__file__).resolve()
# The sha256 of the verifier's bytes, read once as the module is imported, and
# whether the code this process runs is what those bytes compile to. A cached
# .pyc whose recorded size and modification time match the source runs in its
# place without being read against it, so the module's own code object is
# compared with the bytes compiled as the import system compiles them. A
# certifying run names the verifier in its record and a reader checks the file
# on disk, so certification is refused when that file is no longer these bytes
# or when the code that ran was not compiled from them.
_VERIFIER_BYTES = VERIFIER.read_bytes()
VERIFIER_LOADED = hashlib.sha256(_VERIFIER_BYTES).hexdigest()
_VERIFIER_CODE = compile(_VERIFIER_BYTES, str(VERIFIER), "exec", dont_inherit=True)
VERIFIER_FROM_SOURCE = sys._getframe().f_code == _VERIFIER_CODE
# The code of every function the module defines at its top level, as its bytes
# compile it. A function replaced in the module after the import runs code that
# was never hashed, which certification_gaps finds by comparing against these.
# A def compiles to optimized code under its own name; a class body, a lambda
# and a generator expression are not defs.
VERIFIER_FUNCTIONS = {code.co_name: code for code in _VERIFIER_CODE.co_consts
                      if isinstance(code, types.CodeType) and code.co_name.isidentifier()
                      and code.co_flags & inspect.CO_OPTIMIZED}
del _VERIFIER_BYTES, _VERIFIER_CODE


def replaced_functions():
    """Names in this module whose function is not the one the verifier's bytes define.

    A name defined by a def at the top level that now holds another function,
    or no function at all, is named; so is a top-level name holding a function
    that the bytes define under another name.
    """
    module = sys.modules[__name__]
    replaced = []
    for name, code in VERIFIER_FUNCTIONS.items():
        held = getattr(module, name, None)
        if not (isinstance(held, types.FunctionType) and held.__code__ == code):
            replaced.append(name)
    return sorted(replaced)


class ProbeFailure(AssertionError):
    """One or more requested mutation checks were not proved."""

    def __init__(self, failures, report="", outcomes=()):
        self.failures = tuple(failures)
        self.report = report
        self.outcomes = tuple(outcomes)
        super().__init__("mutation probe failed: " + "; ".join(self.failures))


@dataclasses.dataclass(frozen=True)
class Run:
    """One suite process as the verifier saw it.

    returncode is None when the process ran out of time and its group was
    stopped, negative when a signal ended it, and the exit status otherwise.
    stdout and stderr are kept whole whatever happened, so a refusal can show
    what the process said.
    """

    argv: tuple
    returncode: object
    stdout: str
    stderr: str
    timeout: float = SUITE_TIMEOUT_S
    # Processes the suite recorded and was still running when it exited by
    # itself, which the verifier then ended; a certifying run is refused for any.
    left_running: tuple = ()


@dataclasses.dataclass(frozen=True)
class Outcome:
    """One run judged.

    rows are what the run printed, in order and including repeats: (id,
    description) pairs for a listing, (id, description, ok, detail) for a
    report. failed holds the ids of the FAIL rows. reasons holds every
    refusal, as a sentence; an empty tuple is acceptance. stray is the text
    printed before the first row, kept for the reader and refused by nothing
    here: whether a suite talks between its verdicts is the suite's own check.
    """

    label: str
    run: Run
    rows: tuple
    failed: frozenset
    reasons: tuple
    summary: object = None
    stray: tuple = ()


@dataclasses.dataclass(frozen=True)
class Expectation:
    """A retained expected mapping for one selection, and where it came from.

    selection is read-only or live-jobs-N. mapping is {id: description}. path
    and sha256 identify the file it was read from, and source is what that
    file says it was frozen for, so a report can say whether the tested tool
    is that source or a candidate that differs from it. adjudications and
    adjudications_sha256 identify the approved record of the decisions the
    frozen rows carry, when one was named.
    """

    selection: str
    mapping: dict
    path: object = None
    sha256: str = ""
    source: dict = dataclasses.field(default_factory=dict)
    adjudications: object = None
    adjudications_sha256: str = ""


class Results(tuple):
    """What expect returns: one result dictionary per case, in case order.

    certified says whether the call asked for certification, had what it
    needs and ran the verifier's own selftest rather than a stand-in, and
    report is the record expect printed, so a caller that keeps the result
    keeps the provenance with it.
    """

    def __new__(cls, results, certified, report):
        made = super().__new__(cls, results)
        made.certified = certified
        made.report = report
        return made


ReportText = collections.namedtuple("ReportText", "rows summary problems complete stray")


def selection_name(live, jobs):
    """The name a tier and jobs value go by in the expectation and the report."""
    return f"live-jobs-{jobs}" if live else "read-only"


def suite_argv(tool, *, live=False, jobs=1, listing=False, failed=False):
    """The command for one suite run, with the tier and jobs value spelled out.

    Every command the verifier runs is built here, so the listing, the clean
    run and each mutant are asked the same question. --jobs is passed for one
    job and for the read-only tier too, which ignores it: the command then
    says which configuration it is evidence for.
    """
    argv = [sys.executable, str(tool), "--selftest"]
    if live:
        argv.append("--live")
    argv += ["--jobs", str(jobs)]
    if listing:
        argv.append("--list")
    if failed:
        argv.append("--failed")
    return tuple(argv)


def end_group(child, grace=None):
    """Stop a suite and its process group, politely and then not.

    The signal goes to the group rather than the one process because a suite
    spawns children of its own. Signalling only the parent leaves those running
    and holding the pipe open, so the read that was supposed to end never does.
    """
    grace = SUITE_GRACE_S if grace is None else grace
    # A process that leaves the group, by setsid or its own setpgid, is not
    # reached by this and is not claimed to be.
    for number in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(child.pid, number)
        except (ProcessLookupError, PermissionError):
            return
        with contextlib.suppress(subprocess.TimeoutExpired):
            child.wait(timeout=grace)
            return


def owned_groups_variable(tool):
    """The environment variable naming the file a suite writes its own-session groups to.

    The tool names it from the resolved name of its own file, as agent-resume's
    OWNED_GROUPS does, so the name is taken from the resolved path here too: a
    private copy or a mutant keeps the tool's name, and a tool reached through
    a symlink names the variable after the file it is.
    """
    return f"{Path(tool).resolve().name.upper().replace('-', '_')}_SELFTEST_OWNED_GROUPS"


def _proc_identity(pid):
    """(parent pid, process group, start time) from /proc/<pid>/stat, or None for none.

    The start time is the kernel's, field 22, as the tool writes it beside a
    pid, so a pid the kernel has handed to another process does not match. A
    zombie has ended and is not a process to stop.
    """
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        rest = stat[stat.rindex(")") + 2:].split()
        state, parent, group, started = rest[0], int(rest[1]), int(rest[2]), rest[19]
    except (OSError, ValueError, IndexError):
        return None
    return None if state in ("Z", "X") else (parent, group, started)


def _started(pid):
    """The kernel's start time of pid as a string, or None for no live process."""
    identity = _proc_identity(pid)
    return identity[2] if identity else None


def _read_text(path):
    """The stripped text of path, or "" when it cannot be read."""
    try:
        return Path(path).read_text().strip()
    except OSError:
        return ""


def never_signalled(pid):
    """Why the verifier must never signal pid, or None.

    pid 1, the verifier itself, every process it descends from, and any systemd
    process: the user's service manager adopts every orphan, so a recorded pid
    that is really its own would otherwise be signalled, and that manager ends
    the whole desktop session on SIGTERM. These hold whatever a record says
    and whenever the process started.
    """
    if pid == 1:
        return "pid 1"
    if pid == os.getpid():
        return "the verifier itself"
    ancestor, walked = os.getppid(), 0
    while ancestor > 1 and walked < 4096:
        if pid == ancestor:
            return "an ancestor of the verifier"
        identity = _proc_identity(ancestor)
        ancestor, walked = (identity[0] if identity else 0), walked + 1
    if _read_text(f"/proc/{pid}/comm") == "systemd":
        return "a systemd process"
    return None


def _family(leader):
    """Every live process in leader's process group or under it, with its start time.

    Read while the leader still runs: a terminal child sits in a session of its
    own, so a group signal misses it, and once the leader ends its children
    are handed to another parent and cannot be told from anyone else's.
    """
    table = {}
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit() and (identity := _proc_identity(int(entry.name))):
            table[int(entry.name)] = identity
    found = {pid: identity[2] for pid, identity in table.items() if identity[1] == leader}
    children = {}
    for pid, identity in table.items():
        children.setdefault(identity[0], []).append(pid)
    frontier = [leader, *found]
    while frontier:
        for pid in children.get(frontier.pop(), ()):
            if pid not in found:
                found[pid] = table[pid][2]
                frontier.append(pid)
    return found


def end_recorded_groups(record, suite_started, grace=3.0, seen=None):
    """End what a suite wrote down in record; returns (ended, left running, refused).

    A suite writes "<pid> <start time>" for each process group it starts in a
    session of its own, which ending the suite's group does not reach, and
    "record <path>" for the file a worker of its writes its own groups to.
    Anything in the suite's environment can write to record, a mutant or a
    fixture included, so no line is trusted beyond what this checks: a leader
    is signalled only while its start time is the one written down, and every
    process signalled, the leader and each process in its group or under it,
    must have started after the suite, in a later clock tick than
    suite_started, and must not be one never_signalled names. Each process is signalled by itself, never
    through its group, so nothing older is reached that way. A file is read at
    most once, and its groups are ended before the files it names are read,
    because a worker's own group carries what that worker started while it
    still runs. Refused lines and processes are returned as sentences.
    """
    seen = set() if seen is None else seen
    record = Path(record)
    if record in seen:
        return [], [], []
    seen.add(record)
    try:
        lines = record.read_text().splitlines()
    except OSError:
        return [], [], []
    ended, left, refused, nested = [], [], [], []
    bound = int(suite_started)

    def why_not(pid, started):
        """Why pid is not to be signalled, or None."""
        guard = never_signalled(pid)
        if guard:
            return guard
        # Start times count clock ticks, so a process that started in the
        # suite's own tick cannot be shown to be younger than it.
        if not started.isdigit() or int(started) <= bound:
            return "it did not start after the suite"
        return None

    for line in lines:
        first, _, rest = line.partition(" ")
        if first == "record" and rest:
            nested.append(rest)
            continue
        if not (first.isdigit() and rest):
            refused.append(f"{record}: {line!r} is not a recorded group")
            continue
        leader = int(first)
        if _started(leader) != rest:
            continue
        reason = why_not(leader, rest)
        if reason:
            refused.append(f"{record}: pid {leader}, {reason}")
            continue
        family = {leader: rest, **_family(leader)}
        for member, started in list(family.items()):
            reason = why_not(member, started)
            if reason:
                refused.append(f"{record}: pid {member} under pid {leader}, {reason}")
                del family[member]
        for number in (signal.SIGTERM, signal.SIGKILL):
            for member, started in family.items():
                if _started(member) == started:
                    with contextlib.suppress(OSError):
                        os.kill(member, number)
            deadline = time.monotonic() + grace
            while time.monotonic() < deadline and any(
                    _started(member) == started for member, started in family.items()):
                time.sleep(0.05)
            running = [member for member, started in family.items()
                       if _started(member) == started]
            if not running:
                break
        ended += [member for member in family if member not in running]
        left += running
    for path in nested:
        more = end_recorded_groups(path, suite_started, grace, seen)
        ended, left, refused = ended + more[0], left + more[1], refused + more[2]
    return sorted(ended), sorted(left), refused


def run_suite(argv, timeout=None):
    """Run one bounded suite in its own group and return its Run.

    The bound is read here rather than bound as a default, so the one constant
    above is what decides it and a caller can lower it without editing this.
    A run that reaches the bound is stopped with its group and comes back with
    returncode None and whatever it had printed.

    The suite is told, through the variable owned_groups_variable names, a
    private file to write down the process groups it starts in sessions of
    their own. After every run, whatever is written there and still running is
    ended under end_recorded_groups' rules, bounded by the suite's own start
    time, read as it starts. A suite that exited by itself and left any of it
    running comes back with those processes in left_running; whatever was
    ended, left running or refused is named at the end of the run's stderr.
    """
    timeout = SUITE_TIMEOUT_S if timeout is None else timeout
    with tempfile.TemporaryDirectory(prefix="probe-groups-") as owned:
        record = Path(owned) / "owned-groups"
        env = {**os.environ, owned_groups_variable(argv[1]): str(record)}
        with subprocess.Popen(list(argv), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, start_new_session=True, env=env) as child:
            # The bound for everything the record may name. A suite whose
            # start cannot be read is bounded by now, which is later still.
            suite_started = _started(child.pid) or str(1 << 62)
            try:
                out, err = child.communicate(timeout=timeout)
                returncode = child.returncode
                said = end_recorded_groups(record, suite_started)
            except subprocess.TimeoutExpired:
                end_group(child)
                # Before the rest is read: a process the suite recorded can
                # hold its pipes open, and the read would wait for it.
                said = end_recorded_groups(record, suite_started)
                try:
                    out, err = child.communicate(timeout=SUITE_GRACE_S)
                except subprocess.TimeoutExpired:
                    out, err = "", ""
                returncode = None
        ended, left, refused = said
        notes = [f"probe: refused to signal {sentence}" for sentence in refused]
        if ended:
            notes.append("probe: ended processes the suite recorded and left running: "
                         + ", ".join(map(str, ended)))
        if left:
            notes.append("probe: still running after the suite and the groups it recorded "
                         f"were ended: {', '.join(map(str, left))}")
        if notes:
            err += "\n" + "\n".join(notes) + "\n"
        left_running = tuple(ended + left) if returncode is not None and returncode >= 0 else ()
        return Run(tuple(argv), returncode, out, err, timeout, left_running)


def read_listing(stdout):
    """The (id, description) pairs of one --list output, in order, with repeats.

    Returns (pairs, problems). A non-empty line without the two-space
    separator or without a description is a problem, named with its text.
    """
    pairs, problems = [], []
    for line in stdout.splitlines():
        said = line.strip()
        if not said:
            continue
        cid, separator, description = said.partition("  ")
        if not separator or not description.strip():
            problems.append(f"malformed listing line: {said!r}")
            continue
        pairs.append((cid.strip(), description.strip()))
    return tuple(pairs), tuple(problems)


def read_report(stdout):
    """Rows, summary and problems of one unfiltered report.

    rows are (id, description, ok, detail) in printed order, including repeats.
    summary is (passed, total) when the last line is a well-formed summary.
    complete says whether a summary line was there at all, well-formed or not:
    a run without one stopped before its end. problems are sentences about the
    text itself: a missing summary, a malformed one, one that disagrees with
    the rows, a verdict line without an id and description. stray is the text
    before the first row. A detail that spans lines is read whole, because its
    continuation lines belong to the row above them, and nothing in the text
    raises here: whatever a run printed comes back as rows and problems.
    """
    lines = stdout.splitlines()
    tail = [at for at, line in enumerate(lines) if line.strip()]
    summary, complete, problems = None, False, []
    body = lines
    if tail and "checks passed" in lines[tail[-1]]:
        complete = True
        last = lines[tail[-1]].strip()
        match = SUMMARY.fullmatch(last)
        if match:
            summary = (int(match.group(1)), int(match.group(2)))
        else:
            problems.append(f"malformed summary: {last!r}")
        body = lines[:tail[-1]]
    else:
        problems.append("no summary line; the run is incomplete")
    rows, stray = [], []
    for line in body:
        said = line.strip()
        if not said:
            continue
        if said[:6] in ("PASS  ", "FAIL  "):
            cid, separator, rest = said[6:].partition("  ")
            # The description is padded to its column before the detail opens,
            # so the padding is not part of it.
            description, _, detail = rest.strip().partition("  (")
            description = description.rstrip()
            if not separator or not cid.strip() or not description:
                problems.append(f"malformed row: {said!r}")
                continue
            rows.append([cid.strip(), description, said.startswith("PASS"), detail])
        elif rows:
            rows[-1][3] = f"{rows[-1][3]}\n{said}" if rows[-1][3] else said
        else:
            stray.append(said)
    for row in rows:
        if row[3].endswith(")"):
            row[3] = row[3][:-1]
    if summary is not None:
        passed = sum(1 for row in rows if row[2])
        if summary != (passed, len(rows)):
            problems.append(f"summary counts {summary[0]}/{summary[1]} but "
                            f"{len(rows)} row(s) were read, {passed} passed")
    return ReportText(tuple(tuple(row) for row in rows), summary, tuple(problems),
                      complete, tuple(stray))


def read_failed_report(stdout, expected):
    """Rows and summary of one --failed report.

    A failure line carries no verdict word, so a row is recognised by an
    expected id and description at its start; a detail that spans lines cannot
    be read as a row that way. Returns (pairs, summary, problems), where
    summary is (ok, failed) or None.
    """
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    summary, problems = None, []
    if lines and lines[-1].startswith("checks: "):
        match = FAILED_SUMMARY.fullmatch(lines[-1])
        if match:
            summary = (int(match.group(1)), int(match.group(2)))
        else:
            problems.append(f"malformed summary: {lines[-1]!r}")
        lines = lines[:-1]
    else:
        problems.append("no summary line; the run is incomplete")
    pairs = []
    for line in lines:
        for cid, description in expected.items():
            head = f"{cid}  {description}"
            if line == head or line.startswith(f"{head}  ("):
                pairs.append((cid, description))
                break
    return tuple(pairs), summary, tuple(problems)


def mapping_problems(expected, pairs, verb="reported"):
    """How (id, description) pairs differ from the expected mapping.

    Four sentences at most, one per kind: the expected ids that are absent, the
    ids that were not expected, the ids that appear more than once, and the ids
    whose description is not the expected one. Each names every id of its kind.
    """
    counts = collections.Counter(cid for cid, _description in pairs)
    problems = []
    missing = sorted(set(expected) - set(counts))
    if missing:
        problems.append("missing expected check(s): " + ", ".join(missing))
    unexpected = sorted(set(counts) - set(expected))
    if unexpected:
        problems.append("unexpected check(s): " + ", ".join(unexpected))
    repeated = sorted(cid for cid, count in counts.items() if count > 1)
    if repeated:
        problems.append("duplicate check(s): " + ", ".join(
            f"{cid} ({counts[cid]} times)" for cid in repeated))
    wrong = {}
    for cid, description in pairs:
        if cid in expected and description != expected[cid]:
            wrong.setdefault(cid, []).append(description)
    for cid in sorted(wrong):
        said = ", ".join(repr(description) for description in dict.fromkeys(wrong[cid]))
        problems.append(f"description mismatch for {cid!r}: expected "
                        f"{expected[cid]!r}, {verb} {said}")
    return problems


def signal_name(number):
    """SIGTERM for 15, and the number itself for one without a name."""
    try:
        return signal.Signals(number).name
    except ValueError:
        return f"signal {number}"


def unfinished(run):
    """Why a run did not finish: ran out of time, or a signal ended it."""
    if run.returncode is None:
        return [f"did not finish within {run.timeout:g}s; its process group was stopped"]
    if run.returncode < 0:
        return [f"was ended by {signal_name(-run.returncode)} (returncode {run.returncode})"]
    return []


def status_problems(run, failed_rows):
    """How the exit status of a finished run disagrees with its rows.

    A suite exits 1 when a check failed and 0 when none did. Every other
    pairing is named for what it is: passing text with a nonzero exit, failing
    rows with exit 0, or failing rows with a status that is not the failure
    exit. Each is a different defect and gets a different sentence.
    """
    code = run.returncode
    if failed_rows and code == 0:
        return [f"{failed_rows} failed row(s) but the suite exited 0; "
                "a failing suite exits 1"]
    if failed_rows and code != 1:
        return [f"exited {code} with {failed_rows} failed row(s); a failing suite exits 1"]
    if not failed_rows and code != 0:
        return [f"every row passed but the suite exited {code}"]
    return []


def judge_listing(run, expected):
    """Judge one --list run against the expected mapping.

    With no expectation the listing is only checked against itself: a repeated
    id, or one id listed under two descriptions, is still refused.
    """
    pairs, problems = read_listing(run.stdout)
    reasons = unfinished(run)
    if not reasons:
        if run.returncode != 0:
            reasons.append(f"the listing exited {run.returncode}")
        reasons.extend(problems)
        reasons.extend(mapping_problems(
            dict(pairs) if expected is None else expected, pairs, "listed"))
    return Outcome("listing", run, pairs, frozenset(), tuple(reasons))


def judge_report(run, expected, label, check=None, co_failures=None):
    """Judge one unfiltered run against the expected mapping and failure set.

    With check None the run is the clean suite and nothing may fail. Otherwise
    the FAIL rows must be exactly the named check and the co-failures, each of
    which the caller justified with a reason; an absent one is named with that
    reason, an extra one is named as unjustified.

    A run that did not finish, or printed no summary, is judged for that alone,
    with the expected ids it never reported, so an incomplete run is never
    read as evidence about which checks failed. A finished run is judged in
    full: status against rows, the text's own problems, the mapping, a unit
    that ended the run early, and the failure set.
    """
    text = read_report(run.stdout)
    pairs = [(cid, description) for cid, description, _ok, _detail in text.rows]
    failed = frozenset(cid for cid, _description, ok, _detail in text.rows if not ok)
    reasons = unfinished(run)
    if reasons or not text.complete:
        if not reasons:
            reasons.extend(text.problems)
        reasons.extend(problem for problem in mapping_problems(expected, pairs)
                       if problem.startswith("missing expected"))
        return Outcome(label, run, text.rows, failed, tuple(reasons), text.summary, text.stray)
    reasons.extend(status_problems(run, len(failed)))
    reasons.extend(text.problems)
    reasons.extend(mapping_problems(expected, pairs))
    for cid, _description, ok, detail in text.rows:
        if cid in ENDED_CHECKS and not ok:
            reasons.append(f"ended before every unit reported: {detail}")
    co_failures = dict(co_failures or {})
    if check is None:
        if failed:
            reasons.append("already failing: " + ", ".join(sorted(failed)))
    else:
        if check not in failed:
            reasons.append(f"{check!r} did not fail")
        for cid, why in co_failures.items():
            if cid not in failed:
                reasons.append(f"justified co-failure {cid!r} did not fail (its reason: {why})")
        for cid in sorted(failed - {check, *co_failures}):
            reasons.append(f"also failed {cid!r}, which the case does not justify")
    return Outcome(label, run, text.rows, failed, tuple(reasons), text.summary, text.stray)


def judge_filtering(full, run, expected):
    """Judge a --failed run as the filtered form of its unfiltered run.

    The failure list must name exactly the ids the full report failed, once
    each and under the same descriptions, count them the way the full report
    did, and exit as the full run exited. It proves nothing about the
    population: a check the filtered report does not name may have passed or
    may never have run, and only the unfiltered comparison can tell.
    """
    reasons = unfinished(run)
    pairs, summary, problems = read_failed_report(run.stdout, expected)
    if not reasons:
        reasons.extend(problems)
        listed = collections.Counter(cid for cid, _description in pairs)
        failed = {cid for cid, _description, ok, _detail in full.rows if not ok}
        extra = sorted(set(listed) - failed)
        if extra:
            reasons.append("lists " + ", ".join(extra) + ", which the full report passed")
        gone = sorted(failed - set(listed))
        if gone:
            reasons.append("omits " + ", ".join(gone) + ", which the full report failed")
        repeated = sorted(cid for cid, count in listed.items() if count > 1)
        if repeated:
            reasons.append("lists " + ", ".join(
                f"{cid} {listed[cid]} times" for cid in repeated))
        passed = sum(1 for _cid, _description, ok, _detail in full.rows if ok)
        if summary is not None and summary != (passed, len(full.rows) - passed):
            reasons.append(f"summary counts {summary[0]} ok, {summary[1]} failed but the "
                           f"full report had {passed} passed, {len(full.rows) - passed} failed")
        if summary is not None and summary[1] != len(pairs):
            reasons.append(f"summary says {summary[1]} failed but {len(pairs)} "
                           "known row(s) were readable")
        if run.returncode != full.run.returncode:
            reasons.append(f"exited {run.returncode} where the unfiltered run "
                           f"exited {full.run.returncode}")
    return Outcome(f"{full.label} --failed", run, pairs, frozenset(cid for cid, _d in pairs),
                   tuple(reasons), summary)


def watched_files(tool, comp):
    """Every file whose bytes decide what a run proved.

    The tool and its companion are what the suites run; the test tree beside
    the tool is what their fixtures copy; this verifier chooses what each
    mutant runs against and how a verdict is read.
    """
    corpus = tool.parent / "test"
    return (tool, comp, VERIFIER,
            *sorted(item for item in corpus.rglob("*") if item.is_file()))


def fingerprints(paths):
    """{path: sha256} for files a run must not change underneath itself."""
    prints = {}
    for item in paths:
        try:
            prints[item] = hashlib.sha256(item.read_bytes()).hexdigest()
        except OSError as exc:
            prints[item] = f"unreadable ({exc})"
    return prints


def tree_digest(prints, root):
    """(file count, sha256) for the fingerprinted files under root.

    Each file adds its path relative to root and its own sha256, in path
    order, so editing, adding, removing or renaming a file changes the digest.
    """
    entries = sorted((str(item.relative_to(root)), digest)
                     for item, digest in prints.items() if root in item.parents)
    hasher = hashlib.sha256()
    for name, digest in entries:
        hasher.update(f"{name}\0{digest}\n".encode())
    return len(entries), hasher.hexdigest()


def _private_copy(tool, comp, room):
    """Copy the tool, its companion and its test tree into room; return the copies' paths.

    The paths are the copied tool, the copied companion and every file of the
    copied test tree, which certification hashes before the runs and again
    after them.
    """
    room.mkdir()
    copied_tool, copied_comp = room / tool.name, room / comp.name
    shutil.copy2(tool, copied_tool)
    shutil.copy2(comp, copied_comp)
    shutil.copytree(tool.parent / "test", room / "test")
    return copied_tool, copied_comp, _copy_files(room, copied_tool, copied_comp)


def _copy_files(room, copied_tool, copied_comp):
    """Every file a private copy holds that a run could read: tool, companion, test tree."""
    return [copied_tool, copied_comp,
            *sorted(item for item in (room / "test").rglob("*") if item.is_file())]


def raw_output(outcome):
    """The refused run as the reader needs it: command, status, both streams."""
    run = outcome.run
    status = "timed out" if run.returncode is None else f"exit {run.returncode}"
    lines = [f"--- {outcome.label}: {shlex.join(run.argv)} ({status})"]
    for name, text in (("stdout", run.stdout), ("stderr", run.stderr)):
        lines.append(f"{name}:" if text else f"{name}: (empty)")
        lines.extend(f"  {line}" for line in text.splitlines())
    return lines


def said(outcome):
    """One line for what a run came to."""
    if outcome.run.returncode is None:
        return f"timed out after {outcome.run.timeout:g}s"
    if outcome.summary is not None:
        return f"{outcome.summary[0]}/{outcome.summary[1]} checks passed"
    return f"exit {outcome.run.returncode}, no summary"


def batch(entries, run):
    """Run every entry through run, at most MAX_PARALLEL_SUITES at once, in order."""
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=min(MAX_PARALLEL_SUITES, len(entries))
    ) as pool:
        futures = [pool.submit(run, entry) for entry in entries]
        return [future.result() for future in futures]


def mutation_target(old, paths=None):
    """Return the sole file containing one match, or a precise refusal."""
    paths = tuple(paths or (TOOL, COMP))
    counts = {path: path.read_text().count(old) if path.exists() else 0
              for path in paths}
    total = sum(counts.values())
    said = ", ".join(f"{path.name}: {count}" for path, count in counts.items())
    if total == 0:
        return None, f"the text to replace was not found ({said})"
    if total != 1:
        return None, (
            f"the text to replace appears {total} times ({said}); "
            "a probe replaces exactly one thing, so narrow it"
        )
    return next(path for path, count in counts.items() if count == 1), None


def _json_record(path, what):
    """(bytes, parsed object) of a JSON file, or a refusal naming it as what."""
    path = Path(path)
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ProbeFailure([f"{what} {path} cannot be read: {exc.strerror or exc}"])
    try:
        return data, json.loads(data)
    except ValueError as exc:
        raise ProbeFailure([f"{what} {path} is not JSON: {exc}"])


def retained_expectation(path, *, live=False, jobs=1, adjudications=None):
    """Read the expected mapping for one selection from a frozen expected-mapping-v1 file.

    The file is the reviewer's record: a schema name, the modes it covers, the
    source it was frozen for, and rows that each carry an id, a description,
    emitted_in with the selections a clean run reports the row in and
    not_emitted_in with the ones that withhold it. The mapping for a selection
    is every row emitted in it. A file that does not name the source it was
    frozen for by sha256, a selection it does not cover, a row that is not an
    object or is both emitted and withheld, and an id named twice are all
    refused, since an expectation that is unsure of its own rows or origin
    decides nothing.

    adjudications names the approved record of the decisions the rows carry.
    It must name this mapping by sha256 under frozen_mapping, so a record
    about another mapping cannot stand behind this one. Nothing here reads the
    tested tool.
    """
    path = Path(path)
    data, record = _json_record(path, "expected mapping")
    if adjudications is None:
        return _expectation_from(path, data, record, live, jobs)
    adjudications = Path(adjudications)
    return _expectation_from(path, data, record, live, jobs, adjudications,
                             *_json_record(adjudications, "adjudications"))


def _expectation_from(path, data, record, live, jobs, adjudications=None, adjudicated=b"",
                      decided=None):
    """The Expectation a mapping file's bytes give for one selection, or a refusal.

    data and record are the mapping file's bytes and what they parse to;
    adjudicated and decided the same for the adjudications record, when one
    is named. Nothing is read here, so what is judged is exactly what the
    caller read and hashed: retained_expectation reads each file once, and
    certification reads each once again and judges those bytes.
    """
    if not isinstance(record, dict) or record.get("schema") != "expected-mapping-v1":
        raise ProbeFailure([f"{path} does not carry schema expected-mapping-v1"])
    digest = hashlib.sha256(data).hexdigest()
    source = record.get("frozen_for_source")
    if not (isinstance(source, dict) and isinstance(source.get("sha256"), str)
            and source["sha256"]):
        raise ProbeFailure([f"{path} does not name the source it was frozen for "
                            "(frozen_for_source with its sha256)"])
    selection = selection_name(live, jobs)
    modes = record.get("modes")
    if not isinstance(modes, dict) or selection not in modes:
        raise ProbeFailure([f"{path} has no selection {selection}; it has: "
                            + ", ".join(sorted(modes if isinstance(modes, dict) else ()))])
    rows = record.get("rows")
    if not isinstance(rows, list):
        raise ProbeFailure([f"{path}: rows is not a list"])
    pairs, failures = [], []
    for at, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            failures.append(f"row {at} is not an object")
            continue
        placed = {}
        for key in ("emitted_in", "not_emitted_in"):
            where = row.get(key, {})
            if not isinstance(where, (dict, list)):
                failures.append(f"row {at} ({row.get('id')!r}): {key} is neither an "
                                "object nor a list")
            placed[key] = selection in where if isinstance(where, (dict, list)) else False
        if placed["emitted_in"] and placed["not_emitted_in"]:
            failures.append(f"row {at} ({row.get('id')!r}) is both emitted and "
                            f"withheld in {selection}")
        elif placed["emitted_in"]:
            cid, description = row.get("id"), row.get("description")
            if not (isinstance(cid, str) and cid):
                failures.append(f"row {at}: its id is missing or not a non-empty string")
            elif not (isinstance(description, str) and description):
                failures.append(f"row {at} ({cid!r}): its description is missing or not a "
                                "non-empty string")
            else:
                pairs.append((cid, description))
    if failures:
        raise ProbeFailure(failures)
    # Read-only, so the mapping that judges a run is the one the file's bytes
    # gave, and no later edit in place can change what a certifying run cites.
    mapping = types.MappingProxyType(_normalize_expected(pairs))
    adjudications_digest = ""
    if adjudications is not None:
        named = (decided.get("frozen_mapping") or {}) if isinstance(decided, dict) else {}
        if not isinstance(named, dict) or named.get("sha256") != digest:
            raise ProbeFailure([f"adjudications {adjudications} do not name this mapping "
                                f"by its sha256 {digest} under frozen_mapping"])
        adjudications_digest = hashlib.sha256(adjudicated).hexdigest()
    return Expectation(selection, mapping, path, digest, dict(source),
                       adjudications, adjudications_digest)


def _normalize_expected(expected):
    """Freeze a retained expectation into {id: description}, or refuse it.

    A mapping is taken as is; an iterable of (id, description) pairs is read in
    order and refused when an id comes twice, because an expectation that
    names one check under two descriptions decides nothing.
    """
    items = expected.items() if isinstance(expected, Mapping) else expected
    frozen, failures = {}, []
    for at, item in enumerate(items, 1):
        if not isinstance(item, (tuple, list)) or len(item) != 2:
            failures.append(f"expected entry {at} is not an (id, description) pair")
            continue
        cid, description = item
        if not (isinstance(cid, str) and cid):
            failures.append(f"expected entry {at}: its id is empty or not a string")
        elif not (isinstance(description, str) and description):
            failures.append(f"expected entry {at} ({cid!r}): its description is empty or "
                            "not a string")
        elif cid in frozen:
            failures.append(f"the expectation names {cid!r} twice")
        else:
            frozen[cid] = description
    if not frozen and not failures:
        failures.append("the expectation names no check")
    if failures:
        raise ProbeFailure(failures)
    return frozen


def _normalize_cases(cases):
    """Validate and freeze cases into (check, old, new, co_failures) tuples.

    A case is (check, old, new), or that with a fourth element mapping each
    check the mutation also fails to the reason it does. The reason is what
    makes a co-failure reviewable, so an empty one is refused like a missing
    one, and the named check cannot be its own co-failure.
    """
    normalized = []
    failures = []
    for at, case in enumerate(cases, 1):
        if not isinstance(case, (tuple, list)) or len(case) not in (3, 4):
            failures.append(f"case {at} is not a (check, old, new) triple, with or "
                            "without a co-failures mapping")
            continue
        check, old, new = case[:3]
        co_failures = case[3] if len(case) == 4 else {}
        if not all(isinstance(value, str) and value for value in (check, old, new)):
            failures.append(f"case {at} contains an empty or non-string value")
            continue
        if old == new:
            failures.append(f"case {at} replaces text with itself")
            continue
        if not isinstance(co_failures, Mapping) or not all(
                isinstance(cid, str) and cid and isinstance(why, str) and why
                for cid, why in co_failures.items()):
            failures.append(f"case {at}: co-failures must map each check id to the "
                            "reason the mutation fails it too")
            continue
        if check in co_failures:
            failures.append(f"case {at}: {check!r} is the named check, not one of its "
                            "co-failures")
            continue
        normalized.append((check, old, new, dict(co_failures)))
    if not normalized and not failures:
        failures.append("no mutation cases were supplied")
    if failures:
        raise ProbeFailure(failures)
    return tuple(normalized)


def _refusal(outcomes, lines, stream):
    """Print the report with the raw output of every refused run, then the exception."""
    refused = [outcome for outcome in outcomes if outcome.reasons]
    report = "\n".join([*lines, "", *(line for outcome in refused
                                       for line in raw_output(outcome))])
    print(report, file=stream)
    return ProbeFailure([f"{outcome.label}: {reason}" for outcome in refused
                         for reason in outcome.reasons], report=report, outcomes=refused)


def undecided(record):
    """The entries of an approved-adjudications record that carry no decision, as sentences.

    The record must hold an entries list, and an empty one says there was
    nothing to adjudicate. An entry is decided when its decision is a string
    that begins with approved or refused; with a missing, empty or pending
    one, the frozen rows carry a decision nobody made.
    """
    entries = record.get("entries") if isinstance(record, dict) else None
    if not isinstance(entries, list):
        return ["the adjudications record holds no entries list"]
    gaps = []
    for at, entry in enumerate(entries, 1):
        if not isinstance(entry, dict):
            gaps.append(f"adjudication entry {at} is not an object")
            continue
        named = f"adjudication entry {at}" + (f" ({entry['id']!r})" if entry.get("id") else "")
        decision = entry.get("decision")
        if "decision" not in entry:
            gaps.append(f"{named} has no decision")
        elif not (isinstance(decision, str) and decision.strip()):
            gaps.append(f"{named} has an empty decision: {decision!r}")
        elif not decision.startswith(("approved", "refused")):
            gaps.append(f"{named} is not decided: its decision is {decision!r}")
    return gaps


def misplaced(record, mapping):
    """The adjudication entries that point where their decision cannot, as sentences.

    record is the approved-adjudications record and mapping the frozen
    mapping file it names, both as parsed. An approved entry decides that a
    row belongs to a selection, so its id must be a row of the mapping and its
    mode a selection in which that row is emitted, a key of the row's
    emitted_in. A refused entry decides that a row does not, so its id must be
    no row of the mapping, or a row its mode, a selection of the mapping,
    withholds. An entry that is not an object or carries no decision is
    undecided's to name.
    """
    rows = {row.get("id"): row for row in (mapping.get("rows") or [])
            if isinstance(row, dict)} if isinstance(mapping, dict) else {}
    modes = (mapping.get("modes") or {}) if isinstance(mapping, dict) else {}
    gaps = []
    entries = (record.get("entries") or []) if isinstance(record, dict) else []
    for at, entry in enumerate(entries, 1):
        decision = entry.get("decision") if isinstance(entry, dict) else None
        if not isinstance(decision, str):
            continue
        cid, mode = entry.get("id"), entry.get("mode")
        emitted = rows.get(cid, {}).get("emitted_in")
        selection = isinstance(modes, (dict, list)) and mode in modes
        in_mode = isinstance(emitted, (dict, list)) and mode in emitted
        if decision.startswith("approved"):
            if cid not in rows:
                gaps.append(f"adjudication entry {at} names {cid!r}, which is no row of the "
                            "mapping")
            elif not selection:
                gaps.append(f"adjudication entry {at} ({cid!r}) names the mode {mode!r}, which is "
                            "no selection of the mapping")
            elif not in_mode:
                gaps.append(f"adjudication entry {at} ({cid!r}) names the mode {mode!r}, in which "
                            "that row is not emitted")
        elif decision.startswith("refused") and cid in rows:
            if not selection:
                gaps.append(f"adjudication entry {at} ({cid!r}) is refused for the mode {mode!r}, "
                            "which is no selection of the mapping")
            elif in_mode:
                gaps.append(f"adjudication entry {at} ({cid!r}) is refused, but the mapping still "
                            f"emits that row in {mode!r}")
    return gaps


def certification_gaps(expected, live, jobs, tool_digest, verifier_digest):
    """Why expected cannot back a certifying run of this tool; empty when it can.

    A certifying run rests on a retained expectation read from its frozen
    file, with the file's hash, the source it was frozen for, named by sha256,
    commit and path, and the approved adjudications behind it, because those
    are what a later reader checks the record against. The fields an
    Expectation carries are only what it says of itself, so the file and the
    adjudications record are each read once more here, hashed, and judged
    from those same bytes: they must give the same mapping and the same
    source, every adjudication must be decided, and every entry must name a
    row of the mapping in a selection that emits it. The plan accepts one
    exact executable, so the tool must be the source the mapping was frozen
    for; a changed tool needs its own mapping. verifier_digest is the sha256
    of the verifier file as the run found it on disk, which must be the
    VERIFIER_LOADED this process runs.
    """
    if expected is None:
        return ["no expectation was supplied, so the listing would stand in for "
                "the population"]
    if not isinstance(expected, Expectation):
        return ["the expectation is a bare mapping with no recorded origin; read it "
                "with retained_expectation"]
    gaps = []
    if verifier_digest != VERIFIER_LOADED:
        gaps.append(f"the verifier {VERIFIER} on disk has sha256 {verifier_digest}, not the "
                    f"{VERIFIER_LOADED} this process loaded, so the record would name a "
                    "verifier that did not judge the run")
    if not VERIFIER_FROM_SOURCE:
        gaps.append(f"the code this process runs is not what {VERIFIER} compiles to: cached "
                    "bytecode ran in its place; remove the verifier's .pyc from __pycache__ "
                    "and import it again")
    if not (expected.path and expected.sha256):
        gaps.append("the expectation names no file and hash it was read from")
    gaps += [f"the expectation's frozen source names no {key}"
             for key in ("sha256", "commit", "path_in_repo")
             if not (isinstance(expected.source.get(key), str) and expected.source[key])]
    if not (expected.adjudications and expected.adjudications_sha256):
        gaps.append("the expectation names no approved adjudications; pass them to "
                    "retained_expectation")
    if gaps:
        return gaps
    if expected.source["sha256"] != tool_digest:
        gaps.append(f"the tool's sha256 {tool_digest} is not {expected.source['sha256']}, "
                    "the source the expectation was frozen for; a changed tool needs its "
                    "own frozen mapping")
    # One read of each file. The bytes come first: a file that changed since
    # it was read is named as changed, before anything read from it is
    # compared, and what is compared is what those bytes parse to.
    try:
        data, record = _json_record(expected.path, "a certified input")
        adjudicated, decided = _json_record(expected.adjudications, "a certified input")
    except ProbeFailure as exc:
        return gaps + [f"the expectation no longer reads from its files: {failure}"
                       for failure in exc.failures]
    for path, read, recorded in ((expected.path, data, expected.sha256),
                                 (expected.adjudications, adjudicated,
                                  expected.adjudications_sha256)):
        current = hashlib.sha256(read).hexdigest()
        if current != recorded:
            gaps.append(f"{path} now has sha256 {current}, not the {recorded} the "
                        "expectation was read with")
    if gaps:
        return gaps
    try:
        again = _expectation_from(expected.path, data, record, live, jobs,
                                  expected.adjudications, adjudicated, decided)
    except ProbeFailure as exc:
        return gaps + [f"the expectation no longer reads from its files: {failure}"
                       for failure in exc.failures]
    if dict(again.mapping) != dict(expected.mapping):
        gaps.append(f"the mapping in use is not the one {expected.path} gives for "
                    f"{again.selection}")
    if again.source != expected.source:
        gaps.append(f"the frozen source in use is not the one {expected.path} names")
    # Asked last, so the inputs are read and judged whatever it finds.
    replaced = replaced_functions()
    if replaced:
        gaps.append(f"functions replaced in the verifier after it was imported would run "
                    f"code its bytes do not hold: {', '.join(replaced)}")
    return gaps + undecided(decided) + misplaced(decided, record)


def verifier_selftest(run=None):
    """The verifier's own selftest, run before a certifying run, as its summary line.

    Section 5A has the verifier tested before its conclusions are used as
    acceptance, so certification runs it first and refuses when it fails. run
    is the selftest to run: selftest itself, or a stand-in a caller hands in,
    as the verifier's own tests do. Its output is taken into a buffer; its
    last line goes into the record.
    """
    said = io.StringIO()
    try:
        with contextlib.redirect_stdout(said):
            (selftest if run is None else run)()
    except ProbeFailure as exc:
        raise ProbeFailure([f"the verifier's own selftest failed: {failure}"
                            for failure in exc.failures])
    lines = said.getvalue().strip().splitlines()
    if not lines:
        raise ProbeFailure(["the verifier's own selftest printed nothing, so nothing says it ran"])
    return lines[-1]


def expect(cases, *, live=False, jobs=1, expected=None, tool=None, timeout=None,
           filtering=False, certify=False, stream=None, verifier=None):
    """Run expected mutations and raise unless every case is proved.

    ``cases`` holds ``(check_id, old_text, new_text)`` triples, each with an
    optional fourth element: a mapping from the id of every check the mutation
    also fails to the reason it does. The mutant must fail exactly that set.
    ``expected`` is the retained {check id: description} mapping for the
    selected ``live`` tier and ``jobs`` value, and every command carries both.
    ``tool`` names the executable to probe; its companion is the file beside
    it with an underscore before its name, and the test tree beside it is
    copied with them. ``timeout`` bounds each run in seconds. With
    ``filtering`` every run also happens with --failed, and that report must
    be the failures of the unfiltered one and nothing else.

    ``certify`` asks for a certifying run. It is refused before any suite runs
    unless it names its timeout, ``expected`` came from
    ``retained_expectation`` with its source and approved adjudications, reads
    again from its files as it was read, every adjudication is decided and
    points where its decision can (misplaced), the tool is the source the
    mapping was frozen for, the verifier on disk is the one this process
    loaded, the code running was compiled from it, and the verifier's own
    selftest passes. ``verifier`` is that selftest: selftest itself when it is
    None, or a stand-in the caller supplies. The verifier's own tests supply
    one, since a selftest that started itself would not end; a run given one
    judges as a certifying run does, its result says it certified nothing, and
    its record says why. A certifying run runs its listing and its clean suite
    from a private copy of the tool, its companion and its test tree, hashed
    after copying, where it must equal the originals, and again after the
    runs, where any change is refused as a change to the originals is; and it
    checks the clean run's --failed report too. Its record names the inputs
    and the copy with their hashes, the interpreter, the timeout, the
    selftest's result, the runs whose --failed output was checked and every
    co-failure's reason. Without it the run judges just as strictly and its
    result says it certified nothing.

    The successful return is a Results tuple with one result dictionary per
    case: the check, the mutated file, the ids that failed and the
    co-failures, with the judged outcome; its ``certified`` and ``report``
    say whether the run certified and what it recorded. ``ProbeFailure``
    carries ``failures``, the printed ``report`` and the refused ``outcomes``
    with their raw output.
    """
    cases = _normalize_cases(cases)
    tool = Path(TOOL if tool is None else tool)
    comp = tool.parent / f"_{tool.name}"
    for needed, what in ((tool, "tool"), (comp, "companion")):
        if not needed.is_file():
            raise ProbeFailure([f"the {what} {needed} is not a file; every suite run needs "
                                "the tool and its companion side by side"])
    stream = sys.stdout if stream is None else stream
    # The default bound is for development runs; a certifying record states
    # the bound its caller chose for the runs it certifies.
    if certify and timeout is None:
        raise ProbeFailure(["a certifying run names its timeout: pass timeout, since the "
                            "default is for development runs"])
    timeout = SUITE_TIMEOUT_S if timeout is None else timeout
    selection = selection_name(live, jobs)
    watched = fingerprints(watched_files(tool, comp))
    tool_digest = watched[tool]
    if isinstance(expected, Expectation) and expected.selection != selection:
        raise ProbeFailure([f"the expectation is for {expected.selection}; "
                            f"this run is {selection}"])
    checked_verifier = None
    if certify:
        gaps = certification_gaps(expected, live, jobs, tool_digest, watched[VERIFIER])
        if gaps:
            raise ProbeFailure([f"a certifying run needs a retained expectation: {gap}"
                                for gap in gaps])
        checked_verifier = verifier_selftest(verifier)
        if verifier is not None:
            checked_verifier = f"a stand-in the caller supplied said: {checked_verifier}"
    # A run that did not run the verifier's own selftest certifies nothing,
    # though it judges everything else a certifying run judges.
    certified = certify and verifier is None
    count, digest = tree_digest(watched, tool.parent / "test")
    lines = [f"tool: {tool} sha256 {tool_digest}",
             f"companion: {comp} sha256 {watched[comp]}",
             f"test tree: {tool.parent / 'test'} {count} file(s), digest {digest}",
             f"verifier: {VERIFIER} sha256 {watched[VERIFIER]}",
             f"interpreter: {sys.executable} (Python {platform.python_version()})",
             f"timeout: {timeout:g}s for each run",
             f"selection: {selection}"]
    if checked_verifier is not None:
        lines.append(f"verifier selftest: {checked_verifier}")
    if isinstance(expected, Expectation):
        lines.append(f"expectation: {expected.path} sha256 {expected.sha256}")
        frozen_for = expected.source.get("sha256")
        if frozen_for:
            named = ", ".join(f"{key} {expected.source[key]}"
                              for key in ("commit", "path_in_repo") if expected.source.get(key))
            lines.append(f"expectation frozen for source sha256 {frozen_for}"
                         + (f" ({named})" if named else "") + ": the tool "
                         + ("is that source" if frozen_for == tool_digest
                            else "differs from it"))
        if expected.adjudications:
            lines.append(f"adjudications: {expected.adjudications} sha256 "
                         f"{expected.adjudications_sha256}")
        expected = expected.mapping
    elif expected is not None:
        expected = _normalize_expected(expected)
        lines.append(f"expectation: a supplied mapping of {len(expected)} check(s), "
                     "with no recorded origin")
    else:
        lines.append("expectation: none supplied, so the listing is the population")
    lines.insert(0, "certifying: yes" if certified else
                 "certifying: no; the verifier's own selftest was a stand-in the caller "
                 "supplied, so this run certifies nothing" if certify else
                 "certifying: no; this run certifies nothing about the tool")

    # A certifying run checks the clean run's --failed report whatever filtering
    # says, because the plan has --failed tested against the same run.
    def run_one(entry):
        path, filtered_too = entry
        full = run_suite(suite_argv(path, live=live, jobs=jobs), timeout)
        filtered = (run_suite(suite_argv(path, live=live, jobs=jobs, failed=True), timeout)
                    if filtered_too else None)
        return full, filtered

    with tempfile.TemporaryDirectory() as scratch:
        # A certifying run's listing and clean suite run from a copy nothing
        # else touches, hashed after copying and again after the runs, so the
        # record names the bytes that ran and a run that wrote into its own
        # copy is refused as one that wrote into the original would be.
        clean_tool, copied, copy_changed = tool, {}, []
        if certify:
            room = Path(scratch) / "clean"
            clean_tool, clean_comp, files = _private_copy(tool, comp, room)
            copied = fingerprints(files)
            copied_count, copied_digest = tree_digest(copied, room / "test")
            if (copied.get(clean_tool), copied.get(clean_comp), copied_digest) != (
                    tool_digest, watched[comp], digest):
                raise ProbeFailure([f"the private copy {room} is not the tool, companion and "
                                    "test tree it was copied from"])
            lines.append(f"clean run from a private copy: {room}: tool sha256 "
                         f"{copied[clean_tool]}, companion sha256 {copied[clean_comp]}, "
                         f"test tree {copied_count} file(s), digest {copied_digest}")

        listed = judge_listing(
            run_suite(suite_argv(clean_tool, live=live, jobs=jobs, listing=True), timeout),
            expected)
        population = expected if expected is not None else dict(listed.rows)
        lines.append(f"listing: {len(listed.rows)} check(s)")
        if listed.reasons:
            raise _refusal([listed], lines, stream)

        failures = []
        for at, (check, _old, _new, co_failures) in enumerate(cases, 1):
            for cid in (check, *co_failures):
                if cid not in population:
                    failures.append(f"case {at}: {cid!r} names no expected check")
        if failures:
            examples = ", ".join(sorted(population)[:5])
            raise ProbeFailure([*failures, f"expected checks include: {examples}"])

        prepared = []
        for at, (check, old, new, co_failures) in enumerate(cases, 1):
            target, reason = mutation_target(old, (tool, comp))
            if reason:
                failures.append(f"case {at}: {reason}")
            else:
                prepared.append((check, old, new, co_failures, target))
        if failures:
            raise ProbeFailure(failures)

        copies = []
        for at, (_check, old, new, _co_failures, target) in enumerate(prepared, 1):
            room = Path(scratch) / f"case-{at}"
            room.mkdir()
            copy = room / tool.name
            shutil.copy2(tool, copy)
            shutil.copy2(comp, room / comp.name)
            (room / target.name).write_text(target.read_text().replace(old, new, 1))
            shutil.copytree(tool.parent / "test", room / "test")
            copies.append(copy)
        runs = batch([(clean_tool, filtering or certify),
                      *((copy, filtering) for copy in copies)], run_one)
        if certify:
            again = fingerprints(_copy_files(clean_tool.parent, clean_tool, clean_comp))
            copy_changed = sorted(str(item) for item in set(copied) | set(again)
                                  if copied.get(item) != again.get(item))
    labels = ["clean", *(f"case {at}" for at in range(1, len(prepared) + 1))]
    lines.append("--failed checked for: " + (", ".join(
        label for label, (_full, filtered) in zip(labels, runs) if filtered is not None)
        or "no run"))

    # The report names the bytes it judged. A file that changed while the
    # suites ran, the tool itself or a corpus file a unit wrote into, makes
    # every verdict above describe something no longer on disk. The runs are
    # discarded unjudged, and their raw output is kept for whoever has to find
    # out which of them wrote.
    after = fingerprints(watched_files(tool, comp))
    changed = sorted(str(item) for item in set(watched) | set(after)
                     if watched.get(item) != after.get(item)) + copy_changed
    if changed:
        lines.append("changed while the suites ran: " + ", ".join(changed))
        discarded = []
        for label, (full, filtered) in zip(labels, runs):
            for name, run in ((label, full), (f"{label} --failed", filtered)):
                if run is not None:
                    discarded.append(Outcome(name, run, (), frozenset(),
                                             ("discarded: the inputs changed while it ran",)))
        report = "\n".join([*lines, "", *(line for outcome in discarded
                                          for line in raw_output(outcome))])
        print(report, file=stream)
        raise ProbeFailure(["the tool, its companion, the verifier or the test tree "
                            f"changed while the suites ran ({', '.join(changed)}); no "
                            "verdict here describes what is on disk now"],
                           report=report, outcomes=discarded)

    clean = judge_report(runs[0][0], population, "clean")
    outcomes = [listed, clean]
    lines.append(f"clean: {said(clean)}")
    # A certified run is one that cleaned up after itself: a suite that exited
    # with processes it recorded still running would leave them on the machine
    # had the verifier not ended them.
    left = sorted({pid for run in runs[0] if run is not None for pid in run.left_running})
    if certify and left:
        lines.append("clean: the suite exited with processes it recorded still running, "
                     f"which the verifier ended: {', '.join(map(str, left))}")
        report = "\n".join(lines)
        print(report, file=stream)
        raise ProbeFailure(["the clean suite exited with processes it recorded still running "
                            f"({', '.join(map(str, left))}); a certified run ends what it "
                            "started"], report=report, outcomes=tuple(outcomes))
    if clean.stray:
        lines.append(f"clean: {len(clean.stray)} line(s) of output before the first row")
    if runs[0][1] is not None:
        outcomes.append(judge_filtering(clean, runs[0][1], population))
    if any(outcome.reasons for outcome in outcomes):
        # A failing baseline proves nothing about the mutants, and judging them
        # against it would only add noise to the report.
        raise _refusal(outcomes, lines, stream)

    results = []
    indent = "  " if len(prepared) > 1 else ""
    for at, (case, (full, filtered)) in enumerate(zip(prepared, runs[1:]), 1):
        check, _old, _new, co_failures, target = case
        label = f"case {at}"
        outcome = judge_report(full, population, label, check, co_failures)
        judged = [outcome]
        if filtered is not None:
            judged.append(judge_filtering(outcome, filtered, population))
        outcomes.extend(judged)
        if len(prepared) > 1:
            lines.extend(["", f"{label}: expected {check} ({target.name})"])
        lines.extend(f"{indent}co-failure {cid}: {why}" for cid, why in co_failures.items())
        lines.append(f"{indent}probed: {said(outcome)}")
        lines.append(f"{indent}failed: " + ", ".join(sorted(outcome.failed))
                     if outcome.failed else f"{indent}nothing failed")
        lines.extend(f"{indent}refused: {reason}" for judged_outcome in judged
                     for reason in judged_outcome.reasons)
        results.append({
            "check": check,
            "target": target,
            "bit": tuple(sorted(outcome.failed)),
            "co_failures": dict(co_failures),
            "outcome": outcome,
            "certified": certified,
        })

    if any(outcome.reasons for outcome in outcomes):
        raise _refusal(outcomes, lines, stream)
    report = "\n".join(lines)
    print(report, file=stream)
    return Results(results, certified, report)


# What probe's own tests say about the stand-in runner. KNOBS is the block of
# literals in SYNTHETIC_RUNNER, in file order; a test mutation rewrites the
# whole block, so any combination of knobs is one contiguous replacement.
KNOBS = ("FAILING", "SUMMARY", "EXIT", "DROP", "EXTRA", "DUPLICATE", "DESCRIBE",
         "SLEEP", "SIGNAL", "FILTER_LIES", "WRITE_BESIDE", "OWNED_DRIVER", "RECORD_LINES")
KNOB_DEFAULTS = {"FAILING": [], "SUMMARY": "exact", "EXIT": None, "DROP": [], "EXTRA": [],
                 "DUPLICATE": [], "DESCRIBE": {}, "SLEEP": 0, "SIGNAL": None,
                 "FILTER_LIES": False, "WRITE_BESIDE": None, "OWNED_DRIVER": None,
                 "RECORD_LINES": []}
SYNTHETIC_EXPECTED = {
    "target-check": "the check a case names",
    "other-check": "a check a case may justify as a co-failure",
    "third-check": "a check nothing touches",
    "every-unit-reported": "no unit ended the run before it reported",
}


def knobs(**changes):
    """The stand-in runner's knob block with these values, as its text."""
    values = {**KNOB_DEFAULTS, **changes}
    return "".join(f"{name} = {values[name]!r}\n" for name in KNOBS)


def selftest():
    """Exercise reading, judging, running and raising, on records and on processes.

    Records are Runs built in place, one per rule, so each refusal sentence is
    shown to come from exactly the input it is about. The processes are copies
    of the stand-in runner in a private room, so the whole path from command
    to refusal is exercised on real exits, a real signal and a real timeout.
    A certifying expect among its checks is handed a stand-in for this
    selftest, which says it is one of these checks, since one that started
    the selftest again would not end.
    """
    return _selftest_checks(lambda: print("running; this certifying run is one of its own "
                                          "checks"))


def _selftest_checks(verifier):
    """The checks selftest runs; see selftest. verifier is the stand-in for certifying runs."""
    checks = []

    def check(name, ok, detail=""):
        checks.append((name, bool(ok), detail))

    def refused(name, call, phrase):
        """call must raise a ProbeFailure carrying phrase; anything else fails name."""
        try:
            call()
        except ProbeFailure as exc:
            check(name, phrase in str(exc), str(exc))
        except Exception as exc:
            check(name, False, f"raised {type(exc).__name__} instead: {exc}")
        else:
            check(name, False, "did not raise")

    def only(reasons, *starts):
        """Exactly these refusals, in this order, each starting as given."""
        return (len(reasons) == len(starts)
                and all(reason.startswith(start) for reason, start in zip(reasons, starts)))

    source = Path(__file__).read_text()
    check("no-command-line-interface",
          "def " + "main(" not in source and "sys." + "argv" not in source
          and not Path(__file__).with_suffix("").exists(),
          "a probe entry point remains")

    with tempfile.TemporaryDirectory() as td:
        left, right = Path(td) / "agent-resume", Path(td) / "_agent-resume"

        def target_check(name, left_text, right_text, target, phrase, absent=""):
            left.write_text(left_text)
            right.write_text(right_text)
            got, objection = mutation_target("needle", (left, right))
            said_ = objection or ""
            ok = (got == target and phrase in said_
                  and (not absent or absent not in said_))
            check(name, ok, objection or str(got))

        target_check("zero", "plain", "plain", None, "was not found", "narrow it")
        target_check("one-main", "needle", "plain", left, "")
        target_check("one-completion", "plain", "needle", right, "")
        target_check("many-one-file", "needle needle", "plain", None,
                     "appears 2 times", "was not found")
        target_check("many-across-files", "needle", "needle", None,
                     "agent-resume: 1, _agent-resume: 1", "was not found")

    expected = {
        "alpha": "the first check",
        "beta": "the second check",
        "every-unit-reported": "no unit ended the read-only run before it reported",
    }

    def record(stdout, returncode=0, stderr=""):
        return Run(("python3", "agent-resume", "--selftest", "--jobs", "1"),
                   returncode, stdout, stderr, 1.0)

    def listing(*pairs):
        width = max(len(cid) for cid, _description in pairs)
        return "".join(f"{cid:<{width}}  {description}\n" for cid, description in pairs)

    def table(rows, summary=True, passed=None, total=None, before="", extra_lines=()):
        """Rows rendered the way agent-resume prints them, summary included."""
        idwidth = max(len(row[0]) for row in rows)
        width = max(len(row[1]) for row in rows)
        lines = [f"  {'PASS' if ok else 'FAIL'}  {cid:<{idwidth}}  {description:<{width}}"
                 + (f"  ({detail})" if detail else "")
                 for cid, description, ok, detail in rows]
        lines.extend(extra_lines)
        if summary:
            passed = sum(1 for row in rows if row[2]) if passed is None else passed
            total = len(rows) if total is None else total
            lines.extend(["", f"{passed}/{total} checks passed"])
        return before + "\n".join(lines) + "\n"

    passing = tuple((cid, description, True, "") for cid, description in expected.items())
    beta_fails = tuple((cid, description, cid != "beta", "scripted" if cid == "beta" else "")
                       for cid, description in expected.items())
    pairs = tuple(expected.items())

    # Listings against the expectation.
    outcome = judge_listing(record(listing(*pairs)), expected)
    check("listing-complete-accepted", outcome.reasons == () and outcome.rows == pairs,
          repr(outcome.reasons))
    outcome = judge_listing(record(listing(pairs[0], pairs[2])), expected)
    check("listing-missing-expected-check",
          only(outcome.reasons, "missing expected check(s): beta"), repr(outcome.reasons))
    outcome = judge_listing(record(listing(pairs[0], ("beta", "another"), pairs[2])), expected)
    check("listing-wrong-description",
          only(outcome.reasons, "description mismatch for 'beta': expected 'the second "
               "check', listed 'another'"), repr(outcome.reasons))
    outcome = judge_listing(record(listing(*pairs, ("gamma", "a third check"))), expected)
    check("listing-unexpected-check",
          only(outcome.reasons, "unexpected check(s): gamma"), repr(outcome.reasons))
    outcome = judge_listing(record(listing(*pairs, pairs[1])), expected)
    check("listing-duplicate-check",
          only(outcome.reasons, "duplicate check(s): beta (2 times)"), repr(outcome.reasons))
    outcome = judge_listing(record(listing(*pairs), returncode=1), expected)
    check("listing-nonzero-exit", only(outcome.reasons, "the listing exited 1"),
          repr(outcome.reasons))
    outcome = judge_listing(record(listing(*pairs) + "lonely\n"), expected)
    check("listing-malformed-line",
          only(outcome.reasons, "malformed listing line: 'lonely'"), repr(outcome.reasons))
    outcome = judge_listing(record(listing(*pairs, ("beta", "another"))), None)
    check("listing-without-expectation-refuses-repeats",
          only(outcome.reasons, "duplicate check(s): beta (2 times)",
               "description mismatch for 'beta'"), repr(outcome.reasons))

    # Reports against the expectation and the clean rule.
    outcome = judge_report(record(table(passing)), expected, "clean")
    check("report-complete-accepted", outcome.reasons == () and outcome.summary == (3, 3)
          and outcome.rows == passing, repr(outcome.reasons))
    outcome = judge_report(record(table((passing[0], passing[2]))), expected, "clean")
    check("report-missing-expected-check",
          only(outcome.reasons, "missing expected check(s): beta"), repr(outcome.reasons))
    renamed = (passing[0], ("beta", "another", True, ""), passing[2])
    outcome = judge_report(record(table(renamed)), expected, "clean")
    check("report-wrong-description",
          only(outcome.reasons, "description mismatch for 'beta': expected 'the second "
               "check', reported 'another'"), repr(outcome.reasons))
    outcome = judge_report(record(table((*passing, ("gamma", "a third check", True, "")))),
                           expected, "clean")
    check("report-unexpected-check",
          only(outcome.reasons, "unexpected check(s): gamma"), repr(outcome.reasons))
    outcome = judge_report(record(table((*passing, passing[1]))), expected, "clean")
    check("report-duplicate-check",
          only(outcome.reasons, "duplicate check(s): beta (2 times)"), repr(outcome.reasons))
    spanning = (passing[0], ("beta", "the second check", True, "first line\nsecond line"),
                passing[2])
    outcome = judge_report(record(table(spanning)), expected, "clean")
    check("report-multiline-detail-is-one-row", outcome.reasons == ()
          and len(outcome.rows) == 3 and outcome.rows[1][3] == "first line\nsecond line",
          repr((outcome.reasons, outcome.rows)))
    outcome = judge_report(record(table(passing, before="rewrote the team\n  more\n")),
                           expected, "clean")
    check("report-keeps-text-before-rows", outcome.reasons == ()
          and outcome.stray == ("rewrote the team", "more"), repr((outcome.reasons, outcome.stray)))

    # Exit status against rows.
    outcome = judge_report(record(table(passing), returncode=3), expected, "clean")
    check("pass-text-with-nonzero-exit-refused",
          only(outcome.reasons, "every row passed but the suite exited 3"),
          repr(outcome.reasons))
    outcome = judge_report(record(table(beta_fails), returncode=1), expected, "case 1", "beta")
    check("fail-rows-with-exit-1-accepted", outcome.reasons == ()
          and outcome.failed == {"beta"}, repr(outcome.reasons))
    outcome = judge_report(record(table(beta_fails), returncode=0), expected, "case 1", "beta")
    check("fail-rows-with-exit-0-refused",
          only(outcome.reasons, "1 failed row(s) but the suite exited 0; a failing suite "
               "exits 1"), repr(outcome.reasons))
    outcome = judge_report(record(table(beta_fails), returncode=2), expected, "case 1", "beta")
    check("fail-rows-with-other-exit-refused",
          only(outcome.reasons, "exited 2 with 1 failed row(s); a failing suite exits 1"),
          repr(outcome.reasons))

    # Summaries and rows that cannot be read as a result.
    outcome = judge_report(record(table(passing, summary=False), returncode=1), expected, "clean")
    check("missing-summary-refused-alone",
          only(outcome.reasons, "no summary line; the run is incomplete"),
          repr(outcome.reasons))
    outcome = judge_report(record(table(passing[:1], summary=False), returncode=1),
                           expected, "clean")
    check("missing-summary-names-unexecuted-checks",
          only(outcome.reasons, "no summary line; the run is incomplete",
               "missing expected check(s): beta, every-unit-reported"),
          repr(outcome.reasons))
    outcome = judge_report(record(table(passing) + "3/x checks passed\n"), expected, "clean")
    check("malformed-summary-refused",
          only(outcome.reasons, "malformed summary: '3/x checks passed'"),
          repr(outcome.reasons))
    outcome = judge_report(record(table(passing, passed=2, total=2)), expected, "clean")
    check("summary-disagreeing-with-rows-refused",
          only(outcome.reasons, "summary counts 2/2 but 3 row(s) were read, 3 passed"),
          repr(outcome.reasons))
    outcome = judge_report(record(table(passing, extra_lines=("  PASS  lonely",))),
                           expected, "clean")
    check("malformed-row-refused", only(outcome.reasons, "malformed row: 'PASS  lonely'"),
          repr(outcome.reasons))

    # A run that did not finish is judged for that alone.
    partial = Run(("python3", "agent-resume", "--selftest", "--jobs", "1"), None,
                  table(passing[:1], summary=False), "", 1.0)
    outcome = judge_report(partial, expected, "case 1", "beta")
    check("timeout-refused-alone",
          only(outcome.reasons, "did not finish within 1s; its process group was stopped",
               "missing expected check(s): beta, every-unit-reported"),
          repr(outcome.reasons))
    outcome = judge_report(record(table(passing), returncode=-15), expected, "case 1", "beta")
    check("signal-refused-alone",
          only(outcome.reasons, "was ended by SIGTERM (returncode -15)"),
          repr(outcome.reasons))
    ended = (passing[0], passing[1],
             ("every-unit-reported", expected["every-unit-reported"], False,
              "fixture crashed"))
    outcome = judge_report(
        record(table(ended), returncode=1,
               stderr="Traceback (most recent call last):\nFixtureError: broken"),
        expected, "clean")
    dump = "\n".join(raw_output(outcome))
    check("ended-run-refused-with-trace-kept",
          only(outcome.reasons, "ended before every unit reported: fixture crashed",
               "already failing: every-unit-reported")
          and "Traceback (most recent call last)" in dump and "FixtureError: broken" in dump,
          repr((outcome.reasons, dump)))

    # The failure set.
    outcome = judge_report(record(table(passing)), expected, "case 1", "beta")
    check("target-did-not-fail", only(outcome.reasons, "'beta' did not fail"),
          repr(outcome.reasons))
    both = tuple((cid, description, cid == "every-unit-reported",
                  "" if cid == "every-unit-reported" else "scripted")
                 for cid, description in expected.items())
    outcome = judge_report(record(table(both), returncode=1), expected, "case 1", "beta")
    check("unjustified-extra-failure-refused",
          only(outcome.reasons, "also failed 'alpha', which the case does not justify"),
          repr(outcome.reasons))
    outcome = judge_report(record(table(both), returncode=1), expected, "case 1", "beta",
                           {"alpha": "the mutation removes the input both read"})
    check("justified-co-failure-accepted", outcome.reasons == ()
          and outcome.failed == {"alpha", "beta"}, repr(outcome.reasons))
    outcome = judge_report(record(table(beta_fails), returncode=1), expected, "case 1", "beta",
                           {"alpha": "the mutation removes the input both read"})
    check("justified-co-failure-absent-refused",
          only(outcome.reasons, "justified co-failure 'alpha' did not fail (its reason: "
               "the mutation removes the input both read)"), repr(outcome.reasons))
    outcome = judge_report(record(table(beta_fails), returncode=1), expected, "clean")
    check("clean-run-already-failing", only(outcome.reasons, "already failing: beta"),
          repr(outcome.reasons))

    # --failed as filtering of the unfiltered run.
    full = judge_report(record(table(beta_fails), returncode=1), expected, "case 1", "beta")
    filtered = "beta  the second check  (scripted)\nchecks: 2 ok, 1 failed\n"
    outcome = judge_filtering(full, record(filtered, returncode=1), expected)
    check("failed-only-matches-full-report", outcome.reasons == ()
          and outcome.rows == (("beta", "the second check"),), repr(outcome.reasons))
    outcome = judge_filtering(
        full, record("alpha  the first check\n" + filtered, returncode=1), expected)
    check("failed-only-lists-a-passing-check",
          only(outcome.reasons, "lists alpha, which the full report passed",
               "summary says 1 failed but 2 known row(s) were readable"),
          repr(outcome.reasons))
    outcome = judge_filtering(full, record("checks: 2 ok, 1 failed\n", returncode=1), expected)
    check("failed-only-omits-a-failed-check",
          only(outcome.reasons, "omits beta, which the full report failed",
               "summary says 1 failed but 0 known row(s) were readable"),
          repr(outcome.reasons))
    outcome = judge_filtering(
        full, record("beta  the second check  (scripted)\nchecks: 1 ok, 1 failed\n",
                     returncode=1), expected)
    check("failed-only-summary-disagrees",
          only(outcome.reasons, "summary counts 1 ok, 1 failed but the full report had "
               "2 passed, 1 failed"), repr(outcome.reasons))
    outcome = judge_filtering(full, record(filtered, returncode=0), expected)
    check("failed-only-exit-differs",
          only(outcome.reasons, "exited 0 where the unfiltered run exited 1"),
          repr(outcome.reasons))
    outcome = judge_filtering(
        full, record("not-a-check  continuation\nchecks: 2 ok, 1 failed\n", returncode=1),
        expected)
    check("failed-only-unknown-line-is-not-a-row",
          only(outcome.reasons, "omits beta, which the full report failed",
               "summary says 1 failed but 0 known row(s) were readable"),
          repr(outcome.reasons))
    outcome = judge_filtering(
        full, record("beta  the second check  (scripted)\nchecks: 2 ok,\n", returncode=1),
        expected)
    check("failed-only-malformed-summary",
          only(outcome.reasons, "malformed summary: 'checks: 2 ok,'"), repr(outcome.reasons))
    outcome = judge_filtering(
        full, record("beta  the second check  (scripted)\n" * 2 + "checks: 2 ok, 1 failed\n",
                     returncode=1), expected)
    check("failed-only-repeated-row",
          only(outcome.reasons, "lists beta 2 times",
               "summary says 1 failed but 2 known row(s) were readable"),
          repr(outcome.reasons))
    outcome = judge_filtering(
        full, record("beta  the second check  (scripted)\n", returncode=1), expected)
    check("failed-only-missing-summary",
          only(outcome.reasons, "no summary line; the run is incomplete"),
          repr(outcome.reasons))
    stopped = Run(("python3", "agent-resume", "--selftest", "--failed"), None,
                  "beta  the second check  (scripted)\n", "", 1.0)
    outcome = judge_filtering(full, stopped, expected)
    check("failed-only-timeout-judged-alone",
          only(outcome.reasons, "did not finish within 1s; its process group was stopped"),
          repr(outcome.reasons))
    outcome = judge_filtering(full, record(filtered, returncode=-9), expected)
    check("failed-only-signal-judged-alone",
          only(outcome.reasons, "was ended by SIGKILL (returncode -9)"), repr(outcome.reasons))

    # Cases and expectations that cannot be judged.
    try:
        _normalize_cases([])
    except ProbeFailure as exc:
        empty_raised = "no mutation cases" in str(exc)
    else:
        empty_raised = False
    check("empty-request-raises", empty_raised, "did not raise")
    for name, case, phrase in (
        ("co-failure-without-reason-refused", ("a", "old", "new", {"b": ""}),
         "co-failures must map each check id to the reason"),
        ("co-failure-naming-the-check-refused", ("a", "old", "new", {"a": "why"}),
         "'a' is the named check, not one of its co-failures"),
        ("case-of-wrong-length-refused", ("a", "old"), "is not a (check, old, new) triple"),
        ("case-with-empty-value-refused", ("a", "", "new"),
         "case 1 contains an empty or non-string value"),
        ("case-replacing-text-with-itself-refused", ("a", "same", "same"),
         "case 1 replaces text with itself"),
    ):
        refused(name, lambda case=case: _normalize_cases([case]), phrase)
    normalized = _normalize_cases([("a", "old", "new"), ("b", "old", "new", {"c": "why"})])
    check("cases-carry-their-co-failures",
          normalized == (("a", "old", "new", {}), ("b", "old", "new", {"c": "why"})),
          repr(normalized))
    for name, given, phrase in (
        ("expectation-repeating-an-id-refused", [("a", "x"), ("a", "y")], "names 'a' twice"),
        ("expectation-with-empty-value-refused", {"a": ""},
         "expected entry 1 ('a'): its description is empty or not a string"),
        ("expectation-with-empty-id-refused", [("", "x")],
         "expected entry 1: its id is empty or not a string"),
        ("empty-expectation-refused", {}, "names no check"),
        ("expectation-entry-not-a-pair-refused", [("a", "x", "y")],
         "expected entry 1 is not an (id, description) pair"),
    ):
        refused(name, lambda given=given: _normalize_expected(given), phrase)
    check("expectation-from-pairs", _normalize_expected([("a", "x"), ("b", "y")])
          == {"a": "x", "b": "y"})

    # The frozen expected-mapping-v1 file, read per selection.
    def frozen(entries, modes=("read-only", "live-jobs-1"), schema="expected-mapping-v1",
               source=None, **replaced):
        """A mapping file's bytes; a replaced key takes the given value, ... drops it."""
        record = {
            "schema": schema,
            "frozen_for_source": {"sha256": "f" * 64} if source is None else source,
            "modes": {mode: {} for mode in modes},
            "rows": [{"id": cid, "description": description,
                      "emitted_in": {mode: "MECHANISM" for mode in emitted},
                      "not_emitted_in": {mode: "WITHHELD" for mode in withheld}}
                     for cid, description, emitted, withheld in entries],
        }
        record.update(replaced)
        return json.dumps({key: value for key, value in record.items()
                           if value is not ...}).encode()

    def adjudicating(mapping_path, digest=None, entries=(), record=None):
        """An approved-adjudications record naming the mapping at mapping_path, beside it.

        record replaces the whole record, for the forms a test spells out itself.
        """
        digest = digest or hashlib.sha256(mapping_path.read_bytes()).hexdigest()
        where = mapping_path.with_name(f"{mapping_path.stem}.adjudications.json")
        where.write_text(json.dumps(record if record is not None else {
            "frozen_mapping": {"sha256": digest}, "entries": list(entries)}))
        return where

    with tempfile.TemporaryDirectory() as td:
        record_path = Path(td) / "expected-mapping-v1.json"
        rows = (("alpha", "the first check", ("read-only", "live-jobs-1"), ()),
                ("beta", "the second check", ("read-only", "live-jobs-1"), ()),
                ("live-only", "a live check", ("live-jobs-1",), ("read-only",)))
        record_path.write_bytes(frozen(rows))
        read_only = retained_expectation(record_path)
        live_one = retained_expectation(record_path, live=True, jobs=1)
        check("retained-expectation-per-selection",
              read_only.mapping == {"alpha": "the first check", "beta": "the second check"}
              and live_one.mapping == {**read_only.mapping, "live-only": "a live check"}
              and (read_only.selection, live_one.selection) == ("read-only", "live-jobs-1")
              and read_only.sha256 == hashlib.sha256(record_path.read_bytes()).hexdigest()
              and read_only.path == record_path and read_only.source == {"sha256": "f" * 64}
              and read_only.adjudications is None,
              repr((read_only, live_one)))
        approved = adjudicating(record_path)
        backed = retained_expectation(record_path, adjudications=approved)
        check("retained-expectation-records-adjudications",
              backed.adjudications == approved
              and backed.adjudications_sha256 == hashlib.sha256(approved.read_bytes()).hexdigest()
              and backed.mapping == read_only.mapping, repr(backed))
        try:
            backed.mapping["alpha"] = "an edit in place"
        except TypeError:
            edited = False
        else:
            edited = True
        check("retained-expectation-mapping-is-read-only",
              not edited and backed.mapping["alpha"] == "the first check", repr(backed.mapping))
        adjudicating(record_path, digest="0" * 64)
        refused("retained-expectation-adjudications-for-another-mapping-refused",
                lambda: retained_expectation(record_path, adjudications=approved),
                "do not name this mapping by its sha256")
        approved.write_text("{")
        refused("retained-expectation-adjudications-not-json-refused",
                lambda: retained_expectation(record_path, adjudications=approved),
                f"adjudications {approved} is not JSON")
        approved.unlink()
        refused("retained-expectation-missing-adjudications-refused",
                lambda: retained_expectation(record_path, adjudications=approved),
                f"adjudications {approved} cannot be read")
        refused("retained-expectation-missing-file-refused",
                lambda: retained_expectation(Path(td) / "absent.json"),
                f"expected mapping {Path(td) / 'absent.json'} cannot be read")
        no_description = {"id": "delta", "emitted_in": {"read-only": "MECHANISM"},
                          "not_emitted_in": {}}
        no_id = {"description": "a row with no id", "emitted_in": {"read-only": "MECHANISM"}}
        for name, text, options, phrase in (
            ("retained-expectation-unknown-selection-refused", frozen(rows),
             {"live": True, "jobs": 3}, "has no selection live-jobs-3; it has: live-jobs-1, "
             "read-only"),
            ("retained-expectation-wrong-schema-refused", frozen(rows, schema="other"), {},
             "does not carry schema expected-mapping-v1"),
            ("retained-expectation-emitted-and-withheld-refused",
             frozen((*rows, ("gamma", "a torn row", ("read-only",), ("read-only",)))), {},
             "row 4 ('gamma') is both emitted and withheld in read-only"),
            ("retained-expectation-repeated-id-refused",
             frozen((*rows, ("alpha", "again", ("read-only",), ()))), {},
             "names 'alpha' twice"),
            ("retained-expectation-not-json-refused", b"{", {}, "is not JSON"),
            ("retained-expectation-without-source-refused", frozen(rows, source=...), {},
             "does not name the source it was frozen for"),
            ("retained-expectation-source-without-sha256-refused",
             frozen(rows, source={"commit": "abc"}), {},
             "does not name the source it was frozen for"),
            ("retained-expectation-rows-not-a-list-refused", frozen(rows, rows={}), {},
             "rows is not a list"),
            ("retained-expectation-row-not-an-object-refused", frozen(rows, rows=["alpha"]), {},
             "row 1 is not an object"),
            ("retained-expectation-placement-not-a-collection-refused",
             frozen(rows, rows=[{"id": "alpha", "description": "the first check",
                                 "emitted_in": "read-only"}]), {},
             "row 1 ('alpha'): emitted_in is neither an object nor a list"),
            ("retained-expectation-row-without-description-refused",
             frozen(rows, rows=[{"id": "alpha", "description": "the first check",
                                 "emitted_in": {"read-only": "MECHANISM"}}, no_description]), {},
             "row 2 ('delta'): its description is missing or not a non-empty string"),
            ("retained-expectation-row-without-id-refused", frozen(rows, rows=[no_id]), {},
             "row 1: its id is missing or not a non-empty string"),
            ("retained-expectation-modes-not-an-object-refused",
             json.dumps({"schema": "expected-mapping-v1", "frozen_for_source": {"sha256": "f" * 64},
                         "modes": ["read-only"], "rows": []}).encode(), {},
             "has no selection read-only; it has: "),
        ):
            record_path.write_bytes(text)
            refused(name, lambda options=options: retained_expectation(record_path, **options),
                    phrase)

    # Each way an adjudication entry can lack a decision is named its own way,
    # and a refusal is a decision.
    for name, entries, wanted in (
        ("undecided-accepts-a-refused-decision",
         [{"id": "x", "decision": "refused: not a row of this mapping"}], []),
        ("undecided-names-an-empty-decision", [{"id": "x", "decision": ""}],
         ["adjudication entry 1 ('x') has an empty decision: ''"]),
        ("undecided-names-a-missing-decision", [{"id": "x"}],
         ["adjudication entry 1 ('x') has no decision"]),
        ("undecided-names-an-entry-that-is-not-an-object", ["x"],
         ["adjudication entry 1 is not an object"]),
    ):
        try:
            got = undecided({"entries": entries})
        except Exception as exc:
            got = f"raised {type(exc).__name__}: {exc}"
        check(name, got == wanted, repr(got))

    # The digest of a test tree follows every file's name and bytes.
    with tempfile.TemporaryDirectory() as td:
        tree = Path(td) / "test"
        (tree / "corpus").mkdir(parents=True)
        (tree / "corpus" / "a.jsonl").write_text("one")
        (tree / "b.txt").write_text("two")

        def digest_now():
            return tree_digest(fingerprints(sorted(item for item in tree.rglob("*")
                                                   if item.is_file())), tree)

        first = digest_now()
        (tree / "b.txt").write_text("three")
        edited = digest_now()
        (tree / "b.txt").rename(tree / "c.txt")
        renamed = digest_now()
        (tree / "d.txt").write_text("four")
        added = digest_now()
        check("tree-digest-follows-names-and-contents",
              first[0] == 2 and added[0] == 3
              and len({first[1], edited[1], renamed[1], added[1]}) == 4
              and first == tree_digest(fingerprints([tree / "corpus" / "a.jsonl"]) | {
                  tree / "b.txt": hashlib.sha256(b"two").hexdigest()}, tree),
              repr((first, edited, renamed, added)))

    # The command every run is built from.
    check("argv-carries-tier-and-jobs",
          suite_argv("t", live=True, jobs=4) == (sys.executable, "t", "--selftest", "--live",
                                                 "--jobs", "4")
          and suite_argv("t", jobs=2, listing=True) == (sys.executable, "t", "--selftest",
                                                        "--jobs", "2", "--list")
          and suite_argv("t", failed=True)[-1] == "--failed")

    # The batch: one clean run, concurrency, and its cap.
    def overlap(mutant_count):
        wanted = min(MAX_PARALLEL_SUITES, mutant_count + 1)
        state = {"active": 0, "maximum": 0, "calls": []}
        lock = threading.Lock()
        release = threading.Event()

        def fake(path):
            with lock:
                state["active"] += 1
                state["maximum"] = max(state["maximum"], state["active"])
                state["calls"].append(path)
                if state["maximum"] == wanted:
                    release.set()
            release.wait(2)
            with lock:
                state["active"] -= 1
            return f"ran {path}"

        paths = [f"mutant-{at}" for at in range(mutant_count)]
        return state, batch(["clean", *paths], fake), paths

    state, ran, paths = overlap(2)
    capped, _ran, _paths = overlap(MAX_PARALLEL_SUITES * 2)
    check("batch-reuses-one-clean-run",
          state["calls"].count("clean") == 1
          and sorted(state["calls"]) == sorted(["clean", *paths])
          and ran == [f"ran {path}" for path in ["clean", *paths]],
          repr((state, ran)))
    check("batch-runs-concurrently", state["maximum"] == 3,
          f"maximum active={state['maximum']}")
    check("batch-concurrency-capped", capped["maximum"] == MAX_PARALLEL_SUITES,
          f"maximum active={capped['maximum']}")

    # The whole path, on the stand-in runner as real processes.
    runner_text = SYNTHETIC_RUNNER.read_text()
    check("synthetic-runner-has-the-knob-block", knobs() in runner_text,
          f"{SYNTHETIC_RUNNER} does not contain the block knobs() renders")
    was_log = os.environ.get("PROBE_SYNTHETIC_LOG")
    with tempfile.TemporaryDirectory() as td:
        room = Path(td)
        tool = room / "agent-resume"
        tool.write_text(runner_text)
        tool.chmod(0o755)
        (room / "_agent-resume").write_text("")
        (room / "test").mkdir()
        log = room / "argv.log"
        os.environ["PROBE_SYNTHETIC_LOG"] = str(log)

        def probe(cases, **options):
            options.setdefault("expected", SYNTHETIC_EXPECTED)
            options.setdefault("timeout", 20.0)
            try:
                return expect(cases, live=True, jobs=3, tool=tool, stream=io.StringIO(),
                              **options), None
            except ProbeFailure as exc:
                return None, exc

        def refusals(exc, label):
            return tuple(failure[len(label) + 2:] for failure in exc.failures
                         if failure.startswith(f"{label}: "))

        try:
            bite = knobs(FAILING=["target-check"])
            results, exc = probe([("target-check", knobs(), bite)])
            check("synthetic-bite-accepted", exc is None and results is not None
                  and results[0]["bit"] == ("target-check",),
                  exc.report if exc else repr(results))
            commands = [json.loads(line) for line in log.read_text().splitlines()]
            check("tier-and-jobs-reach-every-command",
                  len(commands) == 3
                  and all(argv[:4] == ["--selftest", "--live", "--jobs", "3"]
                          for argv in commands)
                  and sum(argv == ["--selftest", "--live", "--jobs", "3", "--list"]
                          for argv in commands) == 1
                  and not any("--failed" in argv for argv in commands),
                  repr(commands))
            results, exc = probe([
                ("target-check", knobs(), knobs(FAILING=["target-check", "other-check"]),
                 {"other-check": "the scripted failure names both"}),
            ])
            check("synthetic-justified-co-failure-accepted", exc is None
                  and results[0]["bit"] == ("other-check", "target-check"),
                  exc.report if exc else repr(results))
            results, exc = probe([("target-check", knobs(), bite)], filtering=True)
            commands = [json.loads(line) for line in log.read_text().splitlines()]
            check("synthetic-filtering-accepted", exc is None
                  and sum("--failed" in argv for argv in commands) == 2,
                  exc.report if exc else repr(commands))

            _results, exc = probe([
                ("target-check", knobs(), knobs(FAILING=["target-check"], DROP=["third-check"])),
                ("target-check", knobs(), knobs(SLEEP=30)),
                ("target-check", knobs(), knobs(SIGNAL="SIGTERM")),
                ("target-check", knobs(), knobs(FAILING=["target-check"], EXIT=0)),
                ("target-check", knobs(), knobs(FAILING=["target-check"], SUMMARY="missing")),
                ("target-check", knobs(), knobs(FAILING=["target-check"], SUMMARY="malformed")),
                ("target-check", knobs(), knobs(FAILING=["target-check"], SUMMARY="short")),
                ("target-check", knobs(),
                 knobs(FAILING=["target-check"], EXTRA=[("rogue-check", "a row nobody listed")])),
                ("target-check", knobs(),
                 knobs(FAILING=["target-check"], DUPLICATE=["other-check"])),
                ("target-check", knobs(),
                 knobs(FAILING=["target-check"], DESCRIBE={"other-check": "renamed"})),
                ("target-check", knobs(), knobs(FAILING=["target-check", "other-check"])),
                ("target-check", knobs(), knobs(FAILING=["target-check"]),
                 {"other-check": "claimed, but the mutation does not touch it"}),
                ("target-check", knobs(), knobs(EXIT=3)),
            ], timeout=1.5)
            check("synthetic-refusals-raise", exc is not None, "expect returned")
            if exc is not None:
                for at, name, starts in (
                    (1, "synthetic-declaration-and-check-removed-together",
                     ("missing expected check(s): third-check",)),
                    (2, "synthetic-timeout-refused",
                     ("did not finish within 1.5s; its process group was stopped",
                      "missing expected check(s): every-unit-reported, other-check, "
                      "target-check, third-check")),
                    (3, "synthetic-signal-refused", ("was ended by SIGTERM (returncode -15)",)),
                    (4, "synthetic-wrong-failure-exit",
                     ("1 failed row(s) but the suite exited 0; a failing suite exits 1",)),
                    (5, "synthetic-missing-summary", ("no summary line; the run is incomplete",)),
                    (6, "synthetic-malformed-summary", ("malformed summary: '3/x checks passed'",)),
                    (7, "synthetic-short-summary",
                     ("summary counts 2/3 but 4 row(s) were read, 3 passed",)),
                    (8, "synthetic-unexpected-row", ("unexpected check(s): rogue-check",)),
                    (9, "synthetic-duplicate-row", ("duplicate check(s): other-check (2 times)",)),
                    (10, "synthetic-wrong-description",
                     ("description mismatch for 'other-check': expected 'a check a case may "
                      "justify as a co-failure', reported 'renamed'",)),
                    (11, "synthetic-unjustified-extra-failure",
                     ("also failed 'other-check', which the case does not justify",)),
                    (12, "synthetic-justified-co-failure-absent",
                     ("justified co-failure 'other-check' did not fail (its reason: claimed, "
                      "but the mutation does not touch it)",)),
                    (13, "synthetic-pass-text-nonzero-exit",
                     ("every row passed but the suite exited 3", "'target-check' did not fail")),
                ):
                    got = refusals(exc, f"case {at}")
                    check(name, only(got, *starts), repr(got))
                check("synthetic-refusals-keep-raw-output",
                      "--- case 3:" in exc.report and "FAIL  target-check" in exc.report
                      and all(outcome.reasons for outcome in exc.outcomes)
                      and len(exc.outcomes) == 13, exc.report[-600:])
            _results, exc = probe([("target-check", knobs(), bite)], expected={
                **SYNTHETIC_EXPECTED, "fourth-check": "a check the runner never had"})
            check("synthetic-listing-missing-expected-check", exc is not None
                  and exc.failures == ("listing: missing expected check(s): fourth-check",),
                  repr(exc and exc.failures))
            _results, exc = probe([("target-check", knobs(), knobs(FILTER_LIES=True,
                                                                   FAILING=["target-check"]))],
                                  filtering=True)
            check("synthetic-filtering-lie-refused", exc is not None
                  and only(refusals(exc, "case 1 --failed"),
                           "lists third-check, which the full report passed",
                           "summary says 1 failed but 2 known row(s) were readable"),
                  repr(exc and exc.failures))
            _results, exc = probe([("fourth-check", knobs(), bite)])
            check("synthetic-unknown-check-refused", exc is not None
                  and exc.failures[0] == "case 1: 'fourth-check' names no expected check",
                  repr(exc and exc.failures))
            _results, exc = probe([("target-check", "not in the runner", bite)])
            check("synthetic-absent-text-refused", exc is not None
                  and exc.failures == ("case 1: the text to replace was not found "
                                       "(agent-resume: 0, _agent-resume: 0)",),
                  repr(exc and exc.failures))

            synthetic_rows = [(cid, description, ("live-jobs-3",), ())
                              for cid, description in SYNTHETIC_EXPECTED.items()]
            record_path = room / "expected-mapping-v1.json"
            record_path.write_bytes(frozen(synthetic_rows, modes=("live-jobs-3",)))
            retained = retained_expectation(record_path, live=True, jobs=3)
            stream = io.StringIO()
            results = expect([("target-check", knobs(), bite)], live=True, jobs=3, tool=tool,
                             expected=retained, timeout=20.0, stream=stream)
            tool_digest = hashlib.sha256(tool.read_bytes()).hexdigest()
            printed = stream.getvalue()
            check("expect-records-the-expectation-and-tool-identity",
                  results[0]["bit"] == ("target-check",)
                  and f"expectation: {record_path} sha256 {retained.sha256}" in printed
                  and f"tool: {tool} sha256 {tool_digest}" in printed
                  and (f"companion: {room / '_agent-resume'} sha256 "
                       f"{hashlib.sha256(b'').hexdigest()}") in printed
                  and (f"test tree: {room / 'test'} 0 file(s), digest "
                       f"{hashlib.sha256().hexdigest()}") in printed
                  and "selection: live-jobs-3" in printed
                  and f"frozen for source sha256 {'f' * 64}: the tool differs from it" in printed,
                  printed)
            check("uncertified-run-says-so",
                  results.certified is False and results[0]["certified"] is False
                  and results.report == printed.rstrip("\n")
                  and printed.startswith("certifying: no; this run certifies nothing"),
                  printed)
            stream = io.StringIO()
            results = expect([("target-check", knobs(), bite)], live=True, jobs=3, tool=tool,
                             timeout=20.0, stream=stream)
            check("run-without-expectation-says-the-listing-stood-in",
                  results.certified is False
                  and "expectation: none supplied, so the listing is the population"
                  in stream.getvalue()
                  and "--failed checked for: no run\n" in stream.getvalue(), stream.getvalue())
            stream = io.StringIO()
            results = expect([("target-check", knobs(), bite)], live=True, jobs=3, tool=tool,
                             expected=SYNTHETIC_EXPECTED, timeout=20.0, stream=stream)
            check("run-with-a-bare-mapping-says-it-has-no-origin",
                  results.certified is False
                  and "expectation: a supplied mapping of 4 check(s), with no recorded origin"
                  in stream.getvalue(), stream.getvalue())

            # Every suite run needs the companion beside the tool, so its absence
            # is refused before any suite runs.
            companion = room / "_agent-resume"
            companion.rename(room / "companion-aside")
            commands_before = log.read_text()
            try:
                refused("missing-companion-refused-before-any-run",
                        lambda: expect([("target-check", knobs(), bite)], live=True, jobs=3,
                                       tool=tool, timeout=20.0, stream=io.StringIO()),
                        f"the companion {companion} is not a file")
                check("missing-companion-runs-nothing", log.read_text() == commands_before,
                      log.read_text()[len(commands_before):])
            finally:
                (room / "companion-aside").rename(companion)
            # A tool that is not there could not run whatever expect did, so what
            # tells the refusal apart is that it names the tool.
            absent_tool = room / "no-tool-here"
            refused("missing-tool-refused-before-any-run",
                    lambda: expect([("target-check", knobs(), bite)], live=True, jobs=3,
                                   tool=absent_tool, timeout=20.0, stream=io.StringIO()),
                    f"the tool {absent_tool} is not a file")

            # Certification: refused before any suite runs unless the expectation
            # is read from its frozen file with its source and decided
            # adjudications, still reads that way, and was frozen for this tool.
            source = {"commit": "c0ffee", "path_in_repo": "team-necromancy/agent-resume",
                      "sha256": tool_digest}

            def frozen_beside(name, source=source, entries=(), record=None):
                where = room / f"{name}.json"
                where.write_bytes(frozen(synthetic_rows, modes=("live-jobs-3",), source=source))
                return where, adjudicating(where, entries=entries, record=record)

            source_path, approved = frozen_beside("expected-mapping-source")
            certifying = retained_expectation(source_path, live=True, jobs=3,
                                              adjudications=approved)
            sourced = retained_expectation(source_path, live=True, jobs=3)
            unnamed_path, unnamed_approved = frozen_beside("expected-mapping-unnamed",
                                                           source={"sha256": tool_digest})
            other_path, other_approved = frozen_beside(
                "expected-mapping-other-tool", source={**source, "sha256": "f" * 64})
            pending_path, pending_approved = frozen_beside(
                "expected-mapping-pending",
                entries=[{"id": "target-check", "mode": "live-jobs-3",
                          "decision": "pending review"}])
            # The entry rule: an id that is no row, a mode that is no selection,
            # and a mode in which the row is withheld. The last needs a mapping
            # with a second selection that withholds a row.
            stray_path, stray_approved = frozen_beside(
                "expected-mapping-entry-no-row",
                entries=[{"id": "no-such-check", "mode": "live-jobs-3", "decision": "approved"}])
            unmoded_path, unmoded_approved = frozen_beside(
                "expected-mapping-entry-no-selection",
                entries=[{"id": "target-check", "mode": "live-jobs-9", "decision": "approved"}])
            withheld_path = room / "expected-mapping-entry-withheld.json"
            withheld_path.write_bytes(frozen(
                [(cid, description, ("live-jobs-3",) if cid == "target-check"
                  else ("live-jobs-3", "read-only"), ("read-only",) if cid == "target-check"
                  else ()) for cid, description in SYNTHETIC_EXPECTED.items()],
                modes=("live-jobs-3", "read-only"), source=source))
            withheld_approved = adjudicating(withheld_path, entries=[
                {"id": "target-check", "mode": "read-only", "decision": "approved"}])
            # A refused entry decides that a row is not in a selection.
            refused_emitted_path, refused_emitted_approved = frozen_beside(
                "expected-mapping-refused-but-emitted",
                entries=[{"id": "target-check", "mode": "live-jobs-3",
                          "decision": "refused: not a row of this selection"}])
            refused_unmoded_path, refused_unmoded_approved = frozen_beside(
                "expected-mapping-refused-no-selection",
                entries=[{"id": "target-check", "mode": "live-jobs-9",
                          "decision": "refused: not a row of this selection"}])
            bare_path, _bare = frozen_beside("expected-mapping-no-entries")
            bare_approved = adjudicating(bare_path, record={"frozen_mapping": {
                "sha256": hashlib.sha256(bare_path.read_bytes()).hexdigest()}})
            changed_path, changed_approved = frozen_beside("expected-mapping-changed")
            changed = retained_expectation(changed_path, live=True, jobs=3,
                                           adjudications=changed_approved)
            changed_path.write_bytes(changed_path.read_bytes() + b"\n")
            moved_path, moved_approved = frozen_beside("expected-mapping-moved-record")
            moved = retained_expectation(moved_path, live=True, jobs=3,
                                         adjudications=moved_approved)
            moved_approved.write_text(moved_approved.read_text() + "\n")
            wanted = "a certifying run needs a retained expectation: "
            reworded = types.MappingProxyType({**certifying.mapping, "target-check": "reworded"})
            for name, given, gaps in (
                ("certify-without-expectation-refused", None,
                 ["no expectation was supplied, so the listing would stand in for the "
                  "population"]),
                ("certify-with-bare-mapping-refused", SYNTHETIC_EXPECTED,
                 ["the expectation is a bare mapping with no recorded origin; read it with "
                  "retained_expectation"]),
                ("certify-without-adjudications-refused", sourced,
                 ["the expectation names no approved adjudications; pass them to "
                  "retained_expectation"]),
                ("certify-without-source-commit-and-path-refused",
                 retained_expectation(unnamed_path, live=True, jobs=3,
                                      adjudications=unnamed_approved),
                 ["the expectation's frozen source names no commit",
                  "the expectation's frozen source names no path_in_repo"]),
                ("certify-hand-built-without-its-file-refused",
                 dataclasses.replace(certifying, path=None),
                 ["the expectation names no file and hash it was read from"]),
                ("certify-hand-built-without-its-source-refused",
                 dataclasses.replace(certifying, source={}),
                 ["the expectation's frozen source names no sha256",
                  "the expectation's frozen source names no commit",
                  "the expectation's frozen source names no path_in_repo"]),
                ("certify-with-a-replaced-mapping-refused",
                 dataclasses.replace(certifying, mapping=reworded),
                 [f"the mapping in use is not the one {source_path} gives for live-jobs-3"]),
                ("certify-hand-built-expectation-refused",
                 Expectation("live-jobs-3", reworded, source_path, certifying.sha256,
                             dict(source), approved, certifying.adjudications_sha256),
                 [f"the mapping in use is not the one {source_path} gives for live-jobs-3"]),
                ("certify-another-tool-refused",
                 retained_expectation(other_path, live=True, jobs=3,
                                      adjudications=other_approved),
                 [f"the tool's sha256 {tool_digest} is not {'f' * 64}, the source the "
                  "expectation was frozen for; a changed tool needs its own frozen mapping"]),
                ("certify-mapping-changed-after-reading-refused", changed,
                 [f"{changed_path} now has sha256 "
                  f"{hashlib.sha256(changed_path.read_bytes()).hexdigest()}, not the "
                  f"{changed.sha256} the expectation was read with"]),
                ("certify-adjudications-changed-after-reading-refused", moved,
                 [f"{moved_approved} now has sha256 "
                  f"{hashlib.sha256(moved_approved.read_bytes()).hexdigest()}, not the "
                  f"{moved.adjudications_sha256} the expectation was read with"]),
                ("certify-undecided-adjudication-refused",
                 retained_expectation(pending_path, live=True, jobs=3,
                                      adjudications=pending_approved),
                 ["adjudication entry 1 ('target-check') is not decided: its decision is "
                  "'pending review'"]),
                ("certify-adjudications-without-entries-refused",
                 retained_expectation(bare_path, live=True, jobs=3, adjudications=bare_approved),
                 ["the adjudications record holds no entries list"]),
                ("certify-with-a-replaced-source-refused",
                 dataclasses.replace(certifying, source={**source, "commit": "beefcafe"}),
                 [f"the frozen source in use is not the one {source_path} names"]),
                ("certify-entry-naming-no-row-refused",
                 retained_expectation(stray_path, live=True, jobs=3, adjudications=stray_approved),
                 ["adjudication entry 1 names 'no-such-check', which is no row of the mapping"]),
                ("certify-entry-naming-no-selection-refused",
                 retained_expectation(unmoded_path, live=True, jobs=3,
                                      adjudications=unmoded_approved),
                 ["adjudication entry 1 ('target-check') names the mode 'live-jobs-9', which is "
                  "no selection of the mapping"]),
                ("certify-refused-entry-for-a-row-still-emitted-refused",
                 retained_expectation(refused_emitted_path, live=True, jobs=3,
                                      adjudications=refused_emitted_approved),
                 ["adjudication entry 1 ('target-check') is refused, but the mapping still emits "
                  "that row in 'live-jobs-3'"]),
                ("certify-refused-entry-for-no-selection-refused",
                 retained_expectation(refused_unmoded_path, live=True, jobs=3,
                                      adjudications=refused_unmoded_approved),
                 ["adjudication entry 1 ('target-check') is refused for the mode 'live-jobs-9', "
                  "which is no selection of the mapping"]),
                ("certify-entry-in-a-mode-withholding-its-row-refused",
                 retained_expectation(withheld_path, live=True, jobs=3,
                                      adjudications=withheld_approved),
                 ["adjudication entry 1 ('target-check') names the mode 'read-only', in which "
                  "that row is not emitted"]),
            ):
                commands_before = log.read_text()
                try:
                    expect([("target-check", knobs(), bite)], live=True, jobs=3, tool=tool,
                           expected=given, timeout=20.0, certify=True, stream=io.StringIO(),
                           verifier=verifier)
                except ProbeFailure as exc:
                    check(name, exc.failures == tuple(wanted + gap for gap in gaps)
                          and log.read_text() == commands_before, repr(exc.failures))
                except Exception as exc:
                    check(name, False, f"raised {type(exc).__name__} instead: {exc}")
                else:
                    check(name, False, "expect returned")

            # The form of mapping v1's approved record, field for field, certifies,
            # against a mapping that has the rows and modes its entries name.
            v1_path = room / "expected-mapping-v1-form.json"
            v1_rows = [(cid, description, ("live-jobs-1", "live-jobs-2", "live-jobs-3",
                                           "live-jobs-4"), ())
                       for cid, description in SYNTHETIC_EXPECTED.items()]
            v1_rows.append(("read-only-tier-runs-no-tmux", "the read-only tier runs no tmux",
                            ("live-jobs-1", "live-jobs-2", "live-jobs-4"), ("live-jobs-3",)))
            v1_path.write_bytes(frozen(v1_rows, modes=("live-jobs-1", "live-jobs-2", "live-jobs-3",
                                                       "live-jobs-4"), source=source))
            v1_approved = adjudicating(v1_path, record={})
            v1_approved.write_text(json.dumps({
                "recorded_at_zagreb": "2026-10-07T03:32:05+02:00", "recorded_by": "a reviewer",
                "decided_by": "the lead",
                "frozen_mapping": {"file": "expected/expected-mapping-v1.json",
                                   "bytes": v1_path.stat().st_size,
                                   "sha256": hashlib.sha256(v1_path.read_bytes()).hexdigest(),
                                   "unchanged_by_this_record": True},
                "source_frozen_for": f"{tool_digest} (the stand-in)",
                "decision": "All six entries approved.",
                "ground": {"file": "inputs/first-step.txt", "line": 72, "requirement": "R1",
                           "text": "the plan's words"},
                "entries": [{"mode": mode, "id": cid, "description": "a description",
                             "kind": "executed but absent from the listing",
                             "mapping_v1_state": "included in the expected set for this mode",
                             "decision": "approved: expected row"}
                            for mode in ("live-jobs-1", "live-jobs-2", "live-jobs-4")
                            for cid in ("every-unit-reported", "read-only-tier-runs-no-tmux")],
                "consequence": "the listing changes", "note": "a note"}))
            v1_form = retained_expectation(v1_path, live=True, jobs=3, adjudications=v1_approved)
            check("certify-accepts-the-v1-adjudications-form",
                  certification_gaps(v1_form, True, 3, tool_digest, VERIFIER_LOADED) == [],
                  repr(certification_gaps(v1_form, True, 3, tool_digest, VERIFIER_LOADED)))

            # Each input is read once and judged as read.
            reads = collections.Counter()
            real_record = globals()["_json_record"]

            def counting(path, what):
                reads[Path(path)] += 1
                return real_record(path, what)

            globals()["_json_record"] = counting
            try:
                once = certification_gaps(certifying, True, 3, tool_digest, VERIFIER_LOADED)
            finally:
                globals()["_json_record"] = real_record
            check("certification-reads-each-input-once",
                  once == ["functions replaced in the verifier after it was imported would "
                           "run code its bytes do not hold: _json_record"]
                  and reads == {source_path: 1, approved: 1}, repr((once, reads)))

            # A certifying run names its timeout.
            commands_before = log.read_text()
            refused("certify-without-a-timeout-refused",
                    lambda: expect([("target-check", knobs(), bite)], live=True, jobs=3, tool=tool,
                                   expected=certifying, certify=True, stream=io.StringIO(),
                                   verifier=verifier),
                    "a certifying run names its timeout")
            check("certify-without-a-timeout-runs-nothing", log.read_text() == commands_before,
                  log.read_text()[len(commands_before):])

            # The verifier on disk is the one this process loaded.
            loaded = globals()["VERIFIER_LOADED"]
            globals()["VERIFIER_LOADED"] = "0" * 64
            try:
                refused("certify-refused-when-the-verifier-changed-on-disk",
                        lambda: expect([("target-check", knobs(), bite)], live=True, jobs=3,
                                       tool=tool, expected=certifying, timeout=20.0,
                                       certify=True, stream=io.StringIO(), verifier=verifier),
                        f"the verifier {VERIFIER} on disk has sha256 {loaded}, not the "
                        f"{'0' * 64} this process loaded")
            finally:
                globals()["VERIFIER_LOADED"] = loaded

            # A refused entry for a row its mode withholds, or for an id that is
            # no row, is a decision the mapping carries.
            for name, cid, mode in (
                ("certify-accepts-a-refused-entry-for-a-withheld-row", "target-check",
                 "read-only"),
                ("certify-accepts-a-refused-entry-for-an-id-that-is-no-row", "no-such-check",
                 "read-only"),
            ):
                adjudicating(withheld_path, entries=[
                    {"id": cid, "mode": mode, "decision": "refused: not in this selection"}])
                gaps = certification_gaps(
                    retained_expectation(withheld_path, live=True, jobs=3,
                                         adjudications=withheld_approved),
                    True, 3, tool_digest, VERIFIER_LOADED)
                check(name, gaps == [], repr(gaps))

            # Cached bytecode that is not what the verifier's bytes compile to.
            commands_before = log.read_text()
            from_source = globals()["VERIFIER_FROM_SOURCE"]
            globals()["VERIFIER_FROM_SOURCE"] = False
            try:
                refused("certify-refused-when-the-code-was-not-compiled-from-the-verifier",
                        lambda: expect([("target-check", knobs(), bite)], live=True, jobs=3,
                                       tool=tool, expected=certifying, timeout=20.0,
                                       certify=True, stream=io.StringIO(), verifier=verifier),
                        f"the code this process runs is not what {VERIFIER} compiles to")
            finally:
                globals()["VERIFIER_FROM_SOURCE"] = from_source
            check("certify-refused-for-bytecode-runs-nothing", log.read_text() == commands_before,
                  log.read_text()[len(commands_before):])
            stale = room / "stale"
            stale.mkdir()
            source_text = VERIFIER.read_text()
            (stale / "probe.py").write_text(source_text)
            (stale / "test" / "fixtures").mkdir(parents=True)
            importer = (f"import sys; sys.path.insert(0, {str(stale)!r}); import probe; "
                        "print(probe.VERIFIER_FROM_SOURCE)")

            def loaded_from_source():
                said = subprocess.run([sys.executable, "-B", "-c", importer],
                                      capture_output=True, text=True, timeout=60)
                return said.stdout.strip() or said.stderr.strip()[-300:]

            fresh = loaded_from_source()
            py_compile.compile(str(stale / "probe.py"), cfile=importlib.util.cache_from_source(
                str(stale / "probe.py")), invalidation_mode=py_compile.PycInvalidationMode.TIMESTAMP)
            kept = (stale / "probe.py").stat()
            first_line = source_text.splitlines()[0]
            changed_text = source_text.replace(first_line, first_line[:-1] + "!", 1)
            (stale / "probe.py").write_text(changed_text)
            os.utime(stale / "probe.py", ns=(kept.st_atime_ns, kept.st_mtime_ns))
            behind = loaded_from_source()
            check("verifier-knows-when-cached-bytecode-ran",
                  fresh == "True" and behind == "False"
                  and len(changed_text.encode()) == kept.st_size, repr((fresh, behind)))

            # verifier_selftest on stand-ins: a failing one refuses, a silent one
            # refuses, a passing one gives its last line; given none, it runs
            # selftest.
            def failing_stand_in():
                raise ProbeFailure(["a stand-in check"])

            refused("verifier-selftest-refuses-a-selftest-that-prints-nothing",
                    lambda: verifier_selftest(lambda: None),
                    "the verifier's own selftest printed nothing")
            real_selftest = globals()["selftest"]
            globals()["selftest"] = lambda: print("probe checks: 9 ok, 0 failed")
            try:
                ran = verifier_selftest()
            except ProbeFailure as exc:
                ran = f"refused: {exc.failures}"
            finally:
                globals()["selftest"] = real_selftest
            check("verifier-selftest-runs-selftest-when-given-none",
                  ran == "probe checks: 9 ok, 0 failed", repr(ran))

            refused("verifier-selftest-refuses-a-failing-selftest",
                    lambda: verifier_selftest(failing_stand_in),
                    "the verifier's own selftest failed: a stand-in check")
            check("verifier-selftest-gives-the-summary-line",
                  verifier_selftest(lambda: print("a first line\nprobe checks: 3 ok, 0 failed"))
                  == "probe checks: 3 ok, 0 failed")

            # Certification refuses a verifier with functions replaced after its
            # import, so what a certifying run does differently is shown on a
            # copy of this verifier with its text changed, certifying in an
            # interpreter of its own where nothing is replaced. The copy's
            # selftest says one fixed line, since the real one cannot run
            # inside itself.
            quick_selftest = (
                '    return _selftest_checks(lambda: print("running; this certifying run is one '
                'of its own "\n                                          "checks"))\n',
                '    print("probe checks: 9 ok, 0 failed")\n    return 9\n')

            def certified_by_a_copy(name, edits, case, extra_env=None):
                """Certify case on a copy of this verifier changed by edits, in another interpreter."""
                text = Path(__file__).read_text()
                missing = [old[:60] for old, _new in edits if text.count(old) != 1]
                if missing:
                    return {"error": f"edits that do not apply once: {missing}"}
                for old, new in edits:
                    text = text.replace(old, new)
                here = room / name
                here.mkdir()
                (here / "probe.py").write_text(text)
                script = (
                    "import io, json, os, sys\n"
                    "given = json.loads(os.environ['PROBE_COPY_CALL'])\n"
                    "sys.path.insert(0, given['copy'])\n"
                    "import probe\n"
                    "expected = probe.retained_expectation(given['mapping'], live=True, jobs=3, "
                    "adjudications=given['adjudications'])\n"
                    "stream = io.StringIO()\n"
                    "try:\n"
                    "    results = probe.expect([tuple(given['case'])], live=True, "
                    "jobs=3, tool=probe.Path(given['tool']), expected=expected, timeout=20.0, "
                    "certify=True, stream=stream)\n"
                    "    said = {'certified': results.certified, 'first': results[0]['certified'],"
                    " 'printed': stream.getvalue() or results.report}\n"
                    "except probe.ProbeFailure as exc:\n"
                    "    said = {'failures': list(exc.failures), "
                    "'printed': stream.getvalue() or exc.report}\n"
                    "print(json.dumps(said))\n")
                call = {"copy": str(here), "case": list(case), "tool": str(tool),
                        "mapping": str(source_path), "adjudications": str(approved)}
                done = subprocess.run(
                    [sys.executable, "-B", "-c", script], capture_output=True, text=True,
                    timeout=180, env={**os.environ, **(extra_env or {}),
                                      "PROBE_COPY_CALL": json.dumps(call)})
                try:
                    return json.loads(done.stdout.strip().splitlines()[-1])
                except (ValueError, IndexError):
                    return {"error": f"exit {done.returncode}: {done.stderr[-800:]}"}

            # A private copy that is not what it was copied from is refused
            # before any run.
            commands_before = log.read_text()
            differing = certified_by_a_copy(
                "verifier-differing-copy",
                [quick_selftest,
                 ('    shutil.copytree(tool.parent / "test", room / "test")\n'
                  '    return copied_tool, copied_comp, _copy_files(room, copied_tool, '
                  'copied_comp)\n',
                  '    shutil.copytree(tool.parent / "test", room / "test")\n'
                  '    with open(copied_comp, "ab") as companion_copy:\n'
                  '        companion_copy.write(b"#")\n'
                  '    return copied_tool, copied_comp, _copy_files(room, copied_tool, '
                  'copied_comp)\n')],
                ("target-check", knobs(), bite))
            check("certify-refused-when-the-private-copy-differs",
                  any("is not the tool, companion and test tree it was copied from" in failure
                      for failure in differing.get("failures", ())), repr(differing)[:1500])
            check("certify-refused-for-a-differing-copy-runs-nothing",
                  log.read_text() == commands_before, log.read_text()[len(commands_before):])

            # A failing verifier selftest refuses certification before any suite runs.
            commands_before = log.read_text()
            try:
                expect([("target-check", knobs(), bite)], live=True, jobs=3, tool=tool,
                       expected=certifying, timeout=20.0, certify=True, stream=io.StringIO(),
                       verifier=failing_stand_in)
            except ProbeFailure as exc:
                check("certify-refused-when-the-verifier-selftest-fails",
                      exc.failures == ("the verifier's own selftest failed: a stand-in check",)
                      and log.read_text() == commands_before, repr(exc.failures))
            else:
                check("certify-refused-when-the-verifier-selftest-fails", False,
                      "expect returned")

            stream = io.StringIO()
            standing_in = expect([("target-check", knobs(), bite)], live=True, jobs=3, tool=tool,
                                 expected=certifying, timeout=20.0, certify=True, stream=stream,
                                 verifier=verifier)
            check("certify-with-a-stand-in-certifies-nothing",
                  standing_in.certified is False and standing_in[0]["certified"] is False
                  and stream.getvalue().startswith(
                      "certifying: no; the verifier's own selftest was a stand-in the caller "
                      "supplied, so this run certifies nothing\n")
                  and ("verifier selftest: a stand-in the caller supplied said: running; this "
                       "certifying run is one of its own checks\n") in stream.getvalue(),
                  stream.getvalue()[:600])

            # The certifying path itself, on a copy whose selftest says one fixed
            # line: every run of it, the listing too, uses the private copy, and
            # the record names what the run rests on.
            paths_log = room / "paths.log"
            said = certified_by_a_copy(
                "verifier-certifying", [quick_selftest],
                ("target-check", knobs(), knobs(FAILING=["target-check", "other-check"]),
                 {"other-check": "the scripted failure names both"}),
                extra_env={"PROBE_SYNTHETIC_PATHS": str(paths_log)})
            printed = said.get("printed") or said.get("error", "")
            ran_from = collections.Counter(
                paths_log.read_text().splitlines() if paths_log.exists() else ())
            copy_line = next((line for line in printed.splitlines()
                              if line.startswith("clean run from a private copy: ")), "")
            copy_room = copy_line[len("clean run from a private copy: "):].split(":")[0]
            check("certified-clean-run-uses-a-private-copy",
                  copy_line.endswith(f": tool sha256 {tool_digest}, companion sha256 "
                                     f"{hashlib.sha256(b'').hexdigest()}, test tree 0 file(s), "
                                     f"digest {hashlib.sha256().hexdigest()}")
                  and copy_room and Path(copy_room) != room
                  and ran_from[str(Path(copy_room) / tool.name)] == 3
                  and ran_from[str(tool)] == 0, repr((copy_line, ran_from)))
            check("certifying-run-records-its-provenance",
                  said.get("certified") is True and said.get("first") is True
                  and printed.startswith("certifying: yes\n")
                  and f"expectation: {source_path} sha256 {certifying.sha256}" in printed
                  and (f"expectation frozen for source sha256 {tool_digest} (commit c0ffee, "
                       "path_in_repo team-necromancy/agent-resume): the tool is that source")
                  in printed
                  and f"adjudications: {approved} sha256 {certifying.adjudications_sha256}"
                  in printed
                  and "verifier selftest: probe checks: 9 ok, 0 failed\n" in printed
                  and f"interpreter: {sys.executable} (Python {platform.python_version()})"
                  in printed
                  and "timeout: 20s for each run" in printed
                  and "--failed checked for: clean\n" in printed
                  and "co-failure other-check: the scripted failure names both" in printed,
                  printed)

            # X4: a function replaced after the import is named, and certification refused.
            real_listing = globals()["judge_listing"]
            globals()["judge_listing"] = lambda run, expected: real_listing(run, expected)
            try:
                gaps = certification_gaps(certifying, True, 3, tool_digest, VERIFIER_LOADED)
            finally:
                globals()["judge_listing"] = real_listing
            check("certify-refused-when-a-function-was-replaced",
                  gaps == ["functions replaced in the verifier after it was imported would run "
                           "code its bytes do not hold: judge_listing"]
                  and certification_gaps(certifying, True, 3, tool_digest, VERIFIER_LOADED)
                  == [], repr(gaps))

            # X2: a suite that exits with a driver it recorded still running is
            # not certified; the verifier ends what it left.
            leaving_room = room / "leaving"
            (leaving_room / "test").mkdir(parents=True)
            leaving_tool = leaving_room / "agent-resume"
            leaving_tool.write_text(runner_text.replace(knobs(), knobs(OWNED_DRIVER="direct")))
            leaving_tool.chmod(0o755)
            (leaving_room / "_agent-resume").write_text("")
            leaving_digest = hashlib.sha256(leaving_tool.read_bytes()).hexdigest()
            leaving_path, leaving_approved = frozen_beside(
                "expected-mapping-leaving", source={**source, "sha256": leaving_digest})
            witness = room / "leaving-groups"
            os.environ["PROBE_SYNTHETIC_GROUPS"] = str(witness)
            try:
                expect([("target-check", knobs(OWNED_DRIVER="direct"),
                         knobs(OWNED_DRIVER="direct", FAILING=["target-check"]))],
                       live=True, jobs=3, tool=leaving_tool,
                       expected=retained_expectation(leaving_path, live=True, jobs=3,
                                                     adjudications=leaving_approved),
                       timeout=20.0, certify=True, stream=io.StringIO(), verifier=verifier)
                left_refusal = None
            except ProbeFailure as exc:
                left_refusal = exc.failures
            finally:
                os.environ.pop("PROBE_SYNTHETIC_GROUPS", None)
            named = ([tuple(line.split()) for line in witness.read_text().splitlines()]
                     if witness.exists() else [])
            still = [int(pid) for pid, born in named
                     if (_proc_identity(int(pid)) or (None, None, None))[2] == born]
            for pid in still:
                with contextlib.suppress(OSError):
                    os.kill(pid, signal.SIGKILL)
            check("certify-refused-when-the-suite-left-recorded-processes",
                  bool(left_refusal) and "exited with processes it recorded still running"
                  in left_refusal[0] and bool(named) and not still,
                  repr((left_refusal, named, still)))
            try:
                expect([("target-check", knobs(), bite)], live=True, jobs=2, tool=tool,
                       expected=retained, timeout=20.0, stream=io.StringIO())
            except ProbeFailure as exc:
                check("expect-refuses-an-expectation-for-another-selection",
                      exc.failures == ("the expectation is for live-jobs-3; this run is "
                                       "live-jobs-2",), repr(exc.failures))
            else:
                check("expect-refuses-an-expectation-for-another-selection", False,
                      "expect returned")

            # A certifying run whose suite writes into its own copy of the test
            # tree is refused as a run writing into the original is, and the
            # original is not where it wrote.
            leaking = knobs(WRITE_BESIDE="test/leak")
            tool.write_text(runner_text.replace(knobs(), leaking))
            leak_digest = hashlib.sha256(tool.read_bytes()).hexdigest()
            leak_path, leak_approved = frozen_beside(
                "expected-mapping-leaking", source={**source, "sha256": leak_digest})
            try:
                expect([("target-check", leaking,
                         knobs(WRITE_BESIDE="test/leak", FAILING=["target-check"]))],
                       live=True, jobs=3, tool=tool, timeout=20.0, certify=True,
                       expected=retained_expectation(leak_path, live=True, jobs=3,
                                                     adjudications=leak_approved),
                       stream=io.StringIO(), verifier=verifier)
                wrote = "expect returned"
            except ProbeFailure as exc:
                wrote = exc.failures
            check("certify-run-writing-into-its-copy-refused",
                  isinstance(wrote, tuple) and len(wrote) == 1
                  and "changed while the suites ran" in wrote[0]
                  and "/clean/test/leak" in wrote[0]
                  and not (room / "test" / "leak").exists(), repr(wrote))
            tool.write_text(runner_text)

            # A runner that writes into the test tree beside itself, as a unit
            # leaking into the corpus would, changes what the report describes.
            leaking = knobs(WRITE_BESIDE="test/leak")
            tool.write_text(runner_text.replace(knobs(), leaking))
            _results, exc = probe([("target-check", leaking,
                                    knobs(WRITE_BESIDE="test/leak", FAILING=["target-check"]))])
            leak = room / "test" / "leak"
            check("changed-inputs-refused", exc is not None
                  and exc.failures == ("the tool, its companion, the verifier or the test "
                                       f"tree changed while the suites ran ({leak}); no "
                                       "verdict here describes what is on disk now",)
                  and f"changed while the suites ran: {leak}" in exc.report,
                  repr(exc and exc.failures))
            check("changed-inputs-keep-the-discarded-output", exc is not None
                  and [outcome.label for outcome in exc.outcomes] == ["clean", "case 1"]
                  and all(outcome.reasons == ("discarded: the inputs changed while it ran",)
                          for outcome in exc.outcomes)
                  and "--- clean: " in exc.report and "--- case 1: " in exc.report
                  and "FAIL  target-check" in exc.report,
                  exc.report[-800:] if exc else "expect returned")
            leak.unlink(missing_ok=True)
            _results, exc = probe([("target-check", leaking,
                                    knobs(WRITE_BESIDE="test/leak", FAILING=["target-check"]))],
                                  filtering=True)
            check("changed-inputs-keep-the-discarded-failed-only-output", exc is not None
                  and [outcome.label for outcome in exc.outcomes]
                  == ["clean", "clean --failed", "case 1", "case 1 --failed"]
                  and "--- clean --failed: " in exc.report
                  and "--- case 1 --failed: " in exc.report,
                  exc.report[-800:] if exc else "expect returned")
            leak.unlink(missing_ok=True)
            tool.write_text(runner_text)
        finally:
            if was_log is None:
                os.environ.pop("PROBE_SYNTHETIC_LOG", None)
            else:
                os.environ["PROBE_SYNTHETIC_LOG"] = was_log

    # A suite that does not end by itself leaves the groups it started in
    # sessions of their own to the verifier. The stand-in's driver and its
    # terminal child stand for the real suite's completion drive.
    was_groups = os.environ.pop("PROBE_SYNTHETIC_GROUPS", None)
    with tempfile.TemporaryDirectory() as td:

        def recorded_driver_run(label, timeout, **values):
            """Run the stand-in with an owned driver; returns the run, the named
            processes and the ones still running, which are then ended here so a
            failing rule leaves nothing behind."""
            here = Path(td) / label
            here.mkdir()
            copy = here / "agent-resume"
            copy.write_text(runner_text.replace(knobs(), knobs(**values)))
            copy.chmod(0o755)
            witness = here / "groups"
            os.environ["PROBE_SYNTHETIC_GROUPS"] = str(witness)
            try:
                run = run_suite(suite_argv(copy, live=True), timeout=timeout)
            finally:
                os.environ.pop("PROBE_SYNTHETIC_GROUPS", None)
            named = ([tuple(line.split()) for line in witness.read_text().splitlines()]
                     if witness.exists() else [])
            alive = [int(pid) for pid, started in named if _started(int(pid)) == started]
            for pid in alive:
                with contextlib.suppress(OSError):
                    os.kill(pid, signal.SIGKILL)
            return run, named, alive

        run, named, alive = recorded_driver_run("direct", 3.0, OWNED_DRIVER="direct", SLEEP=30)
        check("stopped-suite-ends-its-recorded-groups",
              run.returncode is None and len(named) == 2 and not alive,
              f"returncode {run.returncode}, named {named}, still running {alive}")
        run, named, alive = recorded_driver_run("nested", 3.0, OWNED_DRIVER="nested", SLEEP=30)
        check("stopped-suite-ends-the-groups-its-workers-recorded",
              run.returncode is None and len(named) == 2 and not alive,
              f"returncode {run.returncode}, named {named}, still running {alive}")
        run, named, alive = recorded_driver_run("signalled", 20.0, OWNED_DRIVER="direct",
                                                SIGNAL="SIGKILL")
        check("signalled-suite-has-its-recorded-groups-ended",
              run.returncode == -signal.SIGKILL and len(named) == 2 and not alive,
              f"returncode {run.returncode}, named {named}, still running {alive}")

        # A pid written beside another start time is somebody else's process.
        stranger = subprocess.Popen(["sleep", "30"], start_new_session=True)
        try:
            record = Path(td) / "stranger-groups"
            record.write_text(f"{stranger.pid} 1\n")
            said = end_recorded_groups(record, _started(stranger.pid) or "0", grace=0.5)
            # A signal takes effect when the process next runs, so the
            # stranger is asked about a second later rather than at once.
            with contextlib.suppress(subprocess.TimeoutExpired):
                stranger.wait(timeout=1.0)
            check("recorded-pid-with-another-start-time-left-alone",
                  stranger.poll() is None and said == ([], [], []),
                  f"returncode {stranger.poll()}, said {said}")
        finally:
            stranger.kill()
            stranger.wait()

        # X1: a process that started before the suite is never signalled,
        # whatever a record says. The only process recorded here is this
        # test's own sleeper, started before the suite.
        older = subprocess.Popen(["sleep", "60"], start_new_session=True)
        try:
            older_line = f"{older.pid} {_started(older.pid)}"
            # Some clock ticks apart, so the sleeper is older than the suite
            # and not only in the same tick.
            time.sleep(0.1)
            run, named, alive = recorded_driver_run("older", 3.0, RECORD_LINES=[older_line],
                                                    SLEEP=30)
            with contextlib.suppress(subprocess.TimeoutExpired):
                older.wait(timeout=1.0)
            check("recorded-process-older-than-the-suite-left-alone",
                  run.returncode is None and older.poll() is None
                  and "refused to signal" in run.stderr
                  and f"pid {older.pid}, it did not start after the suite" in run.stderr,
                  f"returncode {run.returncode}, older {older.poll()}, stderr "
                  f"{run.stderr[-400:]!r}")
        finally:
            older.kill()
            older.wait()

        # The guards that hold whatever a record says: read, never signalled.
        sleeper = subprocess.Popen(["sleep", "30"], start_new_session=True)
        # A process of this test's own named systemd, which no other guard
        # covers: the machine's own managers are pid 1 or this run's ancestors.
        named_systemd = Path(td) / "systemd"
        named_systemd.symlink_to(shutil.which("sleep"))
        imitation = subprocess.Popen([str(named_systemd), "30"], start_new_session=True)
        # Named only once it has executed; until then it is a copy of this process.
        deadline = time.monotonic() + 10
        while (_read_text(f"/proc/{imitation.pid}/comm") != "systemd"
               and time.monotonic() < deadline):
            time.sleep(0.01)
        try:
            managers = [int(entry.name) for entry in Path("/proc").iterdir()
                        if entry.name.isdigit() and _read_text(entry / "comm") == "systemd"]
            guarded = {
                "pid 1": never_signalled(1) == "pid 1",
                "the verifier": never_signalled(os.getpid()) == "the verifier itself",
                "its parent": never_signalled(os.getppid()) == "an ancestor of the verifier",
                # Asked of each one still named systemd when it is asked: other
                # runs' processes come and go between the scan and the question.
                "every systemd process": all(
                    never_signalled(pid) or _read_text(f"/proc/{pid}/comm") != "systemd"
                    for pid in managers),
                "a process named systemd": never_signalled(imitation.pid) == "a systemd process",
                "not a process this test started": never_signalled(sleeper.pid) is None,
            }
        finally:
            for started in (sleeper, imitation):
                started.kill()
                started.wait()
        wrong = sorted(name for name, ok in guarded.items() if not ok)
        check("never-signalled-guards-the-verifier-its-ancestors-and-the-manager",
              not wrong, ", ".join(wrong) + f"; {len(managers)} systemd process(es)")

        # X2: a suite that exits by itself with a recorded driver still
        # running has it ended, and says so.
        run, named, alive = recorded_driver_run("exited", 20.0, OWNED_DRIVER="direct")
        check("suite-exiting-with-recorded-processes-running-has-them-ended",
              run.returncode == 0 and len(named) == 2 and not alive
              and sorted(run.left_running) == sorted(int(pid) for pid, _born in named)
              and "ended processes the suite recorded and left running" in run.stderr,
              f"returncode {run.returncode}, left_running {run.left_running}, named {named}, "
              f"alive {alive}, stderr {run.stderr[-300:]!r}")
        run, named, alive = recorded_driver_run("clean", 20.0)
        check("self-exiting-suite-that-recorded-nothing-has-nothing-ended",
              run.returncode == 0 and run.left_running == () and "probe:" not in run.stderr,
              f"returncode {run.returncode}, left_running {run.left_running}, "
              f"stderr {run.stderr[-300:]!r}")

        # X3: what a record file can be other than a list of live groups.
        missing = Path(td) / "no-such-record"
        malformed = Path(td) / "malformed-record"
        malformed.write_text("not a group\n12\n")
        first, second = Path(td) / "record-a", Path(td) / "record-b"
        first.write_text(f"record {second}\n")
        second.write_text(f"record {first}\n")
        try:
            missing_said = end_recorded_groups(missing, "0")
        except OSError as exc:
            missing_said = f"raised {exc!r}"
        check("missing-record-file-ends-nothing", missing_said == ([], [], []),
              repr(missing_said))
        malformed_said = end_recorded_groups(malformed, "0")
        check("malformed-record-lines-are-refused-and-named",
              malformed_said[:2] == ([], []) and len(malformed_said[2]) == 2
              and all("is not a recorded group" in sentence for sentence in malformed_said[2]),
              repr(malformed_said))
        try:
            cycled = end_recorded_groups(first, "0")
        except RecursionError:
            cycled = "the two files kept naming each other"
        check("record-files-are-read-once", cycled == ([], [], []), repr(cycled))
        real_ending = globals()["end_recorded_groups"]
        globals()["end_recorded_groups"] = lambda record, started, *rest: ([], [4242], [])
        try:
            run, _named, _alive = recorded_driver_run("leftover", 20.0)
        finally:
            globals()["end_recorded_groups"] = real_ending
        check("processes-left-running-are-named-on-stderr",
              "still running after the suite and the groups it recorded were ended: 4242"
              in run.stderr, repr(run.stderr[-300:]))

        # X5: the variable is named after the file the tool is, as the tool names it.
        named_file = Path(td) / "agent-resume"
        named_file.write_text("")
        other_name = Path(td) / "other-name"
        other_name.symlink_to(named_file)
        check("owned-groups-variable-is-named-from-the-resolved-tool",
              owned_groups_variable(other_name) == "AGENT_RESUME_SELFTEST_OWNED_GROUPS",
              owned_groups_variable(other_name))
    if was_groups is not None:
        os.environ["PROBE_SYNTHETIC_GROUPS"] = was_groups

    failed = [(name, detail) for name, ok, detail in checks if not ok]
    print(f"probe checks: {len(checks) - len(failed)} ok, {len(failed)} failed")
    if failed:
        raise ProbeFailure([f"{name}: {detail}" for name, detail in failed])
    return len(checks)
