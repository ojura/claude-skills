#!/usr/bin/env python3
"""Hydrate claude.ai conversations into resumable Claude Code sessions on disk.

The pure export<->CC transform lives in bijection.py; this is the orchestration around it
(network fetch + file layout + orientation). See TELEPORT_RECIPE.md.

LAYOUT: all of an org's conversations share one Claude Code project.

    ~/.claude/teleports/<org-uuid>/                 <- the single cwd == the single project
      CLAUDE.md                                     <- shared orientation + conversation index
      .teleport-sessions.json                       <- session_id -> conversation (hook index)
      .claude/{settings.json,orient.py}             <- SessionStart re-orientation hook
      <conv-uuid>/CLAUDE.md                         <- per-conversation path remap
      <conv-uuid>/home/claude/                      <- sandbox /home/claude         (recipe §2)
      <conv-uuid>/mnt/user-data/{outputs,uploads}/  <- sandbox /mnt/user-data       (recipe §3)

CC derives the project directory from the session cwd, so one project requires one shared
cwd: every session resumes at the org root and reaches its own tree at ./<conv-uuid>/. A
sandbox absolute path /X is therefore <conv-uuid>/X locally, which is why `home/claude` and
`mnt/user-data` are mirrored literally under each conversation.

Because the cwd is shared, the CLAUDE.md that CC injects from it is common to every session
and cannot say which conversation a given session is. Three things carry that identity
instead, and each takes over where the one before it stops applying:
  1. an isMeta <system_notice> injected as the root of each transcript, naming that
     conversation and its tree;
  2. a project-scoped SessionStart hook that injects the same text again on
     startup/resume/clear/compact. CC's compact_boundary line carries parentUuid:None, so
     nothing before a compaction is sent to the API and the notice is no longer in context;
  3. the shared CLAUDE.md, which is always present but states only the general rule.

A conversation arrives here in four parts. The messages and their thinking blocks come from
the account export through bijection.to_cc, which is the only place the thinking signatures
survive. Tool-result images need a second fetch: claude.ai stores them as
{type:image, file_uuid:…} with no bytes attached, so each one is read from
/api/{org}/files/{uuid}/preview and written into the transcript as a base64 image block. The
/home/claude tree is copied from a directory or unpacked from a tarball. And /mnt/user-data is
listed with find_files() and downloaded file by file, which often comes up short because
claude.ai deletes user uploads from its asset store after a while.

Getting the /home/claude tree is the one step that needs the model. Nothing in the API lists
that directory, and the file listings that happen to appear in the export's own tool results
cover only a small part of what a conversation built, so ask the model for a live `find` or
`tar` and pass the result in as `home_src`. TELEPORT_RECIPE.md §2 goes into this.

Teleporting one conversation at a time into the same org root merges into the session index
and rebuilds the shared CLAUDE.md, so the conversations already there keep working.
"""
import os, re, json, uuid, base64, shutil, tarfile, subprocess, urllib.parse
import bijection as B

DEFAULT_BASE = "~/.claude/teleports"
SESSION_INDEX = ".teleport-sessions.json"


# ---- helpers ----

def cc_project_slug(cwd):
    """Work out which directory under ~/.claude/projects CC will use for a given cwd. CC builds
    the name by replacing every character that is not a letter or digit with '-'. Being written
    in JavaScript it walks UTF-16 code units, so a character outside the basic plane, such as an
    emoji, produces two dashes rather than one, because it is stored as a surrogate pair. We
    reproduce that, otherwise the name we compute differs from the one CC looks in and --resume
    finds nothing. Plain ASCII paths, which is almost all of them, are unaffected."""
    b = os.path.abspath(cwd).encode("utf-16-le")
    out = []
    for i in range(0, len(b), 2):
        cu = b[i] | (b[i + 1] << 8)               # one UTF-16 code unit
        out.append(chr(cu) if (48 <= cu <= 57 or 65 <= cu <= 90 or 97 <= cu <= 122) else "-")
    return "".join(out)

