#!/usr/bin/env python3
import argparse
import base64
import hashlib
import json
import mimetypes
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = None
DAEMON = "http://127.0.0.1:7799"
BASE = "https://chatgpt.com"
CHATGPT_ORIGIN = "https://chatgpt.com/"
TARGET_FILTER = "chatgpt.com"
SAVE_SENSITIVE_SESSION = False


def default_timezone_offset_min():
    offset = datetime.now().astimezone().utcoffset()
    return -int(offset.total_seconds() / 60) if offset else 0


def parse_args():
    parser = argparse.ArgumentParser(
        description="Archive a logged-in ChatGPT workspace/account via the local cdp-daemon."
    )
    parser.add_argument("output_dir", help="archive directory to create or resume")
    parser.add_argument("--daemon", default=DAEMON, help="cdp-daemon base URL")
    parser.add_argument("--base-url", default=BASE, help="ChatGPT base URL")
    parser.add_argument(
        "--target-filter",
        default=None,
        help="substring used to select the Chrome page target; defaults to the base URL host",
    )
    parser.add_argument("--account-id", default=None, help="override Oai-Account-Id")
    parser.add_argument(
        "--sentinel-token",
        default=os.environ.get("OPENAI_SENTINEL_CHAT_REQUIREMENTS_TOKEN"),
        help="optional value for OpenAI-Sentinel-Chat-Requirements-Token",
    )
    parser.add_argument(
        "--empty-sentinel-header",
        action="store_true",
        help="send OpenAI-Sentinel-Chat-Requirements-Token with an empty value",
    )
    parser.add_argument(
        "--timezone-offset-min",
        type=int,
        default=default_timezone_offset_min(),
        help="value for the accounts/check timezone_offset_min query parameter",
    )
    parser.add_argument(
        "--max-conversations",
        type=int,
        default=None,
        help="limit full conversation fetches after listing, useful for smoke tests",
    )
    parser.add_argument(
        "--skip-endpoint-snapshots",
        action="store_true",
        help="skip raw account/settings/models/tasks endpoint snapshots",
    )
    parser.add_argument("--skip-media-downloads", action="store_true", help="skip direct URL media downloads")
    parser.add_argument("--skip-file-downloads", action="store_true", help="skip file_id signing/downloads")
    parser.add_argument("--no-viewer", action="store_true", help="do not install the bundled browser UI")
    parser.add_argument(
        "--include-browser-storage",
        action="store_true",
        help="save localStorage/sessionStorage; may contain sensitive data",
    )
    parser.add_argument(
        "--save-sensitive-session",
        action="store_true",
        help="also save auth/session_raw_sensitive.json with the live access token",
    )
    return parser.parse_args()


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def mkdir(path):
    path.mkdir(parents=True, exist_ok=True)


