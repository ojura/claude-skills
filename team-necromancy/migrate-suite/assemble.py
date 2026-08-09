"""Assemble the registered suite from run_selftest's linear body.

The preamble becomes a context built once. Each run of statements ending at a
check becomes a unit registered with the ids it produces. The runner walks the
registry, so listing is a read and a subset is selectable, and nothing about
what a check tests moves.
"""
import ast
import re
import sys

PATH = sys.argv[1]
src = open(PATH).read()
lines = src.splitlines()
tree = ast.parse(src)
WHICH = sys.argv[2] if len(sys.argv) > 2 else "run_selftest"
fn = next(n for n in ast.walk(tree)
          if isinstance(n, ast.FunctionDef) and n.name == WHICH)
body = fn.body
LIVE = WHICH == "live_checks"


def free_loads(node, bound=frozenset()):
    """Names this node reads from outside itself.

    A read inside a nested def, lambda or comprehension belongs to that scope
    when that scope binds the name, so it is not a read of a neighbour's value.
    """
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        args = node.args
        inner = {a.arg for a in list(args.args) + list(args.posonlyargs)
                 + list(args.kwonlyargs) + [args.vararg, args.kwarg] if a}
        for sub in ast.walk(node):
            if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Store):
                inner.add(sub.id)
            elif isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                inner.add(sub.name)
        out = set()
        for st in (node.body if not isinstance(node, ast.Lambda) else [node.body]):
            out |= free_loads(st, bound | inner)
        for d in getattr(node, "decorator_list", []):
            out |= free_loads(d, bound)
        return out
    if isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
        # Each generator's target is in scope for the ones after it, so the
        # second iterable sees the first target and only the first iterable is
        # evaluated outside the comprehension.
        parts = [node.elt] if hasattr(node, "elt") else [node.key, node.value]
        out, inner = set(), set()
        for gen in node.generators:
            out |= free_loads(gen.iter, bound | inner)
            inner |= {t.id for t in ast.walk(gen.target) if isinstance(t, ast.Name)}
            for cond in gen.ifs:
                out |= free_loads(cond, bound | inner)
        for p in parts:
            out |= free_loads(p, bound | inner)
        return out
    if isinstance(node, ast.ExceptHandler) and node.name:
        # The handler binds its name for its own block, and Python deletes it
        # at the end, so a read of it inside is never a read of a neighbour's.
        out = free_loads(node.type, bound) if node.type else set()
        for st in node.body:
            out |= free_loads(st, bound | {node.name})
        return out
    out = set()
    if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
        if node.id not in bound:
            out.add(node.id)
    for child in ast.iter_child_nodes(node):
        out |= free_loads(child, bound)
    return out


