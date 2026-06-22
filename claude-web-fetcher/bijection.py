#!/usr/bin/env python3
"""
claude.ai export  ->  LOADABLE Claude Code session JSONL   (+ optional lossless reverse)

Goal (primary): emit a session that actually (a) appears in the picker and (b) `--resume`s.
Design is measured, not guessed — from real CC JSONLs and the bijection council:

  THREADING (bj-tree, measured): a single linear `parentUuid` SPINE in execution order
    that flows THROUGH the role:user tool_result lines. Every line's parent = the
    immediately preceding line. Within a node, blocks chain linearly; a node's head
    parents off its parent node's TAIL (last line); sibling regenerations fork off the
    shared parent tail. There is no star and no per-node reset.

  SCHEMA (measured required floor): every threadable (user/assistant) line carries
    {cwd, entrypoint, gitBranch, isSidechain, message, parentUuid, sessionId, timestamp,
     type, userType, uuid, version}.  assistant message{} carries
     {content, id, model, role, stop_reason, stop_sequence, type, usage}.
    tool_result -> role:user line + top-level toolUseResult + sourceToolAssistantUUID.

  SYNTHESIS: the export lacks id/model/usage/stop_reason/requestId/timestamp — these are
    synthesized (deterministically) so the file loads; they are CC-only and not recovered
    from the export. message.id is shared per API-response round (fresh after each
    tool_result); stop_reason = tool_use if the round called a tool, else end_turn.

  THINKING REPLAY GATE (bj-replay): the dev API cryptographically verifies replayed
    signatures, and a claude.ai-minted signature is the unproven/breaking case (D1);
    16% are null (D2, certain reject under thinking-enabled replay).
      thinking='carry'  -> replay {thinking, signature} verbatim (fidelity; D1 risk)
      thinking='strip'  -> drop thinking blocks (guaranteed resume; loses raw reasoning)

  ESCROW (escrow=True): export-only fields ride in a LINE-LEVEL `exportEscrow` key (CC's
    reader tolerates unknown line keys), letting to_export rebuild the conversation
    losslessly. escrow=False is a one-way loadable teleport; dropped fields are reported.
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
def _toolid(*p): return "toolu_synth_" + uuid.uuid5(NS, "t|" + "|".join(map(str, p))).hex  # sentinel prefix: disjoint from real toolu_0… ids


# ---- block <-> native CC content (core natively, residue escrowed) ----

def _api_tool_result_content(content, images=None):
    """claude.ai tool_result.content items carry extra fields (e.g. `uuid`) and non-API
    item types (knowledge/local_resource/rag_reference) that the dev API rejects on resume.
    Reduce to API-valid items: text->{type,text}, image->{type,source}, others flattened to
    text. The original is escrowed for the lossless reverse.

    claude.ai stores tool_result images as {type:image, file_uuid:…} — a server-side asset
    reference with no inline `source`, which the dev API can't process (it strips them on
    resume). `images` is an optional resolver {file_uuid: {media_type, data(=base64)}} (bytes
    fetched from /api/{org}/files/{uuid}/preview); when present we emit the native CC form
    {type:image, source:{type:base64, media_type, data}}. Unresolved -> text placeholder.
    Reverse is unaffected: the whole original content rides in tool_result `_orig_content`."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return "" if content is None else str(content)
    # API-valid tool_result content item types (measured from the API's own 400):
    VALID = {"document", "image", "search_result", "text", "tool_reference"}
    out = []
    for it in content:
        if not isinstance(it, dict):
            continue
        t = it.get("type")
        if t == "text":
            out.append({k: v for k, v in it.items() if k != "uuid"})   # shed ONLY the field that 400s
        elif t == "image":
            src = it.get("source")
            if isinstance(src, dict) and ((src.get("type") == "base64" and src.get("data"))
                                          or (src.get("type") == "url" and src.get("url"))):
                out.append({"type": "image", "source": src})           # already API-valid
            elif images and it.get("file_uuid") in images:
                r = images[it["file_uuid"]]
                out.append({"type": "image", "source": {"type": "base64",
                            "media_type": r["media_type"], "data": r["data"]}})
            else:                                                      # no bytes -> valid text placeholder
                out.append({"type": "text", "text": f"[image omitted from teleport: {it.get('file_uuid', '?')}]"})
        elif t in VALID:
            out.append(it)                                             # document/search_result/etc. pass verbatim
        else:                                                          # unknown type -> minimal valid text
            out.append({"type": "text", "text": it.get("text") or f"[{t}]"})
    return out


