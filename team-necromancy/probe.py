"""Mutation-test API for agent-resume.

Import this module from a mutation script and call ``expect``. The clean suite is
run once, every mutation receives its own copy of the tool and corpus, and the
mutants run with at most four suites active at once.

    import probe

    probe.expect([
        ("check-id", "old text", "new text"),
        ("other-id", "old text", "new text", {"second-id": "why it fails too"}),
    ], live=True, jobs=2, expected=retained_mapping)

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
expected-mapping-v1 file the reviewer keeps, with the file's hash and the
source it was frozen for, and ``expect`` writes both into its report so a
verification record says which expectation judged it.

Without ``expected`` the verifier has no independent population. The clean
listing stands in for it, which checks that the listing, the clean run and the
mutants agree with each other and nothing more. That mode does not certify a
change; the unit's retained expectation does.
"""

import collections
import concurrent.futures
import contextlib
import dataclasses
import hashlib
import io
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
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
    is that source or a candidate that differs from it.
    """

    selection: str
    mapping: dict
    path: object = None
    sha256: str = ""
    source: dict = dataclasses.field(default_factory=dict)


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


def retained_expectation(path, *, live=False, jobs=1):
    """Read the expected mapping for one selection from a frozen expected-mapping-v1 file.

    The file is the reviewer's record: a schema name, the modes it covers, the
    source it was frozen for, and rows that each carry an id, a description,
    emitted_in with the selections a clean run reports the row in and
    not_emitted_in with the ones that withhold it. The mapping for a selection
    is every row emitted in it. A selection the file does not cover, a row
    that is both emitted and withheld, or an id named twice is refused, since
    an expectation that is unsure of its own rows decides nothing. Nothing
    here reads the tested tool.
    """
    path = Path(path)
    data = path.read_bytes()
    try:
        record = json.loads(data)
    except ValueError as exc:
        raise ProbeFailure([f"{path} is not JSON: {exc}"])
    if not isinstance(record, dict) or record.get("schema") != "expected-mapping-v1":
        raise ProbeFailure([f"{path} does not carry schema expected-mapping-v1"])
    selection = selection_name(live, jobs)
    modes = record.get("modes") or {}
    if selection not in modes:
        raise ProbeFailure([f"{path} has no selection {selection}; it has: "
                            + ", ".join(sorted(modes))])
    pairs, failures = [], []
    for at, row in enumerate(record.get("rows") or (), 1):
        emitted = selection in (row.get("emitted_in") or {})
        withheld = selection in (row.get("not_emitted_in") or {})
        if emitted and withheld:
            failures.append(f"row {at} ({row.get('id')!r}) is both emitted and "
                            f"withheld in {selection}")
        elif emitted:
            pairs.append((row.get("id"), row.get("description")))
    if failures:
        raise ProbeFailure(failures)
    return Expectation(selection, _normalize_expected(pairs), path,
                       hashlib.sha256(data).hexdigest(),
                       dict(record.get("frozen_for_source") or {}))


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
        if not all(isinstance(value, str) and value for value in item):
            failures.append(f"expected entry {at} has an empty or non-string value")
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


def expect(cases, *, live=False, jobs=1, expected=None, tool=None, timeout=None,
           filtering=False, stream=None):
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

    The successful return is a tuple with one result dictionary per case:
    the check, the mutated file, the ids that failed and the co-failures, with
    the judged outcome. ``ProbeFailure`` carries ``failures``, the printed
    ``report`` and the refused ``outcomes`` with their raw output.
    """
    cases = _normalize_cases(cases)
    tool = Path(TOOL if tool is None else tool)
    comp = tool.parent / f"_{tool.name}"
    stream = sys.stdout if stream is None else stream
    selection = selection_name(live, jobs)
    watched = fingerprints(watched_files(tool, comp))
    tool_digest = watched[tool]
    lines = [f"tool: {tool} sha256 {tool_digest}",
             f"verifier: {VERIFIER} sha256 {watched[VERIFIER]}",
             f"selection: {selection}"]
    if isinstance(expected, Expectation):
        if expected.selection != selection:
            raise ProbeFailure([f"the expectation is for {expected.selection}; "
                                f"this run is {selection}"])
        lines.append(f"expectation: {expected.path} sha256 {expected.sha256}")
        frozen_for = expected.source.get("sha256")
        if frozen_for:
            lines.append(f"expectation frozen for source sha256 {frozen_for}: the tool "
                         + ("is that source" if frozen_for == tool_digest
                            else "differs from it"))
        expected = expected.mapping
    elif expected is not None:
        expected = _normalize_expected(expected)
        lines.append(f"expectation: a supplied mapping of {len(expected)} check(s)")
    else:
        lines.append("expectation: none supplied, so the listing is the population "
                     "and nothing here certifies the tool")

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

    def run_one(path):
        full = run_suite(suite_argv(path, live=live, jobs=jobs), timeout)
        filtered = (run_suite(suite_argv(path, live=live, jobs=jobs, failed=True), timeout)
                    if filtering else None)
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
        runs = batch([tool, *copies], run_one)

    # The report names the bytes it judged. A file that changed while the
    # suites ran, the tool itself or a corpus file a unit wrote into, makes
    # every verdict above describe something no longer on disk.
    after = fingerprints(watched_files(tool, comp))
    changed = sorted(str(item) for item in set(watched) | set(after)
                     if watched.get(item) != after.get(item))
    if changed:
        lines.append("changed while the suites ran: " + ", ".join(changed))
        report = "\n".join(lines)
        print(report, file=stream)
        raise ProbeFailure(["the tool, its companion, the verifier or the test tree "
                            f"changed while the suites ran ({', '.join(changed)}); no "
                            "verdict here describes what is on disk now"], report=report)

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
        })

    if any(outcome.reasons for outcome in outcomes):
        raise _refusal(outcomes, lines, stream)
    print("\n".join(lines), file=stream)
    return tuple(results)


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
    """
    checks = []

    def check(name, ok, detail=""):
        checks.append((name, bool(ok), detail))

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
    outcome = judge_filtering(full, record(filtered, returncode=1), expected)
    outcome = judge_filtering(
        full, record("beta  the second check  (scripted)\nchecks: 2 ok,\n", returncode=1),
        expected)
    check("failed-only-malformed-summary",
          only(outcome.reasons, "malformed summary: 'checks: 2 ok,'"), repr(outcome.reasons))

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
    ):
        try:
            _normalize_cases([case])
        except ProbeFailure as exc:
            check(name, phrase in str(exc), str(exc))
        else:
            check(name, False, "did not raise")
    normalized = _normalize_cases([("a", "old", "new"), ("b", "old", "new", {"c": "why"})])
    check("cases-carry-their-co-failures",
          normalized == (("a", "old", "new", {}), ("b", "old", "new", {"c": "why"})),
          repr(normalized))
    for name, given, phrase in (
        ("expectation-repeating-an-id-refused", [("a", "x"), ("a", "y")], "names 'a' twice"),
        ("expectation-with-empty-value-refused", {"a": ""}, "empty or non-string value"),
        ("empty-expectation-refused", {}, "names no check"),
    ):
        try:
            _normalize_expected(given)
        except ProbeFailure as exc:
            check(name, phrase in str(exc), str(exc))
        else:
            check(name, False, "did not raise")
    check("expectation-from-pairs", _normalize_expected([("a", "x"), ("b", "y")])
          == {"a": "x", "b": "y"})

    # The frozen expected-mapping-v1 file, read per selection.
    def frozen(rows, modes=("read-only", "live-jobs-1"), schema="expected-mapping-v1"):
        return json.dumps({
            "schema": schema,
            "frozen_for_source": {"sha256": "f" * 64},
            "modes": {mode: {} for mode in modes},
            "rows": [{"id": cid, "description": description,
                      "emitted_in": {mode: "MECHANISM" for mode in emitted},
                      "not_emitted_in": {mode: "WITHHELD" for mode in withheld}}
                     for cid, description, emitted, withheld in rows],
        }).encode()

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
              and read_only.path == record_path and read_only.source == {"sha256": "f" * 64},
              repr((read_only, live_one)))
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
        ):
            record_path.write_bytes(text)
            try:
                retained_expectation(record_path, **options)
            except ProbeFailure as exc:
                check(name, phrase in str(exc), str(exc))
            else:
                check(name, False, "did not raise")

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

            record_path = room / "expected-mapping-v1.json"
            record_path.write_bytes(frozen(
                [(cid, description, ("live-jobs-3",), ())
                 for cid, description in SYNTHETIC_EXPECTED.items()],
                modes=("live-jobs-3",)))
            retained = retained_expectation(record_path, live=True, jobs=3)
            stream = io.StringIO()
            results = expect([("target-check", knobs(), bite)], live=True, jobs=3, tool=tool,
                             expected=retained, timeout=20.0, stream=stream)
            tool_digest = hashlib.sha256(tool.read_bytes()).hexdigest()
            check("expect-records-the-expectation-and-tool-identity",
                  results[0]["bit"] == ("target-check",)
                  and f"expectation: {record_path} sha256 {retained.sha256}" in stream.getvalue()
                  and f"tool: {tool} sha256 {tool_digest}" in stream.getvalue()
                  and "selection: live-jobs-3" in stream.getvalue()
                  and f"frozen for source sha256 {'f' * 64}: the tool differs from it"
                  in stream.getvalue(),
                  stream.getvalue())
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
