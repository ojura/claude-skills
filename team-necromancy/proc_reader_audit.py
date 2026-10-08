#!/usr/bin/env python3
"""Which selftest units read processes the process fence refuses them.

Runs agent-resume's selftest with the record variable naming a file of this
audit's own, so the fence writes each refused read there, one JSON object a
line, and lists every one by unit, reader and pid: those of the selftest
process, those outside any unit, and those of every tool run a unit starts,
which installs its unit's fence from the variable the unit hands down. Without
an audit a run writes them to a file of its own. Refusals by the selftest's own
cleanup readers on _ProcessFence (architecture.md) are written the same way,
under the reader "cleanup stat", or, for a pid pidfd_open could not judge,
under a reader that names the error, which the audit lists as a reader of its
own. Reads the fence allows are not counted. The pids each run learned go to a
file beside the record, its name with ".known" after it, which every fence of
that run reads (architecture.md); the audit keeps it with the record. Rerun it
after each package of the consolidation is adapted; an empty result is the
pass.

    proc_reader_audit.py [--live] [--jobs N ...] [--tool PATH] [--record PATH]
                         [--static proc-readers.json]

--tool is the agent-resume to audit, by default the one beside this file; the
corpus beside it is the one its selftest copies. --live audits the live tier as
well, which starts real processes of its own (never a real tmux or claude), at
each --jobs value given, 1, 2 and 4 unless named. --record keeps the raw
refusals there, and each tier's output and run record beside it. --static
compares the result with a list of units that a static trace found able to
reach a reader, such as the reviewer's proc-readers.json.

Each tier runs under script(1), so it has a terminal of its own on stdin and
stdout, with its stderr kept apart, and with TMPDIR set to a fresh private
directory whose path is 11 characters long, with XDG_CACHE_HOME inside it.
A split live run's socket roots sit two room levels below TMPDIR, and at this
length the suite's longest socket name still fits in the 107 bytes a Unix
socket path may have. Each tier's run record names the tool's SHA-256, the
interpreter, the command and every variable of the environment the tool ran
with. The audit writes it inside the terminal session: script(1) runs this
file with --within, which writes the record and then becomes the tool through
exec, so the environment the record names is the one the tool ran with, after
the shell script(1) starts added its own variables. It writes a value only
for a name on SHOWN, matched exactly, and names every other variable with no
value, no length and no digest, because a rule on names would let a secret
through under a name it does not match. Whether each variable the audit
handed to script(1) was the same in every tier is compared in memory, and the
audit names the ones that were not.

Before the tiers it makes one refusal on purpose on each path a refusal takes
to the record: it runs the tool's selftest with one unit of its own, which
reads pid 1, outside every run, once before the unit runs, outside any unit,
once in the unit, and once in a tool run the unit starts. The run builds its
fence from the record variable as a tier does, and the tool run installs its
unit's fence as main does first. The audit reads those three lines back before
it runs a tier and leaves them out of the count, so an empty count is evidence
that nothing was refused on any of those paths, and not a record that nothing
could reach. It judges each tier by its exit status and by its refusal rows,
read-only-tier-reads-only-admitted-processes and, in a live tier, also
live-tier-reads-only-admitted-processes, as well as by the record, so a refusal
that went elsewhere still counts. The directories it makes for the control and
the tiers are removed after each run, and one it could not remove is named; the
record and the outputs beside it are kept.

Exit status: 0 when every tier exited 0 and passed its refusal rows and the
record holds no refusal but the control's; 1 when the record holds one or a
tier's refusal row failed; 2 when the control did not reach the record as it
should, or a tier could not run, ran out of time, did not state a refusal row
or exited nonzero for another reason, such as a failing check of another kind.
"""
import argparse
import collections
import hashlib
import json
import os
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
# The variables a run record writes the value of, matched by exact name; every
# other variable is named only. This is the lead's list for every run record of
# this work, environment-values-2026-10-08.txt of 8 October 2026: the process
# and terminal, LANG, LANGUAGE and the thirteen LC_ names of glibc, tmux and the
# suite's directories, CLAUDE_CONFIG_DIR, the XDG base directories and session,
# and the 29 names python3 --help-env lists in Python 3.12.3.
SHOWN = frozenset((
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TERM", "COLUMNS", "LINES", "TZ", "ZDOTDIR",
    "NO_COLOR", "FORCE_COLOR",
    "LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE", "LC_NUMERIC", "LC_TIME", "LC_COLLATE",
    "LC_MONETARY", "LC_MESSAGES", "LC_PAPER", "LC_NAME", "LC_ADDRESS", "LC_TELEPHONE",
    "LC_MEASUREMENT", "LC_IDENTIFICATION",
    "TMUX", "TMUX_PANE", "TMUX_TMPDIR", "TMPDIR",
    "CLAUDE_CONFIG_DIR",
    "XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME", "XDG_RUNTIME_DIR",
    "XDG_DATA_DIRS", "XDG_CONFIG_DIRS", "XDG_SESSION_TYPE", "XDG_SESSION_CLASS",
    "XDG_SESSION_DESKTOP", "XDG_CURRENT_DESKTOP", "XDG_SESSION_ID", "XDG_SEAT", "XDG_VTNR",
    "XDG_MENU_PREFIX",
    "PYTHONBREAKPOINT", "PYTHONCASEOK", "PYTHONCOERCECLOCALE", "PYTHONDEBUG", "PYTHONDEVMODE",
    "PYTHONDONTWRITEBYTECODE", "PYTHONFAULTHANDLER", "PYTHONHASHSEED", "PYTHONHOME",
    "PYTHONINSPECT", "PYTHONINTMAXSTRDIGITS", "PYTHONIOENCODING", "PYTHONMALLOC",
    "PYTHONNODEBUGRANGES", "PYTHONNOUSERSITE", "PYTHONOPTIMIZE", "PYTHONPATH",
    "PYTHONPERFSUPPORT", "PYTHONPLATLIBDIR", "PYTHONPROFILEIMPORTTIME", "PYTHONPYCACHEPREFIX",
    "PYTHONSAFEPATH", "PYTHONSTARTUP", "PYTHONTRACEMALLOC", "PYTHONUNBUFFERED", "PYTHONUTF8",
    "PYTHONVERBOSE", "PYTHONWARNDEFAULTENCODING", "PYTHONWARNINGS",
))
# How long the control and each tier may take, in seconds.
CONTROL_BOUND, TIER_BOUND = 600, 1800
# The rows a tier states about the reads its fence refused: the read-only
# tier's, which a live tier's read-only child states too, and the live tier's.
READ_ONLY_PROCS = "read-only-tier-reads-only-admitted-processes"
LIVE_PROCS = "live-tier-reads-only-admitted-processes"