def session_id(conv_uuid):
    """The session id for a conversation, derived from its uuid so that it never changes.
    Re-running the teleport therefore overwrites the previous session instead of adding one."""
    return str(uuid.uuid5(B.NS, "teleport-session|" + conv_uuid))

def cc_version(default="2.1.185"):
    try:
        out = subprocess.run(["claude", "--version"], capture_output=True, text=True, timeout=10).stdout
        m = re.search(r"(\d+\.\d+\.\d+)", out)
        return m.group(1) if m else default
    except Exception:
        return default

def git_branch(home, default="main"):
    git = os.path.join(home, ".git")
    if os.path.isfile(git):                              # worktree/submodule: .git is a "gitdir: <path>" pointer
        try:
            gd = open(git).read().split("gitdir:", 1)[1].strip()
            head = os.path.join(gd if os.path.isabs(gd) else os.path.join(home, gd), "HEAD")
        except Exception:
            return default
    else:
        head = os.path.join(git, "HEAD")
    try:
        ref = open(head).read().strip()
    except Exception:
        return default                                   # no .git -> default
    if not ref.startswith("ref:"):
        return "HEAD"                                    # detached HEAD (raw sha) -> CC records literal "HEAD"
    return ref.split("refs/heads/", 1)[1] if "refs/heads/" in ref else ref.split("/", 1)[-1]  # keep full slash branch

def load_convo(export_json, conv_uuid):
    data = json.load(open(export_json))
    for c in data:
        u = c.get("uuid", "")
        if u == conv_uuid or u.startswith(conv_uuid):
            return c
    raise KeyError(f"conversation {conv_uuid} not in {export_json}")

def org_uuid(export_json):
    """The org uuid, read offline from the export directory name:

        data-<org-uuid>-<unix-ts>-<job-nonce>-batch-0000

    The leading uuid is the same for every export of one account and differs between accounts.
    It is the {org} of POST /api/organizations/{org}/export_data, the same id the file API is
    addressed by. Falls back to the account uuid inside conversations.json when the export sits
    in a renamed directory."""
    d = os.path.basename(os.path.dirname(os.path.abspath(export_json)))
    m = re.match(r"data-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})-\d+-", d)
    if m:
        return m.group(1)
    convos = json.load(open(export_json))
    acct = (convos[0].get("account") or {}).get("uuid") if convos else None
    if not acct:
        raise ValueError("cannot determine the org uuid from the export dir name; pass org=…")
    return acct


# ---- tool-result images ----

def collect_image_uuids(convo):
    out = []
    for m in convo.get("chat_messages") or []:
        for b in m.get("content") or []:
            if b.get("type") == "tool_result" and isinstance(b.get("content"), list):
                for cb in b["content"]:
                    if (isinstance(cb, dict) and cb.get("type") == "image"
                            and cb.get("file_uuid") and "source" not in cb
                            and cb["file_uuid"] not in out):
                        out.append(cb["file_uuid"])
    return out

def _fetch_bytes_b64(client, url):
    """Fetch a URL from inside the logged-in browser tab and return {status, ct, b64}. The
    base64 encoding runs over 32k slices, because building the string one character at a time
    exhausts the argument limit on a large file."""
    js = (f"(async()=>{{const r=await fetch({json.dumps(url)},{{credentials:'include'}});"
          f"const b=await r.arrayBuffer();const u=new Uint8Array(b);let s='';const CH=0x8000;"
          f"for(let i=0;i<u.length;i+=CH)s+=String.fromCharCode.apply(null,u.subarray(i,i+CH));"
          f"return JSON.stringify({{status:r.status,ct:r.headers.get('content-type'),b64:btoa(s)}});}})()")
    return json.loads(client._evaluate(js))

def fetch_images(client, org, uuids):
    """Fetch the bytes for each image, keyed by file_uuid, from the files preview endpoint.
    An image that cannot be fetched is left out, and the caller writes a placeholder for it."""
    res = {}
    for u in uuids:
        try:
            r = _fetch_bytes_b64(client, f"https://claude.ai/api/{org}/files/{u}/preview")
            if r.get("status") == 200 and r.get("b64"):
                res[u] = {"media_type": (r.get("ct") or "image/webp").split(";")[0].strip(),
                          "data": r["b64"]}
        except Exception:
            pass
    return res


