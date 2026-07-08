#!/usr/bin/env python3
"""Reconstruct the active conversation path from a Claude Code session JSONL.

Walks `parentUuid` from the most-recent non-sidechain leaf up to the root, so only
the live conversation is emitted: abandoned retry/interrupt branches and subagent
sidechains are dropped. Keeps user/assistant TEXT; `--mode enriched` also keeps
assistant `tool_use` (name + input, clipped); `tool_result` and `thinking` are
always excluded. `<system-reminder>...</system-reminder>` blocks are stripped.

Usage:
    python3 extract.py <SESSION.jsonl> <OUT.md> [--mode text|enriched] [--cap 300]

Then read OUT.md back in ~600-line chunks (the Read tool caps ~25k tokens/call).
In enriched mode each turn is tagged `uuid=<id>`; recover a clipped tool input via
    grep '"uuid":"<id>"' <SESSION.jsonl>   ->  parse message.content
"""
import json
import re
import argparse
import collections
import datetime

SR = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)


def load(path):
    objs = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                objs.append(json.loads(line))
            except Exception:
                pass  # non-JSON / partial lines
    return objs


def active_path(objs):
    """Leaf->root walk; returns root-first list of nodes (the full live history).

    At each step prefer `logicalParentUuid` over `parentUuid`. A /compact writes its summary
    against a synthetic boundary node whose `parentUuid` dead-ends; that node stores the real
    pre-compaction tip in `logicalParentUuid`, so following it walks straight through every
    compaction back to the root. Normal turns have no `logicalParentUuid` and fall back to
    `parentUuid`. The walk always runs to the end — `main()` then reports which slice is newly
    recovered vs. already in context (and where any prior reingest sits) so the caller decides
    how far back to actually read.
    """
    by = {o["uuid"]: o for o in objs if "uuid" in o}
    leaf = None
    for o in reversed(objs):
        if o.get("type") in ("user", "assistant") and "uuid" in o and not o.get("isSidechain"):
            leaf = o
            break
    path, cur, seen = [], leaf, set()
    while cur is not None and cur.get("uuid") not in seen:
        seen.add(cur.get("uuid"))
        path.append(cur)
        nxt = cur.get("logicalParentUuid") or cur.get("parentUuid")
        cur = by.get(nxt) if nxt else None
    path.reverse()
    return path


def clip(v, cap):
    if isinstance(v, str):
        return v if len(v) <= cap else v[:cap] + "… [+%d chars]" % (len(v) - cap)
    if isinstance(v, list):
        return [clip(x, cap) for x in v]
    if isinstance(v, dict):
        return {k: clip(x, cap) for k, x in v.items()}
    return v


