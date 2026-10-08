#!/usr/bin/env python3
"""Which selftest units read processes the process fence refuses them.

Runs agent-resume's selftest with the record variable naming a file of this
audit's own, so the fence writes each refused read there, one JSON object a
line, and lists them by unit, reader and pid: those of the selftest process,
those outside any unit, and those of every tool run a unit starts, which
installs its unit's fence from the variable the unit hands down. Without an
audit a run writes them to a file of its own. Refusals by the selftest's own
cleanup readers on _ProcessFence
(architecture.md) are written the same way, under the reader "cleanup stat".
Reads the fence allows are not counted. A run of the selftest goes on past a
refusal and fails its tier's row read-only-tier-reads-only-admitted-processes
or live-tier-reads-only-admitted-processes, so a tier with a refusal exits 1
here as well. Rerun it after each package of the consolidation is adapted; an
empty result is the pass.

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
interpreter, the command and every variable of the environment the tier ran
with. It writes a value only for a name on SHOWN, matched exactly, and names
every other variable with no value, no length and no digest, because a rule on
names would let a secret through under a name it does not match. Whether each
variable was the same in every tier is compared in memory, and the audit names
the ones that were not.

Before the tiers it makes one refusal on purpose: a tool run started the way a
unit starts one, with the unit variable naming this audit as its root and the
record variable naming the record, reads pid 1, which is outside every run. The
audit reads that line back before it runs a tier and leaves it out of the count,
so an empty count is evidence that nothing was refused and not a record that
nothing could reach. The directories it makes for the tiers are removed after
each tier; the record and the outputs beside it are kept.

Exit status: 0 when nothing was refused, 1 when something was, 2 when a tier
could not run or the control did not reach the record.
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
        return root
    raise SystemExit("no free 11-character directory under /tmp after 1000 tries")


# The tool run that makes the control refusal: it installs its fence from the
# unit variable, as main does first, and reads pid 1 through the engine.
CONTROL = ("import importlib.machinery, importlib.util, sys\n"
           "loader = importlib.machinery.SourceFileLoader('audited_tool', sys.argv[1])\n"
           "tool = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, "
           "loader))\n"
           "tool.__file__ = sys.argv[1]\n"
           "loader.exec_module(tool)\n"
           "tool.adopt_inherited_fence()\n"
           "tool.Resolver(needs=tool.Needs(maybe=set(tool.NEED)))._proc_stat(1)\n")
CONTROL_TOKEN = "audit-control"


def control(tool, record):
    """Make the control refusal under the record variable; returns the record lines it wrote."""
    prefix = tool.name.upper().replace("-", "_")
    own = Path("/proc/self/stat").read_bytes()
    start = own[own.rindex(b")") + 2:].split()[19].decode()
    env = dict(os.environ)
    env[f"{prefix}_SELFTEST_UNIT"] = (f"{os.getpid()} {start} {CONTROL_TOKEN} 0 "
                                     f"{CONTROL_TOKEN}")
    env[f"{prefix}_SELFTEST_FENCE"] = str(record)
    subprocess.run([sys.executable, "-c", CONTROL, str(tool)], env=env, capture_output=True,
                   timeout=120, stdin=subprocess.DEVNULL)
    rows = [json.loads(line) for line in record.read_text().splitlines() if line.strip()]
    return [row for row in rows if row.get("fence") == CONTROL_TOKEN]


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


def run_tier(tool, label, args, record, environments):
    """Run one tier with the fence writing into record; return its exit status.

    The environment the tier ran with is kept in environments under label, in
    memory, for main to say which variables differed between the tiers.
    """
    root = private_root()
    (root / "c").mkdir(mode=0o700)
    prefix = tool.name.upper().replace("-", "_")
    env = dict(os.environ, TMPDIR=str(root), XDG_CACHE_HOME=str(root / "c"))
    for name in ("TMUX", "TMUX_PANE", "TMUX_TMPDIR", f"{prefix}_SELFTEST_UNIT"):
        env.pop(name, None)
    env[f"{prefix}_SELFTEST_FENCE"] = str(record)
    environments[label] = env
    argv = [sys.executable, str(tool), *args]
    kept = record.with_name(f"{record.stem}.{label}.out")
    errors = record.with_name(f"{record.stem}.{label}.err")
    record.with_name(f"{record.stem}.{label}.run").write_text(
        "\n".join(run_record(tool, argv, env) + [f"TMPDIR {root}"]) + "\n")
    try:
        done = subprocess.run(["script", "-qec", f"{shlex.join(argv)} 2> {shlex.quote(str(errors))}",
                               "/dev/null"], cwd=tool.parent, env=env, capture_output=True,
                              text=True, timeout=1800, stdin=subprocess.DEVNULL)
    finally:
        shutil.rmtree(root, ignore_errors=True)
    printed = done.stdout.replace("\r", "")
    kept.write_text(printed)
    last = (printed.strip().splitlines() or ["(no output)"])[-1]
    failed = [line.split()[1] for line in printed.splitlines()
              if line.strip().startswith("FAIL ")]
    said = errors.read_text() if errors.exists() else ""
    print(f"{label}: exit {done.returncode}, {last.strip()}  (output {kept}"
          f"{', stderr ' + str(len(said)) + ' bytes' if said else ''})")
    for check_id in failed:
        print(f"  FAIL {check_id}")
    return done.returncode


def main():
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
    controls = control(tool, record)
    if [(row.get("unit"), row.get("reader"), row.get("pid")) for row in controls] \
            != [(CONTROL_TOKEN, "stat", 1)]:
        print(f"control: the refusal made on purpose did not reach {record} as one line "
              f"(found {controls}); a count read from this record would mean nothing")
        return 2
    print(f"control: the refusal made on purpose reached {record}")
    tiers = [("read-only", ["--selftest"])]
    if args.live:
        tiers += [(f"live-jobs{jobs}", ["--selftest", "--live", "--jobs", str(jobs)])
                  for jobs in args.jobs]
    environments = {}
    statuses = {label: run_tier(tool, label, tier_args, record, environments)
                for label, tier_args in tiers}
    names = set().union(*environments.values())
    differing = sorted(name for name in names
                       if len({env.get(name) for env in environments.values()}) > 1)
    print("environment: every variable the same in every tier"
          + (f" but {', '.join(differing)}" if differing else ""))
    rows = [row for row in (json.loads(line) for line in record.read_text().splitlines()
                            if line.strip())
            if row.get("fence") != CONTROL_TOKEN]
    by_unit = collections.defaultdict(lambda: {"readers": collections.Counter(), "pids": set(),
                                               "tool runs": set()})
    for row in rows:
        unit = row.get("unit") or "(outside any unit)"
        by_unit[unit]["readers"][row.get("reader")] += 1
        by_unit[unit]["pids"].add(row.get("pid"))
        if row.get("process") != row.get("root"):
            by_unit[unit]["tool runs"].add(row.get("process"))
    print(f"{len(rows)} refusal(s) in {len(by_unit)} unit(s); raw: {record}")
    for unit, seen in sorted(by_unit.items(), key=lambda kv: -sum(kv[1]["readers"].values())):
        pids = sorted(seen["pids"], key=str)
        shown = ", ".join(map(str, pids[:6])) + (" ..." if len(pids) > 6 else "")
        runs = (f"; in {len(seen['tool runs'])} tool run(s) it started"
                if seen["tool runs"] else "")
        print(f"  {unit}: {dict(seen['readers'])}; {len(pids)} pid(s): {shown}{runs}")
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
