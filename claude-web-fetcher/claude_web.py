"""
Claude.ai web session client.

Fetches conversations, files (user-uploaded and sandbox-generated),
and Claude Code-web (epitaxy) sessions.

All HTTP requests go through a patchright browser context. This solves
Cloudflare and avoids TLS fingerprint mismatches.
"""

import json
import urllib.parse
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


class ClaudeWeb:
    def __init__(self, session_key: str):
        from patchright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=True)
        self._context = self._browser.new_context()
        self._context.add_cookies([{
            "name": "sessionKey",
            "value": session_key,
            "domain": ".claude.ai",
            "path": "/",
            "httpOnly": True,
            "secure": True,
            "sameSite": "Lax",
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
        self.org_id = self._ccr_headers.get("x-organization-uuid") or self._discover_org()

    def close(self):
        self._browser.close()
        self._pw.stop()

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
        b64 = self._page.evaluate(f"""async () => {{
            const r = await fetch({json.dumps(url)}, {{ headers: {hdrs} }});
            if (!r.ok) throw new Error('HTTP ' + r.status + ': ' + (await r.text()).slice(0,500));
            const buf = await r.arrayBuffer();
            const bytes = new Uint8Array(buf);
            let binary = '';
            for (let i = 0; i < bytes.length; i++) binary += String.fromCharCode(bytes[i]);
            return btoa(binary);
        }}""")
        return base64.b64decode(b64)

    def _get_json(self, path: str, headers: dict | None = None):
        return json.loads(self._get(path, headers))

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

    def list_sessions(self):
        return self._get_json("/v1/sessions", self._ccr_headers).get("data", [])

    def get_session(self, session_id: str):
        return self._get_json(f"/v1/sessions/{session_id}", self._ccr_headers)

    def get_session_events(self, session_id: str, limit=1000, after_id: str | None = None):
        params = f"?limit={limit}"
        if after_id:
            params += f"&after_id={urllib.parse.quote(after_id)}"
        return self._get_json(f"/v1/sessions/{session_id}/events{params}", self._ccr_headers)

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
        return self._get(f"/api/{self.org_id}/files/{ref.path}/preview")

    def download_file_to(self, ref: FileRef, dest: Path) -> Path:
        dest.write_bytes(self.download_file(ref))
        return dest


# --- Session key acquisition ---

def acquire_session_key(save_to: str = "~/claude_desktop_login") -> str:
    """Try all available sources. Saves to file for future use."""
    save_path = Path(save_to).expanduser()

    for loader in (_load_from_file, _load_from_desktop, _load_from_firefox, _load_from_chrome_cdp):
        try:
            key = loader()
            if key:
                save_path.write_text(f"sessionKey={key}\n")
                save_path.chmod(0o600)
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
