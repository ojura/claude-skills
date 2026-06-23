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
                src = {k: v for k, v in src.items() if k in ("type", "media_type", "data", "url")}  # shed non-API keys
                out.append({"type": "image", "source": src})           # already API-valid (whitelisted)
            elif images and images.get(it.get("file_uuid"), {}).get("data"):   # empty bytes -> fall through to placeholder
                r = images[it["file_uuid"]]
                out.append({"type": "image", "source": {"type": "base64",
                            "media_type": r["media_type"], "data": r["data"]}})
            else:                                                      # no bytes -> valid text placeholder
                out.append({"type": "text", "text": f"[image omitted from teleport: {it.get('file_uuid', '?')}]"})
        elif t == "document" and not it.get("source") and it.get("file_uuid"):
            out.append({"type": "text", "text": f"[document omitted from teleport: {it['file_uuid']}]"})  # bare file_uuid -> API can't resolve
        elif t in VALID:
            out.append(it)                                             # search_result/tool_reference/resolvable document pass verbatim
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

    # ORPHAN tool_use (bj-tree, measured): claude.ai assistant turns sometimes end on a tool_use
    # that the source never answers (interrupted/abandoned call — measured shape is uniformly
    # [..., tool_use, text], the orphan in the node's last round). The dev API rejects an
    # assistant tool_use not answered by a tool_result in the next user message (400). On the
    # active-leaf path this 400s resume in 9/145 convos (carry AND strip). We answer every
    # convo-wide-unanswered tool_use with a synthetic role:user tool_result stub on the spine,
    # marked synthetic so to_export DROPS it (the export never had it → reverse stays exact).
    answered = set()
    for m in msgs:
        for b in m.get("content") or []:
            if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("tool_use_id"):
                answered.add(b["tool_use_id"])

    def base(uid, parent, ts):
        return {"parentUuid": parent, "isSidechain": False, "uuid": uid, "timestamp": ts,
                "sessionId": sid, "cwd": ctx["cwd"], "version": ctx["version"],
                "gitBranch": ctx["gitBranch"], "userType": ctx["userType"], "entrypoint": ctx["entrypoint"]}

    node_tail = {}   # node_uuid -> uuid of its last ACTUALLY-EMITTED line. Skip-aware: under
                     # thinking='strip' a stripped trailing block must not stay a parent target
                     # (else children dangle); a node that emits nothing maps to its own prev (pass-through).

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

    # forest -> tree (deliberated): claude.ai exports a FOREST — each branch's root parents off the
    # SENTINEL, and editing the first message spawns multiple SENTINEL-rooted branches (39/145
    # convos). CC wants a single-root tree, so when there are 2+ roots we emit ONE virtual null root
    # node and parent every branch off it — the whole forest becomes one connected tree (a 1-root
    # convo is already a tree; no virtual node added). to_export drops it (no escrow), so reverse is exact.
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
        # council guards: ≤1 None-id tool_use per node (the None-tuid pairing relies on it),
        # and real tool ids must stay disjoint from the synth sentinel namespace (no silent cross-wire)
        _tu = [b for b in blocks if isinstance(b, dict) and b.get("type") == "tool_use"]
        assert sum(1 for b in _tu if not (isinstance(b.get("id"), str) and b.get("id"))) <= 1, f"node {nu}: >1 None-id tool_use"
        assert not any(isinstance(b.get("id"), str) and b["id"].startswith("toolu_synth_") for b in _tu), f"node {nu}: real tool_use id in synth namespace"
        ts = m.get("created_at") or "1970-01-01T00:00:00.000Z"
        pmu = m.get("parent_message_uuid")
        prev = tail(pmu) if (pmu in nblk and pmu != SENTINEL) else vroot   # roots hang off the virtual node (tree), else None
        tu_uid = {}                                       # tool_use id -> emitted line uuid (for sourceToolAssistantUUID)
        last_tool_id = None                               # most recent emitted tool_use id, real OR synth (for None-tuid pairing)
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
                # API requires tool_use.id / tool_result.tool_use_id to be valid strings;
                # claude.ai emits id=None on degenerate blocks. Synthesize + pair, escrow originals.
                if t == "tool_use" and not (isinstance(nat.get("id"), str) and nat["id"]):
                    residue["_orig_id"] = nat["id"]; nat["id"] = _toolid(nu, bi)
                elif t == "tool_result" and not (isinstance(nat.get("tool_use_id"), str) and nat["tool_use_id"]):
                    # pair a None tool_use_id to the most recent emitted tool_use id (real OR synth), then
                    # CLEAR it — a tool_use takes exactly ONE result, so a 2nd orphan can't re-grab the same
                    # id (that produced a duplicate tool_use_id -> API 400). Reverse stays exact via _orig_tuid.
                    residue["_orig_tuid"] = nat["tool_use_id"]; nat["tool_use_id"] = last_tool_id or _toolid(nu, bi)
                    last_tool_id = None
                if t == "tool_use":
                    last_tool_id = nat["id"]
                if t == "tool_result":
                    if nat and not nat.get("content"):          # API 400s on empty tool_result content (orig in _orig_content escrow)
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

        # answer any orphan tool_use of this node with a synthetic tool_result stub on the spine
        # (immediately after the owning assistant turn → satisfies the API's next-user-message rule).
        # exportEscrow={_synthetic_stub:True} but NO node_uuid → to_export never sees it (reverse exact).
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
        node_tail[nu] = prev          # node's emitted tail (skip-aware) — children chain off this

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
        for b in m.get("content") or []:                  # empty inner content -> API 400 on replay
            if isinstance(b, dict) and b.get("type") == "tool_result" and not b.get("content"):
                probs.append((l.get("uuid", "?")[:8], "empty tool_result content"))
            elif isinstance(b, dict) and b.get("type") == "thinking" and not (b.get("thinking") or "").strip():
                probs.append((l.get("uuid", "?")[:8], "empty thinking block"))

    # tool_use <-> tool_result pairing on the ACTIVE-LEAF PATH (what `--resume` replays to the API).
    # An assistant tool_use whose id is not answered by a tool_result later on the path 400s the
    # Messages API on the first new turn. Walk leaf->root, then check pairing in path order.
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
    strip_probs, _ = conformance(to_cc(convo, thinking="strip", escrow=True))   # strip is the 'guaranteed resume' mode; test it
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
