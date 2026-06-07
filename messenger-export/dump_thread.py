#!/usr/bin/env python3
"""Dump a non-E2EE Messenger conversation (full history + media) to a self-contained HTML bundle.

Scope and consent: this exports YOUR OWN Messenger account's conversations from a browser
YOU are logged into. The vault key it reads decrypts every thread in that profile. Do not
run it against an account you do not own, or a thread whose other party has not consented to
being archived. It is a privacy-sensitive data extractor, not a surveillance tool.

Authoritative method: force a COMPLETE LightSpeed sync via the range-query sproc to both
thread boundaries, then read the local DB and decrypt every message with the profile vault
key. Do NOT rely on UI scroll/WS capture - the scroll stalls and silently drops middle
ranges (observed: a thread that scrolled to 2,940 messages actually had 15,454 in the DB).

The completeness guarantee is ENFORCED, not hoped for: the script refuses to write any output
unless exactly one contiguous range row remains with hasMoreBefore=false AND hasMoreAfter=false.
Every failure path (CDP error, stalled walk, fragmented ranges, decrypt failures, expired or
junk media) aborts loudly or is marked visibly in the artifact, rather than producing a
confident partial.

Requires:
  - cdp-daemon running on 127.0.0.1:7799 (see ../cdp-daemon), CONNECTED to the user's Chrome.
  - A messenger.com tab OPEN to the target thread (ReStore materializes messages per-tab,
    so the sync + reads must run against that tab; this script opens/navigates one).
  - python3 -m pip install pynacl, and curl on PATH.

Usage:
  dump_thread.py <threadKey> <contactName> <outDir> [selfId] [selfName=Me] [tz]

  selfId   - your own account FBID. Omit to auto-derive it from the page (CurrentUserInitialData
             .USER_ID, falling back to the c_user cookie). Messages from this id render right/blue.
  tz       - an IANA timezone for rendered timestamps (e.g. Europe/Zagreb). Omit for the machine's
             zone (resolved from /etc/localtime so historical DST is correct).

threadKey = the contact's FBID. For a regular thread it's the /t/<id> URL id; for an
/e2ee/t/<e2eeid> thread it's still the contact FBID, which you must look up first - see SKILL.md
("Finding the threadKey for an /e2ee/ thread") for the exact in-page query.

Outputs (a bundle, written 0600 inside an OUT dir created 0700, because they are decrypted
private data; keep the directory together and off shared/synced locations):
  index.html       - the transcript; references media/ by relative path
  media/           - downloaded photos/files/videos/audio
  messages.json    - the decrypted rows {mid, sender, ts, text, status}; FULL plaintext, the
                     single most sensitive artifact
"""
import sys, json, urllib.request, base64, html, datetime, os, re, urllib.parse, hashlib, subprocess, concurrent.futures, time, shutil
from nacl.secret import SecretBox   # fail fast if pynacl is missing, before any CDP work

BASE = "http://127.0.0.1:7799"
TK        = sys.argv[1]
NAME      = sys.argv[2]
OUT       = sys.argv[3]
SELF_ID   = sys.argv[4] if len(sys.argv) > 4 and sys.argv[4] else None   # None -> auto-derive
SELF_NAME = sys.argv[5] if len(sys.argv) > 5 else "Me"

def _local_tz():
    try:
        from zoneinfo import ZoneInfo
        name = os.readlink("/etc/localtime").split("zoneinfo/", 1)[1]   # real IANA zone -> correct historical DST
        return ZoneInfo(name), name
    except Exception:
        tz = datetime.datetime.now().astimezone().tzinfo
        print("[WARN] could not resolve an IANA local zone; using fixed offset %s (pass tz for correct historical DST)" % tz)
        return tz, str(tz)
if len(sys.argv) > 6 and sys.argv[6]:
    from zoneinfo import ZoneInfo; TZ = ZoneInfo(sys.argv[6]); TZNAME = sys.argv[6]
else:
    TZ, TZNAME = _local_tz()

IMG_EXT   = {"jpg", "jpeg", "png", "webp", "gif"}
AUD_EXT   = {"mp3", "m4a", "aac", "ogg", "wav", "opus"}

def die(msg): sys.exit("[abort] " + msg)
def get(p, t=60):  return json.loads(urllib.request.urlopen(BASE + p, timeout=t).read().decode())
def post(p, o, t=120):
    try:    return json.loads(urllib.request.urlopen(BASE + p, data=json.dumps(o).encode(), timeout=t).read().decode())
    except Exception as e: return {"_err": str(e)}