# ---- filesystem hydration ----

def hydrate_home(home_src, dest_home, force=False):
    """Fill dest_home, which is <conv>/home/claude, from either a directory or a tarball, and
    return how many files ended up there.

    A marker file records which source the copy finished from. That distinguishes a completed
    copy from one that was interrupted part way, and it also means that handing over a second,
    more complete home_src copies again instead of deciding there is nothing to do.

    home_src was produced inside the sandbox, so it is not trusted. The directory branch copies
    symlinks as symlinks rather than following them, then removes any whose target lands outside
    dest_home. The tarball branch gets the same protection from filter='data', which refuses
    absolute paths, paths containing .. and links that point outside the archive."""
    os.makedirs(dest_home, exist_ok=True)
    sentinel = os.path.join(dest_home, ".teleport_hydrated")
    recorded = open(sentinel).read().strip() if os.path.exists(sentinel) else None
    if home_src and (force or recorded != os.path.abspath(home_src)):
        if os.path.isdir(home_src):
            for name in os.listdir(home_src):
                s, d = os.path.join(home_src, name), os.path.join(dest_home, name)
                if os.path.lexists(d):
                    (shutil.rmtree if os.path.isdir(d) and not os.path.islink(d) else os.remove)(d)
                if os.path.islink(s):
                    shutil.copy2(s, d, follow_symlinks=False)   # copy the link AS a link, don't deref
                elif os.path.isdir(s):
                    shutil.copytree(s, d, dirs_exist_ok=True, symlinks=True)   # symlinks=True: don't deref host files in
                else:
                    shutil.copy2(s, d)
            droot = os.path.realpath(dest_home)           # prune symlinks whose target escapes the tree (untrusted source)
            for r, dirs, files in os.walk(dest_home):
                for n in dirs + files:
                    p = os.path.join(r, n)
                    if os.path.islink(p) and not (os.path.realpath(p) + os.sep).startswith(droot + os.sep):
                        os.remove(p)
        elif tarfile.is_tarfile(home_src):
            with tarfile.open(home_src) as t:
                # the home tarball is produced inside the untrusted sandbox; filter='data'
                # rejects ../, absolute paths, and escaping links (tar-slip / CVE-2007-4559)
                t.extractall(dest_home, filter="data")
        else:
            raise ValueError(f"home_src is neither a dir nor a tarball: {home_src}")
        with open(sentinel, "w") as f:                   # mark complete only after a full copy/extract
            f.write(os.path.abspath(home_src) + "\n")
    return sum(len(f) for _, _, f in os.walk(dest_home)) - (1 if os.path.exists(sentinel) else 0)  # don't count the sentinel

def _download_vm_path(client, org, conv, path):
    """Read one absolute path off the conversation's VM as raw bytes. download-file?path= is not
    restricted to any subtree; it reaches the /mnt/user-data mount as readily as anywhere else,
    which is how both uploads and presented outputs come back. Returns {status, ct, b64}."""
    url = (f"https://claude.ai/api/organizations/{org}/conversations/{conv}"
           f"/wiggle/download-file?path={urllib.parse.quote(path, safe='')}")
    return _fetch_bytes_b64(client, url)