def private_root():
    """A fresh 0700 directory whose path is 11 characters long: /tmp/fq and four random ones.

    Made by hand because mkdtemp always adds eight characters.
    """
    alphabet = "abcdefghijklmnopqrstuvwxyz0123456789"
    for _attempt in range(1000):
        root = Path("/tmp/fq" + "".join(secrets.choice(alphabet) for _ in range(4)))
        try:
            root.mkdir(mode=0o700)
        except FileExistsError:
            continue
        (root / "c").mkdir(mode=0o700)
        return root
    raise SystemExit("no free 11-character directory under /tmp after 1000 tries")


def removed(root, label):
    """Remove root, a directory the audit made; returns a sentence naming it when that failed."""
    try:
        shutil.rmtree(root)
    except OSError as failed:
        return f"{label}: could not remove {root}: {failed}"
    return None


def run_environment(tool, root, record):
    """The environment the control and each tier run with: the audit's own, with its directories and record."""
    prefix = tool.name.upper().replace("-", "_")
    env = dict(os.environ, TMPDIR=str(root), XDG_CACHE_HOME=str(root / "c"))
    for name in ("TMUX", "TMUX_PANE", "TMUX_TMPDIR", f"{prefix}_SELFTEST_UNIT"):
        env.pop(name, None)
    env[f"{prefix}_SELFTEST_FENCE"] = str(record)
    return env


# A tool run a unit starts: its fence goes in from the unit variable, as main's
# does first, and it reads pid 1, which is outside every run.
TOOL_RUN = ("import importlib.machinery, importlib.util, sys\n"
            "loader = importlib.machinery.SourceFileLoader('audited_tool_run', sys.argv[1])\n"
            "tool = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, "
            "loader))\n"
            "tool.__file__ = sys.argv[1]\n"
            "loader.exec_module(tool)\n"
            "tool.adopt_inherited_fence()\n"
            "tool.Resolver(needs=tool.Needs(maybe=set(tool.NEED)))._proc_stat(1)\n")
