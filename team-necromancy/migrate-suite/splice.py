"""Splice the registry, the context and the units into the file, then convert
every place the suite excluded itself by naming one function.

The exclusions are the half a mechanical cut cannot do for itself: a check
that scans the file's own functions used to skip run_selftest, and the suite
is now 186 functions instead of one. Each becomes the registry's set, so a
unit added later is covered without anyone remembering it.
"""
import ast
import sys

PATH = sys.argv[1]
src = open(PATH).read()
lines = src.splitlines()
tree = ast.parse(src)
fn = next(n for n in ast.walk(tree)
          if isinstance(n, ast.FunctionDef) and n.name == "run_selftest")
units_src = open("/tmp/converted_units.py").read() + '\n\n@unit(("check-line-one-owner", "a check reads the same way wherever it is printed"))\ndef _check_check_line_one_owner(s):\n    checked = []\n\n    def check(cid, name, ok, detail=""):\n        checked.append((cid, name, ok, detail))\n\n    # Three places used to put a check\'s id next to its name, so a listing\n    # could stop printing the name and the other two would still look right.\n    # Anything assembling both fields itself is a fourth spelling.\n    printers = set()\n    for fnode in ast.walk(s.tree):\n        if not isinstance(fnode, ast.FunctionDef) or fnode.name == "check_line":\n            continue\n        for piece in ast.walk(fnode):\n            if not isinstance(piece, ast.JoinedStr):\n                continue\n            filled = {getattr(v.value, "id", None) for v in piece.values\n                      if isinstance(v, ast.FormattedValue)}\n            if {"cid", "name"} <= filled or {"cid", "said"} <= filled:\n                printers.add(fnode.name)\n    check("check-line-one-owner", "a check reads the same way wherever it is printed",\n          not printers, ", ".join(sorted(printers)))\n    return checked\n\n'
live_src = open("/tmp/converted_live.py").read()
disp = fn.body[0]
dispatch = "\n".join(lines[disp.lineno - 1:disp.end_lineno])

reg = '''# Every check this suite defines, registered as the file loads, so what the
# suite contains is a lookup rather than something a run discovers.
UNITS = []

# Checks the runner performs itself, because their subject is the run rather
# than anything a unit can reach.
RUNNER_CHECKS = (("live-checks-say-nothing-mid-run",
                  "a live check prints nothing between the verdicts"),)


def unit(*ids, live=False):
    """Register one unit of the suite under the ids it produces."""
    def take(run):
        UNITS.append((run.__name__, ids, run, live))
        return run
    return take


'''

runner = '''def run_selftest(live=False, failed_only=False, list_only=False):
    if list_only:
        # Both tiers are registered, so asking what exists reads the registry
        # and runs nothing.
        rows = [(cid, said)
                for _name, ids, _run, is_live in UNITS if live or not is_live
                for cid, said in ids]
        # One check belongs to the runner rather than to a unit: it watches
        # what the live tier printed while running, which only the caller of
        # the tier can see. It is listed here so the listing stays complete.
        rows += list(RUNNER_CHECKS) if live else []
        column = max((len(cid) for cid, _said in rows), default=0)
        for cid, said in rows:
            print(check_line(cid, said, idwidth=column))
        return 0
''' + dispatch + '''
    s = _suite_context()
    results, seen_ids = [], set()
    for _name, ids, run, is_live in UNITS:
        if is_live:
            continue
        produced = run(s)
        for cid, name, ok, detail in produced:
            if cid in seen_ids:
                raise RuntimeError(f"duplicate check id: {cid!r}")
            seen_ids.add(cid)
            results.append((cid, name, ok, detail))
        # The registry is a claim about what a unit does, and a claim this
        # suite makes about itself is checked like any other.
        said = sorted(cid for cid, _n, _o, _d in produced)
        if said != sorted(cid for cid, _s in ids):
            results.append(("registry-declares-what-it-runs",
                            "a unit produces the ids it registered", False,
                            f"{_name} registered {sorted(c for c, _s in ids)}, "
                            f"produced {said}"))
    return _selftest_report(results, failed_only, list_only)
'''