def cdp(m, p, sid=None, t=120):
    b = {"method": m, "params": p, "timeout": t}   # daemon-side cdp_call timeout (defaults to 15s if omitted)
    if sid: b["sessionId"] = sid
    return post("/cdp", b, t + 15)                  # HTTP read timeout a bit longer than the daemon's wait
def val(r): return r.get("result", {}).get("result", {}).get("value")
def ev(expr, sid, t=60, await_promise=False):
    return val(cdp("Runtime.evaluate", {"expression": expr, "returnByValue": True, "awaitPromise": await_promise}, sid, t))

# --- preflight: pynacl (imported above), curl, and a CONNECTED daemon ---
if not shutil.which("curl"):
    die("curl not found on PATH - install curl (used to download media)")
os.umask(0o077)   # everything this process creates (incl. curl-downloaded media) defaults to owner-only
try:
    _st = get("/status", 5)
except Exception:
    die("cdp-daemon not reachable at %s - start it: python3 ../cdp-daemon/cdp_daemon.py & (see cdp-daemon SKILL)" % BASE)
if not _st.get("connected"):
    die("cdp-daemon is up but CDP is NOT connected (state=%s) - launch Chrome with the debug port, click Allow, "
        "then POST /reconnect (see cdp-daemon SKILL)" % (_st.get("state") or _st.get("cdp_state")))

# --- attach to (or open) the thread's page, and confirm it actually materialized ---
# Thread is "ready" when its LightSpeed runtime is up AND a range row for TK is materialized.
# (An innerText heuristic misses loaded-but-backgrounded restored tabs.) Activate the tab first
# so a discarded/restored tab actually loads. Match on host+path, not a bare URL substring.
def _is_thread_tab(url):
    try:
        u = urllib.parse.urlparse(url)
        return u.netloc.endswith("messenger.com") and re.match(r"^/t/%s(?:[/?#]|$)" % re.escape(TK), u.path) is not None
    except Exception:
        return False
def ls_ready(sid):
    js = ("(async function(){try{var db=await require('LSDatabaseSingleton').getLSDatabaseSingletonPromiseOrValue();"
          "var I64=require('I64'),ReQL=require('ReQL');"
          "var rr=await ReQL.toArrayAsync(ReQL.fromTableAscending(db.tables.messages_ranges_v2__generated));"
          "return rr.some(function(r){try{return I64.to_string(r.threadKey)==='%s';}catch(e){return false;}})?1:0;"
          "}catch(e){return 0;}})()" % TK)
    return ev(js, sid, 15, await_promise=True)
def wait_ls(sid, tries=20):
    for _ in range(tries):
        if ls_ready(sid): return True
        time.sleep(2)
    return False
def thread_page():
    try:
        targets = get("/targets")
    except Exception as e:
        die("could not list browser tabs - cdp-daemon/CDP not reachable (%s); POST /reconnect" % e)
    for tgt in [t for t in targets if t["type"] == "page" and _is_thread_tab(t.get("url", ""))]:
        sid = post("/attach", {"targetId": tgt["targetId"]}).get("sessionId")
        if not sid: continue
        cdp("Page.bringToFront", {}, sid)                   # un-discard a backgrounded restored tab
        if wait_ls(sid): return sid
        cdp("Page.navigate", {"url": "https://www.messenger.com/t/%s" % TK}, sid)
        if wait_ls(sid): return sid
    ptid = cdp("Target.createTarget", {"url": "https://www.messenger.com/t/%s" % TK}).get("result", {}).get("targetId")
    if not ptid: die("could not open a tab for thread %s" % TK)
    sid = post("/attach", {"targetId": ptid}).get("sessionId")
    if sid:
        cdp("Page.bringToFront", {}, sid)
        if wait_ls(sid): return sid
    die("thread %s did not materialize in tab after wait - check login / threadKey / network" % TK)

PGS = thread_page()

# --- resolve selfId (auto-derive if not supplied) ---
if SELF_ID is None:
    SELF_ID = ev("(function(){try{var u=require('CurrentUserInitialData').USER_ID;if(u)return String(u);}"
                 "catch(e){}var m=document.cookie.match(/c_user=(\\d+)/);return m?m[1]:'';})()", PGS, 15)
    if not SELF_ID:
        die("could not auto-derive selfId - pass it explicitly as argv[4] (your account FBID)")
print("[self] selfId=%s (%s)" % (SELF_ID, SELF_NAME))