def write_json(path, obj, mode=0o600):
    mkdir(path.parent)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(obj, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(tmp, path)
    os.chmod(path, mode)


def read_json(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def relpath(path):
    path = Path(path)
    try:
        return str(path.resolve().relative_to(ROOT))
    except Exception:
        return str(path)


def post_daemon(path, payload):
    req = urllib.request.Request(
        DAEMON + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=180) as response:
        return json.loads(response.read().decode("utf-8"))


def get_daemon(path):
    with urllib.request.urlopen(DAEMON + path, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def find_chatgpt_session():
    targets = get_daemon("/targets")
    pages = [
        target
        for target in targets
        if target.get("type") == "page" and TARGET_FILTER in target.get("url", "")
    ]
    if not pages:
        raise RuntimeError(f"No ChatGPT page target found matching {TARGET_FILTER!r}")
    target = pages[0]
    attached = post_daemon("/attach", {"targetId": target["targetId"]})
    return target, attached["sessionId"]


def eval_js(session_id, expression, timeout=180):
    result = post_daemon(
        "/eval",
        {
            "sessionId": session_id,
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": True,
        },
    )
    if "exceptionDetails" in result:
        raise RuntimeError(json.dumps(result["exceptionDetails"], ensure_ascii=False))
    remote = result.get("result", {})
    if "value" in remote:
        return remote["value"]
    if "description" in remote:
        return remote["description"]
    return remote


def js_string(value):
    return json.dumps(value, ensure_ascii=False)


def fetch_session_from_page(session_id):
    js = f"""
(async () => {{
  const res = await fetch({js_string(BASE + "/api/auth/session")}, {{
    credentials: "include",
    cache: "no-store"
  }});
  const body = await res.json();
  return {{
    status: res.status,
    ok: res.ok,
    body,
    href: location.href,
    title: document.title,
    bodyText: document.body ? document.body.innerText.slice(0, 2000) : ""
  }};
}})()
"""
    out = eval_js(session_id, js)
    if not out.get("ok") or not out.get("body", {}).get("accessToken"):
        raise RuntimeError(f"Could not obtain ChatGPT access token from page: {out}")
    return out


def redact_session(session_body):
    redacted = json.loads(json.dumps(session_body))
    for key in ("accessToken", "sessionToken"):
        if key in redacted:
            token = redacted[key]
            redacted[key] = {
                "redacted": True,
                "length": len(token) if isinstance(token, str) else None,
                "sha256": hashlib.sha256(token.encode("utf-8")).hexdigest()
                if isinstance(token, str)
                else None,
            }
    if "WARNING_BANNER" in redacted:
        redacted["WARNING_BANNER"] = redacted["WARNING_BANNER"][:120]
    return redacted


class Backend:
    def __init__(self, access_token, account_id, sentinel_token=None, empty_sentinel_header=False):
        self.access_token = access_token
        self.account_id = account_id
        self.sentinel_token = sentinel_token
        self.empty_sentinel_header = empty_sentinel_header
        self.last_request_at = 0.0

    def headers(self, extra=None):
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Accept": "application/json, text/plain, */*",
            "Origin": BASE,
            "Referer": CHATGPT_ORIGIN,
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/136.0 Safari/537.36"
            ),
            "Oai-Account-Id": self.account_id,
        }
        if self.sentinel_token is not None:
            headers["OpenAI-Sentinel-Chat-Requirements-Token"] = self.sentinel_token
        elif self.empty_sentinel_header:
            headers["OpenAI-Sentinel-Chat-Requirements-Token"] = ""
        if extra:
            headers.update(extra)
        return headers

    def request(self, path_or_url, method="GET", data=None, headers=None, retries=3):
        if path_or_url.startswith("http"):
            url = path_or_url
        else:
            url = BASE + path_or_url
        payload = None
        all_headers = self.headers(headers)
        if data is not None:
            payload = json.dumps(data).encode("utf-8")
            all_headers["Content-Type"] = "application/json"

        for attempt in range(retries):
            elapsed = time.time() - self.last_request_at
            if elapsed < 0.05:
                time.sleep(0.05 - elapsed)
            self.last_request_at = time.time()
            req = urllib.request.Request(url, data=payload, headers=all_headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=120) as response:
                    raw = response.read()
                    return self._response(url, method, response.status, response.headers, raw)
            except urllib.error.HTTPError as error:
                raw = error.read()
                result = self._response(url, method, error.code, error.headers, raw)
                if error.code in (408, 409, 425, 429, 500, 502, 503, 504) and attempt < retries - 1:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                return result
            except Exception as error:
                if attempt < retries - 1:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                return {
                    "request": {"method": method, "url": url},
                    "ok": False,
                    "status": None,
                    "headers": {},
                    "error": f"{type(error).__name__}: {error}",
                    "fetched_at": now_iso(),
                }

    def _response(self, url, method, status, headers, raw):
        content_type = headers.get("content-type", "")
        text = raw.decode("utf-8", "replace")
        body = None
        if "json" in content_type:
            try:
                body = json.loads(text)
            except json.JSONDecodeError:
                body = text
        else:
            try:
                body = json.loads(text)
            except json.JSONDecodeError:
                body = text
        return {
            "request": {"method": method, "url": url},
            "ok": 200 <= status < 300,
            "status": status,
            "headers": {k.lower(): v for k, v in headers.items()},
            "body": body,
            "fetched_at": now_iso(),
        }

    def download(self, url, dest):
        req = urllib.request.Request(
            url,
            headers=self.headers(
                {
                    "Accept": "*/*",
                    "Referer": CHATGPT_ORIGIN,
                }
            ),
        )
        with urllib.request.urlopen(req, timeout=240) as response:
            mkdir(dest.parent)
            tmp = dest.with_suffix(dest.suffix + ".tmp")
            digest = hashlib.sha256()
            size = 0
            with open(tmp, "wb") as handle:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    handle.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
            os.replace(tmp, dest)
            os.chmod(dest, 0o600)
            return {
                "ok": True,
                "status": response.status,
                "headers": {k.lower(): v for k, v in response.headers.items()},
                "path": str(dest),
                "bytes": size,
                "sha256": digest.hexdigest(),
            }


