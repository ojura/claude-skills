#!/usr/bin/env python3
"""
claude.ai export  ->  LOADABLE Claude Code session JSONL   (+ optional lossless reverse)

The session this emits has to do two things: appear in the resume picker, and survive
`--resume`. Both requirements come from reading real CC session files, not from the
documented format.

  THREADING: the `parentUuid` links form one straight chain in execution order, and that
    chain runs through the role:user tool_result lines as well. Each line's parent is the
    line immediately before it. Blocks inside a node chain one to the next, a node's first
    line parents off the last line of its parent node, and sibling regenerations fork off
    that same last line. Nothing fans out from a single root, and the chain does not restart
    at each node.

  SCHEMA: every user or assistant line has to carry {cwd, entrypoint, gitBranch, isSidechain,
    message, parentUuid, sessionId, timestamp, type, userType, uuid, version}, and an
    assistant message{} additionally needs {content, id, model, role, stop_reason,
    stop_sequence, type, usage}. A tool_result becomes a role:user line plus a top-level
    toolUseResult and sourceToolAssistantUUID. Anything less than this fails to load.

  SYNTHESIS: the export has no id, model, usage, stop_reason, requestId or timestamp, so we
    generate them from the conversation and message uuids. The same input always produces the
    same values, but they are Claude Code's own bookkeeping and the export cannot tell us what
    they were. All lines of one API response share a message.id, and a fresh one starts after
    each tool_result. stop_reason is tool_use when that response called a tool, end_turn
    otherwise.

  THINKING: the account export is the only place thinking signatures survive, so carry is the
    default.
      thinking='carry'  -> replay {thinking, signature} unchanged
      thinking='strip'  -> drop the thinking blocks, keeping only the visible turns
    A block whose signature is null goes out as signature:"", which the API accepts.

  ESCROW (escrow=True): fields that exist only in the export ride along in an `exportEscrow`
    key on the line envelope, which CC's reader ignores, and to_export reads them back to
    rebuild the original conversation exactly. With escrow=False the conversion only runs one
    way and reports what it dropped.
"""
import json, uuid

NS = uuid.UUID("b1ce0000-0000-4000-8000-c1aede000000")
SENTINEL = "00000000-0000-4000-8000-000000000000"

CONV_KEYS = {"uuid", "name", "summary", "created_at", "updated_at", "account", "chat_messages"}
MSG_KEYS  = {"uuid", "content", "sender", "text", "created_at", "updated_at",
             "attachments", "files", "parent_message_uuid"}

REQ_LINE     = {"cwd", "entrypoint", "gitBranch", "isSidechain", "message", "parentUuid",
                "sessionId", "timestamp", "type", "userType", "uuid", "version"}
REQ_ASST_MSG = {"content", "id", "model", "role", "stop_reason", "stop_sequence", "type", "usage"}
REQ_USER_MSG = {"content", "role"}

CTX = {"sessionId": "teleport", "cwd": "/home/juraj/claude-skills", "version": "2.1.185",
       "gitBranch": "main", "model": "claude-opus-4-8", "userType": "external", "entrypoint": "cli"}

USAGE = {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 0,
         "cache_read_input_tokens": 0, "service_tier": "standard"}


def _uid(*p):  return str(uuid.uuid5(NS, "|".join(map(str, p))))
def _mid(*p):  return "msg_" + uuid.uuid5(NS, "m|" + "|".join(map(str, p))).hex
def _rid(*p):  return "req_" + uuid.uuid5(NS, "r|" + "|".join(map(str, p))).hex
def _toolid(*p): return "toolu_synth_" + uuid.uuid5(NS, "t|" + "|".join(map(str, p))).hex  # the prefix keeps these apart from real toolu_0… ids


# ---- converting one block between the export and CC, with the remainder in escrow ----

