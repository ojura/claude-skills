"""Mutation-test API for agent-resume.

Import this module from a mutation script and call ``expect``. The clean suite is
run once, every mutation receives its own copy of the tool and corpus, and the
mutants run with at most four suites active at once.

    import probe

    probe.expect([
        ("check-id", "old text", "new text"),
        ("another-check", "old text", "new text"),
    ])

A case succeeds only when its exact expected check newly fails. ``expect`` raises
``ProbeFailure`` when the clean suite fails, a mutation cannot be applied, a
mutant crashes, no check bites, or only unrelated checks bite. Callers cannot
mistake redirected output for success because success is an ordinary return and
failure is an exception.
"""

import concurrent.futures
import contextlib
import io
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
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


class ProbeFailure(AssertionError):
    """One or more requested mutation checks were not proved."""

    def __init__(self, failures, report=""):
        self.failures = tuple(failures)
        self.report = report
        super().__init__("mutation probe failed: " + "; ".join(self.failures))


def parse_verdicts(stdout, valid):
    """Parse one failed-only report without treating detail lines as check IDs."""
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    summaries = [line for line in lines if line.startswith("checks: ")]
    if not summaries:
        return None, "the suite produced no summary"
    summary = summaries[-1]
    match = re.fullmatch(r"checks: (\d+) ok, (\d+) failed", summary)
    if not match:
        return None, f"the suite produced an unreadable summary: {summary}"
    failed_ids = {
        check_id
        for line in lines
        if line != summary
        for check_id, description in valid.items()
        if line.startswith(f"{check_id}  {description}")
    }
    expected = int(match.group(2))
    if len(failed_ids) != expected:
        return None, (
            f"the suite reported {expected} failures but "
            f"{len(failed_ids)} known check IDs were readable"
        )
    return failed_ids, summary


def interpret_run(run, valid):
    """Read one suite process without hiding an early exit or its stderr."""
    parsed, said = parse_verdicts(run.stdout, valid)
    stderr = run.stderr.strip()
    if parsed is None:
        if "no summary" in said:
            output = stderr or run.stdout.strip() or "no output"
            return None, f"the suite did not finish:\n{output}"
        return None, f"{said}\n{stderr}" if stderr else said
    ended = parsed & ENDED_CHECKS
    if ended:
        output = "\n".join(part for part in (stderr, run.stdout.strip()) if part)
        return None, (
            "the suite ended before every unit reported"
            + (f":\n{output}" if output else "")
        )
    return parsed, f"{said}\nstderr:\n{stderr}" if stderr else said


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
    """Run one bounded suite in its own group. Returns (code, out, err).

    `code` is None when the suite ran out of time, which callers report as
    having produced no verdict rather than as any kind of result. The bound is
    read here rather than bound as a default, so the one constant above is what
    decides it and a caller can lower it without editing this.
    """
    timeout = SUITE_TIMEOUT_S if timeout is None else timeout
    with subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, start_new_session=True) as child:
        try:
            out, err = child.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            end_group(child)
            try:
                out, err = child.communicate(timeout=SUITE_GRACE_S)
            except subprocess.TimeoutExpired:
                out, err = "", ""
            return None, out, err
        return child.returncode, out, err


def verdicts(path, live, valid):
    """Return ({failed check ids}, summary), or (None, reason)."""
    argv = [sys.executable, str(path), "--selftest", "--failed"]
    if live:
        argv.append("--live")
    code, out, err = run_suite(argv)
    if code is None:
        return None, (f"the suite did not finish within {SUITE_TIMEOUT_S:g}s; "
                      "its process group was stopped")
    return interpret_run(
        subprocess.CompletedProcess(argv, code, out, err), valid)


def verdict_batch(clean_path, mutant_paths, live, valid, run=verdicts):
    """Run the clean suite once and each independent mutant, with a fixed limit."""
    paths = [clean_path, *mutant_paths]
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=min(MAX_PARALLEL_SUITES, len(paths))
    ) as pool:
        futures = [pool.submit(run, path, live, valid) for path in paths]
        clean = futures[0].result()
        mutants = [future.result() for future in futures[1:]]
    return clean, mutants