def safe_name(value, max_len=120):
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("_")
    return (value or "item")[:max_len]


def save_endpoint(backend, name, path, extra_index):
    print(f"api {name}", flush=True)
    result = backend.request(path)
    write_json(ROOT / "raw_api" / f"{safe_name(name)}.json", result)
    extra_index.append(
        {
            "name": name,
            "url": result.get("request", {}).get("url"),
            "status": result.get("status"),
            "ok": result.get("ok"),
            "path": f"raw_api/{safe_name(name)}.json",
        }
    )
    return result


def conversation_list_path(variant, offset):
    return ROOT / "raw_conversation_lists" / f"{safe_name(variant)}_offset_{offset:06d}.json"


def paginate_conversation_lists(backend):
    variants = {
        "all": {},
        "unarchived_unstarred": {"is_archived": "false", "is_starred": "false"},
        "archived": {"is_archived": "true"},
        "starred": {"is_starred": "true"},
        "unarchived": {"is_archived": "false"},
    }
    all_items = {}
    list_pages = []
    for variant, filters in variants.items():
        offset = 0
        limit = 100
        while True:
            query = {"offset": offset, "limit": limit, "order": "updated"}
            query.update(filters)
            path = "/backend-api/conversations?" + urllib.parse.urlencode(query)
            result = backend.request(path)
            write_json(conversation_list_path(variant, offset), result)
            body = result.get("body") if result.get("ok") else {}
            items = body.get("items", []) if isinstance(body, dict) else []
            total = body.get("total") if isinstance(body, dict) else None
            list_pages.append(
                {
                    "variant": variant,
                    "offset": offset,
                    "limit": limit,
                    "status": result.get("status"),
                    "ok": result.get("ok"),
                    "items": len(items),
                    "total": total,
                    "path": f"raw_conversation_lists/{safe_name(variant)}_offset_{offset:06d}.json",
                }
            )
            for item in items:
                cid = item.get("id")
                if cid:
                    all_items.setdefault(cid, item)
            print(f"list {variant} offset={offset} items={len(items)} total={total}", flush=True)
            if not result.get("ok") or not items:
                break
            offset += len(items)
            if total is not None and offset >= total:
                break
            if len(items) < limit:
                break
    ordered = sorted(
        all_items.values(),
        key=lambda item: item.get("update_time") or item.get("create_time") or "",
        reverse=True,
    )
    write_json(ROOT / "indexes" / "conversation_summaries.json", ordered)
    write_json(ROOT / "indexes" / "conversation_list_pages.json", list_pages)
    return ordered, list_pages


def fetch_full_conversations(backend, summaries):
    rows = []
    for index, summary in enumerate(summaries, 1):
        cid = summary["id"]
        title = summary.get("title") or cid
        out_path = ROOT / "conversations" / f"{cid}.json"
        if out_path.exists():
            result = read_json(out_path)
        else:
            print(f"conversation {index}/{len(summaries)} {cid} {title[:70]}", flush=True)
            result = backend.request(f"/backend-api/conversation/{urllib.parse.quote(cid)}")
            write_json(out_path, result)
        body = result.get("body") if isinstance(result, dict) else {}
        mapping = body.get("mapping") if isinstance(body, dict) else None
        rows.append(
            {
                "id": cid,
                "title": title,
                "create_time": summary.get("create_time"),
                "update_time": summary.get("update_time"),
                "is_archived": summary.get("is_archived"),
                "is_starred": summary.get("is_starred"),
                "gizmo_id": summary.get("gizmo_id"),
                "project_id": summary.get("project_id") or summary.get("workspace_id"),
                "status": result.get("status"),
                "ok": result.get("ok"),
                "message_nodes": len(mapping) if isinstance(mapping, dict) else None,
                "path": f"conversations/{cid}.json",
            }
        )
    write_json(ROOT / "indexes" / "conversations.json", rows)
    return rows