def _api_tool_result_content(content, images=None):
    """The items inside a claude.ai tool_result.content carry fields the API rejects, such as
    `uuid`, and item types it does not know at all: knowledge, local_resource and
    rag_reference. Resuming with any of them returns a 400. This rewrites each item into a
    form the API accepts, turning text into {type,text}, image into {type,source}, and
    everything else into plain text. The item as it arrived goes into escrow, so the reverse
    conversion still reproduces it.

    Images are the awkward case. claude.ai writes them as {type:image, file_uuid:…}, which
    points at a file on its own servers and carries no inline `source`, so the API has nothing
    to read and drops the block. Pass `images` as {file_uuid: {media_type, data}} with the
    bytes fetched from /api/{org}/files/{uuid}/preview and each one becomes a
    {type:image, source:{type:base64, media_type, data}} block instead. A file_uuid with no
    entry becomes a line of text saying the image was left out. Either way the original
    content sits in the tool_result's `_orig_content`, so the reverse is unaffected."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return "" if content is None else str(content)
    # The item types the API accepts inside a tool_result, taken from its own rejection message:
    VALID = {"document", "image", "search_result", "text", "tool_reference"}
    out = []
    for it in content:
        if not isinstance(it, dict):
            continue
        t = it.get("type")
        if t == "text":
            out.append({k: v for k, v in it.items() if k != "uuid"})   # drop only `uuid`, which is the field that fails
        elif t == "image":
            src = it.get("source")
            if isinstance(src, dict) and ((src.get("type") == "base64" and src.get("data"))
                                          or (src.get("type") == "url" and src.get("url"))):
                src = {k: v for k, v in src.items() if k in ("type", "media_type", "data", "url")}  # shed non-API keys
                out.append({"type": "image", "source": src})           # this one already has bytes the API can read
            elif images and images.get(it.get("file_uuid"), {}).get("data"):   # no bytes means fall through to the text below
                r = images[it["file_uuid"]]
                out.append({"type": "image", "source": {"type": "base64",
                            "media_type": r["media_type"], "data": r["data"]}})
            else:                                                      # nothing to show, so say so in text
                out.append({"type": "text", "text": f"[image omitted from teleport: {it.get('file_uuid', '?')}]"})
        elif t == "document" and not it.get("source") and it.get("file_uuid"):
            out.append({"type": "text", "text": f"[document omitted from teleport: {it['file_uuid']}]"})  # a file_uuid alone is not something the API can look up
        elif t in VALID:
            out.append(it)                                             # search_result, tool_reference and readable documents pass through
        else:                                                          # a type we do not recognise becomes plain text
            out.append({"type": "text", "text": it.get("text") or f"[{t}]"})
    return out


def _block_native(b, images=None):
    """Split one export block into the CC content block and whatever did not fit in it. The
    fields CC understands go into the first, everything else into the second, which travels in
    escrow. `images` supplies bytes for tool_result images, as in _api_tool_result_content."""
    t = b.get("type")
    if t == "text":
        if not (b.get("text") or "").strip():
            return (None, {"_raw": b})                       # the API rejects an empty text block, so keep the whole thing in escrow
        consumed = {"type", "text"}
        nat = {"type": "text", "text": b.get("text", "")}
    elif t == "thinking":
        if not (b.get("thinking") or "").strip():
            return (None, {"_raw": b})   # empty thinking: API 400s "each thinking block must contain thinking"
        consumed = {"type", "thinking", "signature"}
        nat = {"type": "thinking", "thinking": b.get("thinking", ""), "signature": b.get("signature") or ""}
    elif t == "tool_use":
        consumed = {"type", "id", "name", "input"}
        nat = {"type": "tool_use", "id": b.get("id"), "name": b.get("name"), "input": b.get("input")}
    elif t == "tool_result":
        consumed = {"type", "tool_use_id", "content", "is_error"}
        nat = {"type": "tool_result", "tool_use_id": b.get("tool_use_id"),
               "content": _api_tool_result_content(b.get("content"), images)}
        if "is_error" in b:
            nat["is_error"] = b["is_error"]
    elif t in ("image", "document"):
        # top-level image/document (NOT nested in a tool_result): route through the same resolver
        # so it emits a native source/text block instead of being escrow-only (invisible on resume).
        items = _api_tool_result_content([b], images)
        return (items[0] if items else None, {"_raw": b})
    else:
        return (None, {"_raw": b})                       # flag/unknown: no CC home -> escrow whole
    esc = {k: v for k, v in b.items() if k not in consumed}
    if t == "thinking":
        esc["_sig_null"] = b.get("signature") is None
    if t == "tool_result":
        esc["_is_error_present"] = "is_error" in b
        esc["_orig_content"] = b.get("content")   # API-sanitized in nat; original here for reverse
    return (nat, esc)


def _block_restore(nat, esc):
    if "_raw" in esc:
        return esc["_raw"]
    t = nat["type"]
    b = {k: v for k, v in esc.items() if not k.startswith("_")}
    b["type"] = t
    if t == "text":
        b["text"] = nat["text"]
    elif t == "thinking":
        b["thinking"] = nat["thinking"]
        b["signature"] = None if esc.get("_sig_null") else nat["signature"]
    elif t == "tool_use":
        b.update(id=(esc["_orig_id"] if "_orig_id" in esc else nat["id"]), name=nat["name"], input=nat["input"])
    elif t == "tool_result":
        b.update(tool_use_id=(esc["_orig_tuid"] if "_orig_tuid" in esc else nat["tool_use_id"]),
                 content=esc.get("_orig_content"))
        if esc.get("_is_error_present"):
            b["is_error"] = nat["is_error"]
    return b


def _rounds(blocks):
    """Group a node's blocks by the API response each one came from. A response ends at a
    tool_result, so a new group starts after every one of them."""
    out, cur = [], []
    for i, b in enumerate(blocks):
        cur.append(i)
        if b.get("type") == "tool_result":
            out.append(cur); cur = []
    if cur:
        out.append(cur)
    return out


# ---- export -> CC ----

def to_cc(convo, ctx=CTX, thinking="carry", escrow=True, images=None):
    assert thinking in ("carry", "strip")
    msgs = convo.get("chat_messages") or []
    nblk = {m["uuid"]: len(m.get("content") or []) for m in msgs}
    sid = ctx["sessionId"]

    # An assistant turn sometimes ends on a tool_use the conversation never answers, because
    # the call was interrupted or abandoned. The API requires the next user message to answer
    # every tool_use the assistant made, and returns a 400 otherwise, whether we carry thinking
    # or strip it. So for each tool_use that nothing anywhere in the conversation answers, we
    # add a role:user tool_result stub saying the call did not complete. The stub is marked as
    # ours, and to_export drops it again, so the reverse still reproduces the export exactly.
    answered = set()
    for m in msgs:
        for b in m.get("content") or []:
            if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("tool_use_id"):
                answered.add(b["tool_use_id"])

    def base(uid, parent, ts):
        return {"parentUuid": parent, "isSidechain": False, "uuid": uid, "timestamp": ts,
                "sessionId": sid, "cwd": ctx["cwd"], "version": ctx["version"],
                "gitBranch": ctx["gitBranch"], "userType": ctx["userType"], "entrypoint": ctx["entrypoint"]}

    node_tail = {}   # node_uuid -> the uuid of the last line we actually wrote for that node.
                     # Under thinking='strip' a dropped trailing block must not be left as a
                     # parent, or its children point at a line that is not in the file. A node
                     # that writes nothing at all maps to whatever preceded it.

    def tail(node_uuid):
        if node_uuid in node_tail:
            return node_tail[node_uuid]
        n = nblk.get(node_uuid, 0)               # fallback before the node is emitted (parents precede children)
        return _uid(node_uuid, n - 1) if n else _uid(node_uuid, "empty")

    lines = []
    # picker / UI sidecars (off-thread; carry conversation escrow on the title line)
    title = {"type": "ai-title", "aiTitle": convo.get("name") or "(untitled)", "sessionId": sid}
    if escrow:
        title["exportEscrow"] = {"conversation": {k: convo.get(k) for k in CONV_KEYS if k != "chat_messages"},
                                 "conversation_extra": {k: v for k, v in convo.items() if k not in CONV_KEYS}}
    lines += [title, {"type": "mode", "mode": "normal", "sessionId": sid},
              {"type": "permission-mode", "permissionMode": "default", "sessionId": sid}]

    # An export is a forest, not a tree: every branch root parents off the SENTINEL, and
    # editing the first message of a conversation starts another branch rooted there. CC needs
    # a single root, so when a conversation has more than one we add a root node of our own and
    # parent every branch off it, which connects the forest into one tree. A conversation with
    # a single root is already a tree and gets no extra node. The added node carries no escrow,
    # so to_export leaves it out and the reverse still matches the export.
    roots0 = [m for m in msgs if (m.get("parent_message_uuid") in (SENTINEL, None))
              or (m.get("parent_message_uuid") not in nblk)]
    vroot = None
    if len(roots0) > 1:
        vroot = _uid(SENTINEL, "vroot")
        ts0 = (msgs[0].get("created_at") if msgs else None) or "1970-01-01T00:00:00.000Z"
        lines.append({**base(vroot, None, ts0), "type": "user", "isVirtualRoot": True,
                      "message": {"role": "user", "content": [{"type": "text", "text": "."}]}})
        node_tail[vroot] = vroot

    for ni, m in enumerate(msgs):
        nu = m["uuid"]; sender = m.get("sender"); blocks = m.get("content") or []
        # Two things the pairing below relies on. A node may hold at most one tool_use whose
        # id is None, because a second one would have nothing to distinguish it by. And no real
        # tool id may start with toolu_synth_, or a real call and one we invented could collide.
        _tu = [b for b in blocks if isinstance(b, dict) and b.get("type") == "tool_use"]
        assert sum(1 for b in _tu if not (isinstance(b.get("id"), str) and b.get("id"))) <= 1, f"node {nu}: >1 None-id tool_use"
        assert not any(isinstance(b.get("id"), str) and b["id"].startswith("toolu_synth_") for b in _tu), f"node {nu}: real tool_use id in synth namespace"
        ts = m.get("created_at") or "1970-01-01T00:00:00.000Z"
        pmu = m.get("parent_message_uuid")
        prev = tail(pmu) if (pmu in nblk and pmu != SENTINEL) else vroot   # a branch root hangs off the node we added, if there is one
        tu_uid = {}                                       # tool_use id -> the line we wrote it on, for sourceToolAssistantUUID
        last_tool_id = None                               # the id of the most recent tool_use, to match a result that carries none
        node_unanswered = []                              # (emitted_id, line_uuid) for this node's tool_use never answered convo-wide
        first_emitted = True

        node_esc = {"sender": sender, "node_index": ni,
                    "node": {k: m.get(k) for k in ("text", "files", "attachments",
                                                   "created_at", "updated_at", "parent_message_uuid")},
                    "node_extra": {k: v for k, v in m.items() if k not in MSG_KEYS}}

        def attach_escrow(line, bi, residue):
            if not escrow:
                return
            e = {"node_uuid": nu, "ordinal": bi}
            if residue is not None:
                e["block"] = residue
            line["exportEscrow"] = e

        if not blocks:                                    # empty node: one carrier line in the node's OWN role
            u = _uid(nu, "empty")
            role = "user" if sender == "human" else "assistant"
            msg = {"role": role, "content": [{"type": "text", "text": "."}]}   # API rejects empty content
            if role == "assistant":
                msg.update(id=_mid(nu, 0), model=ctx["model"], type="message",
                           stop_reason="end_turn", stop_sequence=None, usage=dict(USAGE))
            ln = {**base(u, prev, ts), "type": role, "message": msg}
            if role == "assistant":
                ln["requestId"] = _rid(nu, 0)
            attach_escrow(ln, "empty", None)
            if escrow:
                ln["exportEscrow"].update(node_esc); ln["exportEscrow"]["empty_node"] = True
            lines.append(ln); node_tail[nu] = u; continue

        for ridx, rnd in enumerate(_rounds(blocks)):
            mid = _mid(nu, ridx)
            stop = "tool_use" if blocks[rnd[-1]].get("type") == "tool_result" else "end_turn"
            for bi in rnd:
                b = blocks[bi]; t = b.get("type")
                if thinking == "strip" and t == "thinking":
                    continue
                u = _uid(nu, bi)
                nat, residue = _block_native(b, images)
                # The API needs tool_use.id and tool_result.tool_use_id to be real strings, but
                # claude.ai leaves them None on some blocks. Invent an id, match the result to the
                # call it belongs to, and keep the original None in escrow for the reverse.
                if t == "tool_use" and not (isinstance(nat.get("id"), str) and nat["id"]):
                    residue["_orig_id"] = nat["id"]; nat["id"] = _toolid(nu, bi)
                elif t == "tool_result" and not (isinstance(nat.get("tool_use_id"), str) and nat["tool_use_id"]):
                    # Attach this result to the most recent tool_use we wrote, then forget that id.
                    # One call takes one result, so if another result also arrives with no id it must
                    # not claim the same call again; that produced a duplicate tool_use_id and a 400.
                    residue["_orig_tuid"] = nat["tool_use_id"]; nat["tool_use_id"] = last_tool_id or _toolid(nu, bi)
                    last_tool_id = None
                if t == "tool_use":
                    last_tool_id = nat["id"]
                if t == "tool_result":
                    if nat and not nat.get("content"):          # the API rejects an empty tool_result; the original is in escrow
                        nat["content"] = [{"type": "text", "text": "[empty result]"}]
                    ln = {**base(u, prev, ts), "type": "user",
                          "message": {"role": "user", "content": [nat] if nat else []},
                          "toolUseResult": (b.get("structured_content") or {"content": b.get("content")}),
                          "sourceToolAssistantUUID": tu_uid.get(b.get("tool_use_id"))}
                else:
                    role = "user" if sender == "human" else "assistant"
                    content = [nat] if nat else []
                    if not content:                            # API rejects empty content for BOTH roles (orig in escrow)
                        content = [{"type": "text", "text": "."}]
                    msg = {"role": role, "content": content}
                    if role == "assistant":
                        msg.update(id=mid, model=ctx["model"], type="message",
                                   stop_reason=stop, stop_sequence=None, usage=dict(USAGE))
                    ln = {**base(u, prev, ts), "type": role, "message": msg}
                    if role == "assistant":
                        ln["requestId"] = _rid(nu, ridx)
                    if t == "tool_use":
                        tu_uid[b.get("id")] = u
                        if b.get("id") not in answered:    # never answered convo-wide (None-id synths are never answered)
                            node_unanswered.append((nat["id"], u))
                attach_escrow(ln, bi, residue)
                if first_emitted and escrow:
                    ln["exportEscrow"].update(node_esc); first_emitted = False
                lines.append(ln)
                prev = u

        # Answer this node's unanswered tool_use calls. The stub goes immediately after the
        # assistant turn that made the call, which is where the API expects to find it. It is
        # marked _synthetic_stub and carries no node_uuid, so to_export never picks it up.
        for oidx, (oid, osrc) in enumerate(node_unanswered):
            su = _uid(nu, "stub", oidx)
            stub = {**base(su, prev, ts), "type": "user",
                    "message": {"role": "user", "content": [
                        {"type": "tool_result", "tool_use_id": oid,
                         "content": "[no result: tool call was not completed in the source conversation]",
                         "is_error": True}]},
                    "toolUseResult": {"content": "[no result: tool call was not completed in the source conversation]"},
                    "sourceToolAssistantUUID": osrc}
            if escrow:
                stub["exportEscrow"] = {"_synthetic_stub": True}
            lines.append(stub)
            prev = su
        node_tail[nu] = prev          # node's emitted tail (skip-aware); children chain off this

    # Resuming starts at the leafUuid on the last last-prompt line and walks parentUuid back to
    # the root to rebuild the conversation. Without that line CC has no entry point and cannot
    # find the conversation at all, so point it at the last message line we wrote.
    leaf = next((l["uuid"] for l in reversed(lines) if l.get("type") in ("user", "assistant")), None)
    if leaf is not None:
        last_user = next((m.get("text") for m in reversed(msgs) if m.get("sender") == "human"), None)
        lines.append({"type": "last-prompt", "leafUuid": leaf,
                      "lastPrompt": (last_user or convo.get("name") or "")[:200], "sessionId": ctx["sessionId"]})
    return lines


# ---- CC -> export (reverse; requires escrow=True output) ----

def to_export(lines):
    from collections import defaultdict
    groups = defaultdict(list); conv = {}
    for ln in lines:
        e = ln.get("exportEscrow")
        if not e:
            continue
        if "conversation" in e:
            conv = dict(e["conversation"]); conv.update(e.get("conversation_extra") or {})
        if "node_uuid" in e:
            groups[e["node_uuid"]].append(ln)
    nodes = []
    for nu, grp in groups.items():
        grp.sort(key=lambda l: (l["exportEscrow"]["ordinal"] != "empty", l["exportEscrow"]["ordinal"]))
        head = next(l for l in grp if "node" in l["exportEscrow"])
        he = head["exportEscrow"]
        msg = {"uuid": nu, "sender": he["sender"]}
        msg.update(he["node"]); msg.update(he.get("node_extra") or {})
        content = []
        for l in grp:
            e = l["exportEscrow"]
            if e.get("empty_node"):
                continue
            nat = (l["message"]["content"] or [None])[0]
            content.append(_block_restore(nat, e.get("block", {})))
        msg["content"] = content
        nodes.append((he["node_index"], msg))
    nodes.sort(key=lambda x: x[0])
    out = dict(conv); out["chat_messages"] = [m for _, m in nodes]
    return out


# ---- validators ----

def conformance(lines):
    """Check the emitted lines against what a real CC session file requires, and against the
    threading rules. Returns the problems found as (uuid, description) pairs, along with how
    many lines have no parent; more than one of those means the file is not a single tree."""
    probs = []
    uids = {l["uuid"] for l in lines if "uuid" in l}
    roots = 0
    for l in lines:
        if l.get("type") not in ("user", "assistant"):
            continue                                      # title, mode and last-prompt lines are not part of the chain
        miss = REQ_LINE - set(l)
        if miss:
            probs.append((l.get("uuid", "?")[:8], f"missing line keys {sorted(miss)}"))
        m = l.get("message", {})
        need = REQ_ASST_MSG if l["type"] == "assistant" else REQ_USER_MSG
        mmiss = need - set(m)
        if mmiss:
            probs.append((l.get("uuid", "?")[:8], f"missing message keys {sorted(mmiss)}"))
        if l["type"] == "assistant" and m.get("stop_reason") not in ("end_turn", "tool_use", "max_tokens", "stop_sequence"):
            probs.append((l.get("uuid", "?")[:8], f"bad stop_reason {m.get('stop_reason')!r}"))
        p = l.get("parentUuid")
        if p is None:
            roots += 1
        elif p not in uids:
            probs.append((l.get("uuid", "?")[:8], f"dangling parentUuid {str(p)[:8]}"))
        for b in m.get("content") or []:                  # an empty block inside the message also fails on replay
            if isinstance(b, dict) and b.get("type") == "tool_result" and not b.get("content"):
                probs.append((l.get("uuid", "?")[:8], "empty tool_result content"))
            elif isinstance(b, dict) and b.get("type") == "thinking" and not (b.get("thinking") or "").strip():
                probs.append((l.get("uuid", "?")[:8], "empty thinking block"))

    # Resuming replays only the path from the leaf back to the root, so that is the path where
    # every tool_use has to be answered. An assistant tool_use with no matching tool_result
    # later on it makes the first new turn fail with a 400. Walk the path, collect the results
    # it contains, then look for calls none of them answer.
    by_uuid = {l["uuid"]: l for l in lines if "uuid" in l}
    leaf = next((l.get("leafUuid") for l in reversed(lines) if l.get("type") == "last-prompt"), None)
    if leaf is not None:
        path, cur, seen = [], leaf, set()
        while cur is not None and cur in by_uuid and cur not in seen:
            seen.add(cur); l = by_uuid[cur]
            if l.get("type") in ("user", "assistant"):
                path.append(l)
            cur = l.get("parentUuid")
        path.reverse()
        answered_on_path = set()
        for l in path:
            for b in (l.get("message") or {}).get("content") or []:
                if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("tool_use_id"):
                    answered_on_path.add(b["tool_use_id"])
        for l in path:
            for b in (l.get("message") or {}).get("content") or []:
                if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("id") not in answered_on_path:
                    probs.append((l.get("uuid", "?")[:8], f"unanswered tool_use {str(b.get('id'))[:14]} on active path"))
    return probs, roots


def audit(convo):
    cc = to_cc(convo, thinking="carry", escrow=True)
    back = to_export(cc)
    probs, roots = conformance(cc)
    strip_probs, _ = conformance(to_cc(convo, thinking="strip", escrow=True))   # check the other thinking mode too
    return {"roundtrip": _diff(convo, back), "conformance": probs, "roots": roots,
            "strip_conformance": strip_probs,
            "lines": len(cc), "msgs": len(convo.get("chat_messages", []))}


def _diff(a, b, path="$"):
    if type(a) is not type(b):
        return f"{path}: {type(a).__name__}!={type(b).__name__}"
    if isinstance(a, dict):
        if set(a) != set(b):
            return f"{path}: keys +{sorted(set(a)-set(b))} -{sorted(set(b)-set(a))}"
        for k in a:
            d = _diff(a[k], b[k], f"{path}.{k}")
            if d: return d
    elif isinstance(a, list):
        if len(a) != len(b):
            return f"{path}: len {len(a)}!={len(b)}"
        for i, (x, y) in enumerate(zip(a, b)):
            d = _diff(x, y, f"{path}[{i}]")
            if d: return d
    elif a != b:
        return f"{path}: {repr(a)[:60]}!={repr(b)[:60]}"
    return None


if __name__ == "__main__":
    import sys
    EXP = "/home/juraj/Downloads/data-54e1eaf8-cb03-42c6-b761-82b9a1387500-1782002660-a8a3e5ec-batch-0000/conversations.json"
    TARGETS = {"79f1c713": "HRZZ (15 branch pts)", "ebd3aff0": "bushy (290)", "3bb854a6": "bushy (46)"}
    data = open(EXP).read(); dec = json.JSONDecoder(); n = len(data); i = data.find("[") + 1
    def skip(i):
        while i < n and data[i] in " \t\r\n,": i += 1
        return i
    convos = []
    while i < n:
        i = skip(i)
        if i >= n or data[i] == "]": break
        o, end = dec.raw_decode(data, i); i = end
        convos.append(o)
    found = {(o.get("uuid") or "")[:8]: o for o in convos if (o.get("uuid") or "")[:8] in TARGETS}
    print("=== v2: loadable real-CC emit + conformance + reverse ===")
    ok_all = True
    for u, lab in TARGETS.items():
        if u not in found: print(f"  {u} {lab}: NOT FOUND"); continue
        r = audit(found[u])
        ok = not r["conformance"] and not r["strip_conformance"] and r["roundtrip"] is None and r["roots"] <= 1
        ok_all &= ok
        print(f"  {u} {lab:22s} {'PASS' if ok else 'FAIL'}  msgs={r['msgs']:4d} lines={r['lines']:5d} "
              f"roots={r['roots']} conformance_violations={len(r['conformance'])} strip_violations={len(r['strip_conformance'])} roundtrip={'ok' if r['roundtrip'] is None else 'BROKEN'}")
        if r["conformance"]: print("      conformance:", r["conformance"][:3])
        if r["strip_conformance"]: print("      strip conformance:", r["strip_conformance"][:3])
        if r["roundtrip"]: print("      roundtrip diff:", r["roundtrip"])
    # Check every conversation, so a future edit that breaks reversibility on an
    # unusual one fails the test instead of slipping through.
    print(f"=== full corpus sweep ({len(convos)} conversations) ===")
    npass = 0; fails = []
    for o in convos:
        u = (o.get("uuid") or "")[:8]
        try:
            r = audit(o)
        except Exception as e:
            fails.append((u, f"raised {type(e).__name__}: {e}")); continue
        if not r["conformance"] and not r["strip_conformance"] and r["roundtrip"] is None and r["roots"] <= 1:
            npass += 1
        else:
            fails.append((u, f"roundtrip={r['roundtrip']} conformance={r['conformance'][:2]} strip={r['strip_conformance'][:2]} roots={r['roots']}"))
    print(f"  {npass}/{len(convos)} PASS (round-trip + conformance)")
    for u, why in fails[:10]:
        print(f"  FAIL {u}: {why}")
    ok_all &= not fails
    print("RESULT:", "ALL PASS" if ok_all else "FAILURES")
    sys.exit(0 if ok_all else 1)