def hydrate_mnt(client, conv_uuid, cdir, org, skip_names=("claude_home.tar.gz",)):
    """Copy /mnt/user-data into <cdir>/mnt/user-data, taking the list of what is there from
    find_files() and the bytes from download-file. Reading the mount directly matters for
    uploads: the /files/{uuid}/preview asset store deletes them after a few weeks, while the
    file stays on the mount. Since each conversation's directory mirrors the sandbox's absolute
    paths, the destination is cdir followed by the sandbox path. Returns how many files were
    copied, failed and skipped; a failure here is not fatal.

    Two ways a name can fail to match. An upload is stored on the mount under a sanitised
    filename, with spaces turned into underscores, while find_files reports the name the user
    gave it, so try both spellings. And an output that was renamed during the conversation is
    still listed under its earlier name, which is no longer on the mount; those 404 and count
    as failures, correctly."""
    ok = fail = skipped = 0
    for r in client.find_files(conv_uuid):
        if r.kind == "wiggle":
            cands = [r.path]                                 # already the /mnt/user-data/outputs/… path
        else:
            nm = getattr(r, "name", None) or r.path          # upload: FileRef.path is a uuid
            names = list(dict.fromkeys([nm, nm.replace(" ", "_")]))   # mount name is sanitized
            cands = ["/mnt/user-data/uploads/" + n for n in names]
        if os.path.basename(cands[0]) in skip_names:
            skipped += 1; continue
        got = False
        for mnt in cands:
            try:
                res = _download_vm_path(client, org, conv_uuid, mnt)
            except Exception:
                continue
            if res.get("status") == 200 and res.get("b64") is not None:
                dest = cdir + mnt
                base = os.path.abspath(cdir)
                if os.path.commonpath([os.path.abspath(dest), base]) != base:
                    fail += 1; got = True; break          # a ../ in a claude.ai filename -> refuse to escape the conv dir
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                open(dest, "wb").write(base64.b64decode(res["b64"]))
                ok += 1; got = True
                break
        if not got:
            fail += 1
    return ok, fail, skipped


# ---- orientation: notice, hook, CLAUDE.md ----

NOTICE = """\
<system_notice>
This session is a **teleported claude.ai conversation**, resumed in Claude Code. The messages
below were written inside a claude.ai sandbox VM that no longer exists. This notice is
orientation only; do not respond to it.

  conversation:  {conv}  ({name})
  session cwd:   {root}   (shared by all teleported conversations in this org)
  YOUR tree:     {root}/{conv}

The absolute paths in the transcript point at that sandbox. Each one maps into your tree by
replacing the leading slash with your tree:

  /home/claude/X    ->  {root}/{conv}/home/claude/X
  /mnt/user-data/X  ->  {root}/{conv}/mnt/user-data/X

Do NOT look for `/home/claude` or `/mnt/user-data` at their old absolute locations; they do
not exist on this machine. If a file the transcript mentions is missing under your tree, it
was not carried over; ask the user rather than recreating it. `./CLAUDE.md` maps the sandbox
tools onto Claude Code's and indexes the other teleported conversations sharing this cwd.
</system_notice>"""

ROOT_MD = """\
<!-- teleport-orientation -->
# Teleported claude.ai conversations

This is one Claude Code project holding **{n} teleported claude.ai conversations** from
claude.ai org `{org}`. The directory is named after the org uuid, so conversations from a
second org sit beside it under `{base}/` rather than mixing in. Each of these conversations
originally ran inside its own claude.ai sandbox VM, and they all now resume here, sharing this
one working directory (`{root}`). Their transcripts were written in those sandboxes, so the
absolute paths in them refer to machines that no longer exist.

## Where the transcript's paths went

Every conversation owns a subdirectory named by its uuid. A sandbox absolute path `/X` is
`./<conv-uuid>/X` here:

| transcript says | it is here |
|---|---|
| `/home/claude/foo.py` | `./<conv-uuid>/home/claude/foo.py` |
| `/mnt/user-data/outputs/report.pdf` | `./<conv-uuid>/mnt/user-data/outputs/report.pdf` |
| `/mnt/user-data/uploads/scan.jpg` | `./<conv-uuid>/mnt/user-data/uploads/scan.jpg` |

**Which `<conv-uuid>` is yours?** Every session in this project shares this working
directory, so this file cannot tell you; your own transcript can. It opens with a
`<system_notice>` that names your conversation and its directory, and a SessionStart hook
prints the same thing again after a compaction, when that opening line is no longer in
context. If you have neither, find your conversation in the index below by its title, or read
`exportEscrow.conversation.uuid` from the first line of your transcript. Each conversation
directory also holds a `CLAUDE.md` giving these same paths with the uuid already filled in.

Do not look for `/home/claude` or `/mnt/user-data` at those absolute paths. They are not on
this machine. If the transcript mentions a file that is not in your conversation's directory,
it was not carried over, so ask the user for it rather than writing a replacement.

## Tools

Each sandbox tool in the transcript has a Claude Code equivalent: `bash_tool` is `Bash`, `view`
is `Read`, `str_replace` is `Edit`, `create_file` is `Write`, `web_search` is `WebSearch`, and
`web_fetch` is `WebFetch`. Where the transcript calls `present_files`, write the file into your
conversation's directory instead. The remaining sandbox tools, such as `conversation_search`
and `message_compose_v1`, have no equivalent here; use your own tools to do the same job.

## Conversation index

| conv-uuid | title | msgs | created |
|---|---|---|---|
{index}
"""

