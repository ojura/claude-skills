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
    ], live=True, jobs=2, expected=retained, certify=True)

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
only with such a retained expectation, adjudications included, that still
reads the same from its files, whose adjudications are all decided and whose
frozen source is the tool under test, and only after the verifier's own
selftest passes; its result says it certified. Every other call is a
development run. Without ``expected`` the clean listing stands in for the
population, which checks that the listing, the clean run and the mutants agree
with each other and nothing more; a bare mapping has no recorded origin. Both
still judge every run strictly, and both return a result that says it
certifies nothing.
"""

import collections
import concurrent.futures
import contextlib
import dataclasses
import hashlib
import io
import json
import os
import platform
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
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

    certified says whether the call asked for certification and had what it
    needs, and report is the record expect printed, so a caller that keeps
    the result keeps the provenance with it.
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


def run_suite(argv, timeout=None):
    """Run one bounded suite in its own group and return its Run.

    The bound is read here rather than bound as a default, so the one constant
    above is what decides it and a caller can lower it without editing this.
    A run that reaches the bound is stopped with its group and comes back with
    returncode None and whatever it had printed.
    """
    timeout = SUITE_TIMEOUT_S if timeout is None else timeout
    with subprocess.Popen(list(argv), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, start_new_session=True) as child:
        try:
            out, err = child.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            end_group(child)
            try:
                out, err = child.communicate(timeout=SUITE_GRACE_S)
            except subprocess.TimeoutExpired:
                out, err = "", ""
            return Run(tuple(argv), None, out, err, timeout)
        return Run(tuple(argv), child.returncode, out, err, timeout)


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
        adjudications = Path(adjudications)
        adjudicated, decided = _json_record(adjudications, "adjudications")
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
        decision = entry.get("decision") if isinstance(entry, dict) else None
        if not (isinstance(decision, str) and decision.startswith(("approved", "refused"))):
            named = entry.get("id") if isinstance(entry, dict) else None
            gaps.append(f"adjudication entry {at}" + (f" ({named!r})" if named else "")
                        + f" is not decided: its decision is {decision!r}")
    return gaps


def certification_gaps(expected, live, jobs, tool_digest):
    """Why expected cannot back a certifying run of this tool; empty when it can.

    A certifying run rests on a retained expectation read from its frozen
    file, with the file's hash, the source it was frozen for, named by sha256,
    commit and path, and the approved adjudications behind it, because those
    are what a later reader checks the record against. The fields an
    Expectation carries are only what it says of itself, so the file and the
    adjudications record are read again here and must give the same bytes,
    the same mapping and the same source, and every adjudication must be
    decided. The plan accepts one exact executable, so the tool must be the
    source the mapping was frozen for; a changed tool needs its own mapping.
    """
    if expected is None:
        return ["no expectation was supplied, so the listing would stand in for "
                "the population"]
    if not isinstance(expected, Expectation):
        return ["the expectation is a bare mapping with no recorded origin; read it "
                "with retained_expectation"]
    gaps = []
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
    # The bytes first: a file that changed since it was read is named as changed,
    # before anything read from it is compared.
    try:
        for path, recorded in ((expected.path, expected.sha256),
                               (expected.adjudications, expected.adjudications_sha256)):
            current = hashlib.sha256(_json_record(path, "a certified input")[0]).hexdigest()
            if current != recorded:
                gaps.append(f"{path} now has sha256 {current}, not the {recorded} the "
                            "expectation was read with")
        if gaps:
            return gaps
        again = retained_expectation(expected.path, live=live, jobs=jobs,
                                     adjudications=expected.adjudications)
        _bytes, decided = _json_record(expected.adjudications, "adjudications")
    except ProbeFailure as exc:
        return gaps + [f"the expectation no longer reads from its files: {failure}"
                       for failure in exc.failures]
    if dict(again.mapping) != dict(expected.mapping):
        gaps.append(f"the mapping in use is not the one {expected.path} gives for "
                    f"{again.selection}")
    if again.source != expected.source:
        gaps.append(f"the frozen source in use is not the one {expected.path} names")
    return gaps + undecided(decided)


# Whether the verifier's own selftest is running, so a certifying expect it
# calls among its own checks does not start it again.
_SELFTEST = {"running": False}


def verifier_selftest():
    """The verifier's own selftest, run before a certifying run, as its summary line.

    Section 5A has the verifier tested before its conclusions are used as
    acceptance, so certification runs it first and refuses when it fails. Its
    output is taken into a buffer; the summary line goes into the record.
    """
    if _SELFTEST["running"]:
        return "running; this certifying run is one of its own checks"
    said = io.StringIO()
    try:
        with contextlib.redirect_stdout(said):
            selftest()
    except ProbeFailure as exc:
        raise ProbeFailure([f"the verifier's own selftest failed: {failure}"
                            for failure in exc.failures])
    return said.getvalue().strip().splitlines()[-1]


def expect(cases, *, live=False, jobs=1, expected=None, tool=None, timeout=None,
           filtering=False, certify=False, stream=None):
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
    unless ``expected`` came from ``retained_expectation`` with its source and
    approved adjudications, reads again from its files as it was read, every
    adjudication is decided, the tool is the source the mapping was frozen for
    and the verifier's own selftest passes. A certifying run always checks the
    clean run's --failed report too, and its record names the inputs with
    their hashes, the interpreter, the timeout, the selftest's result, the runs
    whose --failed output was checked and every co-failure's reason. Without
    it the run judges just as strictly and its result says it certified
    nothing.

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
    timeout = SUITE_TIMEOUT_S if timeout is None else timeout
    selection = selection_name(live, jobs)
    watched = fingerprints(watched_files(tool, comp))
    tool_digest = watched[tool]
    if isinstance(expected, Expectation) and expected.selection != selection:
        raise ProbeFailure([f"the expectation is for {expected.selection}; "
                            f"this run is {selection}"])
    checked_verifier = None
    if certify:
        gaps = certification_gaps(expected, live, jobs, tool_digest)
        if gaps:
            raise ProbeFailure([f"a certifying run needs a retained expectation: {gap}"
                                for gap in gaps])
        checked_verifier = verifier_selftest()
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
    lines.insert(0, "certifying: yes" if certify else
                 "certifying: no; this run certifies nothing about the tool")

    listed = judge_listing(
        run_suite(suite_argv(tool, live=live, jobs=jobs, listing=True), timeout), expected)
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

    # A certifying run checks the clean run's --failed report whatever filtering
    # says, because the plan has --failed tested against the same run.
    def run_one(entry):
        path, filtered_too = entry
        full = run_suite(suite_argv(path, live=live, jobs=jobs), timeout)
        filtered = (run_suite(suite_argv(path, live=live, jobs=jobs, failed=True), timeout)
                    if filtered_too else None)
        return full, filtered

    with tempfile.TemporaryDirectory() as scratch:
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
        runs = batch([(tool, filtering or certify), *((copy, filtering) for copy in copies)],
                     run_one)
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
                     if watched.get(item) != after.get(item))
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
            "certified": certify,
        })

    if any(outcome.reasons for outcome in outcomes):
        raise _refusal(outcomes, lines, stream)
    report = "\n".join(lines)
    print(report, file=stream)
    return Results(results, certify, report)


# What probe's own tests say about the stand-in runner. KNOBS is the block of
# literals in SYNTHETIC_RUNNER, in file order; a test mutation rewrites the
# whole block, so any combination of knobs is one contiguous replacement.
KNOBS = ("FAILING", "SUMMARY", "EXIT", "DROP", "EXTRA", "DUPLICATE", "DESCRIBE",
         "SLEEP", "SIGNAL", "FILTER_LIES", "WRITE_BESIDE")
KNOB_DEFAULTS = {"FAILING": [], "SUMMARY": "exact", "EXIT": None, "DROP": [], "EXTRA": [],
                 "DUPLICATE": [], "DESCRIBE": {}, "SLEEP": 0, "SIGNAL": None,
                 "FILTER_LIES": False, "WRITE_BESIDE": None}
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
    While it runs, a certifying expect among its checks records that it is one
    of them instead of starting the selftest again.
    """
    _SELFTEST["running"] = True
    try:
        return _selftest_checks()
    finally:
        _SELFTEST["running"] = False