def paginate_library_nodes(backend):
    pages = []
    nodes_by_id = {}
    cursor = None
    page_no = 0
    while True:
        query = {}
        if cursor:
            query["cursor"] = cursor
        path = "/backend-api/files/library/nodes"
        if query:
            path += "?" + urllib.parse.urlencode(query)
        result = backend.request(path)
        out_path = ROOT / "raw_library_nodes" / f"page_{page_no:04d}.json"
        write_json(out_path, result)
        body = result.get("body") if result.get("ok") else {}
        items = body.get("items", []) if isinstance(body, dict) else []
        cursor = body.get("cursor") if isinstance(body, dict) else None
        print(f"library page={page_no} items={len(items)} cursor={bool(cursor)}", flush=True)
        pages.append(
            {
                "page": page_no,
                "status": result.get("status"),
                "ok": result.get("ok"),
                "items": len(items),
                "cursor": cursor,
                "path": f"raw_library_nodes/page_{page_no:04d}.json",
            }
        )
        for item in items:
            node_id = item.get("id")
            if node_id:
                nodes_by_id[node_id] = item
        if not result.get("ok") or not cursor:
            break
        page_no += 1
        if page_no > 1000:
            raise RuntimeError("library pagination exceeded 1000 pages")
    nodes = list(nodes_by_id.values())
    write_json(ROOT / "indexes" / "library_nodes.json", nodes)
    write_json(ROOT / "indexes" / "library_pages.json", pages)
    return nodes, pages


def iter_values(obj, path="$"):
    yield path, obj
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield from iter_values(value, f"{path}.{key}")
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            yield from iter_values(value, f"{path}[{index}]")


URL_RE = re.compile(r"https://[^\s\"'<>)}\]]+")
FILE_ID_RE = re.compile(r"\bfile_[0-9a-fA-F]{24,}\b")
SEDIMENT_RE = re.compile(r"sediment://[A-Za-z0-9_./#:-]+")


def collect_artifact_references():
    refs = {}
    search_paths = [
        ROOT / "conversations",
        ROOT / "raw_api",
        ROOT / "raw_conversation_lists",
    ]
    extra_files = [
        ROOT / "page_probe.json",
        ROOT / "api_probe.json",
        ROOT / "browser_storage_full.json",
    ]
    for root in search_paths:
        if not root.exists():
            continue
        for path in root.rglob("*.json"):
            collect_refs_from_file(path, refs)
    for path in extra_files:
        if path.exists():
            collect_refs_from_file(path, refs)
    out = sorted(refs.values(), key=lambda item: (item["kind"], item["value"]))
    write_json(ROOT / "indexes" / "artifact_references.json", out)
    return out


def add_ref(refs, kind, value, source, pointer):
    key = (kind, value)
    item = refs.setdefault(
        key,
        {
            "kind": kind,
            "value": value,
            "sources": [],
            "downloadable": is_downloadable_url(value) if kind == "url" else False,
        },
    )
    if len(item["sources"]) < 20:
        item["sources"].append({"file": str(source.relative_to(ROOT)), "pointer": pointer})


def collect_refs_from_file(path, refs):
    try:
        data = read_json(path)
    except Exception:
        return
    for pointer, value in iter_values(data):
        if isinstance(value, str):
            for match in URL_RE.findall(value):
                add_ref(refs, "url", match, path, pointer)
            for match in FILE_ID_RE.findall(value):
                add_ref(refs, "file_id", match, path, pointer)
            for match in SEDIMENT_RE.findall(value):
                add_ref(refs, "sediment", match, path, pointer)


def is_downloadable_url(url):
    return (
        "chatgpt.com/backend-api/estuary/content" in url
        or "oaiusercontent.com" in url
        or "/backend-api/files/" in url
    )


