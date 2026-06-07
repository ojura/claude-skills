#!/usr/bin/env python3
"""Dump a non-E2EE Messenger conversation (full history + media) to a self-contained HTML.

Authoritative method: force a COMPLETE LightSpeed sync via the range-query sproc to both
thread boundaries, then read the local DB and decrypt every message with the profile vault
key. Do NOT rely on UI scroll/WS capture - the scroll stalls and silently drops middle
ranges (observed: a thread that scrolled to 2,940 messages actually had 15,454 in the DB).

Requires:
  - cdp-daemon running on 127.0.0.1:7799 (see ../cdp-daemon), driving the user's Chrome.
  - A messenger.com tab OPEN to the target thread (ReStore materializes messages per-tab,
    so the sync + reads must run against that tab; this script opens/navigates one).
  - python3 -m pip install pynacl

Usage:
  dump_thread.py <threadKey> <contactName> <outDir> [selfId=739203988] [selfName=Juraj]

threadKey = the contact's FBID. For a regular thread it's the /t/<id> URL id; for an
/e2ee/t/<e2eeid> thread it's still the contact FBID (look it up once via the data, e.g.
e2ee 7385692221555435 -> threadKey 1241408603).
"""
import sys, json, urllib.request, base64, html, datetime, os, re, urllib.parse, hashlib, subprocess, concurrent.futures, time

BASE = "http://127.0.0.1:7799"
TK       = sys.argv[1]
NAME     = sys.argv[2]
OUT      = sys.argv[3]
SELF_ID  = sys.argv[4] if len(sys.argv) > 4 else "739203988"
SELF_NAME= sys.argv[5] if len(sys.argv) > 5 else "Juraj"
try:
    from zoneinfo import ZoneInfo; TZ = ZoneInfo("Europe/Zagreb")
except Exception:
    TZ = datetime.timezone.utc

def get(p, t=60):  return json.loads(urllib.request.urlopen(BASE + p, timeout=t).read().decode())
def post(p, o, t=120):
    try:    return json.loads(urllib.request.urlopen(BASE + p, data=json.dumps(o).encode(), timeout=t).read().decode())
    except Exception as e: return {"_err": str(e)}
def cdp(m, p, sid=None, t=120):
    b = {"method": m, "params": p}
    if sid: b["sessionId"] = sid
    return post("/cdp", b, t)
def val(r): return r.get("result", {}).get("result", {}).get("value")

# --- attach to (or open) the thread's page ---
def thread_page():
    for tgt in get("/targets"):
        if tgt["type"] == "page" and TK in tgt.get("url", ""):
            return post("/attach", {"targetId": tgt["targetId"]}).get("sessionId")
    # open it
    url = "https://www.messenger.com/t/%s" % TK
    ptid = cdp("Target.createTarget", {"url": url}).get("result", {}).get("targetId")
    time.sleep(1.0)
    pgs = post("/attach", {"targetId": ptid}).get("sessionId")
    for _ in range(15):
        time.sleep(2)
        if val(cdp("Runtime.evaluate", {"expression":
            "(function(){var m=document.querySelector('[role=\\\"main\\\"]');var t=m?m.innerText:'';return (t.length>500&&!/Loading/.test(t))?1:0;})()",
            "returnByValue": True}, pgs, 10)):
            break
    return pgs

PGS = thread_page()

# --- 1. force complete sync: gated range-query walk to both boundaries ---
WALK = r"""(async function(){
  const req=require;
  const db=await req('LSDatabaseSingleton').getLSDatabaseSingletonPromiseOrValue();
  const I64=req('I64'),ReQL=req('ReQL');
  const LSFactory=req('LSFactory').default||req('LSFactory');
  const IssueMRQ=req('LSIssueMessagesRangeQueryStoredProcedure').default||req('LSIssueMessagesRangeQueryStoredProcedure');
  const LSIntEnum=req('LSIntEnum').default||req('LSIntEnum');
  const TKSTR="__TK__", TK=I64.of_string(TKSTR);
  const sleep=ms=>new Promise(r=>setTimeout(r,ms));
  const row=async()=>{var rows=await ReQL.toArrayAsync(ReQL.fromTableAscending(db.tables.messages_ranges_v2__generated));var t=null;rows.forEach(function(r){if(I64.to_string(r.threadKey)===TKSTR){if(!t||Number(I64.to_string(r.maxTimestampMs))>Number(I64.to_string(t.maxTimestampMs)))t=r;}});return t;};
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
})()""".replace("__TK__", TK)

def walk(direction, flag, load, ref, budget=40000):
    js = WALK.replace("__DIR__", str(direction)).replace("__FLAG__", flag).replace("__LOAD__", load).replace("__REF__", ref).replace("__BUDGET__", str(budget))
    return json.loads(val(cdp("Runtime.evaluate", {"expression": js, "awaitPromise": True, "returnByValue": True}, PGS, budget/1000 + 30)) or "{}")