CONV_MD = """\
<!-- teleport-orientation -->
# {conv}: {name}

You are in the tree of one teleported claude.ai conversation. Its sandbox paths map here as:

- `/home/claude/…`   -> `{root}/{conv}/home/claude/…`
- `/mnt/user-data/…` -> `{root}/{conv}/mnt/user-data/…`

The session cwd is the org root `{root}`, one level up. See its `CLAUDE.md` for the full
orientation and the tool mapping.
"""

ORIENT_PY = '''#!/usr/bin/env python3
"""SessionStart hook: inject the teleport orientation for this session.

CC's compact_boundary line carries parentUuid:None, so after a compaction nothing before it
is sent to the API and the <system_notice> at the head of the transcript is no longer in
context (it remains reachable through logicalParentUuid, which the display uses). The
CLAUDE.md at the cwd is still injected, but it is shared by every session in this project
and cannot name which conversation this one is.

So this hook runs on startup, resume, clear and compact, maps the session_id (which is
uuid5(conv_uuid), hence stable) to its conversation through .teleport-sessions.json, and
returns that conversation's orientation as additionalContext.

It lives in this project's .claude/settings.json and does nothing anywhere else on the
machine. A session that is not in the index produces {} and injects nothing.
"""
import sys, json, os

def main():
    try:
        ev = json.load(sys.stdin)
    except Exception:
        return {}
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        idx = json.load(open(os.path.join(root, "SESSION_INDEX_NAME")))
    except Exception:
        return {}
    ent = idx.get(ev.get("session_id"))
    if not ent:
        return {}                                   # not a teleported session -> no-op
    return {"hookSpecificOutput": {
        "hookEventName": "SessionStart",
        "additionalContext": ent["notice"] + (
            "\\n(Injected again by the teleport SessionStart hook, source=%s, because a "
            "compaction drops the notice at the head of the transcript.)" % ev.get("source", "?"))}}

json.dump(main(), sys.stdout)
'''.replace("SESSION_INDEX_NAME", SESSION_INDEX)

HOOK_SETTINGS = {"hooks": {"SessionStart": [
    {"matcher": "startup|resume|clear|compact",
     "hooks": [{"type": "command",
                "command": "python3 \"$CLAUDE_PROJECT_DIR/.claude/orient.py\"",
                "timeout": 10}]}]}}


def inject_notice(lines, cid, name, root, ctx):
    """Prepend an isMeta <system_notice> as the transcript's root, naming the conversation this
    session belongs to and where its tree is. CC uses {type:user, isMeta:true} for injected
    non-conversational context and writes such a line at parentUuid:None head position itself
    (the <local-command-caveat> line), so this reuses an existing line type.

    The CLAUDE.md that CC injects from the cwd is common to every session in the project, so it
    cannot name this conversation's tree. The notice can.

    The reverse stays exact: the escrow carries no `node_uuid`, so to_export() skips this line
    exactly as it skips the synthetic tool_result stubs and the virtual root."""
    u = B._uid(cid, "system_notice")
    first = next(i for i, l in enumerate(lines) if l.get("type") in ("user", "assistant"))
    for l in lines:                       # whatever was root until now, including to_cc's added root
        if l.get("type") in ("user", "assistant") and l.get("parentUuid") is None:
            l["parentUuid"] = u
    notice = {"parentUuid": None, "isSidechain": False, "uuid": u,
              "timestamp": lines[first]["timestamp"], "sessionId": ctx["sessionId"],
              "cwd": ctx["cwd"], "version": ctx["version"], "gitBranch": ctx["gitBranch"],
              "userType": ctx["userType"], "entrypoint": ctx["entrypoint"],
              "type": "user", "isMeta": True,
              "message": {"role": "user", "content": [
                  {"type": "text", "text": NOTICE.format(conv=cid, name=name, root=root)}]},
              "exportEscrow": {"_synthetic_notice": True}}
    return lines[:first] + [notice] + lines[first:]