def extension_from_headers(headers, url):
    disposition = headers.get("content-disposition", "")
    match = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", disposition)
    if match:
        name = urllib.parse.unquote(match.group(1).strip('"'))
        suffix = Path(name).suffix
        if suffix:
            return suffix[:16]
    ctype = headers.get("content-type", "").split(";", 1)[0].strip()
    ext = mimetypes.guess_extension(ctype) if ctype else None
    if ext:
        return ext
    suffix = Path(urllib.parse.urlparse(url).path).suffix
    return suffix[:16] if suffix else ".bin"


def download_referenced_media(backend, refs):
    media_dir = ROOT / "media"
    mkdir(media_dir)
    manifest_path = ROOT / "indexes" / "media_downloads.json"
    existing = read_json(manifest_path) if manifest_path.exists() else []
    done = {entry.get("url") for entry in existing}
    rows = existing[:]
    urls = []
    for ref in refs:
        if ref.get("kind") == "url" and ref.get("downloadable"):
            urls.append(ref["value"])
    # Preserve first-seen order while removing duplicates.
    urls = list(dict.fromkeys(urls))
    for index, url in enumerate(urls, 1):
        if url in done:
            continue
        parsed = urllib.parse.urlparse(url)
        query = urllib.parse.parse_qs(parsed.query)
        file_id = (query.get("id") or [""])[0].replace("#", "_")
        stem = safe_name(file_id or hashlib.sha256(url.encode("utf-8")).hexdigest()[:24], 80)
        provisional = media_dir / f"{index:05d}_{stem}.bin"
        print(f"media {index}/{len(urls)} {stem}", flush=True)
        try:
            result = backend.download(url, provisional)
            ext = extension_from_headers(result["headers"], url)
            final = provisional.with_suffix(ext)
            if final != provisional:
                os.replace(provisional, final)
            result["path"] = relpath(final)
            result["url"] = url
            result["source_count"] = len(next((r for r in refs if r.get("value") == url), {}).get("sources", []))
        except Exception as error:
            result = {
                "ok": False,
                "url": url,
                "error": f"{type(error).__name__}: {error}",
                "fetched_at": now_iso(),
            }
        rows.append(result)
        write_json(manifest_path, rows)
    return rows


def collect_workspace_file_ids(refs, library_nodes):
    file_ids = {}
    for item in library_nodes:
        fid = item.get("file_id")
        if fid:
            file_ids.setdefault(fid, {"sources": []})["library_node"] = item
            file_ids[fid]["sources"].append({"kind": "library_node", "id": item.get("id")})
    for ref in refs:
        if ref.get("kind") != "file_id":
            continue
        fid = ref.get("value")
        sources = ref.get("sources", [])
        # Keep this scoped to the current workspace export. Full browser storage may
        # contain stale recentImages/recentUploads from other accounts.
        workspace_sources = [
            source
            for source in sources
            if source.get("file", "").startswith(("conversations/", "raw_api/", "raw_conversation_lists/"))
        ]
        if workspace_sources:
            entry = file_ids.setdefault(fid, {"sources": []})
            entry["sources"].extend({"kind": "artifact_ref", **source} for source in workspace_sources[:5])
    return file_ids


def filename_for_file_id(fid, entry, index):
    item = entry.get("library_node") or {}
    name = item.get("name") or item.get("file_name") or fid
    stem = safe_name(f"{fid}_{name}", 160)
    return ROOT / "files" / f"{index:05d}_{stem}.bin"