def _block_native(b, images=None):
    """Return (cc_content_block, escrow_residue). Core fields go native; rest -> escrow.
    `images` is an optional {file_uuid: {media_type, data}} resolver for tool_result images."""
    t = b.get("type")
    if t == "text":
        if not (b.get("text") or "").strip():
            return (None, {"_raw": b})                       # empty text: API 400s on empty blocks -> escrow whole
        consumed = {"type", "text"}
        nat = {"type": "text", "text": b.get("text", "")}
    elif t == "thinking":
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
    """Partition a node's blocks into API-response rounds (split AFTER each tool_result)."""
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

    def base(uid, parent, ts):
        return {"parentUuid": parent, "isSidechain": False, "uuid": uid, "timestamp": ts,
                "sessionId": sid, "cwd": ctx["cwd"], "version": ctx["version"],
                "gitBranch": ctx["gitBranch"], "userType": ctx["userType"], "entrypoint": ctx["entrypoint"]}

    def tail(node_uuid):
        n = nblk.get(node_uuid, 0)
        return _uid(node_uuid, n - 1) if n else _uid(node_uuid, "empty")

    lines = []
    # picker / UI sidecars (off-thread; carry conversation escrow on the title line)
    title = {"type": "ai-title", "aiTitle": convo.get("name") or "(untitled)", "sessionId": sid}
    if escrow:
        title["exportEscrow"] = {"conversation": {k: convo.get(k) for k in CONV_KEYS if k != "chat_messages"},
                                 "conversation_extra": {k: v for k, v in convo.items() if k not in CONV_KEYS}}
    lines += [title, {"type": "mode", "mode": "normal", "sessionId": sid},
              {"type": "permission-mode", "permissionMode": "default", "sessionId": sid}]

    for ni, m in enumerate(msgs):
        nu = m["uuid"]; sender = m.get("sender"); blocks = m.get("content") or []
        # council guards: ≤1 None-id tool_use per node (the last_synth_id pairing relies on it),
        # and real tool ids must stay disjoint from the synth sentinel namespace (no silent cross-wire)
        _tu = [b for b in blocks if isinstance(b, dict) and b.get("type") == "tool_use"]
        assert sum(1 for b in _tu if not (isinstance(b.get("id"), str) and b.get("id"))) <= 1, f"node {nu}: >1 None-id tool_use"
        assert not any(isinstance(b.get("id"), str) and b["id"].startswith("toolu_synth_") for b in _tu), f"node {nu}: real tool_use id in synth namespace"
        ts = m.get("created_at") or "1970-01-01T00:00:00.000Z"
        pmu = m.get("parent_message_uuid")
        prev = tail(pmu) if (pmu in nblk and pmu != SENTINEL) else None
        tu_uid = {}                                       # tool_use id -> emitted line uuid (for sourceToolAssistantUUID)
        last_synth_id = None                              # most recent synthesized tool_use id (for None-id pairing)
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

        if not blocks:                                    # empty node: one carrier user line
            u = _uid(nu, "empty")
            ln = {**base(u, prev, ts), "type": "user", "message": {"role": "user", "content": ""}}
            attach_escrow(ln, "empty", None)
            if escrow:
                ln["exportEscrow"].update(node_esc); ln["exportEscrow"]["empty_node"] = True
            lines.append(ln); continue

        for ridx, rnd in enumerate(_rounds(blocks)):
            mid = _mid(nu, ridx)
            stop = "tool_use" if blocks[rnd[-1]].get("type") == "tool_result" else "end_turn"
            for bi in rnd:
                b = blocks[bi]; t = b.get("type")
                if thinking == "strip" and t == "thinking":
                    continue
                u = _uid(nu, bi)
                nat, residue = _block_native(b, images)
                # API requires tool_use.id / tool_result.tool_use_id to be valid strings;
                # claude.ai emits id=None on degenerate blocks. Synthesize + pair, escrow originals.
                if t == "tool_use" and not (isinstance(nat.get("id"), str) and nat["id"]):
                    last_synth_id = _toolid(nu, bi); residue["_orig_id"] = nat["id"]; nat["id"] = last_synth_id
                elif t == "tool_result" and not (isinstance(nat.get("tool_use_id"), str) and nat["tool_use_id"]):
                    residue["_orig_tuid"] = nat["tool_use_id"]; nat["tool_use_id"] = last_synth_id or _toolid(nu, bi)
                if t == "tool_result":
                    ln = {**base(u, prev, ts), "type": "user",
                          "message": {"role": "user", "content": [nat] if nat else []},
                          "toolUseResult": (b.get("structured_content") or {"content": b.get("content")}),
                          "sourceToolAssistantUUID": tu_uid.get(b.get("tool_use_id"))}
                else:
                    role = "user" if sender == "human" else "assistant"
                    content = [nat] if nat else []
                    if role == "user" and not content:        # API: user msg must be non-empty (orig in _raw escrow)
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
                attach_escrow(ln, bi, residue)
                if first_emitted and escrow:
                    ln["exportEscrow"].update(node_esc); first_emitted = False
                lines.append(ln)
                prev = u

    # active-leaf pointer: resume reads the last last-prompt's leafUuid, then walks
    # parentUuid to root. Without it CC can't find the conversation (measured: 103/107
    # real sessions carry one). Point at the most-recent leaf = last emitted message line.
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
    """Return list of (uuid, problem) — real-CC schema + threading violations."""
    probs = []
    uids = {l["uuid"] for l in lines if "uuid" in l}
    roots = 0
    for l in lines:
        if l.get("type") not in ("user", "assistant"):
            continue                                      # sidecars are off-thread
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
    return probs, roots


def audit(convo):
    cc = to_cc(convo, thinking="carry", escrow=True)
    back = to_export(cc)
    probs, roots = conformance(cc)
    return {"roundtrip": _diff(convo, back), "conformance": probs, "roots": roots,
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
        ok = not r["conformance"] and r["roundtrip"] is None
        ok_all &= ok
        print(f"  {u} {lab:22s} {'PASS' if ok else 'FAIL'}  msgs={r['msgs']:4d} lines={r['lines']:5d} "
              f"roots={r['roots']} conformance_violations={len(r['conformance'])} roundtrip={'ok' if r['roundtrip'] is None else 'BROKEN'}")
        if r["conformance"]: print("      conformance:", r["conformance"][:3])
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
        if not r["conformance"] and r["roundtrip"] is None:
            npass += 1
        else:
            fails.append((u, f"roundtrip={r['roundtrip']} conformance={r['conformance'][:2]}"))
    print(f"  {npass}/{len(convos)} PASS (round-trip + conformance)")
    for u, why in fails[:10]:
        print(f"  FAIL {u}: {why}")
    ok_all &= not fails
    print("RESULT:", "ALL PASS" if ok_all else "FAILURES")
    sys.exit(0 if ok_all else 1)
