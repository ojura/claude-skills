#!/usr/bin/env python3
"""
Hydrate a claude.ai conversation into a resumable Claude Code session on disk.

The pure export<->CC transform lives in bijection.py; this is the orchestration
around it (network fetch + file layout + orientation). See TELEPORT_RECIPE.md.

Pieces:
  1. messages + thinking -- bijection.to_cc(convo, images=…) from the account export
                            (the only surface carrying thinking signatures).
  2. tool-result images  -- claude.ai stores them as {type:image, file_uuid:…} with no
                            inline source; we resolve each via /api/{org}/files/{uuid}/preview
                            and emit native CC {type:image, source:{base64}} blocks.
  3. /home/claude tree   -- copied (dir) or extracted (tarball) into <base>/<short>/home.
  4. /mnt/user-data      -- find_files() -> downloaded under the home (best-effort; user
                            uploads are often purged server-side -> 404, skipped).
  5. orientation         -- a generated CLAUDE.md telling CC that the transcript's old
                            /home/claude and /mnt/user-data paths now live under the cwd.

Result: <base>/<short>/home is the session cwd; the JSONL is written into the matching
~/.claude/projects/<slug>/ dir; teleport() returns the `claude --resume …` command.

Enumeration caveat (TELEPORT_RECIPE.md §2): there is no client-side listing of /home/claude,
and the export's own tool_results name only a fraction of the tree (HRZZ: ~5%), so the home
grab itself is model-assisted (a live `tar`/`find`) — supply the result as `home_src`.
"""
import os, re, json, uuid, base64, shutil, tarfile, subprocess, urllib.parse
import bijection as B

DEFAULT_BASE = "~/.claude/teleports"


# ---- helpers ----

def cc_project_slug(cwd):
    """CC maps an absolute cwd to its ~/.claude/projects dir by replacing every non-alphanumeric
    character with '-'. CC (Node/JS) iterates UTF-16 CODE UNITS, so a non-BMP char (emoji, astral
    CJK) becomes TWO dashes — its surrogate pair — not one; we match that so the slug can't diverge
    and lose the session on --resume. ASCII paths (the normal case) are unaffected."""
    b = os.path.abspath(cwd).encode("utf-16-le")
    out = []
    for i in range(0, len(b), 2):
        cu = b[i] | (b[i + 1] << 8)               # one UTF-16 code unit
        out.append(chr(cu) if (48 <= cu <= 57 or 65 <= cu <= 90 or 97 <= cu <= 122) else "-")
    return "".join(out)

def session_id(conv_uuid):
    """Deterministic session id for a conversation -> idempotent re-runs overwrite."""
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
    """Fetch a URL inside the live tab and return {status, ct, b64}. Chunked btoa so
    large binaries don't blow up on per-char string building."""
    js = (f"(async()=>{{const r=await fetch({json.dumps(url)},{{credentials:'include'}});"
          f"const b=await r.arrayBuffer();const u=new Uint8Array(b);let s='';const CH=0x8000;"
          f"for(let i=0;i<u.length;i+=CH)s+=String.fromCharCode.apply(null,u.subarray(i,i+CH));"
          f"return JSON.stringify({{status:r.status,ct:r.headers.get('content-type'),b64:btoa(s)}});}})()")
    return json.loads(client._evaluate(js))

def fetch_images(client, org, uuids):
    """file_uuid -> {media_type, data(base64)} via the files preview endpoint."""
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
    """Populate dest_home from a directory or a tarball. The completion sentinel records the source
    path, so a partial/interrupted copy is not mistaken for done AND re-running with a different
    (e.g. more complete) home_src re-hydrates instead of silently skipping. home_src is untrusted
    (sandbox-produced), so the dir branch keeps symlinks AS links and prunes any whose target
    escapes the tree — the same guarantee filter='data' gives the tarball branch. Returns the file
    count under dest_home."""
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
    """download-file?path= reads ANY path on the conversation's VM (incl. the /mnt/user-data
    fuse mount) as raw bytes — uploads and outputs alike. Returns {status, ct, b64}."""
    url = (f"https://claude.ai/api/organizations/{org}/conversations/{conv}"
           f"/wiggle/download-file?path={urllib.parse.quote(path, safe='')}")
    return _fetch_bytes_b64(client, url)

def hydrate_mnt(client, conv_uuid, dest_home, org, skip_names=("claude_home.tar.gz",)):
    """Pull /mnt/user-data into dest_home/mnt/user-data via download-file, which reads the live
    VM mount directly — uploads AND outputs. (The /files/{uuid}/preview asset store purges user
    uploads; the mount does not, so download-file still returns them.) find_files() supplies the
    index. Best-effort; returns (ok, fail, skipped).

    Two name mismatches to absorb: (1) uploads — claude.ai sanitizes the on-mount filename
    (e.g. spaces -> underscores) while find_files reports the original, so try both; (2) some
    listed output names are historical/superseded (renamed during the run) and no longer exist
    on the current mount — those legitimately 404 and count as fail."""
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
                dest = dest_home + mnt
                base = os.path.abspath(dest_home)
                if os.path.commonpath([os.path.abspath(dest), base]) != base:
                    fail += 1; got = True; break          # a ../ in a claude.ai filename -> refuse to escape home
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                open(dest, "wb").write(base64.b64decode(res["b64"]))
                ok += 1; got = True
                break
        if not got:
            fail += 1
    return ok, fail, skipped