# --- 1. force complete sync: gated range-query walk to both boundaries ---
WALK = r"""(async function(){
  try{
  const req=require;
  const db=await req('LSDatabaseSingleton').getLSDatabaseSingletonPromiseOrValue();
  const I64=req('I64'),ReQL=req('ReQL');
  const LSFactory=req('LSFactory').default||req('LSFactory');
  const IssueMRQ=req('LSIssueMessagesRangeQueryStoredProcedure').default||req('LSIssueMessagesRangeQueryStoredProcedure');
  const LSIntEnum=req('LSIntEnum').default||req('LSIntEnum');
  const TKSTR="__TK__", TK=I64.of_string(TKSTR);
  const sleep=ms=>new Promise(r=>setTimeout(r,ms));
  const PICK="__PICK__";
  const row=async()=>{
    var rows=await ReQL.toArrayAsync(ReQL.fromTableAscending(db.tables.messages_ranges_v2__generated));
    var mine=rows.filter(function(r){try{return I64.to_string(r.threadKey)===TKSTR;}catch(e){return false;}});
    if(!mine.length)return null;
    var t=mine[0];
    mine.forEach(function(r){
      if(PICK==="min"){ if(Number(I64.to_string(r.minTimestampMs))<Number(I64.to_string(t.minTimestampMs)))t=r; }
      else            { if(Number(I64.to_string(r.maxTimestampMs))>Number(I64.to_string(t.maxTimestampMs)))t=r; }
    });
    return t;
  };
  const fire=(ref,dir)=>db.runInTransaction(t=>IssueMRQ(LSFactory(t),{threadKey:TK,referenceTimestampMs:I64.of_string(ref),direction:LSIntEnum.ofNumber(dir)}),"readwrite",undefined,undefined,"dumpWalk");
  const DIR=__DIR__, FLAG="__FLAG__", LOAD="__LOAD__", REF="__REF__", BUDGET=__BUDGET__;
  const t0=Date.now(); let r=await row(); if(!r) return JSON.stringify({norow:1});
  let last=I64.to_string(r[REF]), stall=0;
  while(r && r[FLAG] && (Date.now()-t0)<BUDGET){
    if(!r[LOAD]){ try{await fire(I64.to_string(r[REF]),DIR);}catch(e){} }
    let w=0; while(w<6000){ await sleep(300); w+=300; r=await row(); if(I64.to_string(r[REF])!==last){last=I64.to_string(r[REF]);stall=0;break;} if(!r[LOAD])break; }
    if(I64.to_string(r[REF])===last && !r[LOAD]){ if(++stall>5)break; try{await fire(last,DIR);}catch(e){} }
  }
  return JSON.stringify({minTs:I64.to_string(r.minTimestampMs),maxTs:I64.to_string(r.maxTimestampMs),hasMoreBefore:r.hasMoreBefore,hasMoreAfter:r.hasMoreAfter});
  }catch(e){ return JSON.stringify({error:String((e&&e.message)||e)}); }
})()""".replace("__TK__", TK)

def walk(direction, flag, load, ref, pick, budget=40000):
    js = (WALK.replace("__DIR__", str(direction)).replace("__FLAG__", flag).replace("__LOAD__", load)
              .replace("__REF__", ref).replace("__PICK__", pick).replace("__BUDGET__", str(budget)))
    r = cdp("Runtime.evaluate", {"expression": js, "awaitPromise": True, "returnByValue": True}, PGS, budget/1000 + 30)
    v = val(r)
    if v is None: return {"error": "eval failed: %r" % (r.get("result", r))}   # NEVER collapse to {} == "done"
    try:    return json.loads(v)
    except Exception as e: return {"error": "parse: %s" % e}

def final_ranges():
    js = ("(async function(){var db=await require('LSDatabaseSingleton').getLSDatabaseSingletonPromiseOrValue();"
          "var I64=require('I64'),ReQL=require('ReQL');"
          "var rows=await ReQL.toArrayAsync(ReQL.fromTableAscending(db.tables.messages_ranges_v2__generated));"
          "return JSON.stringify(rows.filter(function(r){try{return I64.to_string(r.threadKey)==='%s';}catch(e){return false;}})"
          ".map(function(r){return {minTs:I64.to_string(r.minTimestampMs),maxTs:I64.to_string(r.maxTimestampMs),hb:r.hasMoreBefore,ha:r.hasMoreAfter};}));})()" % TK)
    return json.loads(ev(js, PGS, 60, await_promise=True) or "[]")