def download_file_ids(backend, refs, library_nodes):
    manifest_path = ROOT / "indexes" / "file_downloads.json"
    file_ids = collect_workspace_file_ids(refs, library_nodes)
    existing = read_json(manifest_path) if manifest_path.exists() else []
    by_file_id = {}
    for entry in existing:
        fid = entry.get("file_id")
        if fid not in file_ids:
            continue
        if fid not in by_file_id or entry.get("ok") or not by_file_id[fid].get("ok"):
            by_file_id[fid] = entry
    existing = [by_file_id[fid] for fid in sorted(by_file_id)]
    done = set(by_file_id)
    rows = existing[:]
    write_json(manifest_path, rows)
    ordered = sorted(file_ids)
    for index, fid in enumerate(ordered, 1):
        if fid in done:
            continue
        print(f"file {index}/{len(ordered)} {fid}", flush=True)
        sign = backend.request(f"/backend-api/files/download/{urllib.parse.quote(fid)}?inline=true")
        write_json(ROOT / "raw_file_downloads" / f"{fid}.json", sign)
        body = sign.get("body") if isinstance(sign, dict) else {}
        download_url = body.get("download_url") if isinstance(body, dict) else None
        row = {
            "file_id": fid,
            "sign_status": sign.get("status"),
            "sign_ok": sign.get("ok"),
            "download_url": download_url,
            "sources": file_ids[fid].get("sources", [])[:20],
            "raw_sign_path": f"raw_file_downloads/{fid}.json",
        }
        if download_url:
            dest = filename_for_file_id(fid, file_ids[fid], index)
            try:
                result = backend.download(download_url, dest)
                ext = extension_from_headers(result["headers"], download_url)
                item = file_ids[fid].get("library_node") or {}
                if item.get("file_extension"):
                    ext = "." + str(item["file_extension"]).lstrip(".")
                final = dest.with_suffix(ext)
                if final != dest:
                    os.replace(dest, final)
                result["path"] = relpath(final)
                row.update(result)
            except Exception as error:
                row.update({"ok": False, "error": f"{type(error).__name__}: {error}"})
        else:
            row.update({"ok": False, "error": "No download_url in signing response"})
        rows.append(row)
        write_json(manifest_path, rows)
    return rows


def write_manifest(
    target,
    session,
    options,
    endpoints,
    list_pages,
    conversations,
    refs,
    media,
    library_pages,
    library_nodes,
    file_downloads,
):
    key_files = {
        "conversation_index": "indexes/conversations.json",
        "conversation_summaries": "indexes/conversation_summaries.json",
        "library_nodes": "indexes/library_nodes.json",
        "artifact_references": "indexes/artifact_references.json",
        "media_downloads": "indexes/media_downloads.json",
        "file_downloads": "indexes/file_downloads.json",
        "endpoint_index": "indexes/endpoint_snapshots.json",
        "session_redacted": "auth/session_redacted.json",
    }
    notes = [
        "Full conversation responses are stored under conversations/*.json as raw backend responses.",
        "Message metadata is not normalized away; reasoning/tool/media fields present in backend JSON remain in those raw files.",
        "Hidden chain-of-thought not returned by ChatGPT backend cannot be exported by this method.",
    ]
    if SAVE_SENSITIVE_SESSION:
        key_files["session_raw_sensitive"] = "auth/session_raw_sensitive.json"
        notes.append("session_raw_sensitive.json contains live auth material and should stay private.")
    else:
        notes.append("Live auth material was not saved; rerun with --save-sensitive-session only if explicitly needed.")

    manifest = {
        "created_at": now_iso(),
        "root": str(ROOT),
        "chatgpt_target": target,
        "options": options,
        "account": {
            "id": session["body"].get("account", {}).get("id"),
            "planType": session["body"].get("account", {}).get("planType"),
            "structure": session["body"].get("account", {}).get("structure"),
            "organizationId": session["body"].get("account", {}).get("organizationId"),
            "user": {
                "id": session["body"].get("user", {}).get("id"),
                "email": session["body"].get("user", {}).get("email"),
                "name": session["body"].get("user", {}).get("name"),
            },
        },
        "counts": {
            "endpoint_snapshots": len(endpoints),
            "conversation_list_pages": len(list_pages),
            "conversation_summaries": len(conversations),
            "library_pages": len(library_pages),
            "library_nodes": len(library_nodes),
            "artifact_references": len(refs),
            "media_download_attempts": len(media),
            "media_downloaded": sum(1 for item in media if item.get("ok")),
            "file_download_attempts": len(file_downloads),
            "files_downloaded": sum(1 for item in file_downloads if item.get("ok")),
        },
        "key_files": key_files,
        "notes": notes,
    }
    write_json(ROOT / "manifest.json", manifest)
    return manifest