def write_conv_md(cdir, conv_uuid, conv_name, root):
    """Write the orientation for one conversation, at the top of its directory. If the sandbox
    tree came with a CLAUDE.md of its own, that one stays where it is, at
    <cdir>/home/claude/CLAUDE.md, still describing paths that no longer exist; ours sits a level
    above it and is read first. A marker line in the file stops a second run from stacking
    another copy on top. Returns whether the file was written, prepended to, or already ours."""
    path = os.path.join(cdir, "CLAUDE.md")
    mark = "<!-- teleport-orientation -->"
    body = CONV_MD.format(conv=conv_uuid, name=conv_name or "(untitled)", root=root)
    if not os.path.exists(path):
        open(path, "w").write(body); return "written"
    existing = open(path).read()
    if mark in existing:
        open(path, "w").write(body); return "present"       # ours already: refresh in place
    open(path, "w").write(body + "\n\n" + existing); return "prepended"


def write_root(root, org, sessions):
    """Rewrite the org root's CLAUDE.md and hook from the session index, after merging the
    sessions just written into whatever was already recorded there. Rebuilding from the merged
    index rather than from this run's sessions alone is what lets conversations be teleported
    one at a time without the earlier ones dropping out of the index and the hook."""
    idx_path = os.path.join(root, SESSION_INDEX)
    merged = {}
    if os.path.exists(idx_path):
        try:
            merged = json.load(open(idx_path))
        except Exception:
            merged = {}
    merged.update(sessions)
    json.dump(merged, open(idx_path, "w"), indent=2)

    rows = sorted(merged.values(), key=lambda v: (v.get("created") or "", v.get("conv") or ""))
    index = "\n".join(f"| `{v['conv']}` | {v.get('name') or '(untitled)'} | "
                      f"{v.get('msgs', '?')} | {v.get('created', '?')} |" for v in rows)
    open(os.path.join(root, "CLAUDE.md"), "w").write(
        ROOT_MD.format(n=len(merged), org=org, root=root,
                       base=os.path.expanduser(DEFAULT_BASE), index=index))

    hd = os.path.join(root, ".claude")
    os.makedirs(hd, exist_ok=True)
    p = os.path.join(hd, "orient.py")
    open(p, "w").write(ORIENT_PY)
    os.chmod(p, 0o755)
    settings = os.path.join(hd, "settings.json")
    cur = {}
    if os.path.exists(settings):
        try:
            cur = json.load(open(settings))
        except Exception:
            cur = {}
    cur.setdefault("hooks", {})["SessionStart"] = HOOK_SETTINGS["hooks"]["SessionStart"]
    json.dump(cur, open(settings, "w"), indent=2)
    return len(merged)


# ---- orchestration ----

def conversation_title(convo):
    """The title the resume picker will show. claude.ai leaves `name` empty on a conversation it
    never got round to naming, and the literal "(untitled)" tells you nothing when several of
    them sit in the list together, so fall back to the opening line the user typed, and to the
    date when the export carries no text at all."""
    name = (convo.get("name") or "").strip()
    if name:
        return name
    for m in convo.get("chat_messages") or []:
        if m.get("sender") != "human":
            continue
        text = (m.get("text") or "").strip()
        if not text:
            text = " ".join(b.get("text", "") for b in (m.get("content") or [])
                            if isinstance(b, dict) and b.get("type") == "text").strip()
        if text:
            one = " ".join(text.split())
            return one[:57] + "…" if len(one) > 58 else one
    return "claude.ai %s (no text in export)" % (convo.get("created_at") or "")[:10]