print("[sync] forcing complete LightSpeed sync (range-query walk to both boundaries)...")
for direction, flag, load, ref, pick in [(0, "hasMoreBefore", "isLoadingBefore", "minTimestampMs", "min"),
                                          (1, "hasMoreAfter",  "isLoadingAfter",  "maxTimestampMs", "max")]:
    dirn = "BEFORE" if direction == 0 else "AFTER"
    errs = nrow = 0
    for chunk in range(60):
        s = walk(direction, flag, load, ref, pick)
        if "error" in s:
            print("  %s chunk%d ERROR: %s" % (dirn, chunk, s["error"])); errs += 1
            if errs > 3: print("  %s giving up on this direction (errors); final gate will decide" % dirn); break
            time.sleep(2); continue
        if s.get("norow"):
            nrow += 1; print("  %s chunk%d: no range row yet" % (dirn, chunk))
            if nrow > 5: print("  %s giving up (range row never materialized); final gate will decide" % dirn); break
            time.sleep(2); continue
        nrow = 0
        edge = s.get("minTs" if direction == 0 else "maxTs")
        flagval = s.get(flag)
        print("  %s chunk%d: edge=%s %s=%s" % (dirn, chunk, edge, flag, flagval))
        if flagval is False:
            print("  %s boundary closed" % dirn); break

# --- ENFORCE the completeness proof: refuse to write anything if it was not obtained ---
fr = final_ranges()
if not fr:
    die("SYNC FAILED: no range row for thread %s - tab not on the thread, or wrong threadKey "
        "(did you pass an /e2ee/ id instead of the contact FBID?)" % TK)
if len(fr) > 1:
    die("SYNC INCOMPLETE: %d disjoint range rows remain (gaps between fragments) - refusing to write a "
        "partial export. Re-run; if it persists the engine is not merging ranges. rows=%r" % (len(fr), fr))
R0 = fr[0]
if R0["hb"] is not False or R0["ha"] is not False:
    die("SYNC INCOMPLETE: hasMoreBefore=%s hasMoreAfter=%s after walk - refusing to write a partial export "
        "labelled complete. Re-run (likely a transient stall) or raise the chunk budget." % (R0["hb"], R0["ha"]))
print("[sync] COMPLETE: single contiguous range, both boundaries closed (min=%s max=%s)" % (R0["minTs"], R0["maxTs"]))

# --- 2. vault key + read & decrypt all messages from the DB ---
vm_raw = cdp("Runtime.evaluate", {"expression":
    "(function(){var m=require('MAWVaultMaterials').getVaultMaterials();var k=new Uint8Array(m.encryptionKey);"
    "var s='';for(var i=0;i<k.length;i++)s+=String.fromCharCode(k[i]);return JSON.stringify({key:btoa(s),pas:m.prefixAndSuffix});})()",
    "returnByValue": True}, PGS, 15)
vmv = val(vm_raw)
if not vmv:
    die("vault key read failed - is the thread tab open and loaded on the right thread? raw=%r" % vm_raw.get("result", vm_raw))
vm = json.loads(vmv)
_key = base64.b64decode(vm["key"])
if len(_key) != 32 or not isinstance(vm.get("pas"), str) or not vm["pas"]:
    die("vault materials malformed: key=%d bytes pas=%r - profile not fully initialized? (re-open the thread)"
        % (len(_key), vm.get("pas")))
BOX = SecretBox(_key); PAS = vm["pas"]

def dec(t):
    """Return (status, text). status: 'dec' decrypted | 'plain' passthrough (no PAS envelope) | 'fail' decrypt error."""
    if not t: return ("plain", "")
    if not (t.startswith(PAS) and t.endswith(PAS) and len(t) > 2 * len(PAS)):
        return ("plain", t)                                  # anchored gate, not bare substring
    x = t[len(PAS):-len(PAS)]
    try:
        raw = base64.b64decode(x + "=" * (-len(x) % 4), validate=True)   # exact padding per residue; strict catches a stray byte
        if len(raw) < 42 or raw[0:2] != b"\x01\x02":
            return ("fail", None)                            # unexpected envelope -> loud, not wrong plaintext
        return ("dec", BOX.decrypt(raw[26:], raw[2:26]).decode("utf-8", "replace"))  # [01 02][24 nonce][sealed]
    except Exception:
        return ("fail", None)