def known_checks(path, live):
    """Return {check id: description} from the suite's own listing."""
    argv = [sys.executable, str(path), "--selftest", "--list"]
    if live:
        argv.append("--live")
    code, out, _err = run_suite(argv)
    if code is None:
        raise ProbeFailure([
            f"the check listing did not finish within {SUITE_TIMEOUT_S:g}s, so no "
            "case can be judged against it"])
    rows = (
        line.strip().partition("  ")
        for line in out.splitlines()
        if line.strip()
    )
    return {
        check_id: description.lstrip()
        for check_id, separator, description in rows
        if separator
    }


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


def _assess_case(expected, before, after, after_said):
    """Return report lines, failure, and newly failed checks for one mutant."""
    if after is None:
        lines = [
            f"with the mutation applied, {after_said}",
            "a crash is not a bite: the check must fail without stopping "
            "the suite from answering",
        ]
        return lines, f"{expected!r} produced no complete verdict", ()

    bit = tuple(sorted(after - before))
    healed = tuple(sorted(before - after))
    lines = [f"probed: {after_said}"]
    if healed:
        lines.append("stopped failing:")
        lines.extend(f"  {check_id}" for check_id in healed)
    if not bit:
        lines.append("nothing bit; every check passed with this mutation")
        return lines, f"{expected!r} did not bite", bit

    lines.append("bit:")
    lines.extend(f"  {check_id}" for check_id in bit)
    if expected not in bit:
        lines.append(
            f"{expected!r} did not bite; the failures above are unrelated "
            "to this mutation"
        )
        return lines, f"{expected!r} did not bite", bit

    others = [check_id for check_id in bit if check_id != expected]
    if others:
        lines.append(
            f"{expected!r} bit, and {len(others)} additional check(s) also failed"
        )
    return lines, None, bit


def _normalize_cases(cases):
    """Validate and freeze an iterable of (check, old, new) triples."""
    normalized = []
    failures = []
    for at, case in enumerate(cases, 1):
        if not isinstance(case, (tuple, list)) or len(case) != 3:
            failures.append(f"case {at} is not a (check, old, new) triple")
            continue
        expected, old, new = case
        if not all(isinstance(value, str) and value for value in case):
            failures.append(f"case {at} contains an empty or non-string value")
            continue
        if old == new:
            failures.append(f"case {at} replaces text with itself")
            continue
        normalized.append((expected, old, new))
    if not normalized and not failures:
        failures.append("no mutation cases were supplied")
    if failures:
        raise ProbeFailure(failures)
    return tuple(normalized)


def expect(cases, *, live=False, stream=None):
    """Run expected mutations and raise when any expected check does not bite.

    ``cases`` is an iterable of ``(check_id, old_text, new_text)`` triples. The
    successful return is a tuple containing one result dictionary per case.
    ``ProbeFailure`` carries ``failures`` and the printed ``report``.
    """
    cases = _normalize_cases(cases)
    stream = sys.stdout if stream is None else stream
    valid = known_checks(TOOL, live)
    failures = []

    unknown = [(at, expected) for at, (expected, _old, _new)
               in enumerate(cases, 1) if expected not in valid]
    if unknown:
        examples = ", ".join(sorted(valid)[:5])
        failures.extend(
            f"case {at}: {expected!r} names no check; examples: {examples}"
            for at, expected in unknown
        )
        raise ProbeFailure(failures)

    prepared = []
    for at, (expected, old, new) in enumerate(cases, 1):
        target, reason = mutation_target(old, (TOOL, COMP))
        if reason:
            failures.append(f"case {at}: {reason}")
        else:
            prepared.append((expected, old, new, target))
    if failures:
        raise ProbeFailure(failures)

    with tempfile.TemporaryDirectory() as scratch:
        copies = []
        for at, (_expected, old, new, target) in enumerate(prepared, 1):
            room = Path(scratch) / f"case-{at}"
            room.mkdir()
            copy = room / TOOL.name
            shutil.copy2(TOOL, copy)
            shutil.copy2(COMP, room / COMP.name)
            mutated = room / target.name
            mutated.write_text(target.read_text().replace(old, new, 1))
            shutil.copytree(TOOL.parent / "test", room / "test")
            copies.append(copy)

        (before, before_said), after_runs = verdict_batch(
            TOOL, copies, live, valid
        )

    if before is None:
        raise ProbeFailure([f"clean suite: {before_said}"])
    if before:
        raise ProbeFailure([
            "the clean suite already fails: " + ", ".join(sorted(before))
        ])

    report_lines = [f"clean: {before_said}"]
    results = []
    for at, ((expected, _old, _new, target), (after, after_said)) in enumerate(
        zip(prepared, after_runs), 1
    ):
        if len(prepared) > 1:
            report_lines.extend(["", f"case {at}: expected {expected} ({target.name})"])
        lines, failure, bit = _assess_case(expected, before, after, after_said)
        indent = "  " if len(prepared) > 1 else ""
        report_lines.extend(indent + line for line in lines)
        if failure:
            failures.append(f"case {at}: {failure}")
        results.append({
            "check": expected,
            "target": target,
            "bit": bit,
        })

    report = "\n".join(report_lines)
    print(report, file=stream)
    if failures:
        raise ProbeFailure(failures, report=report)
    return tuple(results)


