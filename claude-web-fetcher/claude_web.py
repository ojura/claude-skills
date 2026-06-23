"""
Claude.ai web session client.

Fetches conversations, files (user-uploaded and sandbox-generated), Claude
Code-web (epitaxy) sessions, and triggers account data exports (the only surface
that preserves thinking-block signatures).

By DEFAULT it drives the user's real logged-in Chrome via the cdp-daemon
(127.0.0.1:7799, see the cdp-daemon skill): no Cloudflare friction, no
session_key needed. Pass backend='patchright' for the headless fallback, which
launches its own browser and needs a session_key (Cloudflare-prone).
"""

import json
import urllib.parse
import urllib.request
import urllib.error
import zipfile
import io
import base64
from pathlib import Path
from dataclasses import dataclass


BASE = "https://claude.ai"


@dataclass
class FileRef:
    name: str
    path: str
    kind: str  # "wiggle" or "upload"
    conversation_uuid: str
    message_uuid: str = ""
    size: int = 0


def _to_iso(d, end=False):
    """'YYYY-MM-DD' -> midnight-UTC ISO8601; for an end date, advance one day so the
    end day is inclusive (mirrors claude.ai's export UI). Full ISO passes through."""
    if isinstance(d, str) and len(d) == 10 and d[4] == "-" and d[7] == "-":
        import datetime
        day = datetime.date.fromisoformat(d) + (datetime.timedelta(days=1) if end else datetime.timedelta())
        return day.isoformat() + "T00:00:00.000Z"
    return d


def _find_gcs_url(obj):
    """Recursively find a storage.googleapis.com URL in a parsed JSON response."""
    if isinstance(obj, str):
        return obj if obj.startswith("https://") and urllib.parse.urlparse(obj).netloc.endswith("storage.googleapis.com") else None
    if isinstance(obj, dict):
        for v in obj.values():
            u = _find_gcs_url(v)
            if u:
                return u
    elif isinstance(obj, list):
        for v in obj:
            u = _find_gcs_url(v)
            if u:
                return u
    return None


def _deep_find(obj, key):
    """First non-container value for `key` anywhere in a nested dict/list."""
    if isinstance(obj, dict):
        if key in obj and not isinstance(obj[key], (dict, list)):
            return obj[key]
        for v in obj.values():
            r = _deep_find(v, key)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _deep_find(v, key)
            if r is not None:
                return r
    return None