CLAUDE_MD = """\
# Teleported claude.ai conversation — orientation for Claude Code

This session is a **teleport of a claude.ai conversation** ({name!r}, `{conv}`). It
originally ran inside a claude.ai sandbox VM; it now runs locally as Claude Code on the
user's machine. The earlier transcript was written there, so its absolute paths point at
the old sandbox, which is gone.

## Path remapping — read before touching the filesystem

- The old sandbox working directory **`/home/claude` is now THIS directory** — your cwd,
  `{home}`. Where the transcript says `/home/claude/X`, the file is `./X` here.
- Old **`/mnt/user-data/uploads`** and **`/mnt/user-data/outputs`** live under
  **`./mnt/user-data/…`** here (where they were hydrated). If a referenced path is missing,
  it simply wasn't carried over (user uploads are often purged server-side) — the original
  source files are usually elsewhere on this machine, so ask the user rather than recreating.
- Do **not** reach for `/home/claude` or `/mnt/user-data` at their old absolute locations;
  they do not exist on this machine. Everything is under this cwd.

## Tools

The claude.ai sandbox tools map onto Claude Code's: `bash_tool`->`Bash`, `view`->`Read`,
`str_replace`->`Edit`, `create_file`->`Write`, `web_search`->`WebSearch`, `web_fetch`->`WebFetch`,
`present_files`->just write into the cwd. Any other historical sandbox tool in the transcript
(`conversation_search`, `message_compose_v1`, …) has no Claude Code equivalent — use your own tools.
"""

def write_claude_md(dest_home, conv_uuid, conv_name):
    """Write the orientation CLAUDE.md. If the hydrated tree shipped its OWN CLAUDE.md (a sandbox
    /home/claude/CLAUDE.md with now-dead absolute paths), PREPEND the orientation instead of
    silently skipping — else the model gets dead-path guidance, the exact flailing this prevents.
    Prepend (not append) so the path remap is read first. A sentinel marker keeps re-runs from
    re-prepending. Returns 'written'/'prepended'/'present'."""
    path = os.path.join(dest_home, "CLAUDE.md")
    mark = "<!-- teleport-orientation -->"
    body = mark + "\n" + CLAUDE_MD.format(name=conv_name or "(untitled)", conv=conv_uuid, home=dest_home)
    if not os.path.exists(path):
        open(path, "w").write(body); return "written"
    existing = open(path).read()
    if mark in existing:
        return "present"                                    # our orientation already there (re-run)
    open(path, "w").write(body + "\n\n" + existing); return "prepended"  # foreign CLAUDE.md -> prepend, read first


# ---- orchestration ----

def teleport(conv_uuid, export_json, home_src=None, base=DEFAULT_BASE, *,
             thinking="carry", model="claude-opus-4-8", client=None, org=None,
             fetch_images_=True, fetch_mnt=True, force_home=False):
    """Build a resumable CC session for one claude.ai conversation. Returns a dict with the
    sessionId, cwd, project jsonl path, and the `claude --resume` command."""
    base = os.path.expanduser(base)
    convo = load_convo(export_json, conv_uuid)
    if not (convo.get("chat_messages") or []):
        raise ValueError(f"conversation {conv_uuid} has no messages — nothing to teleport (would be a leafless husk)")
    cid = convo["uuid"]
    short = cid[:8]
    home = os.path.join(base, short, "home")

    nfiles = hydrate_home(home_src, home, force=force_home)

    images = {}
    if client and fetch_images_:
        images = fetch_images(client, org, collect_image_uuids(convo))

    sid = session_id(cid)
    ctx = {"sessionId": sid, "cwd": home, "version": cc_version(),
           "gitBranch": git_branch(home), "model": model,
           "userType": "external", "entrypoint": "cli"}
    lines = B.to_cc(convo, ctx=ctx, thinking=thinking, escrow=True, images=images)

    projdir = os.path.expanduser(os.path.join("~/.claude/projects", cc_project_slug(home)))
    os.makedirs(projdir, exist_ok=True)
    jsonl = os.path.join(projdir, sid + ".jsonl")
    with open(jsonl, "w") as f:
        for ln in lines:
            f.write(json.dumps(ln) + "\n")

    wrote_md = write_claude_md(home, cid, convo.get("name"))
    mnt = hydrate_mnt(client, cid, home, org) if (client and fetch_mnt) else None

    return {"sessionId": sid, "cwd": home, "jsonl": jsonl, "lines": len(lines),
            "home_files": nfiles, "images": len(images), "claude_md": wrote_md, "mnt": mnt,
            "resume": f"cd {home} && claude --resume {sid}"}


if __name__ == "__main__":
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from claude_web import ClaudeWeb
    if len(sys.argv) < 3:
        print("usage: teleport.py <conv_uuid> <export conversations.json> [home_src dir|tarball]")
        sys.exit(2)
    conv, exp = sys.argv[1], sys.argv[2]
    home_src = sys.argv[3] if len(sys.argv) > 3 else None
    org = os.environ.get("CLAUDE_ORG")
    with ClaudeWeb() as c:
        if org is None:
            org = c.org_id
        r = teleport(conv, exp, home_src=home_src, client=c, org=org)
    print(json.dumps(r, indent=2))
    print("\n" + r["resume"])