# The control: the tool's read-only selftest with one unit, the audit's, put in
# place of the suite's. It reads pid 1 before the unit runs, outside any unit;
# in the unit; and in a tool run the unit starts. run_selftest builds the fence
# all three go through, from the record variable, as it builds a tier's.
CONTROL_UNIT = "audit_control"
CONTROL = r'''
import importlib.machinery, importlib.util, subprocess, sys
path, tool_run = sys.argv[1:3]
loader = importlib.machinery.SourceFileLoader("audited_tool", path)
tool = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
tool.__file__ = path
loader.exec_module(tool)


def read_pid_1():
    tool.Resolver(needs=tool.Needs(maybe=set(tool.NEED)))._proc_stat(1)


said = "the audit's control reads pid 1 here and in a tool run it starts"


def audit_control(_s):
    read_pid_1()
    subprocess.run([sys.executable, "-c", tool_run, path], capture_output=True, timeout=120,
                   stdin=subprocess.DEVNULL)
    return [("audit-control", said, True, "")]


tool.unit(("audit-control", said))(audit_control)
tool.UNITS[:] = [entry for entry in tool.UNITS if entry[2] is audit_control]
units_rows = tool.tier_rows


def tier_rows(*args, **kwargs):
    read_pid_1()
    return units_rows(*args, **kwargs)


tool.tier_rows = tier_rows
sys.exit(tool.run_selftest(failed_only=True))
'''


def control(tool, record):
    """Make the three control refusals; returns (the record lines they wrote, what went wrong)."""
    root = private_root()
    try:
        done = subprocess.run([sys.executable, "-c", CONTROL, str(tool), TOOL_RUN],
                              cwd=tool.parent, env=run_environment(tool, root, record),
                              capture_output=True, text=True, timeout=CONTROL_BOUND,
                              stdin=subprocess.DEVNULL)
    except (subprocess.TimeoutExpired, OSError) as failed:
        done, wrong = None, [f"the control run could not finish: {failed}"]
    else:
        wrong = []
    left = removed(root, "control")
    if left:
        print(left)
    if done is None:
        return [], wrong
    rows = [json.loads(line) for line in record.read_text().splitlines() if line.strip()]
    # It refuses three reads and so fails its own refusal row; any other status
    # is a run that did not get that far.
    if done.returncode != 1:
        wrong.append(f"the control run exited {done.returncode}: {done.stderr.strip()[-500:]!r}")
    root_pid = {row.get("root") for row in rows}
    paths = sorted((row.get("unit") or "", row.get("reader"), row.get("pid"),
                    row.get("process") == row.get("root")) for row in rows)
    if len(root_pid) != 1 or paths != sorted([("", "stat", 1, True),
                                              (CONTROL_UNIT, "stat", 1, True),
                                              (CONTROL_UNIT, "stat", 1, False)]):
        wrong.append(f"the record holds {rows} where one refusal of pid 1 outside any unit, "
                     "one in the control's unit and one in its tool run were made")
    return rows, wrong


def within(run_file, argv):
    """Inside the terminal session: write what the tool is about to run with, then become it."""
    with open(run_file, "a") as out:
        out.write("\n".join(run_record(Path(argv[1]), argv, dict(os.environ))) + "\n")
    os.execv(argv[0], argv)


def run_record(tool, argv, env):
    """What one tier ran with, as lines: the tool's hash, the interpreter, the command, the environment."""
    lines = [f"tool {tool}",
             f"tool sha256 {hashlib.sha256(tool.read_bytes()).hexdigest()}",
             f"python {sys.executable} {sys.version.split()[0]}",
             f"argv {shlex.join(argv)}"]
    for name in sorted(env):
        if name in SHOWN:
            lines.append(f"env {name}={env[name]}")
        else:
            lines.append(f"env {name} (value not recorded)")
    return lines