def teleport_one(convo, root, org, *, home_src=None, thinking="carry", model="claude-opus-4-8",
                 client=None, fetch_images_=True, fetch_mnt=True, force_home=False, version=None):
    """Teleport one conversation into an org root that already exists. Returns what was written
    as a dict. The caller has to call write_root() afterwards, which is what lets a whole export
    write the shared CLAUDE.md, hook and index once at the end rather than once per
    conversation."""
    cid = convo["uuid"]
    cdir = os.path.join(root, cid)
    home = os.path.join(cdir, "home", "claude")
    for sub in ("home/claude", "mnt/user-data/outputs", "mnt/user-data/uploads"):
        os.makedirs(os.path.join(cdir, sub), exist_ok=True)

    nfiles = hydrate_home(home_src, home, force=force_home)

    images = {}
    if client and fetch_images_:
        images = fetch_images(client, org, collect_image_uuids(convo))

    sid = session_id(cid)
    ctx = {"sessionId": sid, "cwd": root, "version": version or cc_version(),
           "gitBranch": git_branch(root), "model": model,
           "userType": "external", "entrypoint": "cli"}
    lines = B.to_cc(convo, ctx=ctx, thinking=thinking, escrow=True, images=images)
    name = conversation_title(convo)
    for ln in lines:                      # the picker reads aiTitle; give it the resolved title
        if ln.get("type") == "ai-title":
            ln["aiTitle"] = name
    lines = inject_notice(lines, cid, name, root, ctx)

    projdir = os.path.expanduser(os.path.join("~/.claude/projects", cc_project_slug(root)))
    os.makedirs(projdir, exist_ok=True)
    jsonl = os.path.join(projdir, sid + ".jsonl")
    with open(jsonl, "w") as f:
        for ln in lines:
            f.write(json.dumps(ln) + "\n")

    wrote_md = write_conv_md(cdir, cid, name, root)
    mnt = hydrate_mnt(client, cid, cdir, org) if (client and fetch_mnt) else None
    probs, roots = B.conformance(lines)

    return {"conv": cid, "name": name, "sessionId": sid, "jsonl": jsonl, "cdir": cdir,
            "home": home, "lines": len(lines), "msgs": len(convo.get("chat_messages") or []),
            "created": (convo.get("created_at") or "")[:10], "images": len(images),
            "home_files": nfiles, "claude_md": wrote_md, "mnt": mnt,
            "conformance": probs, "roots": roots,
            "session_entry": {"conv": cid, "name": name, "msgs": len(convo.get("chat_messages") or []),
                              "created": (convo.get("created_at") or "")[:10],
                              "notice": NOTICE.format(conv=cid, name=name, root=root)}}


def teleport(conv_uuid, export_json, home_src=None, base=DEFAULT_BASE, *,
             thinking="carry", model="claude-opus-4-8", client=None, org=None,
             fetch_images_=True, fetch_mnt=True, force_home=False):
    """Build a resumable Claude Code session for one claude.ai conversation, in the project
    belonging to its org, merging into that project if it already holds other conversations.
    Returns a dict describing what was written, including the `claude --resume` command."""
    convo = load_convo(export_json, conv_uuid)
    if not (convo.get("chat_messages") or []):
        raise ValueError(f"conversation {conv_uuid} has no messages, so there is nothing to teleport")
    org = org or org_uuid(export_json)
    root = os.path.join(os.path.expanduser(base), org)
    os.makedirs(root, exist_ok=True)
    r = teleport_one(convo, root, org, home_src=home_src, thinking=thinking, model=model,
                     client=client, fetch_images_=fetch_images_, fetch_mnt=fetch_mnt,
                     force_home=force_home)
    write_root(root, org, {r["sessionId"]: r["session_entry"]})
    r.update(root=root, org=org, cwd=root,
             resume=f"cd {root} && claude --resume {r['sessionId']}")
    return r