def named(node, ctx):
    """Names a statement reads, or every name it binds.

    Binding is not only assignment: a def, a class, an import and a with-as all
    put a name in scope, and counting only Name-in-Store context is what left
    ast, resume_run and a dozen helpers looking undefined when their statements
    moved into a function.
    """
    if ctx is ast.Load:
        return {n.id for n in ast.walk(node)
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    bound = {n.id for n in ast.walk(node)
             if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
    # A comprehension's variable does not outlive it in Python 3, so counting
    # one as bound publishes a name into the context that exists nowhere.
    for comp in ast.walk(node):
        if isinstance(comp, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            for gen in comp.generators:
                bound -= {t.id for t in ast.walk(gen.target) if isinstance(t, ast.Name)}
    # A name assigned inside a nested def belongs to that def, not to the unit
    # holding it, so publishing one to the context names something that is not
    # there. The def's own name is bound; what it assigns internally is not.
    for outer in ast.walk(node):
        if isinstance(outer, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            for inner in ast.walk(outer):
                if inner is outer:
                    continue
                if isinstance(inner, ast.Name) and isinstance(inner.ctx, ast.Store):
                    bound.discard(inner.id)
            for arg in ast.walk(getattr(outer, "args", ast.arguments())):
                if isinstance(arg, ast.arg):
                    bound.discard(arg.arg)
    for n in ast.walk(node):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(n.name)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            for a in n.names:
                bound.add((a.asname or a.name).split(".")[0])
        # Not an except-as name: Python deletes it when the handler ends, so
        # it is bound inside the block and gone by the time a unit publishes.
    return bound


def has_check(st):
    return any(isinstance(c, ast.Call) and getattr(c.func, "id", None) == "check"
               for c in ast.walk(st))


def spelled(node):
    """The literal a check declares, or its shape when it is built per item."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(p.value if isinstance(p, ast.Constant)
                       else "<" + getattr(p.value, "id", "...") + ">" for p in node.values)
    return None


def ids_in(st):
    """(id, description) for each check a statement reports."""
    out = []
    for c in ast.walk(st):
        if isinstance(c, ast.Call) and getattr(c.func, "id", None) == "check" and c.args:
            cid = spelled(c.args[0])
            if cid is None:
                continue
            said = spelled(c.args[1]) if len(c.args) > 1 else None
            # A description can interpolate too. Its shape is no use to a
            # reader, so the rendered text stands where one is known.
            if said is None or "<" in said:
                said = SAID.get(cid, said or "")
            out.append((cid, said))
    return out


SAID = {}
for _ln in open("/tmp/main-rows.txt"):
    _cid, _, _desc = _ln.rstrip("\n").partition("  ")
    if _cid.strip():
        SAID[_cid.strip()] = _desc.strip()
CONCRETE = list(SAID)


def expand(pairs):
    """Concrete ids for a unit that builds its ids in a loop.

    The loop is over a literal, so the ids exist statically even though the
    code spells them once. Declaring the family would make --list print a
    shape nobody can type; declaring the members keeps the registry a list of
    real ids, and registry-declares-what-it-runs fails if a member is added
    without the declaration following.
    """
    out = []
    for cid, said in pairs:
        if "<" not in cid:
            out.append((cid, said))
            continue
        head = cid.split("<")[0]
        out.extend((c, SAID.get(c, said)) for c in CONCRETE
                   if c.startswith(head) and c not in [x for x, _ in out])
    seen, uniq = set(), []
    for cid, said in out:
        if cid not in seen:
            seen.add(cid)
            uniq.append((cid, said))
    return uniq


def text_of(stmts):
    first, last = stmts[0].lineno, stmts[-1].end_lineno
    return [ln[4:] if ln.startswith("    ") else ln for ln in lines[first - 1:last]]


first_check = next(i for i, st in enumerate(body) if has_check(st))
# the runner keeps the live dispatch and the reporting; the rest is context
runner_owned = {"results", "seen_ids", "check"}
preamble = [st for i, st in enumerate(body[:first_check])
            if not (i == 0 and isinstance(st, ast.If))
            and not (isinstance(st, ast.Assign)
                     and any(getattr(t, "id", None) in runner_owned for t in st.targets))
            and not (isinstance(st, ast.FunctionDef) and st.name in runner_owned)]

units, cur = [], []
for st in body[first_check:]:
    cur.append(st)
    if has_check(st):
        units.append(cur)
        cur = []

made = [set().union(*(named(s, ast.Store) for s in u)) for u in units]
ctx_defines = set().union(*(named(s, ast.Store) for s in preamble)) | {
    n.name for n in preamble if isinstance(n, ast.FunctionDef)}

inbound, outbound, seen = [], [set() for _ in units], set(ctx_defines)
for i, u in enumerate(units):
    used = set().union(*(free_loads(s) for s in u))
    ext = sorted(n for n in used - made[i] if n in seen)
    inbound.append(ext)
    for n in ext:
        for j in range(i):
            if n in made[j]:
                outbound[j].add(n)
    seen |= made[i]

CLAIMED = []
slug = re.compile(r"[^a-z0-9]+")
out, used_names = [], set()
out.append(f"def {'_live_context' if LIVE else '_suite_context'}():")
out.append('    """Everything the units share, computed once.')
out.append("")
out.append("    A dependency between units is written down here rather than")
out.append("    inherited from scope, so a unit run on its own says what it wants")
out.append("    instead of reading whatever a neighbour left behind.")
out.append('    """')
out.append("    s = types.SimpleNamespace()")
for t in text_of(preamble):
    out.append("    " + t if t.strip() else "")
out.append("")
for n in sorted(ctx_defines):
    out.append(f"    s.{n} = {n}")
out.append("    return s")
out.append("")
out.append("")

for i, u in enumerate(units):
    got = expand(ids_in(u[-1]) or ids_in(u[0]))
    if not got:
        # A unit that forwards another function's verdicts declares no id of
        # its own. The ids are literals in that function, so they are taken
        # from there; registry-declares-what-it-runs fails if the two drift.
        called = {c.func.id for st in u for c in ast.walk(st)
                  if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
        claimed = {cid for _n, ids in CLAIMED for cid, _s in ids}
        for helper in ast.walk(tree):
            if (isinstance(helper, ast.FunctionDef) and helper.name in called):
                got += [(c.value, SAID.get(c.value, "")) for c in ast.walk(helper)
                        if isinstance(c, ast.Constant) and isinstance(c.value, str)
                        and c.value in CONCRETE
                        and c.value not in [x for x, _ in got]
                        and c.value not in claimed]
    base = slug.sub("_", (got[0][0] if got else f"unit_{i}").lower()).strip("_")[:44]
    name = f"_check_{base or f'unit_{i}'}"
    while name in used_names:
        name += "_"
    used_names.add(name)
    # A comprehension does not see its own additions, so the same id can be
    # appended twice while it is being collected; the registry must declare
    # each once or --list reports a check the run produces one of.
    # A comprehension does not see its own additions, so an id can be
    # collected twice; each is declared once.
    seen_ids, once = set(), []
    for cid, said in got:
        if cid not in seen_ids:
            seen_ids.add(cid)
            once.append((cid, said))
    got = once
    marks = ", ".join(repr(x) for x in got) + (", live=True" if LIVE else "")
    CLAIMED.append((name, got))
    out.append("@unit(" + marks + ")")
    out.append(f"def {name}(s):")
    out.append("    checked = []")
    out.append("")
    out.append('    def check(cid, name, ok, detail=""):')
    out.append("        checked.append((cid, name, ok, detail))")
    out.append("")
    for n in inbound[i]:
        out.append(f"    {n} = s.{n}")
    if inbound[i]:
        out.append("")
    for t in text_of(u):
        out.append("    " + t if t.strip() else "")
    if outbound[i]:
        out.append("")
        for n in sorted(outbound[i]):
            out.append(f"    s.{n} = {n}")
    out.append("    return checked")
    out.append("")
    out.append("")

open("/tmp/converted_live.py" if LIVE else "/tmp/converted_units.py", "w").write("\n".join(out))
print(f"context defines {len(ctx_defines)} names; {len(units)} units; "
      f"{sum(1 for i in inbound if i)} read from an earlier unit")
