#!/usr/bin/env python3
"""Reconstruct an active main conversation or one worker's local sidechain.

`--scope main` walks `parentUuid` from the most-recent non-sidechain leaf towards the
root, excluding abandoned retries and worker sidechains. `--scope sidechain` walks
only one worker's `isSidechain` records to their local root. For a fork, the separate
`fork-context-ref` record is reported as the boundary and inherited main-session
history is not emitted. For a general-purpose worker without that record, the local
parentless entry is the boundary.

Where a chain is broken because a recorded parent was never written, the walk rejoins
the newest eligible entry before the break and reports that inferred join instead of
stopping without a word. Keeps user/assistant text; `--mode enriched` also keeps
assistant `tool_use` blocks with clipped inputs. `tool_result` and `thinking` are
always excluded, and `<system-reminder>...</system-reminder>` blocks are stripped.

Output is JSONL by default, one record per line, no banners:
    {"i": 12, "r": "user", "t": "..."}
    {"i": 13, "r": "team", "msgs": [{"from": "cli-ux", "idle": "available"}]}
    {"i": 14, "r": "asst", "t": "...", "x": [{"n": "Bash", "in": {...}}]}
`r` is where the turn came from: user (the operator typed it), team (a teammate
message), task (a task notification), asst, meta (a harness injection), sys
(interrupt markers and other harness-inserted text). Teammate and task blocks are
parsed into fields, and their repeated harness boilerplate is dropped; a teammate
record keeps any prose that is NOT that boilerplate, so nothing real is lost quietly.
`--format md` gives banner-delimited prose instead.

Harness-injected user nodes (`isMeta: true`), meaning skill content dumps,
local-command caveats and session-name reminders, are stubbed to one line by default
(`--meta stub`); they are text blocks, not tool_results, so block-type filtering
alone would keep them in full (a single skill load can be thousands of lines). The
stub names the first line, the line count, and the `sourceToolUseID` when the
injection came from a Skill tool call. `--meta drop` removes them silently, `--meta
keep` restores emit-in-full. Compact-summary nodes are never stubbed; short one-line
meta (e.g. `[Image: ...]` placeholders) is kept as-is.

Usage:
    python3 extract.py <SESSION.jsonl> <OUT> [--scope main|sidechain] [--agent-id ID]
                                             [--mode text|enriched] [--format jsonl|md]
                                             [--cap 300] [--meta stub|drop|keep]

Read the COVERAGE report first if it prints: the walk did not reach the root and the
intervals below it describe less than the whole file. Then read the slice the intervals
point at, which state their own cost in tokens (the Read tool caps ~25k per call). In
enriched mode each record carries `u`; recover a clipped tool input via
    grep '"uuid":"<u>"' <SESSION.jsonl>   ->  parse message.content
"""
import json
import re
import argparse
import collections
import datetime

SR = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)


DECODER = json.JSONDecoder()


def records(line):
    """The JSON records on one line, in order. Normally one. At spawn the harness writes a
    teammate's root user entry (the brief) and a metadata record (agent-name, cost-state)
    with no newline between them, so a strict json.loads refuses the line and the walk
    loses the root. Raises ValueError when a remainder is not a record."""
    out, pos = [], 0
    while pos < len(line):
        obj, end = DECODER.raw_decode(line, pos)
        out.append(obj)
        pos = end
        while pos < len(line) and line[pos].isspace():
            pos += 1
    return out