def capture_page_probe(session_id, include_storage=False):
    if include_storage:
        storage_expr = """
  const localStorageDump = {};
  for (const key of Object.keys(localStorage)) localStorageDump[key] = localStorage.getItem(key);
  const sessionStorageDump = {};
  for (const key of Object.keys(sessionStorage)) sessionStorageDump[key] = sessionStorage.getItem(key);
"""
        storage_return = """
    localStorage: localStorageDump,
    sessionStorage: sessionStorageDump,
"""
    else:
        storage_expr = ""
        storage_return = """
    localStorageKeys: Object.keys(localStorage).sort(),
    sessionStorageKeys: Object.keys(sessionStorage).sort(),
"""
    js = f"""
(() => {{
{storage_expr}
  return {{
    href: location.href,
    title: document.title,
    readyState: document.readyState,
    bodyText: document.body ? document.body.innerText.slice(0, 3000) : null,
    cookieNames: document.cookie.split("; ").filter(Boolean).map((x) => x.split("=")[0]).sort(),
{storage_return}
    nextData: typeof __NEXT_DATA__ === "undefined" ? null : {{
      buildId: __NEXT_DATA__.buildId,
      page: __NEXT_DATA__.page,
      propsKeys: Object.keys((__NEXT_DATA__.props || {{}})),
    }},
    globals: Object.keys(window)
      .filter((key) => /chat|openai|workspace|account|org|team|arkose|intercom/i.test(key))
      .slice(0, 300),
  }};
}})()
"""
    probe = eval_js(session_id, js)
    write_json(ROOT / "page_probe.json", probe)
    if include_storage:
        write_json(
            ROOT / "browser_storage_full.json",
            {
                "captured_at": now_iso(),
                "localStorage": probe.get("localStorage", {}),
                "sessionStorage": probe.get("sessionStorage", {}),
            },
        )
    return probe


def install_viewer_template(force=True):
    source = Path(__file__).resolve().parents[1] / "assets" / "browser-template"
    target = ROOT / "browser"
    if not source.is_dir():
        print(f"warning: viewer template not found at {source}", file=sys.stderr)
        return 0
    target.mkdir(parents=True, exist_ok=True)
    copied = 0
    for src in source.rglob("*"):
        if not src.is_file():
            continue
        rel = src.relative_to(source)
        dst = target / rel
        if dst.exists() and not force:
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        copied += 1
    return copied