n_raw = cdp("Runtime.evaluate", {"expression":
    "(async function(){var db=await require('LSDatabaseSingleton').getLSDatabaseSingletonPromiseOrValue();"
    "var I64=require('I64'),ReQL=require('ReQL');"
    "window.__dm=await ReQL.toArrayAsync(ReQL.fromTableAscending(db.tables.messages).getKeyRange(I64.of_string('%s')));"
    "return window.__dm.length;})()" % TK, "awaitPromise": True, "returnByValue": True}, PGS, 90)
n = val(n_raw)
if n is None:
    die("message-count read failed - tab not on thread? raw=%r" % n_raw.get("result", n_raw))
n = int(n)
print("[read] %s message rows in DB" % n)
if n == 0:
    die("no messages found for threadKey %s - wrong key, or thread not materialized in the tab" % TK)

rows = []
for start in range(0, n, 2500):
    chunk_js = ("(function(s,e){var I64=require('I64');return JSON.stringify(window.__dm.slice(s,e).map(function(m){"
                "return {mid:m.messageId,sender:I64.to_string(m.senderId),ts:I64.to_string(m.timestampMs),"
                "text:(typeof m.text==='string'?m.text:'')};}));})(%d,%d)" % (start, start + 2500))
    v = None
    for _ in range(3):
        v = ev(chunk_js, PGS, 60)
        if v is not None: break
        time.sleep(2)
    if v is None:
        die("message read failed at offset %d (refusing to write a partial) - re-run" % start)
    rows += json.loads(v)

dec_ok = plain = fail = nonempty_text = plain_nonempty = 0
for r in rows:
    orig = r.get("text")
    if orig: nonempty_text += 1
    st, txt = dec(orig)
    r["text"] = txt; r["status"] = st
    if st == "dec": dec_ok += 1
    elif st == "fail": fail += 1
    else:
        plain += 1
        if orig: plain_nonempty += 1
wrapped = dec_ok + fail   # envelope-shaped rows (exactly the ones dec() attempted) - single source of truth
print("[decrypt] decrypted=%d plaintext-passthrough=%d failed=%d (wrapped=%d, non-empty-text=%d)"
      % (dec_ok, plain, fail, wrapped, nonempty_text))

# E2EE / wrong-key guard: refuse to emit a confident garbage artifact (fail loud, not silent).
# Guard A keys on EVIDENCE of an unreadable scheme (non-empty text that is NOT vault-wrapped),
# not on a mere absence of text - so a legit media/sticker/call-only thread is NOT false-aborted,
# and a small or mixed E2EE thread (whose bodies are unwrapped) IS caught.
if nonempty_text >= 5 and plain_nonempty >= 0.5 * nonempty_text:
    die("%d/%d non-empty messages are NOT vault-encrypted - likely an E2EE 'secret' thread or an at-rest "
        "scheme this tool cannot read; rendering them would dump unreadable bytes as text. Output suppressed."
        % (plain_nonempty, nonempty_text))
if wrapped and fail > max(20, 0.5 * wrapped):
    die("systemic decrypt failure %d/%d wrapped rows - likely the wrong key or an E2EE thread. Output suppressed."
        % (fail, wrapped))

# participant display-name map (for group threads; 1:1 falls back to NAME)
NAMES = {}
try:
    NAMES = json.loads(ev(
        "(async function(){var db=await require('LSDatabaseSingleton').getLSDatabaseSingletonPromiseOrValue();"
        "var I64=require('I64'),ReQL=require('ReQL');var nm={};"
        "try{var cs=await ReQL.toArrayAsync(ReQL.fromTableAscending(db.tables.contacts));cs.forEach(function(c){try{"
        "var id=I64.to_string(c.id);var nme=c.name||c.firstName;"
        "if(id&&nme)nm[id]=nme;}catch(e){}});}catch(e){}return JSON.stringify(nm);})()", PGS, 60, await_promise=True) or "{}")
except Exception:
    NAMES = {}