print("[sync] forcing complete LightSpeed sync (range-query walk to both boundaries)...")
for direction, flag, load, ref in [(0, "hasMoreBefore", "isLoadingBefore", "minTimestampMs"),
                                    (1, "hasMoreAfter",  "isLoadingAfter",  "maxTimestampMs")]:
    for chunk in range(60):
        s = walk(direction, flag, load, ref)
        if s.get("norow"): time.sleep(2); continue
        done = not s.get(flag)
        print("  %s chunk%d: edge=%s %s=%s" % (("BEFORE" if direction==0 else "AFTER"), chunk, s.get(ref.replace("Timestamp","Ts").replace("minTs","minTs")), flag, s.get(flag)))
        if done:
            print("  %s complete (%s=false)" % ("BEFORE" if direction==0 else "AFTER", flag)); break

# --- 2. vault key + read & decrypt all messages from the DB ---
vm = json.loads(val(cdp("Runtime.evaluate", {"expression":
    "(function(){var m=require('MAWVaultMaterials').getVaultMaterials();var k=new Uint8Array(m.encryptionKey);var s='';for(var i=0;i<k.length;i++)s+=String.fromCharCode(k[i]);return JSON.stringify({key:btoa(s),pas:m.prefixAndSuffix});})()",
    "returnByValue": True}, PGS, 15)))
from nacl.secret import SecretBox
BOX = SecretBox(base64.b64decode(vm["key"])); PAS = vm["pas"]
def dec(t):
    if not t: return ""
    if PAS not in t: return t           # already plaintext
    x = t
    if x.startswith(PAS): x = x[len(PAS):]
    if x.endswith(PAS):   x = x[:-len(PAS)]
    try:
        raw = base64.b64decode(x + "===")
        return BOX.decrypt(raw[26:], raw[2:26]).decode("utf-8", "replace")   # [01 02][24 nonce][ct+tag]
    except Exception:
        return None

n = val(cdp("Runtime.evaluate", {"expression":
    "(async function(){var db=await require('LSDatabaseSingleton').getLSDatabaseSingletonPromiseOrValue();var I64=require('I64'),ReQL=require('ReQL');window.__dm=await ReQL.toArrayAsync(ReQL.fromTableAscending(db.tables.messages).getKeyRange(I64.of_string('%s')));return window.__dm.length;})()" % TK,
    "awaitPromise": True, "returnByValue": True}, PGS, 90))
print("[read] %s message rows in DB" % n)
rows = []
for start in range(0, int(n), 2500):
    rows += json.loads(val(cdp("Runtime.evaluate", {"expression":
        "(function(s,e){var I64=require('I64');return JSON.stringify(window.__dm.slice(s,e).map(function(m){return {mid:m.messageId,sender:I64.to_string(m.senderId),ts:I64.to_string(m.timestampMs),text:(typeof m.text==='string'?m.text:'')};}));})(%d,%d)" % (start, start+2500),
        "returnByValue": True}, PGS, 60)))
ok = fail = 0
for r in rows:
    r["text"] = dec(r.get("text"))
    if r["text"] is None: fail += 1
    elif r["text"]: ok += 1
print("[decrypt] ok=%d fail=%d" % (ok, fail))

# --- 3. media URLs + link cards from the DB ---
linkjs = r"""(async function(){var db=await require('LSDatabaseSingleton').getLSDatabaseSingletonPromiseOrValue();var I64=require('I64'),ReQL=require('ReQL');
async function rd(t){var rows=await ReQL.toArrayAsync(ReQL.fromTableAscending(db.tables[t]));return rows.filter(function(r){try{return I64.to_string(r.threadKey)==="%s";}catch(e){return false;}});}
var att=(await rd('attachments')).map(function(r){return {mid:r.messageId,play:(typeof r.playableUrl==='string'?r.playableUrl:null),prev:(typeof r.previewUrl==='string'?r.previewUrl:null),img:(typeof r.imageUrl==='string'?r.imageUrl:null),mime:r.playableUrlMimeType||r.previewUrlMimeType,fn:r.filename,title:r.titleText,sub:r.subtitleText};});
var cta=(await rd('attachment_ctas')).map(function(r){return {mid:r.messageId,url:r.actionUrl};});
return JSON.stringify({att:att,cta:cta});})()""" % TK
L = json.loads(val(cdp("Runtime.evaluate", {"expression": linkjs, "awaitPromise": True, "returnByValue": True}, PGS, 90)) or "{}")
def realurl(u):
    if not u: return u
    m = re.search(r'[?&]u=([^&]+)', u); return urllib.parse.unquote(m.group(1)) if m else u
media = {}; cards = {}
for a in L.get("att", []):
    u = a.get("play") or a.get("img") or a.get("prev")
    if a.get("mid") and isinstance(u, str) and u.startswith("http"):
        media[a["mid"]] = {"url": u, "mime": a.get("mime"), "fn": a.get("fn")}
    if a.get("mid") and (a.get("title") or a.get("sub")):
        cards.setdefault(a["mid"], {}).update({"title": a.get("title"), "sub": a.get("sub")})
for c in L.get("cta", []):
    if c.get("mid"): cards.setdefault(c["mid"], {})["url"] = realurl(c.get("url"))