live_fn = next(n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "live_checks")
live_wrapper = '''def live_checks():
    """Run every unit registered as live, against the live tier's context.

    The read-only tier's verdicts come from the child the context ran, so a
    live invocation answers those questions once.
    """
    s = _live_context()
    out = list(s.inherited)
    for _name, _ids, run, is_live in UNITS:
        if is_live:
            out.extend(run(s))
    return out
'''

# The later region first, so the earlier one's line numbers still hold.
out = ("\n".join(lines[:fn.lineno - 1]) + "\n" + units_src + "\n"
       + runner + "\n" + "\n".join(lines[fn.end_lineno:]))
head = "\n".join(out.splitlines()[:live_fn.lineno - 1])
tail = "\n".join(out.splitlines()[live_fn.end_lineno:])
# The registry goes in ahead of both tiers, since a unit registers itself as
# the file loads and the live tier is the earlier of the two.
out = head + "\n" + reg + live_src + "\n" + live_wrapper + "\n" + tail

SUBS = [
    # Listing is the registry's answer now and never reaches here, so the
    # branch that assembled a row itself goes with the argument that selected
    # it, rather than being routed through the renderer to keep it alive.
    ("""def _selftest_report(results, failed_only, list_only):
    \"\"\"Print one tier's verdicts, however the run assembled them.\"\"\"
    if list_only:
        for cid, name, _ok, _detail in results:
            print(f"{cid}  {name}")
        return 0
    for line in check_report(results, failed_only):""",
     """def _selftest_report(results, failed_only):
    \"\"\"Print one tier's verdicts, however the run assembled them.\"\"\"
    for line in check_report(results, failed_only):"""),
    ("        return _selftest_report(results, failed_only, list_only)",
     "        return _selftest_report(results, failed_only)"),
    ("    return _selftest_report(results, failed_only, list_only)",
     "    return _selftest_report(results, failed_only)"),
    # the appended unit is written by hand, so it asks the context for the
    # module the generated ones are given by the cutter
    ("    printers = set()\n    for fnode in ast.walk(s.tree):",
     "    ast = s.ast\n\n    printers = set()\n    for fnode in ast.walk(s.tree):"),
    ("def check_report(results, failed_only=False):", 'def check_line(cid, name, detail="", verdict=None, idwidth=0, namewidth=0):\n    """One check as a line, wherever it is printed.\n\n    The failure list, the full table and the listing show the same check with\n    different amounts of it: a verdict where there is one to report, columns\n    where there are rows to line up, a detail where the check left one. Asked\n    here so the three cannot drift, which is how a listing once lost the\n    description while the other two kept it.\n    """\n    said = "" if verdict is None else f"  {\'PASS\' if verdict else \'FAIL\'}  "\n    return (f"{said}{cid:<{idwidth}}  {name:<{namewidth}}"\n            + (f"  ({detail})" if detail else ""))\n\n\ndef check_report(results, failed_only=False):'),
    ('    failed = sum(1 for _, _, ok, _ in results if not ok)\n    if failed_only:\n        lines = [f"{cid}  {name}{\'  (\' + detail + \')\' if detail else \'\'}"\n                 for cid, name, ok, detail in results if not ok]\n        return lines + [f"checks: {len(results) - failed} ok, {failed} failed"]\n    idwidth = max((len(cid) for cid, _, _, _ in results), default=0)\n    width = max((len(name) for _, name, _, _ in results), default=0)\n    lines = [f"  {\'PASS\' if ok else \'FAIL\'}  {cid:<{idwidth}}  {name:<{width}}"\n             + (f"  ({detail})" if detail else "")\n             for cid, name, ok, detail in results]\n    return lines + ["", f"{len(results) - failed}/{len(results)} checks passed"]', '    failed = sum(1 for _, _, ok, _ in results if not ok)\n    if failed_only:\n        lines = [check_line(cid, name, detail)\n                 for cid, name, ok, detail in results if not ok]\n        return lines + [f"checks: {len(results) - failed} ok, {failed} failed"]\n    idwidth = max((len(cid) for cid, _, _, _ in results), default=0)\n    width = max((len(name) for _, name, _, _ in results), default=0)\n    lines = [check_line(cid, name, detail, verdict=ok,\n                        idwidth=idwidth, namewidth=width)\n             for cid, name, ok, detail in results]\n    return lines + ["", f"{len(results) - failed}/{len(results)} checks passed"]'),
    # side-census-covers-every-file counted the unclassifiable files into a
    # variable and never mentioned it: its verdict is that every file is
    # answered for and every answer is one of the three, and unclassifiable is
    # one of the three. The count is already in the detail line beside the
    # others, so the variable said nothing the report did not.
    ('    stray = tallies.get("unclassifiable", 0)\n', ""),
    # the inherited verdicts are returned by the wrapper, not appended to a
    # list the context keeps to itself
    ("    out.extend(inherited)\n", ""),
    # the context knows the suite's own functions, from the registry
    ("    s = types.SimpleNamespace()",
     '''    s = types.SimpleNamespace()
    # The suite's own functions, taken from the registry rather than listed: a
    # unit added later is covered without anyone remembering, which is what a
    # hand-kept exemption list gets wrong.
    s.suite_fns = ({run.__name__ for _n, _i, run, _l in UNITS}
                   | {"_suite_context", "_live_context", "run_selftest",
                      "live_checks", "unit"})'''),
    # the line range the structural checks treat as suite rather than product
    ('''    selftest_node = next(n for n in ast.walk(tree)
                         if isinstance(n, ast.FunctionDef) and n.name == "run_selftest")
    selftest_lines = set(range(selftest_node.lineno, selftest_node.end_lineno + 1))''',
     '''    # Every line the suite occupies: its units, their context and the runner.
    suite_fns = s.suite_fns
    selftest_lines = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef) and n.name in suite_fns:
            selftest_lines |= set(range(n.lineno, n.end_lineno + 1))'''),
    ('               - {"team_keep_reason", "run_selftest", "run_census"})',
     '               - {"team_keep_reason", "run_census"} - s.suite_fns)'),
    ('        and node.name not in {"edit_team_config", "run_selftest"})',
     '        and node.name not in {"edit_team_config"} | s.suite_fns)'),
    ('        and node.name not in {"_complete", "run_selftest"})',
     '        and node.name not in {"_complete"} | s.suite_fns)'),
    ('        and node.name != "run_selftest"', '        and node.name not in s.suite_fns'),
    ('        and node.name not in {"run_selftest"}', '        and node.name not in s.suite_fns'),
    ('        and node.name not in {"install_hook", "run_selftest", "live_checks"}',
     '        and node.name not in {"install_hook"} | s.suite_fns'),
    ('        if not isinstance(fn, ast.FunctionDef) or fn.name == "run_selftest":',
     '        if not isinstance(fn, ast.FunctionDef) or fn.name in s.suite_fns:'),
    ('    if set(reporters) - {"run_one", "run_selftest"}:',
     '    if set(reporters) - {"run_one"} - s.suite_fns:'),
    ('            sorted(set(reporters) - {"run_one", "run_selftest"})))',
     '            sorted(set(reporters) - {"run_one"} - s.suite_fns)))'),
    ('        stray = callers_of(act) - engine - {"rebind_reap", "run_selftest"}',
     '        stray = callers_of(act) - engine - {"rebind_reap"} - s.suite_fns'),
    ('    reached = callers_of("kill_holder") - {"run_selftest"}',
     '    reached = callers_of("kill_holder") - s.suite_fns'),
    ('        - {"run_selftest", "live_checks"})', '        - s.suite_fns)'),
    ('                      - {"already_running", "auto_redirect", "run_selftest"})',
     '                      - {"already_running", "auto_redirect"} - s.suite_fns)'),
    ('         - {"run_promote", "run_demote", "run_selftest"}))',
     '         - {"run_promote", "run_demote"} - s.suite_fns))'),
    ('                     - {"materialize", "embed", "apply_restamp", "run_selftest"})',
     '                     - {"materialize", "embed", "apply_restamp"} - s.suite_fns)'),
    ('    spread = sorted(callers_of("live_led") - {"place", "run_demote", "run_selftest"})',
     '    spread = sorted(callers_of("live_led") - {"place", "run_demote"} - s.suite_fns)'),
    ('    check("roster-one-writer", "one roster writer", writers <= {"edit_team_config", "replace_json", "run_selftest"},',
     '''    check("roster-one-writer", "one roster writer",
          writers - s.suite_fns <= {"edit_team_config", "replace_json"},'''),
    ('          ", ".join(sorted(writers - {"run_selftest"})))',
     '          ", ".join(sorted(writers - s.suite_fns)))'),
    ('''        if not isinstance(node, ast.FunctionDef) or node.name in (
                "live_led", "run_selftest", "diagnose_team", "run_census"):''',
     '''        if not isinstance(node, ast.FunctionDef) or node.name in (
                {"live_led", "diagnose_team", "run_census"} | s.suite_fns):'''),
    ('''        if not isinstance(fn, ast.FunctionDef) or fn.name in (
                "noop_outcome", "run_selftest"):''',
     '''        if not isinstance(fn, ast.FunctionDef) or fn.name in (
                {"noop_outcome"} | s.suite_fns):'''),
    ('''        and node.name not in {"__init__", "_ingest_spawn_lines", "_log", "_every_log",
                              "run_selftest"})''',
     '''        and node.name not in ({"__init__", "_ingest_spawn_lines", "_log", "_every_log"}
                              | s.suite_fns))'''),
    ('''    payload_readers = (callers_of("_spawn_by_agent") | callers_of("_spawn_records")) - {
        "spawn_payload", "_spawn_by_agent", "team_minted_by", "_pid_running_team",
        "members_spawned_into", "_log", "run_selftest"}''',
     '''    payload_readers = ((callers_of("_spawn_by_agent") | callers_of("_spawn_records"))
                       - {"spawn_payload", "_spawn_by_agent", "team_minted_by",
                          "_pid_running_team", "members_spawned_into", "_log"}
                       - s.suite_fns)'''),
    # a check naming a word it keeps out of prose is not printed output
    ('''    suite_lines = set()
    for suite_name in ("run_selftest", "live_checks"):
        suite_fn = funcs.get(suite_name)
        if suite_fn:
            suite_lines.update(range(suite_fn.lineno, suite_fn.end_lineno + 1))''',
     '''    suite_lines = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in s.suite_fns:
            # From the decorator, not the def: a unit registers its ids there,
            # and an id may carry a word this rule keeps out of prose.
            opens = min([node.lineno] + [d.lineno for d in node.decorator_list])
            suite_lines.update(range(opens, node.end_lineno + 1))'''),
]