# --- 3. media URLs (mime paired to the chosen url) + link cards from the DB ---
linkjs = (r"""(async function(){var db=await require('LSDatabaseSingleton').getLSDatabaseSingletonPromiseOrValue();
var I64=require('I64'),ReQL=require('ReQL');
async function rd(t){try{var rows=await ReQL.toArrayAsync(ReQL.fromTableAscending(db.tables[t]));return rows.filter(function(r){try{return I64.to_string(r.threadKey)==="%s";}catch(e){return false;}});}catch(e){return [];}}
var att=(await rd('attachments')).map(function(r){
  var url=null,mime=null;
  if(typeof r.playableUrl==='string'&&r.playableUrl){url=r.playableUrl;mime=r.playableUrlMimeType;}
  else if(typeof r.imageUrl==='string'&&r.imageUrl){url=r.imageUrl;mime=r.imageUrlMimeType;}
  else if(typeof r.previewUrl==='string'&&r.previewUrl){url=r.previewUrl;mime=r.previewUrlMimeType;}
  return {mid:r.messageId,url:url,mime:mime,fn:r.filename,title:r.titleText,sub:r.subtitleText};
});
var cta=(await rd('attachment_ctas')).map(function(r){return {mid:r.messageId,url:r.actionUrl};});
return JSON.stringify({att:att,cta:cta});})()""" % TK)
_lv = ev(linkjs, PGS, 90, await_promise=True)
if _lv is None:
    die("attachment/link read failed - refusing to write a dump with all media silently dropped; re-run")
L = json.loads(_lv)

def realurl(u):
    if not u: return u
    m = re.search(r'[?&]u=([^&]+)', u); return urllib.parse.unquote(m.group(1)) if m else u

media = {}   # mid -> [ {url, mime, fn}, ... ]   (a list: albums keep every attachment)
cards = {}
att_total = 0
for a in L.get("att", []):
    u = a.get("url")
    if a.get("mid") and isinstance(u, str) and u.startswith("http"):
        media.setdefault(a["mid"], []).append({"url": u, "mime": a.get("mime"), "fn": a.get("fn")})
        att_total += 1                                       # per attachment SLOT (matches what render emits)
    if a.get("mid") and (a.get("title") or a.get("sub")):
        cards.setdefault(a["mid"], {}).update({"title": a.get("title"), "sub": a.get("sub")})
for c in L.get("cta", []):
    if c.get("mid"): cards.setdefault(c["mid"], {})["url"] = realurl(c.get("url"))

# --- 4. download media (validate the CONTENT, not an enumerated placeholder; retry once) ---
os.makedirs(OUT, mode=0o700, exist_ok=True);          os.chmod(OUT, 0o700)
os.makedirs(OUT + "/media", mode=0o700, exist_ok=True); os.chmod(OUT + "/media", 0o700)
def ext(u, mime):
    m = {"image/jpeg":"jpg","image/png":"png","image/webp":"webp","image/gif":"gif","video/mp4":"mp4",
         "audio/mpeg":"mp3","audio/mp4":"m4a","audio/ogg":"ogg","application/pdf":"pdf"}.get(mime or "")
    if m: return m
    last = urllib.parse.urlparse(u).path.split("/")[-1]
    return last.split(".")[-1].lower() if "." in last else "bin"   # no truncation; real exts vary in length
def bad_media(p, mime):
    """True if the file is missing/empty, an expired-CDN placeholder, an error page, or its bytes
    do not match the declared media class. Validates the SYMPTOM (content) rather than enumerating
    one known placeholder shape, so HTML/XML error bodies and non-1x1 sentinels are also rejected."""
    try:
        if os.path.getsize(p) == 0: return True
        with open(p, "rb") as f: head = f.read(64)
    except Exception:
        return True
    lead = head.lstrip()[:16].lower()
    if lead[:9] == b"<!doctype" or lead[:5] == b"<html" or lead[:5] == b"<?xml" or lead[:7] == b'{"error':
        return True                                          # CDN/error HTML/JSON page served as 200
    png = head[:8] == b"\x89PNG\r\n\x1a\n"
    if png and int.from_bytes(head[16:20], "big") <= 1 and int.from_bytes(head[20:24], "big") <= 1:
        return True                                          # FB's 1x1 expired-URL placeholder
    m = mime or ""
    if m.startswith("image/"):
        return not (head[:3] == b"\xff\xd8\xff" or png or head[:6] in (b"GIF87a", b"GIF89a")
                    or (head[:4] == b"RIFF" and head[8:12] == b"WEBP") or head[:2] == b"BM")
    if m.startswith("video/"):
        return not (head[4:8] == b"ftyp" or head[:4] == b"\x1aE\xdf\xa3")   # mp4/mov, or webm/matroska
    return False                                             # audio / pdf / docx / other: accept any real body
all_atts = [a for lst in media.values() for a in lst]
jobs = {}
for a in all_atts:
    jobs.setdefault(a["url"], (OUT + "/media/" + hashlib.md5(a["url"].encode()).hexdigest()[:16] + "." + ext(a["url"], a.get("mime")),
                               a.get("mime")))