def teleport_export(export_json, base=DEFAULT_BASE, *, org=None, thinking="carry",
                    model="claude-opus-4-8", client=None, fetch_images_=True, fetch_mnt=True,
                    home_srcs=None, force_home=False):
    """Teleport every conversation in an export into a single project for its org. `home_srcs`
    optionally supplies the /home/claude trees, keyed by conversation uuid or by a prefix of
    one, each pointing at a directory or a tarball. Returns the org root, the project directory
    and one row per conversation; a conversation with no messages is reported with `skipped`
    instead of being written."""
    convos = json.load(open(export_json))
    org = org or org_uuid(export_json)
    root = os.path.join(os.path.expanduser(base), org)
    os.makedirs(root, exist_ok=True)
    version = cc_version()
    home_srcs = home_srcs or {}

    rows, sessions = [], {}
    for c in convos:
        cid = c.get("uuid", "")
        if not (c.get("chat_messages") or []):
            rows.append({"conv": cid, "name": c.get("name") or "", "skipped": "no messages"})
            continue
        hs = next((v for k, v in home_srcs.items() if cid.startswith(k)), None)
        r = teleport_one(c, root, org, home_src=hs, thinking=thinking, model=model,
                         client=client, fetch_images_=fetch_images_, fetch_mnt=fetch_mnt,
                         force_home=force_home, version=version)
        sessions[r["sessionId"]] = r["session_entry"]
        rows.append(r)

    write_root(root, org, sessions)
    projdir = os.path.expanduser(os.path.join("~/.claude/projects", cc_project_slug(root)))
    return root, projdir, rows


if __name__ == "__main__":
    import sys, argparse
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

    ap = argparse.ArgumentParser(description="Teleport claude.ai conversations into a Claude Code project.")
    ap.add_argument("export_json", help="conversations.json from an account data export")
    ap.add_argument("--conv", help="teleport only this conversation (uuid or prefix)")
    ap.add_argument("--home-src", help="dir|tarball of /home/claude (with --conv)")
    ap.add_argument("--org", help="override the org uuid inferred from the export dirname")
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--thinking", choices=["carry", "strip"], default="carry")
    ap.add_argument("--offline", action="store_true",
                    help="do not open a browser; skip tool-result images and /mnt/user-data")
    a = ap.parse_args()

    client = None
    org = a.org or org_uuid(a.export_json)
    if a.offline:
        if a.conv:
            r = teleport(a.conv, a.export_json, home_src=a.home_src, base=a.base,
                         thinking=a.thinking, org=org, fetch_images_=False, fetch_mnt=False)
            rows, root = [r], r["root"]
            projdir = os.path.dirname(r["jsonl"])
        else:
            root, projdir, rows = teleport_export(a.export_json, base=a.base, org=org,
                                                  thinking=a.thinking, fetch_images_=False,
                                                  fetch_mnt=False)
    else:
        from claude_web import ClaudeWeb
        with ClaudeWeb() as c:
            org = a.org or c.org_id
            if a.conv:
                r = teleport(a.conv, a.export_json, home_src=a.home_src, base=a.base,
                             thinking=a.thinking, client=c, org=org)
                rows, root = [r], r["root"]
                projdir = os.path.dirname(r["jsonl"])
            else:
                root, projdir, rows = teleport_export(a.export_json, base=a.base, org=org,
                                                      thinking=a.thinking, client=c)

    for r in rows:
        if "skipped" in r:
            print(f"{r['conv'][:8]}  SKIP ({r['skipped']})"); continue
        bad = r["conformance"] or r["roots"] > 1
        print(f"{r['conv'][:8]}  {r['lines']:5d} lines  imgs={r['images']:2d}  "
              f"home={r['home_files']:5d}  "
              f"{'VIOLATIONS ' + str(r['conformance'][:2]) if bad else 'OK'}  {r['name'][:40]}")
    print(f"\norg          {org}")
    print(f"root (cwd)   {root}")
    print(f"project      {projdir}  ({sum(1 for r in rows if 'skipped' not in r)} sessions)")
    print(f"resume       cd {root} && claude --resume")