def _selftest_checks():
    """The checks selftest runs; see selftest."""
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
                entries=[{"id": "target-check", "decision": "pending review"}])
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
            ):
                commands_before = log.read_text()
                try:
                    expect([("target-check", knobs(), bite)], live=True, jobs=3, tool=tool,
                           expected=given, timeout=20.0, certify=True, stream=io.StringIO())
                except ProbeFailure as exc:
                    check(name, exc.failures == tuple(wanted + gap for gap in gaps)
                          and log.read_text() == commands_before, repr(exc.failures))
                except Exception as exc:
                    check(name, False, f"raised {type(exc).__name__} instead: {exc}")
                else:
                    check(name, False, "expect returned")

            # The form of mapping v1's approved record, field for field, certifies.
            v1_path, v1_approved = frozen_beside("expected-mapping-v1-form", record={})
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
                  certification_gaps(v1_form, True, 3, tool_digest) == [],
                  repr(certification_gaps(v1_form, True, 3, tool_digest)))

            # A failing verifier selftest refuses certification before any suite runs.
            real_selftest = globals()["verifier_selftest"]

            def failing_selftest():
                raise ProbeFailure(["the verifier's own selftest failed: a check"])

            commands_before = log.read_text()
            globals()["verifier_selftest"] = failing_selftest
            try:
                expect([("target-check", knobs(), bite)], live=True, jobs=3, tool=tool,
                       expected=certifying, timeout=20.0, certify=True, stream=io.StringIO())
            except ProbeFailure as exc:
                check("certify-refused-when-the-verifier-selftest-fails",
                      exc.failures == ("the verifier's own selftest failed: a check",)
                      and log.read_text() == commands_before, repr(exc.failures))
            else:
                check("certify-refused-when-the-verifier-selftest-fails", False,
                      "expect returned")
            finally:
                globals()["verifier_selftest"] = real_selftest

            stream = io.StringIO()
            results = expect([("target-check", knobs(), knobs(FAILING=["target-check",
                                                                        "other-check"]),
                               {"other-check": "the scripted failure names both"})],
                             live=True, jobs=3, tool=tool, expected=certifying, timeout=20.0,
                             certify=True, stream=stream)
            printed = stream.getvalue()
            check("certifying-run-records-its-provenance",
                  results.certified is True and results[0]["certified"] is True
                  and printed.startswith("certifying: yes\n")
                  and f"expectation: {source_path} sha256 {certifying.sha256}" in printed
                  and (f"expectation frozen for source sha256 {tool_digest} (commit c0ffee, "
                       "path_in_repo team-necromancy/agent-resume): the tool is that source")
                  in printed
                  and f"adjudications: {approved} sha256 {certifying.adjudications_sha256}"
                  in printed
                  and "verifier selftest: running; this certifying run is one of its own checks"
                  in printed
                  and f"interpreter: {sys.executable} (Python {platform.python_version()})"
                  in printed
                  and "timeout: 20s for each run" in printed
                  and "--failed checked for: clean\n" in printed
                  and "co-failure other-check: the scripted failure names both" in printed,
                  printed)
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

    failed = [(name, detail) for name, ok, detail in checks if not ok]
    print(f"probe checks: {len(checks) - len(failed)} ok, {len(failed)} failed")
    if failed:
        raise ProbeFailure([f"{name}: {detail}" for name, detail in failed])
    return len(checks)
