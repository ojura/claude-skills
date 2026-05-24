"""
Extract claude.ai sessionKey from a running Chrome instance via CDP.

Requires:
- Chrome running with DevTools enabled (chrome://inspect)
- websocket-client (pip install websocket-client)

Connects to Chrome's CDP WebSocket, navigates to claude.ai if needed,
captures the sessionKey from request cookies, and saves to ~/claude_desktop_login.

The value never enters the Claude conversation - run this script directly.
"""

import json
import sys
import time
from pathlib import Path

try:
    import websocket
except ImportError:
    print("pip install websocket-client", file=sys.stderr)
    sys.exit(1)


def get_chrome_ws():
    port_file = Path("~/.config/google-chrome/DevToolsActivePort").expanduser()
    if not port_file.exists():
        raise FileNotFoundError(
            "Chrome DevToolsActivePort not found. "
            "Enable via chrome://inspect/#remote-debugging"
        )
    lines = port_file.read_text().strip().split("\n")
    return f"ws://127.0.0.1:{lines[0]}{lines[1]}"


def extract_session_key():
    ws_url = get_chrome_ws()
    ws = websocket.create_connection(ws_url, suppress_origin=True)

    # Get targets
    ws.send(json.dumps({"id": 1, "method": "Target.getTargets", "params": {}}))
    result = json.loads(ws.recv())
    targets = result.get("result", {}).get("targetInfos", [])

    # Find a claude.ai page target
    claude_target = next(
        (t for t in targets if "claude.ai" in t.get("url", "") and t["type"] == "page"),
        None,
    )
    if not claude_target:
        raise RuntimeError("No claude.ai tab open in Chrome")

    target_id = claude_target["targetId"]

    # Attach to target
    ws.send(json.dumps({
        "id": 2,
        "method": "Target.attachToTarget",
        "params": {"targetId": target_id, "flatten": True},
    }))

    session_id = None
    for _ in range(20):
        msg = json.loads(ws.recv())
        if msg.get("id") == 2:
            session_id = msg["result"]["sessionId"]
            break

    if not session_id:
        raise RuntimeError("Failed to attach to claude.ai target")

    # Enable Network
    ws.send(json.dumps({
        "id": 3,
        "method": "Network.enable",
        "params": {},
        "sessionId": session_id,
    }))
    for _ in range(20):
        msg = json.loads(ws.recv())
        if msg.get("id") == 3:
            break

    # Get cookies
    ws.send(json.dumps({
        "id": 4,
        "method": "Network.getCookies",
        "params": {"urls": ["https://claude.ai"]},
        "sessionId": session_id,
    }))
    for _ in range(20):
        msg = json.loads(ws.recv())
        if msg.get("id") == 4:
            cookies = msg.get("result", {}).get("cookies", [])
            sk = next((c for c in cookies if c["name"] == "sessionKey"), None)
            if sk:
                save_path = Path("~/claude_desktop_login").expanduser()
                save_path.write_text(f"sessionKey={sk['value']}\n")
                save_path.chmod(0o600)
                print(f"Saved sessionKey to {save_path} (len={len(sk['value'])})")
                ws.close()
                return
            else:
                names = [c["name"] for c in cookies]
                raise RuntimeError(f"sessionKey not in cookies: {names}")

    ws.close()
    raise RuntimeError("Timed out waiting for cookies response")


if __name__ == "__main__":
    extract_session_key()