def run_tier(tool, label, live, args, record, environments):
    """Run one tier with the fence writing into record; returns what went wrong with it.

    Each item is (kind, sentence): kind "refused" for a failed refusal row,
    "unjudged" for anything that keeps the tier from being judged. The
    environment handed to script(1) is kept in environments under label, in
    memory, for main to say which variables differed between the tiers.
    """
    root = private_root()
    env = run_environment(tool, root, record)
    environments[label] = env
    argv = [sys.executable, str(tool), *args]
    kept = record.with_name(f"{record.stem}.{label}.out")
    errors = record.with_name(f"{record.stem}.{label}.err")
    run_file = record.with_name(f"{record.stem}.{label}.run")
    run_file.write_text(f"TMPDIR {root}\n"
                        "written inside the terminal session, by the process that then "
                        "became the tool:\n")
    inside = [sys.executable, str(Path(__file__).resolve()), "--within", str(run_file), *argv]
    wrong = []
    try:
        done = subprocess.run(["script", "-qec", f"{shlex.join(inside)} 2> {shlex.quote(str(errors))}",
                               "/dev/null"], cwd=tool.parent, env=env, capture_output=True,
                              text=True, timeout=TIER_BOUND, stdin=subprocess.DEVNULL)
    except (subprocess.TimeoutExpired, OSError) as failed:
        done, wrong = None, [("unjudged", f"{label}: could not finish: {failed}")]
    finally:
        left = removed(root, label)
    if left:
        print(left)
    if done is None:
        print(wrong[0][1])
        return wrong
    printed = done.stdout.replace("\r", "")
    kept.write_text(printed)
    last = (printed.strip().splitlines() or ["(no output)"])[-1]
    stated = {line.split()[1]: line.split()[0] for line in printed.splitlines()
              if len(line.split()) > 1 and line.split()[0] in ("PASS", "FAIL")}
    said = errors.read_text() if errors.exists() else ""
    print(f"{label}: exit {done.returncode}, {last.strip()}  (output {kept}"
          f"{', stderr ' + str(len(said)) + ' bytes' if said else ''})")
    for check_id, verdict in stated.items():
        if verdict == "FAIL":
            print(f"  FAIL {check_id}")
    rows = [READ_ONLY_PROCS, *([LIVE_PROCS] if live else [])]
    for row in rows:
        if stated.get(row) == "FAIL":
            wrong.append(("refused", f"{label}: {row} failed"))
        elif row not in stated:
            wrong.append(("unjudged", f"{label}: its output states no {row} row"))
    others = sorted(check_id for check_id, verdict in stated.items()
                    if verdict == "FAIL" and check_id not in rows)
    refused = any(kind == "refused" for kind, _sentence in wrong)
    if done.returncode not in (0, 1) or others or (done.returncode == 1 and not refused):
        wrong.append(("unjudged", f"{label}: exited {done.returncode}"
                      + (f", failing {', '.join(others)}" if others else "")))
    return wrong


def main():
    if sys.argv[1:2] == ["--within"]:
        return within(sys.argv[2], sys.argv[3:])
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--tool", type=Path, default=HERE / "agent-resume")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--jobs", type=int, nargs="+", default=[1, 2, 4])
    parser.add_argument("--static", type=Path)
    parser.add_argument("--record", type=Path, help="keep the raw refusals here")
    args = parser.parse_args()
    tool = args.tool.resolve()
    # The suite runs from the tool's own directory, so the record is named by
    # its absolute path.
    record = (args.record or Path(tempfile.mkstemp(prefix="proc-audit-", suffix=".jsonl")[1])).resolve()
    record.write_text("")
    controls, wrong = control(tool, record)
    if wrong:
        for sentence in wrong:
            print(f"control: {sentence}")
        print(f"control: the refusals made on purpose did not reach {record} as they should; "
              "a count read from this record would mean nothing")
        return 2
    print(f"control: one refusal outside any unit, one in a unit and one in a tool run reached "
          f"{record}")
    tiers = [("read-only", False, ["--selftest"])]
    if args.live:
        tiers += [(f"live-jobs{jobs}", True, ["--selftest", "--live", "--jobs", str(jobs)])
                  for jobs in args.jobs]
    environments = {}
    faults = [fault for label, live, tier_args in tiers
              for fault in run_tier(tool, label, live, tier_args, record, environments)]
    names = set().union(*environments.values())
    differing = sorted(name for name in names
                       if len({env.get(name) for env in environments.values()}) > 1)
    print("environment handed to script(1): every variable the same in every tier"
          + (f" but {', '.join(differing)}" if differing else ""))
    rows = [json.loads(line) for line in record.read_text().splitlines()
            if line.strip()][len(controls):]
    by_unit = collections.defaultdict(lambda: collections.defaultdict(list))
    for row in rows:
        unit = row.get("unit") or "(outside any unit)"
        pid = str(row.get("pid"))
        if row.get("process") != row.get("root"):
            pid += f" (in tool run {row.get('process')})"
        by_unit[unit][row.get("reader")].append(pid)
    print(f"{len(rows)} refusal(s) in {len(by_unit)} unit(s); raw: {record}")
    for unit, readers in sorted(by_unit.items(), key=lambda kv: -sum(map(len, kv[1].values()))):
        print(f"  {unit}: {sum(map(len, readers.values()))} refusal(s)")
        for reader, pids in sorted(readers.items()):
            print(f"    {reader}: {', '.join(pids)}")
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
    for _kind, sentence in faults:
        print(sentence)
    if rows or any(kind == "refused" for kind, _sentence in faults):
        return 1
    return 2 if faults else 0


if __name__ == "__main__":
    sys.exit(main())