# An exclusion rewritten above can remove a unit's only use of a name it was
# handed, so the handover is trimmed to what the final text reads.
def drop_unused_handovers(text):
    tree = ast.parse(text)
    doomed = set()
    for fnode in ast.walk(tree):
        if not (isinstance(fnode, ast.FunctionDef) and fnode.name.startswith("_check_")):
            continue
        reads = {n.id for n in ast.walk(fnode)
                 if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
        for st in fnode.body:
            if (isinstance(st, ast.Assign) and len(st.targets) == 1
                    and isinstance(st.targets[0], ast.Name)
                    and isinstance(st.value, ast.Attribute)
                    and getattr(st.value.value, "id", None) == "s"
                    and st.targets[0].id == st.value.attr
                    and st.targets[0].id not in reads):
                doomed.add(st.lineno)
    return "\n".join(ln for i, ln in enumerate(text.splitlines(), 1) if i not in doomed)


missed = []
for old, new in SUBS:
    if out.count(old):
        out = out.replace(old, new)
    else:
        missed.append(old.splitlines()[0][:60])
out = drop_unused_handovers(out)
open(PATH, "w").write(out)
print(f"spliced; {len(SUBS) - len(missed)}/{len(SUBS)} exclusions converted")
for m in missed:
    print("  MISSED:", m)