def render_tool(b, cap):
    name = b.get("name", "?")
    inp = b.get("input", {})
    lines = ["    ↳ TOOL: %s" % name]
    if isinstance(inp, dict):
        for k, v in inp.items():
            cv = clip(v, cap)
            if isinstance(cv, str) and "\n" in cv:
                lines.append("      %s: |" % k)
                lines += ["        " + ln for ln in cv.splitlines()]
            else:
                lines.append("      %s: %s" % (k, cv if isinstance(cv, str) else json.dumps(cv, ensure_ascii=False)))
    else:
        lines.append("      " + json.dumps(clip(inp, cap), ensure_ascii=False))
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="Reconstruct the active conversation path from a session JSONL.")
    ap.add_argument("jsonl", help="path to the session .jsonl")
    ap.add_argument("out", help="output markdown path")
    ap.add_argument("--mode", choices=("text", "enriched"), default="enriched",
                    help="text = prose only (small); enriched = prose + tool calls (large)")
    ap.add_argument("--cap", type=int, default=300, help="clip tool inputs to N chars (enriched mode)")
    a = ap.parse_args()

    path = active_path(load(a.jsonl))
    turns = 0
    tools = collections.Counter()
    line = 1                # 1-based cursor: the next line number about to be written to the output
    boundaries = []         # (start_line, timestamp) of every compaction summary emitted
    reingests = []          # (turn, start_line) where a prior /reingest-transcript shows up
    with open(a.out, "w", encoding="utf-8") as f:
        def w(s):
            nonlocal line
            f.write(s)
            line += s.count("\n")
        w("# %s transcript (leaf->root active path)\n" % a.mode)
        w("# Source: %s\n" % a.jsonl)
        if a.mode == "enriched":
            w("# Each turn tagged `uuid=<id>`. Tool inputs clipped to %d chars; recover full input via\n" % a.cap)
            w("#   grep '\"uuid\":\"<id>\"' on the source jsonl -> parse message.content. Results & thinking excluded.\n")
        w("\n")
        for o in path:
            msg = o.get("message")
            if not isinstance(msg, dict):
                continue  # ai-title / file-history-snapshot / mode / etc.
            role = msg.get("role")
            if role not in ("user", "assistant"):
                continue
            content = msg.get("content")
            items = []
            if isinstance(content, str):
                t = SR.sub("", content).strip()
                if t:
                    items.append(("text", t))
            elif isinstance(content, list):
                for b in content:
                    if not isinstance(b, dict):
                        continue
                    bt = b.get("type")
                    if bt == "text":
                        t = SR.sub("", b.get("text", "")).strip()
                        if t:
                            items.append(("text", t))
                    elif bt == "tool_use" and role == "assistant" and a.mode == "enriched":
                        items.append(("tool", b))
                        tools[b.get("name", "?")] += 1
            if not items:
                continue
            turns += 1
            start = line
            w("=" * 70 + "\n[%03d] %s  uuid=%s\n" % (turns, role.upper(), o.get("uuid")) + "=" * 70 + "\n")
            texts = []
            for kind, payload in items:
                if kind == "text":
                    w(payload + "\n")
                    texts.append(payload)
                else:
                    w(render_tool(payload, a.cap) + "\n")
            w("\n")
            if o.get("isCompactSummary"):
                boundaries.append((start, o.get("timestamp") or ""))
            blob = "\n".join(texts).lower()
            if "/reingest-transcript" in blob or "transcript (leaf->root active path)" in blob:
                reingests.append((turns, start))

    print("mode=%s  turns=%d  tool_uses=%d  ->  %s" % (a.mode, turns, sum(tools.values()), a.out))
    if tools:
        print("tools:", ", ".join("%s=%d" % (k, v) for k, v in tools.most_common()))

    # ---- reading guidance -------------------------------------------------------------------
    # The walk ran to root, so map every compaction boundary onto the output and present the
    # read-back INTERVALS between them. Each interval is flagged "+" when a prior /reingest-
    # transcript falls inside it, so the caller picks how far back to read and skips a
    # transcript-of-a-transcript instead of ingesting it twice.
    def localtime(ts):
        try:
            return datetime.datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().strftime("%m-%d %H:%M")
        except Exception:
            return ts or "?"
    last = line - 1
    print()
    print("READ-BACK INTERVALS (+ = contains a prior /reingest-transcript):")
    if not boundaries:
        mark = "+" if reingests else " "
        print("  %s lines 1-%d   whole live conversation (no compaction on the active path)" % (mark, last))
    else:
        boundaries.sort()  # by line == chronological, since the output is root-first
        blines = [ln for ln, _ in boundaries]
        bts = {ln: ts for ln, ts in boundaries}
        edges = [1] + blines + [last + 1]
        for i in range(len(edges) - 1):
            lo, hi = edges[i], edges[i + 1]
            mark = "+" if any(lo <= rl < hi for _, rl in reingests) else " "
            if i == 0:
                span = "before %s" % localtime(bts[blines[0]])
            elif i == len(edges) - 2:
                span = "%s -> now (already in your live context)" % localtime(bts[blines[-1]])
            else:
                span = "%s -> %s" % (localtime(bts[blines[i - 1]]), localtime(bts[blines[i]]))
            note = "   <- what the LATEST compaction dropped" if i == len(edges) - 3 else ""
            print("  %s lines %-13s %s%s" % (mark, "%d-%d" % (lo, hi - 1), span, note))
    if reingests:
        print("  prior /reingest-transcript at line(s): %s" % ", ".join(str(rl) for _, rl in sorted(reingests, key=lambda x: x[1])))


if __name__ == "__main__":
    main()