def load(path):
    objs = []
    with open(path, encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                objs.extend(records(line))
            except Exception:
                print(f"note: line {n} is not JSON and was skipped (a partial line of a killed write)")
    return objs


def walk_path(objs, belongs):
    """Walk one active chain from its newest conversation entry to its local root.

    `belongs` selects every record that may participate in the chain, including tool results
    and attachments. The leaf and inferred joins use only user/assistant records. At each
    step `logicalParentUuid` takes precedence over `parentUuid`, so compacted chains remain
    walkable.
    """
    by = {o["uuid"]: o for o in objs if "uuid" in o and belongs(o)}
    pos, convo = {}, []
    for i, o in enumerate(objs):
        if "uuid" in o and belongs(o):
            pos.setdefault(o["uuid"], i)
            if o.get("type") in ("user", "assistant"):
                convo.append((i, o))
    leaf = convo[-1][1] if convo else None

    path, breaks, seen = [], [], set()
    cur = leaf
    while cur is not None and cur.get("uuid") not in seen:
        seen.add(cur.get("uuid"))
        path.append(cur)
        nxt = cur.get("logicalParentUuid") or cur.get("parentUuid")
        if not nxt:
            break
        if nxt in by:
            cur = by[nxt]
            continue
        # A killed process can leave a parent UUID that was never written. Rejoin only within
        # the selected chain and mark that inferred join in both the rendering and coverage.
        here = pos.get(cur.get("uuid"), len(objs))
        earlier = [o for i, o in convo if i < here and o.get("uuid") not in seen]
        brk = {"at": cur.get("uuid"), "ts": cur.get("timestamp") or "", "missing": nxt,
                "orphaned": len(earlier), "bridged": False, "to": None, "to_ts": ""}
        breaks.append(brk)
        if not earlier:
            break
        brk.update(bridged=True, to=earlier[-1].get("uuid"),
                   to_ts=earlier[-1].get("timestamp") or "")
        cur = earlier[-1]
    path.reverse()
    breaks.reverse()
    return path, breaks


def active_path(objs):
    """Return the root-first active main-session path and its inferred chain breaks."""
    return walk_path(objs, lambda o: not o.get("isSidechain"))


def sidechain_path(objs, requested_agent_id=None):
    """Return one worker's local sidechain without loading its inherited main history.

    Fork transcripts begin with a `fork-context-ref` record followed by local records whose
    `isSidechain` field is true. The reference has no UUID and is not part of the parent chain;
    it is returned as boundary metadata. General-purpose workers may have no reference, in
    which case their local root is the first parentless sidechain record.
    """
    ids = []
    for o in objs:
        if (o.get("isSidechain") and o.get("type") in ("user", "assistant")
                and isinstance(o.get("agentId"), str) and o["agentId"] not in ids):
            ids.append(o["agentId"])
    if requested_agent_id is not None:
        if requested_agent_id not in ids:
            raise ValueError("agent %s has no user/assistant sidechain records" % requested_agent_id)
        agent_id = requested_agent_id
    elif len(ids) == 1:
        agent_id = ids[0]
    elif not ids:
        raise ValueError("the transcript has no user/assistant sidechain records")
    else:
        raise ValueError("the transcript contains multiple sidechain agents (%s); pass --agent-id"
                         % ", ".join(ids))

    belongs = lambda o: o.get("isSidechain") and o.get("agentId") == agent_id
    path, breaks = walk_path(objs, belongs)
    references = [o for o in objs
                  if o.get("type") == "fork-context-ref" and o.get("agentId") == agent_id]
    reference = references[-1] if references else None
    return path, breaks, agent_id, reference, len(references)


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


def meta_stub(txt, node):
    first = txt.lstrip().splitlines()[0].strip()
    if len(first) > 100:
        first = first[:100] + "…"
    src = node.get("sourceToolUseID")
    via = " | via %s" % src if src else ""
    return "[[meta injection stripped: %s (%d lines%s)]]" % (first, txt.count("\n") + 1, via)


TEAM_RE = re.compile(r"<teammate-message\b([^>]*)>(.*?)</teammate-message>", re.S)
ATTR_RE = re.compile(r'([\w-]+)="([^"]*)"')
TASK_RE = re.compile(r"<(task-id|status|summary|result|output-file|tool-use-id)>(.*?)</\1>", re.S)
USAGE_RE = re.compile(r"<(subagent_tokens|tool_uses|duration_ms)>(.*?)</\1>", re.S)


def parse_teammate(text):
    """Structured records for each <teammate-message> block, or None if there are none.

    Each block is `<teammate-message teammate_id=... color=... summary=...>BODY</>`, and BODY
    is either an agent's report or a JSON idle notification. Both get real fields. The prose
    around the blocks is harness boilerplate ("Another Claude session sent a message:" plus
    the permission-laundering paragraph), byte-identical on every one, so it is dropped; any
    residue that is NOT that boilerplate is kept, so nothing real is thrown away silently.
    """
    blocks = TEAM_RE.findall(text)
    if not blocks:
        return None, None
    msgs = []
    for attrs, body in blocks:
        at = dict(ATTR_RE.findall(attrs))
        body = body.strip()
        m = {"from": at.get("teammate_id", "?")}
        if at.get("summary"):
            m["summary"] = at["summary"]
        try:
            payload = json.loads(body)
        except Exception:
            payload = None
        if isinstance(payload, dict) and payload.get("type") == "idle_notification":
            m["idle"] = payload.get("idleReason") or "?"
            if payload.get("failureReason"):
                m["why"] = payload["failureReason"]
            if payload.get("timestamp"):
                m["at"] = payload["timestamp"]
        elif body:
            m["t"] = body
        msgs.append(m)
    residue = TEAM_RE.sub("", text)
    residue = residue.replace("Another Claude session sent a message:", "")
    residue = re.sub(r"This came from another Claude session.*?permission laundering\.", "", residue, flags=re.S)
    residue = residue.strip()
    return msgs, (residue or None)


def parse_task(text):
    """Structured record for a <task-notification>. The <note> element is the same sentence
    every time (a task-notification fires each time the agent stops), so it is dropped."""
    d = {k: v.strip() for k, v in TASK_RE.findall(text)}
    if not d:
        return None
    rec = {k.replace("task-id", "task").replace("output-file", "out").replace("tool-use-id", "call"): v
           for k, v in d.items() if v}
    usage = {k: v.strip() for k, v in USAGE_RE.findall(text)}
    if usage:
        rec["usage"] = usage
    return rec


def source_of(node, text, team_msgs):
    """Who a user-role node actually came from.

    The harness annotates task notifications (`origin.kind`) and prose the operator typed
    (`origin.kind == "human"`, plus `promptSource`). It does NOT annotate teammate messages:
    those carry `type: user`, `userType: external`, no `isMeta`, no `origin`, and a plain
    string content, byte-identical in its marker fields to typed prose. So a teammate message
    is identified by successfully parsing its blocks, not by matching a substring. Everything
    left over (interrupt markers, harness-inserted text) is reported as `sys` rather than
    being passed off as the operator talking.
    """
    if team_msgs:
        return "team"
    origin = node.get("origin")
    kind = origin.get("kind") if isinstance(origin, dict) else None
    if kind == "task-notification":
        return "task"
    if kind == "human":
        return "user"
    if node.get("isMeta"):
        return "meta"
    return "sys"


def main():
    ap = argparse.ArgumentParser(description="Reconstruct an active main or worker conversation path from a session JSONL.")
    ap.add_argument("jsonl", help="path to the session or worker .jsonl")
    ap.add_argument("out", help="output path")
    ap.add_argument("--scope", choices=("main", "sidechain"), default="main",
                    help="main = active lead conversation; sidechain = one worker's local records only")
    ap.add_argument("--agent-id",
                    help="sidechain agent id; inferred when the worker transcript contains exactly one")
    ap.add_argument("--mode", choices=("text", "enriched"), default="enriched",
                    help="text = prose only (small); enriched = prose + tool calls (large)")
    ap.add_argument("--cap", type=int, default=300, help="clip tool inputs to N chars (enriched mode)")
    ap.add_argument("--meta", choices=("stub", "drop", "keep"), default="stub",
                    help="isMeta user nodes (skill dumps, caveats): stub to one line (default), drop, or keep in full")
    ap.add_argument("--format", choices=("jsonl", "md"), default="jsonl",
                    help="jsonl (default): one record per line, no banners. md: banner-delimited prose")
    a = ap.parse_args()

    objs = load(a.jsonl)
    sidechain_agent = None
    fork_reference = None
    fork_reference_count = 0
    if a.scope == "main":
        if a.agent_id:
            ap.error("--agent-id is valid only with --scope sidechain")
        path, breaks = active_path(objs)
    else:
        try:
            path, breaks, sidechain_agent, fork_reference, fork_reference_count = sidechain_path(
                objs, a.agent_id)
        except ValueError as error:
            ap.error(str(error))
    break_at = {s["at"]: s for s in breaks}
    turns = 0
    tools = collections.Counter()
    line = 1                # 1-based cursor: the next line number about to be written to the output
    nbytes = 0              # bytes written so far, so each interval can state its read cost
    boundaries = []         # (start_line, timestamp, start_byte) of every compaction summary emitted
    reingests = []          # (turn, start_line) where a prior /reingest-transcript shows up
    with open(a.out, "w", encoding="utf-8") as f:
        def w(s):
            nonlocal line, nbytes
            f.write(s)
            line += s.count("\n")
            nbytes += len(s.encode("utf-8"))
        def emit(rec, md_body):
            """One record, in whichever format was asked for. `r` is the source of the turn:
            user (typed), team (a teammate message), task (a task notification), asst, meta,
            sys (interrupt markers and other harness-inserted text)."""
            if a.format == "jsonl":
                w(json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n")
            else:
                w(md_body)

        scope_note = ("active main path"
                      if a.scope == "main"
                      else "local sidechain only; inherited main history omitted at fork-context-ref")
        head = {"r": "head", "mode": a.mode, "scope": a.scope, "src": a.jsonl,
                "note": "%s; r=user|team|task|asst|meta|sys" % scope_note}
        if sidechain_agent:
            head["agent"] = sidechain_agent
        if fork_reference:
            head["fork"] = {
                "parentSessionId": fork_reference.get("parentSessionId"),
                "parentLastUuid": fork_reference.get("parentLastUuid"),
            }
        if a.mode == "enriched":
            head["tools"] = ("x[] holds tool calls, inputs clipped to %d chars; recover a full "
                             "input with grep '\"uuid\":\"<u>\"' on src" % a.cap)
        title = "active main path" if a.scope == "main" else "local sidechain"
        emit(head, "# %s transcript (%s)\n# Source: %s\n%s\n"
             % (a.mode, title, a.jsonl,
                "# Each turn tagged `uuid=<id>`. Tool inputs clipped to %d chars; recover full input via\n"
                "#   grep '\"uuid\":\"<id>\"' on the source jsonl -> parse message.content.\n" % a.cap
                if a.mode == "enriched" else ""))
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
            # Harness-injected meta (isMeta) rides ordinary text blocks; stub or drop it
            # at the emission step only, so the walk and the compaction-boundary report
            # below still see every node. Compact summaries are exempt (they anchor the
            # read-back intervals), as are short one-liners like [Image: ...] placeholders,
            # which a stub would lengthen, not shorten.
            if o.get("isMeta") and a.meta != "keep" and not o.get("isCompactSummary"):
                meta_text = "\n".join(p for k, p in items if k == "text")
                if meta_text and not ("\n" not in meta_text and len(meta_text) <= 120):
                    non_text = [it for it in items if it[0] != "text"]
                    if a.meta == "drop":
                        items = non_text
                    else:
                        items = [("text", meta_stub(meta_text, o))] + non_text
            # A break sits between this node and whatever the walk joined below it. Emit it
            # before the skip test so it is never silently dropped along with an empty node.
            brk = break_at.get(o.get("uuid"))
            if brk:
                if brk["bridged"]:
                    note = ("CHAIN BREAK, BRIDGED. The record above is NOT this record's recorded "
                            "parent. This entry's parent %s exists in no file on disk (a killed "
                            "process left a last-prompt naming an entry it never wrote), so the walk "
                            "rejoined the newest preceding conversation entry. THIS JOIN IS INFERRED, "
                            "NOT RECORDED: the records either side may not be consecutive."
                            % brk["missing"])
                else:
                    note = ("CHAIN BREAK. This entry's parent %s exists in no file on disk, and no "
                            "earlier conversation entry remains to rejoin, so the walk ends here."
                            % brk["missing"])
                emit({"r": "break", "t": note},
                     "!" * 70 + "\n[[" + note + "]]\n" + "!" * 70 + "\n\n")
            if not items:
                continue
            turns += 1
            start, start_byte = line, nbytes
            texts = [p for k, p in items if k == "text"]
            body = "\n".join(texts)
            team, residue = (None, None) if role == "assistant" else parse_teammate(body)
            src = "asst" if role == "assistant" else source_of(o, body, team)
            rec = {"i": turns, "r": src}
            lines = []
            if src == "team":
                rec["msgs"] = team
                for m in team:
                    who = m["from"] + (" (%s)" % m["summary"] if m.get("summary") else "")
                    if "idle" in m:
                        lines.append("<%s> idle: %s%s" % (who, m["idle"],
                                                          " | " + m["why"] if m.get("why") else ""))
                    else:
                        lines.append("<%s>\n%s" % (who, m.get("t", "")))
                if residue:
                    rec["t"] = residue
                    lines.append(residue)
            elif src == "task" and parse_task(body):
                rec.update(parse_task(body))
                lines.append("task %s [%s] %s\n%s" % (rec.get("task", "?"), rec.get("status", "?"),
                                                      rec.get("summary", ""), rec.get("result", "")))
            elif body:
                rec["t"] = body
                lines.append(body)
            calls = [{"n": p.get("name", "?"), "in": clip(p.get("input", {}), a.cap)}
                     for k, p in items if k == "tool"]
            if calls:
                rec["x"] = calls
                lines += [render_tool(p, a.cap) for k, p in items if k == "tool"]
            if a.mode == "enriched":
                rec["u"] = o.get("uuid")
            emit(rec, "--- [%03d] %-4s uuid=%s\n%s\n\n"
                 % (turns, src, o.get("uuid"), "\n".join(lines)))
            if o.get("isCompactSummary"):
                boundaries.append((start, o.get("timestamp") or "", start_byte))
            blob = "\n".join(texts).lower()
            if ("/reingest-transcript" in blob
                    or "transcript (leaf->root active path)" in blob
                    or "transcript (active main path)" in blob
                    or "transcript (local sidechain)" in blob):
                reingests.append((turns, start))

    print("scope=%s  mode=%s  turns=%d  tool_uses=%d  ->  %s"
          % (a.scope, a.mode, turns, sum(tools.values()), a.out))
    if tools:
        print("tools:", ", ".join("%s=%d" % (k, v) for k, v in tools.most_common()))

    # ---- reading guidance -------------------------------------------------------------------
    # Map every compaction boundary the walk reached onto the output and present the read-back
    # INTERVALS between them. Each interval is flagged "+" when a prior /reingest-transcript
    # falls inside it, so the caller picks how far back to read and skips a transcript-of-a-
    # transcript instead of ingesting it twice.
    #
    # The walk does NOT always reach the root, so coverage is reported first and never assumed:
    # a dangling parent orphans everything below it, and any compaction boundary down there is
    # invisible to the interval map. Reporting "lines 1-N before <ts>" while silently omitting
    # most of the file is the failure this section exists to prevent.
    def localtime(ts):
        try:
            return datetime.datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().strftime("%m-%d %H:%M")
        except Exception:
            return ts or "?"

    on_path = {o.get("uuid") for o in path}
    if a.scope == "main":
        belongs = lambda o: not o.get("isSidechain")
    else:
        belongs = lambda o: o.get("isSidechain") and o.get("agentId") == sidechain_agent
    all_bounds = [o for o in objs if o.get("isCompactSummary") and belongs(o)]
    unreached = [o for o in all_bounds if o.get("uuid") not in on_path]
    def is_convo(o):
        return o.get("type") in ("user", "assistant") and "uuid" in o and belongs(o)
    convo_total = sum(1 for o in objs if is_convo(o))
    convo_walked = sum(1 for o in path if is_convo(o))

    if a.scope == "sidechain":
        print("sidechain agent=%s" % sidechain_agent)
        if fork_reference:
            print("fork boundary: parent session=%s  parent tail=%s"
                  % (fork_reference.get("parentSessionId") or "?",
                     fork_reference.get("parentLastUuid") or "?"))
            if fork_reference_count > 1:
                print("COVERAGE: %d fork-context-ref records match this agent; using the last one"
                      % fork_reference_count)
        else:
            print("fork boundary: none (general-purpose worker local root)")

    if breaks or unreached:
        print()
        print("COVERAGE: the walk did NOT cleanly reach the root")
        for s in breaks:
            verb = "bridged" if s["bridged"] else "STOPPED"
            print("  %s at %s: parent %s exists in no file" % (verb, localtime(s["ts"]), s["missing"][:8]))
            if s["bridged"]:
                print("      rejoined the newest preceding entry (%s). INFERRED, not recorded;"
                      % localtime(s["to_ts"]))
                print("      the turns either side of that join may not be consecutive")
            else:
                print("      %d earlier conversation entries in this file were never reached"
                      % s["orphaned"])
        if unreached:
            print("  %d compaction boundar%s in this file %s NOT on the walked path, so the"
                  % (len(unreached), "y" if len(unreached) == 1 else "ies",
                     "is" if len(unreached) == 1 else "are"))
            print("  intervals below do not account for %s:"
                  % ("it" if len(unreached) == 1 else "them"))
            for o in unreached:
                kind = "pseudocompaction" if o.get("logicalParentUuid") else "/compact"
                print("      %s  %s  uuid=%s" % (localtime(o.get("timestamp") or ""), kind, str(o.get("uuid"))[:8]))
        print("  walked %d of %d conversation entries in this file." % (convo_walked, convo_total))

    last = line - 1
    print()
    print("READ-BACK INTERVALS (+ = contains a prior /reingest-transcript):")
    if not boundaries:
        mark = "+" if reingests else " "
        if breaks:
            tail = "no compaction on the WALKED path, but the walk broke; see COVERAGE"
        elif a.scope == "sidechain":
            tail = ("whole local sidechain; inherited main history stops at fork-context-ref"
                    if fork_reference else "whole local sidechain; no fork-context-ref in this worker")
        else:
            tail = "whole live conversation (no compaction on the active path)"
        print("  %s lines 1-%d  ~%dk tok  %s" % (mark, last, nbytes // 4000, tail))
    else:
        boundaries.sort()  # by line == chronological, since the output is root-first
        blines = [ln for ln, _, _ in boundaries]
        bts = {ln: ts for ln, ts, _ in boundaries}
        bbytes = {ln: b for ln, _, b in boundaries}
        edges = [1] + blines + [last + 1]
        edge_bytes = [0] + [bbytes[ln] for ln in blines] + [nbytes]
        for i in range(len(edges) - 1):
            lo, hi = edges[i], edges[i + 1]
            mark = "+" if any(lo <= rl < hi for _, rl in reingests) else " "
            if i == 0:
                span = "before %s" % localtime(bts[blines[0]])
                if breaks:
                    span += "  (line 1 is NOT the start; see COVERAGE)"
            elif i == len(edges) - 2:
                span = "%s -> now (already in your live context)" % localtime(bts[blines[-1]])
            else:
                span = "%s -> %s" % (localtime(bts[blines[i - 1]]), localtime(bts[blines[i]]))
            note = "   <- what the LATEST compaction dropped" if i == len(edges) - 3 else ""
            cost = (edge_bytes[i + 1] - edge_bytes[i]) // 4000
            print("  %s lines %-13s ~%3dk tok  %s%s" % (mark, "%d-%d" % (lo, hi - 1), cost, span, note))
    if reingests:
        print("  prior /reingest-transcript at line(s): %s" % ", ".join(str(rl) for _, rl in sorted(reingests, key=lambda x: x[1])))


if __name__ == "__main__":
    main()