class ClaudeWeb:
    DAEMON = "http://127.0.0.1:7799"

    def __init__(self, session_key: str | None = None, backend: str = "cdp",
                 daemon: str | None = None):
        """backend='cdp' (default): drive the user's real logged-in Chrome via the
        cdp-daemon (auto-started if not already running). backend='patchright':
        headless fallback, needs a session_key."""
        self.backend = backend
        if backend == "cdp":
            self.daemon = daemon or self.DAEMON
            self._ccr_headers = {}
            self._cdp_sid = self._cdp_attach()
        elif backend == "patchright":
            self._init_patchright(session_key)
        else:
            raise ValueError("backend must be 'cdp' or 'patchright'")
        self.org_id = self._ccr_headers.get("x-organization-uuid") or self._discover_org()

    def _init_patchright(self, session_key):
        from patchright.sync_api import sync_playwright
        if not session_key:
            raise ValueError("the patchright backend requires a session_key")
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=True)
        self._context = self._browser.new_context()
        self._context.add_cookies([{
            "name": "sessionKey", "value": session_key, "domain": ".claude.ai",
            "path": "/", "httpOnly": True, "secure": True, "sameSite": "Lax",
        }])
        self._page = self._context.new_page()
        with self._page.expect_response(
            lambda r: '/v1/sessions' in r.url and 'watch' not in r.url and 'events' not in r.url,
            timeout=20000,
        ) as resp_info:
            self._page.goto(f"{BASE}/code", wait_until="domcontentloaded", timeout=30000)
        req = resp_info.value.request
        self._ccr_headers = {
            k: v for k, v in req.all_headers().items()
            if k.startswith("anthropic-") or k == "x-organization-uuid"
        }

    # --- CDP backend: talk to the cdp-daemon, drive a real claude.ai tab ---

    def _daemon(self, path, payload=None, timeout=90):
        if payload is None:
            req = urllib.request.Request(self.daemon + path)
        else:
            req = urllib.request.Request(
                self.daemon + path, data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            try:
                body = json.loads(e.read() or b"{}")
            except Exception:
                body = {}
            # any daemon non-2xx is a failure — surface it (never return a value-bearing
            # body that would degrade to a silent None downstream)
            err = body.get("error") if isinstance(body, dict) else None
            raise RuntimeError(f"cdp-daemon {path} HTTP {e.code}: {err or str(body)[:200]}")
        except OSError as e:
            raise ConnectionError(str(e))

    def _cdp(self, method, params=None, sid=None, timeout=60):
        body = {"method": method, "timeout_seconds": timeout}
        if params is not None:
            body["params"] = params
        if sid:
            body["sessionId"] = sid
        return self._daemon("/cdp", body, timeout + 10)

    def _cdp_eval(self, sid, js, await_promise=True, timeout=90):
        r = self._cdp("Runtime.evaluate",
                      {"expression": js, "returnByValue": True, "awaitPromise": await_promise},
                      sid=sid, timeout=timeout)
        res = (r or {}).get("result", {})
        if "exceptionDetails" in res:
            raise RuntimeError("CDP eval exception: " + json.dumps(res["exceptionDetails"])[:300])
        return res.get("result", {}).get("value")

    def _ensure_daemon(self, wait=60):
        """Return /status once the daemon is connected, auto-starting cdp_daemon.py
        if it isn't running. The daemon self-manages the Chrome connection (and
        auto-presses 'Allow remote debugging?' when accessibility is on)."""
        import time, os, subprocess
        try:
            st = self._daemon("/status")
            if st.get("connected"):
                return st
        except ConnectionError:
            script = os.path.expanduser("~/.claude/skills/cdp-daemon/cdp_daemon.py")
            if os.path.exists(script):
                subprocess.Popen(["python3", script], stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True)
        deadline = time.time() + wait
        st = None
        while time.time() < deadline:
            try:
                st = self._daemon("/status")
                if st.get("connected"):
                    return st
            except ConnectionError:
                pass
            time.sleep(1)
        raise RuntimeError(
            "cdp-daemon started but not connected (state=%s). If Chrome is showing the "
            "'Allow remote debugging?' dialog, click Allow once." % (st.get("state") if st else "?"))

    def _cdp_attach(self):
        import time
        self._ensure_daemon()
        targets = self._daemon("/targets")
        pages = [t for t in (targets or [])
                 if t.get("type") == "page" and "claude.ai" in t.get("url", "")]
        # fast path: first tab already live at the claude.ai origin
        for t in pages:
            try:
                sid = (self._daemon("/attach", {"targetId": t["targetId"]}) or {}).get("sessionId")
                if not sid:
                    continue
                self._cdp("Target.activateTarget", {"targetId": t["targetId"]}, timeout=8)
                if self._cdp_eval(sid, "location.origin", await_promise=False, timeout=6) == BASE:
                    return sid
            except Exception:
                continue  # stale/frozen/slow tab — try the next one
        # none responded instantly: reuse existing tabs, polling their async un-freeze/
        # reload round-robin BEFORE opening a duplicate (a discarded tab reloads, not
        # instant). Poll ALL claude.ai tabs, not just the first — the one un-freezing
        # may not be pages[0].
        sids = []
        for t in pages:
            try:
                sid = (self._daemon("/attach", {"targetId": t["targetId"]}) or {}).get("sessionId")
                if sid:
                    self._cdp("Target.activateTarget", {"targetId": t["targetId"]}, timeout=8)
                    sids.append(sid)
            except Exception:
                continue
        for _ in range(20):
            for sid in sids:
                try:
                    if self._cdp_eval(sid, "location.origin", await_promise=False, timeout=6) == BASE:
                        return sid
                except Exception:
                    pass
            time.sleep(0.5)
        # last resort: open a new tab
        r = self._cdp("Target.createTarget", {"url": f"{BASE}/new"}, timeout=15)
        tid = _deep_find(r, "targetId")
        if not tid:
            raise RuntimeError("could not open a claude.ai tab via the cdp-daemon")
        sid = (self._daemon("/attach", {"targetId": tid}) or {}).get("sessionId")
        if not sid:
            raise RuntimeError("could not attach to the opened claude.ai tab")
        try:
            self._cdp("Target.activateTarget", {"targetId": tid}, timeout=8)
        except Exception:
            pass
        for _ in range(20):
            try:
                if self._cdp_eval(sid, "location.origin", await_promise=False, timeout=6) == BASE:
                    return sid
            except Exception:
                pass
            time.sleep(0.5)
        raise RuntimeError(f"claude.ai tab never reached {BASE}")

    def _evaluate(self, js, timeout=90):
        """Run an invoked-IIFE JS string in a claude.ai page context; return its value."""
        if self.backend == "cdp":
            return self._cdp_eval(self._cdp_sid, js, await_promise=True, timeout=timeout)
        return self._page.evaluate(js)

    def close(self):
        if self.backend == "patchright":
            self._browser.close()
            self._pw.stop()
        # cdp backend drives the user's real Chrome — nothing to tear down

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _discover_org(self) -> str:
        return self._get_json("/api/organizations")[0]["uuid"]

    def verify(self) -> dict:
        org = self._get_json("/api/organizations")[0]
        return {
            "name": org.get("name"),
            "uuid": org["uuid"],
            "billing_type": org.get("billing_type"),
            "capabilities": org.get("capabilities", []),
            "rate_limit_tier": org.get("rate_limit_tier"),
        }

    def _get(self, path: str, headers: dict | None = None) -> bytes:
        url = f"{BASE}{path}" if path.startswith("/") else path
        hdrs = json.dumps(headers or {})
        b64 = self._evaluate(f"""(async () => {{
            const r = await fetch({json.dumps(url)}, {{ headers: {hdrs} }});
            if (!r.ok) throw new Error('HTTP ' + r.status + ': ' + (await r.text()).slice(0,500));
            const buf = await r.arrayBuffer();
            const bytes = new Uint8Array(buf);
            let binary = '';
            const CH = 0x8000;
            for (let i = 0; i < bytes.length; i += CH) binary += String.fromCharCode.apply(null, bytes.subarray(i, i + CH));
            return btoa(binary);
        }})()""")
        return base64.b64decode(b64)

    def _get_json(self, path: str, headers: dict | None = None):
        return json.loads(self._get(path, headers))

    def _post(self, path: str, body: dict | None = None, headers: dict | None = None) -> dict:
        """POST JSON via the browser context. Returns {'status': int, 'body': str}."""
        url = f"{BASE}{path}" if path.startswith("/") else path
        hdrs = json.dumps({**(headers or {}), "Content-Type": "application/json"})
        body_js = json.dumps(json.dumps(body or {}))
        res = self._evaluate(f"""(async () => {{
            const r = await fetch({json.dumps(url)}, {{ method: 'POST', headers: {hdrs}, body: {body_js} }});
            return JSON.stringify({{ status: r.status, body: await r.text() }});
        }})()""")
        return json.loads(res)

    # --- Conversations ---

    def list_conversations(self, limit=60):
        return self._get_json(
            f"/api/organizations/{self.org_id}/chat_conversations?limit={limit}"
        )

    def get_conversation(self, uuid: str, full=True):
        params = "?tree=True&rendering_mode=messages&render_all_tools=true" if full else ""
        return self._get_json(
            f"/api/organizations/{self.org_id}/chat_conversations/{uuid}{params}"
        )

    # --- Code-web sessions ---

    def _ccr(self):
        """CCR gating headers for /v1/sessions; captured only under patchright."""
        if not self._ccr_headers:
            raise RuntimeError(
                "Code-web /v1/sessions needs the CCR gating headers, captured only under "
                "backend='patchright'. Re-instantiate ClaudeWeb(backend='patchright').")
        return self._ccr_headers

    def list_sessions(self):
        return self._get_json("/v1/sessions", self._ccr()).get("data", [])

    def get_session(self, session_id: str):
        return self._get_json(f"/v1/sessions/{session_id}", self._ccr())

    def get_session_events(self, session_id: str, limit=1000, after_id: str | None = None):
        params = f"?limit={limit}"
        if after_id:
            params += f"&after_id={urllib.parse.quote(after_id)}"
        return self._get_json(f"/v1/sessions/{session_id}/events{params}", self._ccr())

    # --- Files ---

    def find_files(self, conversation_uuid: str) -> list[FileRef]:
        convo = self.get_conversation(conversation_uuid)
        files: list[FileRef] = []
        seen = set()

        for msg in convo.get("chat_messages", []):
            msg_uuid = msg.get("uuid", "")

            for f in msg.get("files", []):
                if f.get("file_kind") in ("blob", "document"):
                    fid = f.get("file_uuid") or f.get("uuid", "")
                    if ("upload", fid) in seen:
                        continue
                    seen.add(("upload", fid))
                    files.append(FileRef(
                        name=f.get("file_name", ""),
                        path=fid,
                        kind="upload",
                        conversation_uuid=conversation_uuid,
                        message_uuid=msg_uuid,
                        size=f.get("size_bytes", 0),
                    ))

            for block in msg.get("content", []):
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_result" and block.get("name") == "present_files":
                    for resource in block.get("content", []):
                        if resource.get("type") != "local_resource":
                            continue
                        fpath = resource.get("file_path", "")
                        if ("wiggle", fpath) in seen:
                            continue
                        seen.add(("wiggle", fpath))
                        files.append(FileRef(
                            name=resource.get("name", ""),
                            path=fpath,
                            kind="wiggle",
                            conversation_uuid=conversation_uuid,
                            message_uuid=msg_uuid,
                        ))

        return files

    def download_file(self, ref: FileRef) -> bytes:
        if ref.kind == "wiggle":
            path_param = urllib.parse.quote(ref.path)
            zip_bytes = self._get(
                f"/api/organizations/{self.org_id}/conversations/"
                f"{ref.conversation_uuid}/wiggle/download-files?paths={path_param}"
            )
            zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
            names = zf.namelist()
            return zf.read(names[0]) if len(names) == 1 else zip_bytes
        # upload: prefer the files asset endpoint; fall back to the live VM mount if it purged.
        # The /files/{uuid}/preview asset store drops user uploads after a while (404); the
        # /mnt/user-data mount persists, so download-file still reads them (claude.ai sanitizes
        # the on-mount filename, spaces -> underscores).
        try:
            return self._get(f"/api/{self.org_id}/files/{ref.path}/preview")
        except Exception as e:
            if "404" not in str(e):
                raise
        last = None
        for nm in dict.fromkeys(n for n in (ref.name, (ref.name or "").replace(" ", "_")) if n):
            p = urllib.parse.quote(f"/mnt/user-data/uploads/{nm}", safe="")
            try:
                return self._get(
                    f"/api/organizations/{self.org_id}/conversations/"
                    f"{ref.conversation_uuid}/wiggle/download-file?path={p}"
                )
            except Exception as e:
                last = e
        raise RuntimeError(f"upload {ref.name!r}: preview 404 and mount fallback failed: {last}")

    def download_file_to(self, ref: FileRef, dest) -> Path:
        dest = Path(dest)
        dest.write_bytes(self.download_file(ref))
        return dest

    # --- Account data export (the ONLY surface that carries thinking signatures) ---

    def trigger_export(self, start_date=None, end_date=None, skip_file_content=True) -> str:
        """Start an account data export. start/end are 'YYYY-MM-DD' (or full ISO); omit
        both to export everything. A date-only end_date is made inclusive of the whole end
        day (+1 day); a full-ISO end_date is sent verbatim. Returns a one-time `nonce`.
        Endpoint: POST /api/organizations/{org}/export_data."""
        body: dict = {}
        if skip_file_content:
            body["skip_file_content"] = True
        if start_date:
            body["conversations_start_date"] = _to_iso(start_date)
        if end_date:
            body["conversations_end_date"] = _to_iso(end_date, end=True)
        r = self._post(f"/api/organizations/{self.org_id}/export_data", body)
        if r["status"] not in (200, 202):
            raise RuntimeError(f"export_data failed: HTTP {r['status']}: {r['body'][:300]}")
        try:
            nonce = json.loads(r["body"]).get("nonce")
        except Exception:
            nonce = None
        if not nonce:
            raise RuntimeError(f"export_data: no nonce in HTTP {r['status']}: {r['body'][:300]}")
        return nonce

    def export_signed_url(self, nonce: str) -> str | None:
        """POST for the signed download URL. Returns the GCS url, or None if not ready
        yet (non-200). A 200 is the SINGLE-USE success that consumes the link, so it is
        terminal: a 200 carrying no URL raises (never re-poll a consumed link).
        Endpoint: POST /api/organizations/{org}/export_signed_url/{nonce}."""
        r = self._post(f"/api/organizations/{self.org_id}/export_signed_url/{nonce}")
        st = r["status"]
        if st != 200:
            body = r.get("body") or ""
            if "export_link_used" in body:
                raise RuntimeError(f"export link already spent (nonce used): {body[:200]}")
            # terminal failures won't resolve by polling — fail fast instead of
            # spinning until poll_export's timeout.
            if st in (400, 401, 403) or st >= 500:
                raise RuntimeError(f"export_signed_url HTTP {st}: {body[:300]}")
            return None  # still processing (e.g. 404/202 while the export builds)
        try:
            url = _find_gcs_url(json.loads(r["body"]))
        except Exception:
            url = None
        if not url:
            raise RuntimeError(
                f"export ready (HTTP 200) but no download URL in body: {r['body'][:300]}")
        return url

    def poll_export(self, nonce: str, timeout=900, interval=5) -> str:
        """Poll export_signed_url until ready; returns the single-use download url."""
        import time
        deadline = time.time() + timeout
        while time.time() < deadline:
            url = self.export_signed_url(nonce)
            if url:
                return url
            time.sleep(interval)
        raise TimeoutError(f"export {nonce} not ready within {timeout}s")

    def download_export(self, signed_url: str, dest) -> Path:
        """Download the signed GCS url (public, no auth/Cloudflare) with plain urllib,
        streamed to disk (full-content exports can be large)."""
        import shutil
        if not signed_url.startswith("https://"):
            raise ValueError(f"refusing non-https download url: {signed_url[:80]}")
        dest = Path(dest)
        with urllib.request.urlopen(signed_url, timeout=300) as r, open(dest, "wb") as f:
            shutil.copyfileobj(r, f)
        return dest

    def export_account(self, dest, start_date=None, end_date=None, skip_files=True,
                       timeout=900) -> Path:
        """Scriptable end-to-end: trigger -> poll signed url -> download the zip to `dest`
        (trigger/poll run in the real tab under CDP; only the download is browserless).
        The zip's conversations.json carries thinking-block signatures (export-only)."""
        nonce = self.trigger_export(start_date, end_date, skip_file_content=skip_files)
        return self.download_export(self.poll_export(nonce, timeout=timeout), dest)


# --- Session key acquisition ---

def acquire_session_key(save_to: str = "~/claude_desktop_login") -> str:
    """Try all available sources. Saves to file for future use."""
    save_path = Path(save_to).expanduser()

    for loader in (_load_from_file, _load_from_desktop, _load_from_firefox, _load_from_chrome_cdp):
        try:
            key = loader()
            if key:
                # create/tighten to 0600 BEFORE writing the secret (no world-readable window)
                save_path.touch(mode=0o600, exist_ok=True)
                save_path.chmod(0o600)
                save_path.write_text(f"sessionKey={key}\n")
                return key
        except Exception:
            continue

    raise RuntimeError(
        "Could not find sessionKey. Extract manually:\n"
        "  1. Open https://claude.ai in Chrome\n"
        "  2. DevTools > Application > Cookies > claude.ai\n"
        "  3. Copy 'sessionKey' value\n"
        f"  4. echo 'sessionKey=<value>' > {save_path}"
    )


def _load_from_file() -> str | None:
    p = Path("~/claude_desktop_login").expanduser()
    if not p.exists():
        return None
    for line in p.read_text().splitlines():
        if line.startswith("sessionKey="):
            return line.split("=", 1)[1]
    return None


def _load_from_desktop() -> str | None:
    import sqlite3
    db = Path("~/.config/Claude/Cookies").expanduser()
    if not db.exists():
        return None
    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT value FROM cookies WHERE host_key='.claude.ai' AND name='sessionKey'"
    ).fetchone()
    conn.close()
    return row[0] if row and row[0] else None