def selftest():
    """Exercise target selection, verdict reading, concurrency, and raising."""
    checks = []
    source = Path(__file__).read_text()
    checks.append((
        "no-command-line-interface",
        "def " + "main(" not in source and "sys." + "argv" not in source
        and not Path(__file__).with_suffix("").exists(),
        "a probe entry point remains",
    ))
    with tempfile.TemporaryDirectory() as td:
        left, right = Path(td) / "agent-resume", Path(td) / "_agent-resume"

        def target_check(name, left_text, right_text, target, phrase, absent=""):
            left.write_text(left_text)
            right.write_text(right_text)
            got, objection = mutation_target("needle", (left, right))
            said = objection or ""
            ok = (got == target and phrase in said
                  and (not absent or absent not in said))
            checks.append((name, ok, objection or str(got)))

        target_check("zero", "plain", "plain", None, "was not found", "narrow it")
        target_check("one-main", "needle", "plain", left, "")
        target_check("one-completion", "plain", "needle", right, "")
        target_check("many-one-file", "needle needle", "plain", None,
                     "appears 2 times", "was not found")
        target_check("many-across-files", "needle", "needle", None,
                     "agent-resume: 1, _agent-resume: 1", "was not found")

    parsed, summary = parse_verdicts(
        "target-check  description  (first detail line\n"
        "second detail line)\nchecks: 4 ok, 1 failed\n",
        {"target-check": "description", "other-check": "other description"},
    )
    checks.append(("multiline-detail", parsed == {"target-check"}, summary))
    parsed, reason = parse_verdicts(
        "not-a-check  continuation\nchecks: 4 ok, 1 failed\n",
        {"target-check": "description"},
    )
    checks.append(("unknown-failure-line", parsed is None
                   and "0 known check IDs" in reason, reason))

    ended_description = "no unit ended the read-only run before it reported"
    parsed, reason = interpret_run(
        subprocess.CompletedProcess(
            [], 1,
            stdout=(f"every-unit-reported  {ended_description}  "
                    "(fixture crashed)\nchecks: 4 ok, 1 failed\n"),
            stderr="Traceback (most recent call last):\nFixtureError: broken",
        ),
        {"every-unit-reported": ended_description},
    )
    checks.append(("crash-trace-kept", parsed is None
                   and "Traceback (most recent call last)" in reason
                   and "FixtureError: broken" in reason, reason))

    lines, failure, bit = _assess_case(
        "target-check", set(), set(), "checks: 5 ok, 0 failed"
    )
    checks.append(("non-bite-fails", failure == "'target-check' did not bite"
                   and not bit and "nothing bit" in " ".join(lines), failure))
    _lines, failure, bit = _assess_case(
        "target-check", set(), {"other-check"}, "checks: 4 ok, 1 failed"
    )
    checks.append(("unrelated-bite-fails", failure == "'target-check' did not bite"
                   and bit == ("other-check",), failure))
    _lines, failure, bit = _assess_case(
        "target-check", set(), {"target-check"}, "checks: 4 ok, 1 failed"
    )
    checks.append(("expected-bite-passes", failure is None
                   and bit == ("target-check",), failure))
    try:
        _normalize_cases([])
    except ProbeFailure as exc:
        empty_raised = "no mutation cases" in str(exc)
    else:
        empty_raised = False
    checks.append(("empty-request-raises", empty_raised, "did not raise"))

    real_tool, real_comp = globals()["TOOL"], globals()["COMP"]
    real_known = globals()["known_checks"]
    real_batch = globals()["verdict_batch"]
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        fake_tool, fake_comp = root / "agent-resume", root / "_agent-resume"
        fake_tool.write_text("old")
        fake_comp.write_text("completion")
        (root / "test").mkdir()
        globals()["TOOL"], globals()["COMP"] = fake_tool, fake_comp
        globals()["known_checks"] = lambda path, live: {"target-check": "description"}
        globals()["verdict_batch"] = lambda *args, **kwargs: (
            (set(), "checks: 5 ok, 0 failed"),
            [(set(), "checks: 5 ok, 0 failed")],
        )
        try:
            try:
                expect([
                    ("target-check", "old", "new"),
                ], stream=io.StringIO())
            except ProbeFailure as exc:
                public_raised = (
                    exc.failures == ("case 1: 'target-check' did not bite",)
                )
            else:
                public_raised = False
            checks.append(("public-expect-raises-on-non-bite", public_raised,
                           "expect returned normally"))

            globals()["verdict_batch"] = lambda *args, **kwargs: (
                (set(), "checks: 5 ok, 0 failed"),
                [({"target-check"}, "checks: 4 ok, 1 failed")],
            )
            result = expect([
                ("target-check", "old", "new"),
            ], stream=io.StringIO())
            checks.append(("public-expect-returns-on-bite",
                           result[0]["bit"] == ("target-check",), repr(result)))
        finally:
            globals()["TOOL"], globals()["COMP"] = real_tool, real_comp
            globals()["known_checks"] = real_known
            globals()["verdict_batch"] = real_batch

    def overlap(mutant_count):
        expected = min(MAX_PARALLEL_SUITES, mutant_count + 1)
        state = {"active": 0, "maximum": 0, "calls": []}
        lock = threading.Lock()
        release = threading.Event()

        def fake(path, _live, _valid):
            with lock:
                state["active"] += 1
                state["maximum"] = max(state["maximum"], state["active"])
                state["calls"].append(path)
                if state["maximum"] == expected:
                    release.set()
            release.wait(2)
            with lock:
                state["active"] -= 1
            return {path}, str(path)

        paths = [f"mutant-{at}" for at in range(mutant_count)]
        clean, mutants = verdict_batch("clean", paths, False, {}, run=fake)
        return state, clean, mutants, paths

    state, clean, mutants, paths = overlap(2)
    capped, _clean, _mutants, _paths = overlap(MAX_PARALLEL_SUITES * 2)
    checks.append(("batch-reuses-one-clean-run",
                   state["calls"].count("clean") == 1
                   and sorted(state["calls"]) == sorted(["clean", *paths])
                   and clean == ({"clean"}, "clean")
                   and mutants == [({path}, path) for path in paths],
                   repr((state, clean, mutants))))
    checks.append(("batch-runs-concurrently", state["maximum"] == 3,
                   f"maximum active={state['maximum']}"))
    checks.append(("batch-concurrency-capped",
                   capped["maximum"] == MAX_PARALLEL_SUITES,
                   f"maximum active={capped['maximum']}"))

    failed = [(name, detail) for name, ok, detail in checks if not ok]
    print(f"probe checks: {len(checks) - len(failed)} ok, {len(failed)} failed")
    if failed:
        raise ProbeFailure([f"{name}: {detail}" for name, detail in failed])
    return len(checks)