# --- 4. download media ---
os.makedirs(OUT + "/media", exist_ok=True)
def ext(u, mime):
    m = {"image/jpeg":"jpg","image/png":"png","image/webp":"webp","image/gif":"gif","video/mp4":"mp4","audio/mpeg":"mp3","application/pdf":"pdf"}.get(mime or "")
    if m: return m
    last = urllib.parse.urlparse(u).path.split("/")[-1]
    return last.split(".")[-1][:5] if "." in last else "bin"
jobs = {a["url"]: OUT + "/media/" + hashlib.md5(a["url"].encode()).hexdigest()[:16] + "." + ext(a["url"], a.get("mime")) for a in media.values()}
def dl(it):
    u, p = it
    if os.path.exists(p) and os.path.getsize(p) > 0: return (u, p, 1)
    subprocess.run(["curl", "-s", "-m", "40", "-A", "Mozilla/5.0", "-o", p, u])
    return (u, p, 1 if (os.path.exists(p) and os.path.getsize(p) > 0) else 0)
loc = {}
with concurrent.futures.ThreadPoolExecutor(max_workers=12) as ex:
    for u, p, okk in ex.map(dl, jobs.items()):
        if okk: loc[u] = "media/" + os.path.basename(p)
print("[media] %d/%d downloaded" % (len(loc), len(jobs)))

# --- 5. render ---
def dt(ms): return datetime.datetime.fromtimestamp(int(ms)/1000, TZ)
msgs = sorted([r for r in rows if r.get("ts") and str(r["ts"]).isdigit()], key=lambda x: int(x["ts"]))
ts = [int(r["ts"]) for r in msgs]
o = ['<!doctype html><html><head><meta charset=utf-8><title>%s — Messenger</title>' % html.escape(NAME),
'<style>body{font-family:-apple-system,system-ui,sans-serif;max-width:820px;margin:0 auto;background:#f0f2f5;padding:16px;color:#050505}',
'h1{text-align:center}.sub{text-align:center;color:#65676b;font-size:13px;margin-bottom:20px}',
'.m{margin:3px 0;display:flex;flex-direction:column}.m.me{align-items:flex-end}.m.them{align-items:flex-start}',
'.b{max-width:68%;padding:7px 11px;border-radius:16px;white-space:pre-wrap;overflow-wrap:anywhere;font-size:15px;line-height:1.35}',
'.them .b{background:#fff;border:1px solid #e4e6eb}.me .b{background:#0084ff;color:#fff}',
'.t{font-size:11px;color:#8a8d91;margin:1px 10px}img{max-width:300px;border-radius:12px;display:block;margin:3px 0}',
'video{max-width:320px;border-radius:12px;display:block}.day{text-align:center;color:#65676b;font-size:12px;margin:18px 0 8px;font-weight:600}',
'.sys{color:#888;font-size:12px;font-style:italic}.card{border:1px solid #ccd0d5;border-radius:10px;padding:6px 9px;margin-top:4px;font-size:13px}.card a{color:inherit}</style></head><body>']
nph = sum(1 for m in msgs if m["mid"] in media and loc.get(media[m["mid"]]["url"]))
o.append('<h1>%s</h1><div class=sub>%d messages &middot; %s – %s &middot; %d media &middot; %d link cards</div>'
         % (html.escape(NAME), len(msgs), dt(min(ts)).strftime("%d %b %Y"), dt(max(ts)).strftime("%d %b %Y"), nph, len(cards)))
lastday = None
for m in msgs:
    t = dt(m["ts"]); day = t.strftime("%A, %d %B %Y")
    if day != lastday: o.append('<div class=day>%s</div>' % day); lastday = day
    me = m["sender"] == SELF_ID
    body = html.escape(m["text"] or "")
    if m["mid"] in media:
        l = loc.get(media[m["mid"]]["url"])
        if l:
            e = l.rsplit(".", 1)[-1].lower()
            if e in ("jpg","jpeg","png","webp","gif"): body += '<a href="%s" target=_blank><img src="%s" loading=lazy></a>' % (l, l)
            elif e == "mp4": body += '<video controls src="%s"></video>' % l
            else: body += '<br><a href="%s" target=_blank>📎 %s</a>' % (l, html.escape(media[m["mid"]].get("fn") or "file"))
    if m["mid"] in cards:
        c = cards[m["mid"]]; u = c.get("url") or ""
        if u: body += '<div class=card>🔗 <a href="%s" target=_blank>%s</a></div>' % (html.escape(u), html.escape((c.get("title") or c.get("sub") or u))[:60])
    if not body: body = '<span class=sys>[non-text]</span>'
    o.append('<div class="m %s"><div class=b>%s</div><div class=t>%s · %s</div></div>'
             % ("me" if me else "them", body, html.escape(SELF_NAME if me else NAME), t.strftime("%H:%M")))
o.append("</body></html>")
open(OUT + "/index.html", "w").write("\n".join(o))
json.dump(rows, open(OUT + "/messages.json", "w"), ensure_ascii=False)
print("[done] %d messages -> %s/index.html  (%s – %s)" % (len(msgs), OUT, dt(min(ts)).strftime("%Y-%m-%d"), dt(max(ts)).strftime("%Y-%m-%d")))