def dl(it):
    u, p, mime = it
    def keep():
        try: os.chmod(p, 0o600)      # private data, even if reused from a prior run (umask only covers fresh creates)
        except OSError: pass
        return (u, p, 1)
    if os.path.exists(p) and not bad_media(p, mime): return keep()
    for _ in range(2):               # the fresh sync above reissues valid URLs; one retry covers a transient miss
        cp = subprocess.run(["curl", "-s", "-m", "40", "-w", "%{http_code}", "-A", "Mozilla/5.0", "-o", p, u],
                            capture_output=True, text=True)
        if (cp.stdout or "").strip()[-3:] == "200" and not bad_media(p, mime): return keep()
    try:
        if os.path.exists(p): os.remove(p)   # do not leave an expired placeholder / error body on disk
    except OSError: pass
    return (u, p, 0)
loc = {}
with concurrent.futures.ThreadPoolExecutor(max_workers=12) as ex:
    for u, p, okk in ex.map(dl, [(u, pm[0], pm[1]) for u, pm in jobs.items()]):
        if okk: loc[u] = "media/" + os.path.basename(p)
got = sum(1 for a in all_atts if loc.get(a["url"]))   # per-SLOT, so header/banner/markers all agree
print("[media] %d/%d attachments downloaded (failed/expired: %d; %d distinct files)" % (got, att_total, att_total - got, len(loc)))
if got < att_total:
    miss = sorted({mid for mid, lst in media.items() for a in lst if not loc.get(a["url"])})
    print("[ALERT] %d attachment(s) missing (expired URL or download error). messageIds: %s%s"
          % (att_total - got, ", ".join(miss[:20]), " ..." if len(miss) > 20 else ""))
# prune stale/placeholder files from prior runs so media/ mirrors this run exactly
keep = {os.path.basename(v) for v in loc.values()}
for fn in os.listdir(OUT + "/media"):
    if fn not in keep:
        try: os.remove(OUT + "/media/" + fn)
        except OSError: pass

# --- 5. render ---
def dt(ms): return datetime.datetime.fromtimestamp(int(ms)/1000, TZ)
def valid_ts(v):
    return v and str(v).lstrip("-").isdigit() and 0 < int(v) < 4102444800000   # 0 .. year 2100 (ms); bound corrupt values
msgs = sorted([r for r in rows if valid_ts(r.get("ts"))], key=lambda x: int(x["ts"]))
dropped = len(rows) - len(msgs)
if dropped: print("[WARN] %d row(s) dropped from render (no/invalid timestamp)" % dropped)
if not msgs: die("no renderable messages (all rows lacked a valid timestamp) for threadKey %s" % TK)
ts = [int(r["ts"]) for r in msgs]
senders = {m["sender"] for m in msgs}
if SELF_ID not in senders:
    print("[ALERT] selfId %s sent NONE of the %d messages - if you passed it explicitly it is likely wrong "
          "(your own messages will be mislabelled as the other party). Auto-derived ids are usually correct." % (SELF_ID, len(msgs)))
others = senders - {SELF_ID}
is_group = len(others) > 1
if is_group:
    unresolved = [s for s in others if not NAMES.get(s)]
    print("[render] group thread: %d non-self participants" % len(others))
    if unresolved:
        print("[ALERT] %d/%d sender name(s) unresolved (rendered as 'User <id>'): %s"
              % (len(unresolved), len(others), ", ".join(unresolved[:10])))
def who(sid):
    if sid == SELF_ID: return SELF_NAME
    if not is_group:   return NAME
    return NAMES.get(sid) or ("User " + sid)
def safe_url(u):
    return u if isinstance(u, str) and re.match(r"^https?://", u, re.I) else ""   # block javascript:/data: in a file:// archive