def main():
    global ROOT, DAEMON, BASE, CHATGPT_ORIGIN, TARGET_FILTER, SAVE_SENSITIVE_SESSION

    args = parse_args()
    ROOT = Path(args.output_dir).expanduser().resolve()
    DAEMON = args.daemon.rstrip("/")
    BASE = args.base_url.rstrip("/")
    CHATGPT_ORIGIN = BASE + "/"
    TARGET_FILTER = args.target_filter or urllib.parse.urlparse(BASE).netloc or "chatgpt.com"
    SAVE_SENSITIVE_SESSION = bool(args.save_sensitive_session)

    os.umask(0o077)
    for name in [
        "auth",
        "raw_api",
        "raw_conversation_lists",
        "raw_library_nodes",
        "raw_file_downloads",
        "conversations",
        "indexes",
        "media",
        "files",
    ]:
        mkdir(ROOT / name)

    target, session_id = find_chatgpt_session()
    print(f"target {target.get('targetId')} {target.get('url')}", flush=True)
    session = fetch_session_from_page(session_id)
    account_id = args.account_id or session["body"]["account"]["id"]
    backend = Backend(
        session["body"]["accessToken"],
        account_id,
        sentinel_token=args.sentinel_token,
        empty_sentinel_header=args.empty_sentinel_header,
    )

    if args.save_sensitive_session:
        write_json(ROOT / "auth" / "session_raw_sensitive.json", session)
    write_json(ROOT / "auth" / "session_redacted.json", redact_session(session["body"]))
    capture_page_probe(session_id, include_storage=args.include_browser_storage)

    endpoints = []
    endpoint_paths = {
        "accounts_check": f"/backend-api/accounts/check/v4-2023-04-27?timezone_offset_min={args.timezone_offset_min}",
        "me": "/backend-api/me",
        "settings_user": "/backend-api/settings/user",
        "connection_status": "/backend-api/ca/v2/user/connection_status",
        "models": "/backend-api/models?iim=false&is_gizmo=false",
        "pins": "/backend-api/pins",
        "gizmos_bootstrap": "/backend-api/gizmos/bootstrap?limit=20",
        "gizmos_sidebar_owned": "/backend-api/gizmos/snorlax/sidebar?owned_only=true&conversations_per_gizmo=5&limit=20",
        "hazelnuts_installed": "/backend-api/hazelnuts?include_permissions=true&scope=installed",
        "apps_sources_dropdown": "/backend-api/apps/sources_dropdown",
        "images_bootstrap": "/backend-api/images/bootstrap",
        "files_library": "/backend-api/files/library",
        "files_library_nodes": "/backend-api/files/library/nodes",
        "files_library_storage_usage": "/backend-api/files/library/storage/usage",
        "tasks": "/backend-api/tasks",
        "hermes_agents_pinned": "/backend-api/hermes/agents/pinned?limit=100",
        "user_segments": "/backend-api/user_segments",
        "celsius_ws_user": "/backend-api/celsius/ws/user",
        "beacons_home": "/backend-api/beacons/home",
        "conversation_init": "/backend-api/conversation/init",
        "user_surveys_active": "/backend-api/user_surveys/active",
        "checkout_pricing_countries": "/backend-api/checkout_pricing_config/countries",
        "memory_settings_candidate": "/backend-api/memory/settings",
        "memories_candidate": "/backend-api/memories",
        "projects_candidate": "/backend-api/projects",
        "projects_list_candidate": "/backend-api/projects/list",
        "folders_candidate": "/backend-api/folders",
        "connectors_status_candidate": "/backend-api/connectors/status",
    }
    if args.skip_endpoint_snapshots:
        print("skipping endpoint snapshots", flush=True)
    else:
        for name, path in endpoint_paths.items():
            save_endpoint(backend, name, path, endpoints)
    write_json(ROOT / "indexes" / "endpoint_snapshots.json", endpoints)

    summaries, list_pages = paginate_conversation_lists(backend)
    if args.max_conversations is not None:
        summaries = summaries[: max(0, args.max_conversations)]
    conversations = fetch_full_conversations(backend, summaries)
    library_nodes, library_pages = paginate_library_nodes(backend)
    refs = collect_artifact_references()
    if args.skip_media_downloads:
        media = read_json(ROOT / "indexes" / "media_downloads.json") if (ROOT / "indexes" / "media_downloads.json").exists() else []
        write_json(ROOT / "indexes" / "media_downloads.json", media)
    else:
        media = download_referenced_media(backend, refs)
    if args.skip_file_downloads:
        file_downloads = read_json(ROOT / "indexes" / "file_downloads.json") if (ROOT / "indexes" / "file_downloads.json").exists() else []
        write_json(ROOT / "indexes" / "file_downloads.json", file_downloads)
    else:
        file_downloads = download_file_ids(backend, refs, library_nodes)
    if not args.no_viewer:
        copied = install_viewer_template(force=True)
        print(f"viewer files installed: {copied}", flush=True)
    manifest = write_manifest(
        target,
        session,
        {
            "daemon": DAEMON,
            "base_url": BASE,
            "target_filter": TARGET_FILTER,
            "account_id": account_id,
            "sentinel_header": bool(args.sentinel_token is not None or args.empty_sentinel_header),
            "max_conversations": args.max_conversations,
            "skip_endpoint_snapshots": args.skip_endpoint_snapshots,
            "skip_media_downloads": args.skip_media_downloads,
            "skip_file_downloads": args.skip_file_downloads,
            "include_browser_storage": args.include_browser_storage,
            "save_sensitive_session": args.save_sensitive_session,
        },
        endpoints,
        list_pages,
        conversations,
        refs,
        media,
        library_pages,
        library_nodes,
        file_downloads,
    )
    print(
        json.dumps(
            {
                "done": True,
                "manifest": str(ROOT / "manifest.json"),
                "viewer": None if args.no_viewer else str(ROOT / "browser" / "index.html"),
                "counts": manifest["counts"],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