def _load_from_firefox() -> str | None:
    import sqlite3, glob
    for db_path in glob.glob(str(Path("~/.mozilla/firefox/*/cookies.sqlite").expanduser())):
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            row = conn.execute(
                "SELECT value FROM moz_cookies WHERE host='.claude.ai' AND name='sessionKey'"
            ).fetchone()
            conn.close()
            if row and row[0]:
                return row[0]
        except Exception:
            continue
    return None


def _load_from_chrome_cdp() -> str | None:
    """Extract from running Chrome via CDP. Requires websocket-client."""
    import websocket

    port_file = Path("~/.config/google-chrome/DevToolsActivePort").expanduser()
    if not port_file.exists():
        return None

    lines = port_file.read_text().strip().split("\n")
    ws = websocket.create_connection(
        f"ws://127.0.0.1:{lines[0]}{lines[1]}", suppress_origin=True
    )

    def call(mid, method, params=None, **kw):
        ws.send(json.dumps({"id": mid, "method": method, "params": params or {}, **kw}))
        for _ in range(20):
            m = json.loads(ws.recv())
            if m.get("id") == mid:
                return m
        return {}

    targets = call(1, "Target.getTargets").get("result", {}).get("targetInfos", [])
    target = next((t for t in targets if "claude.ai" in t.get("url", "") and t["type"] == "page"), None)
    if not target:
        ws.close()
        return None

    sid = call(2, "Target.attachToTarget", {"targetId": target["targetId"], "flatten": True})
    session_id = sid.get("result", {}).get("sessionId")
    if not session_id:
        ws.close()
        return None

    call(3, "Network.enable", sessionId=session_id)
    cookies_resp = call(4, "Network.getCookies", {"urls": ["https://claude.ai"]}, sessionId=session_id)
    ws.close()

    cookies = cookies_resp.get("result", {}).get("cookies", [])
    sk = next((c["value"] for c in cookies if c["name"] == "sessionKey"), None)
    return sk