o = ['<!doctype html><html><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">',
'<title>%s - Messenger</title>' % html.escape(NAME),
'<style>body{font-family:-apple-system,"Segoe UI",system-ui,sans-serif;max-width:820px;margin:0 auto;background:#f0f2f5;padding:16px;color:#050505}',
'h1{text-align:center}.sub{text-align:center;color:#65676b;font-size:13px;margin-bottom:20px}',
'.banner{background:#ffe1e1;border:1px solid #f5abab;color:#9b1c1c;border-radius:8px;padding:8px 12px;margin:8px 0;font-size:13px;text-align:center}',
'.m{margin:3px 0;display:flex;flex-direction:column}.m.me{align-items:flex-end}.m.them{align-items:flex-start}',
'.b{max-width:68%;padding:7px 11px;border-radius:16px;white-space:pre-wrap;overflow-wrap:anywhere;font-size:15px;line-height:1.35}',
'.them .b{background:#fff;border:1px solid #e4e6eb}.me .b{background:#0084ff;color:#fff}',
'.t{font-size:11px;color:#8a8d91;margin:1px 10px}img{max-width:300px;border-radius:12px;display:block;margin:3px 0}',
'video{max-width:320px;border-radius:12px;display:block}audio{display:block;margin:3px 0}.day{text-align:center;color:#65676b;font-size:12px;margin:18px 0 8px;font-weight:600}',
'.sys{color:#888;font-size:12px;font-style:italic}.err{color:#c00;font-size:12px;font-weight:600}',
'.card{border:1px solid #ccd0d5;border-radius:10px;padding:6px 9px;margin-top:4px;font-size:13px}.me .card a,.them .card a{color:inherit}</style></head><body>']
o.append('<h1>%s</h1><div class=sub>%d messages &middot; %s to %s &middot; %d/%d media &middot; %d link cards &middot; sync COMPLETE &middot; times in %s</div>'
         % (html.escape(NAME), len(msgs), dt(min(ts)).strftime("%d %b %Y"), dt(max(ts)).strftime("%d %b %Y"),
            got, att_total, len(cards), html.escape(TZNAME)))
if fail:   o.append('<div class=banner>%d message(s) failed to decrypt and are marked below.</div>' % fail)
if got < att_total: o.append('<div class=banner>%d media attachment(s) could not be downloaded (expired URLs) and are marked below.</div>' % (att_total - got))
if dropped: o.append('<div class=banner>%d row(s) had no timestamp and are omitted.</div>' % dropped)

lastday = None
for m in msgs:
    t = dt(m["ts"]); day = t.strftime("%A, %d %B %Y")
    if day != lastday: o.append('<div class=day>%s</div>' % day); lastday = day
    me = m["sender"] == SELF_ID
    if m.get("status") == "fail":
        body = '<span class=err>[DECRYPT FAILED - message lost]</span>'
    else:
        body = html.escape(m["text"] or "")
    for a in media.get(m["mid"], []):
        l = loc.get(a["url"])
        if not l:
            body += '<br><span class=err>[media unavailable: %s]</span>' % html.escape(a.get("fn") or "expired URL")
            continue
        e = l.rsplit(".", 1)[-1].lower(); mime = a.get("mime") or ""
        if mime.startswith("image/") or e in IMG_EXT:
            body += '<a href="%s" target=_blank rel=noopener><img src="%s" loading=lazy></a>' % (l, l)
        elif mime.startswith("video/") or e == "mp4":
            body += '<video controls src="%s"></video>' % l
        elif mime.startswith("audio/") or e in AUD_EXT:
            body += '<audio controls src="%s"></audio>' % l
        else:
            body += '<br><a href="%s" target=_blank rel=noopener>📎 %s</a>' % (l, html.escape(a.get("fn") or "file"))
    if m["mid"] in cards:
        c = cards[m["mid"]]; u = safe_url(c.get("url") or "")
        label = (c.get("title") or c.get("sub") or u or "")[:60]
        if u:
            body += '<div class=card>🔗 <a href="%s" target=_blank rel=noopener>%s</a></div>' % (html.escape(u), html.escape(label or u))
        elif label:
            body += '<div class=card>🔗 %s</div>' % html.escape(label)   # title-only preview (or unsafe-scheme url shown as text)
    if not body: body = '<span class=sys>[non-text]</span>'
    o.append('<div class="m %s"><div class=b>%s</div><div class=t>%s · %s</div></div>'
             % ("me" if me else "them", body, html.escape(who(m["sender"])), t.strftime("%H:%M")))
o.append("</body></html>")

def write_private(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f: f.write(data)
    os.chmod(path, 0o600)   # enforce on a pre-existing file too (O_CREAT mode only applies on creation)
write_private(OUT + "/index.html", "\n".join(o))
write_private(OUT + "/messages.json", json.dumps(rows, ensure_ascii=False))
print("[done] %d messages -> %s/index.html  (%s to %s)  media %d/%d  decrypt-failures %d"
      % (len(msgs), OUT, dt(min(ts)).strftime("%Y-%m-%d"), dt(max(ts)).strftime("%Y-%m-%d"), got, att_total, fail))
