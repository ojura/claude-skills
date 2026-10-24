#!/usr/bin/env python3
# Claude Code status line: single-process renderer with an optional daemon.
# Displays: model, effort, org, context bar, cache health bar + TTL + renewal,
# rewrite factor, cost, 5h/7d rate-limit bars + renewal, session id.
#
# Modes:
#   python3 -ES statusline-render.py            one-shot: stdin JSON -> stdout
#   python3 -ES statusline-render.py --daemon   serve renders for ALL sessions
#                                               over statusline.sock in the
#                                               config dir (CLAUDE_CONFIG_DIR,
#                                               else ~/.claude)
#
# The daemon holds the sqlite connection and per-transcript results in
# memory, so a steady-state render is one stat() plus string formatting.
# It exits after 10 idle minutes, and after serving a request if this
# source file changed on disk (the next client spawns a fresh daemon), so
# edits here take effect without hunting processes. The C client
# (statusline-client.c) auto-spawns the daemon and falls back to one-shot
# mode if the socket cannot be reached, so the statusline never goes blank.
#
# --- cache health metric ---
# Share of the conversation's API calls whose cached prefix is still being
# reused. Per API call, redundancy = parent context footprint - cache_read
# (what was cached but failed to be read, hence re-written). Writing new
# content, however large, is not redundancy. A call that loses the majority
# of the parent footprint restarts the count, so health =
# calls_since_last_rewrite / total_calls (rises again as clean calls accrue).
# Compaction is a fresh start: the count resets at compact boundaries, and
# logicalParentUuid (the compaction linkage) is deliberately not followed.
# Incremental: state per (transcript, uuid) in sqlite; only new bytes are
# parsed each render, and a rewind reconnects to its parent through the uuid
# table.
# Also tracks the cumulative rewrite factor, in tokens: redundancy summed
# over the chain divided by the last message's context footprint (a fresh
# root has no prior footprint, so it contributes nothing). It amortizes as the
# conversation grows and resets only at a compaction boundary, so it can
# pass 100% when a conversation has been re-cached more than once over.
# The sqlite state is a disposable cache: schema drift drops + rebuilds.

import json
import math
import os
import re
import sys
import time

HOME = os.path.expanduser("~")
# Same resolution the harness and install.sh use, so a non-default config dir
# does not leave the client spawning a daemon from a path that does not exist.
CLAUDE_DIR = os.environ.get("CLAUDE_CONFIG_DIR") or HOME + "/.claude"
DB_PATH = CLAUDE_DIR + "/statusline-cache-health.db"
SIDECAR = CLAUDE_DIR + "/statusline-cache-health.state.json"
SOCK = CLAUDE_DIR + "/statusline.sock"
IDLE_EXIT_S = 600
CACHE_MEMO_VERSION = 2
AGENT_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,200}\Z")
OWNER_RE = re.compile(r"statusline-view-[A-Za-z0-9]+\Z")

def _env_flag(name):
    return os.environ.get(name, "").lower() in ("1", "true", "yes", "on")


def _rgb_env(name, default):
    raw = os.environ.get(name, "").strip()
    try:
        if raw.startswith("#") and len(raw) == 7:
            return tuple(int(raw[i:i + 2], 16) for i in (1, 3, 5))
        rgb = tuple(int(v) for v in raw.replace(";", ",").split(","))
        if len(rgb) == 3 and all(0 <= v <= 255 for v in rgb):
            return rgb
    except ValueError:
        pass
    return default


def _default_theme():
    try:
        return "light" if int(os.environ.get("COLORFGBG", "").split(";")[-1]) >= 7 else "dark"
    except (ValueError, IndexError):
        return "dark"


PALETTES = {
    "dark": {
        "alarm": (240, 126, 117), "muted": (136, 136, 136),
        "bar_dim": (76, 76, 76), "bar_on_fill": (32, 36, 38),
        "bar_on_dim": (211, 215, 207),
    },
    "light": {
        "alarm": (180, 45, 45), "muted": (90, 90, 90),
        "bar_dim": (225, 225, 225), "bar_on_fill": (32, 36, 38),
        "bar_on_dim": (32, 36, 38),
    },
}
THEME = os.environ.get("CLAUDE_STATUSLINE_THEME", _default_theme()).lower()
PALETTE = PALETTES.get(THEME, PALETTES["dark"])
ALARM_RGB = _rgb_env("CLAUDE_STATUSLINE_ALARM_RGB", PALETTE["alarm"])
MUTED_RGB = _rgb_env("CLAUDE_STATUSLINE_MUTED_RGB", PALETTE["muted"])
BAR_DIM_RGB = _rgb_env("CLAUDE_STATUSLINE_BAR_DIM_RGB", PALETTE["bar_dim"])
BAR_ON_FILL_RGB = _rgb_env("CLAUDE_STATUSLINE_BAR_ON_FILL_RGB", PALETTE["bar_on_fill"])
BAR_ON_DIM_RGB = _rgb_env("CLAUDE_STATUSLINE_BAR_ON_DIM_RGB", PALETTE["bar_on_dim"])
COLOR_MODE = os.environ.get("CLAUDE_STATUSLINE_COLOR", "auto").lower()
COLOR_ENABLED = "NO_COLOR" not in os.environ and COLOR_MODE != "never"
TRUECOLOR = COLOR_ENABLED and (COLOR_MODE == "always" or
    os.environ.get("COLORTERM", "").lower() in ("truecolor", "24bit") or
    "direct" in os.environ.get("TERM", "").lower())
COMPACT = _env_flag("CLAUDE_STATUSLINE_COMPACT")
BAR_WIDTH = 10
TOKEN_WIDTH = 4
BAR_LABEL_WIDTH = TOKEN_WIDTH * 2 + 1
LABELED_BAR_WIDTH = BAR_LABEL_WIDTH + 2
CACHE_ALARM_PCT = 25

# Azure -> yellow -> orange -> red ramp. The cool half traces the GIMP
# azure/yellow gradient (a straight-RGB blend whose desaturated sage midpoint
# skips saturated green); the warm half continues yellow -> orange -> red.
# Piecewise-linear RGB interpolation over the stops; input clamps to [0,100].
RAMP = [(0, (0, 150, 255)), (8, (46, 154, 252)), (17, (99, 168, 240)),
        (26, (154, 192, 215)), (33, (177, 211, 194)), (48, (249, 231, 121)),
        (73, (248, 178, 104)), (100, (240, 126, 117))]


def paint(text, fg=None, bg=None, dim=False):
    if not COLOR_ENABLED:
        return text
    codes = ["2"] if dim else []
    if TRUECOLOR and fg is not None:
        codes.extend(("38", "2", *(str(v) for v in fg)))
    if TRUECOLOR and bg is not None:
        codes.extend(("48", "2", *(str(v) for v in bg)))
    if not codes:
        return text
    return "\033[%sm%s\033[0m" % (";".join(codes), text)


def muted(text):
    return paint(text, fg=MUTED_RGB, dim=not TRUECOLOR)


def dimmed(text):
    return paint(text, dim=True)


def ramp_rgb(p):
    p = 0.0 if p < 0 else 100.0 if p > 100 else float(p)
    lo = 0
    for i in range(len(RAMP) - 1):
        if RAMP[i][0] <= p:
            lo = i
    pos_lo, c_lo = RAMP[lo]
    pos_hi, c_hi = RAMP[lo + 1]
    span = pos_hi - pos_lo
    t = (p - pos_lo) / span if span > 0 else 0.0
    return tuple(int(c_lo[k] + (c_hi[k] - c_lo[k]) * t + 0.5) for k in range(3))


def round_half_up(value, digits=0):
    factor = 10 ** digits
    rounded = int(float(value) * factor + 0.5)
    return rounded if not digits else rounded / factor


def fit_cells(text, width):
    # Status labels are ASCII; normalize here so make_bar owns its cell width.
    return str(text)[:width].ljust(width)


def format_tokens(tokens):
    value = max(0.0, float(tokens))
    if 0 < value < 1000:
        text = "<1k"
    elif value <= 999_000:
        text = "%dk" % round_half_up(value / 1000)
    else:
        units = ((1_000_000, "M"), (1_000_000_000, "G"),
                 (1_000_000_000_000, "T"))
        for i, (scale, suffix) in enumerate(units):
            amount = value / scale
            if amount < 999.5 or i == len(units) - 1:
                amount = min(amount, 999)
                if amount < 9.95:
                    amount = round_half_up(amount, 1)
                    text = ("%.1f%s" if amount < 10 else "%.0f%s") % (amount, suffix)
                else:
                    text = "%d%s" % (round_half_up(amount), suffix)
                break
    return text.rjust(TOKEN_WIDTH)


def format_context_label(used, total):
    return "%s/%s" % (format_tokens(used), format_tokens(total))


def format_pct(pct):
    value = max(0.0, min(100.0, float(pct)))
    return "%3d%%" % round_half_up(value)


def make_bar(pct, color_pct=None, label=None):
    # A label replaces the interior of a fixed-width bar. Its two backgrounds
    # retain the filled and dim colors while neutral text crosses the split;
    # color_pct lets cache health invert the ramp semantics.
    if color_pct is None:
        color_pct = pct
    width = LABELED_BAR_WIDTH if label is not None else BAR_WIDTH
    filled = round_half_up(pct / 100 * width)
    filled = 0 if filled < 0 else width if filled > width else filled
    fill_rgb = ramp_rgb(color_pct)
    if label is None:
        return paint("█" * filled, fg=fill_rgb) + dimmed("░" * (width - filled))

    label = fit_cells(label, BAR_LABEL_WIDTH)
    if not TRUECOLOR:
        return label
    label_filled = max(0, min(BAR_LABEL_WIDTH, filled - 1))
    filled_edge = paint("█", fg=fill_rgb, bg=fill_rgb)
    empty_edge = paint("░", fg=BAR_DIM_RGB, bg=BAR_DIM_RGB)
    left = filled_edge if filled else empty_edge
    text = ""
    if label_filled:
        text += paint(label[:label_filled], fg=BAR_ON_FILL_RGB, bg=fill_rgb)
    if label_filled < BAR_LABEL_WIDTH:
        text += paint(label[label_filled:], fg=BAR_ON_DIM_RGB, bg=BAR_DIM_RGB)
    right = filled_edge if filled == width else empty_edge
    return left + text + right


def open_db():
    import sqlite3
    db = sqlite3.connect(DB_PATH, timeout=0.25)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    NODE_COLS = ["path", "uuid", "total", "lastrw", "msgid", "ttl", "redsum", "ctx",
                 "callts", "bustts", "busthot", "model", "input_ctx"]
    FILE_COLS = ["path", "offset", "leaf", "last_ts", "agent_id", "device", "inode"]
    if ([r[1] for r in db.execute("PRAGMA table_info(nodes)")] != NODE_COLS
            or [r[1] for r in db.execute("PRAGMA table_info(files)")] != FILE_COLS):
        db.execute("DROP TABLE IF EXISTS nodes")
        db.execute("DROP TABLE IF EXISTS files")
    db.execute("CREATE TABLE IF NOT EXISTS nodes(path TEXT, uuid TEXT,"
               " total INT, lastrw INT, msgid TEXT, ttl TEXT, redsum INT, ctx INT,"
               " callts INT, bustts INT, busthot INT, model TEXT, input_ctx INT,"
               " PRIMARY KEY(path,uuid))")
    db.execute("CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY,"
               " offset INT, leaf TEXT, last_ts INT, agent_id TEXT, device INT, inode INT)")
    db.execute("CREATE TABLE IF NOT EXISTS task_metadata("
               " session_id TEXT, path TEXT, agent_id TEXT, payload TEXT,"
               " PRIMARY KEY(session_id,path,agent_id))")
    return db


def context_tokens(usage):
    # Server-side tool loops sum their usage. Their last iteration is the live
    # context, as in Claude's own statusline, rather than all iterations added.
    iterations = usage.get("iterations")
    if isinstance(iterations, list) and iterations:
        last = iterations[-1]
        if isinstance(last, dict) and "input_tokens" in last:
            usage = last
    return sum(usage.get(k) or 0 for k in (
        "input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))


def cache_engine(tp, db, agent_id=None):
    # Parses transcript bytes appended since the stored offset into the
    # sqlite state; returns (health, ttl, rw, exp) or None.
    from datetime import datetime

    row = db.execute("SELECT offset,leaf,last_ts,agent_id,device,inode"
                     " FROM files WHERE path=?", (tp,)).fetchone()
    st = os.stat(tp)
    off, leaf, last_ts = row[:3] if row else (0, None, 0)
    last_ts = last_ts or 0
    if row and (off > st.st_size or row[3:] != (agent_id, st.st_dev, st.st_ino)):
        db.execute("DELETE FROM nodes WHERE path=?", (tp,))
        off, leaf, last_ts = 0, None, 0

    # total, lastrw, msgid, ttl, redsum, ctx, callts, bustts, busthot, model, input_ctx
    ROOT = [0, 1, "", "", 0, 0, 0, 0, 0, "", 0]
    new = {}

    def lookup(u):
        if not u:
            return None
        if u in new:
            return new[u]
        r = db.execute("SELECT total,lastrw,msgid,ttl,redsum,ctx,callts,bustts,busthot,model,input_ctx"
                       " FROM nodes WHERE path=? AND uuid=?", (tp, u)).fetchone()
        return list(r) if r else None

    with open(tp, "rb") as f:
        f.seek(off)
        buf = f.read()
    end = buf.rfind(b"\n")  # complete lines only; a partial tail waits for next render
    lines = buf[:end].split(b"\n") if end >= 0 else []
    new_off = off + end + 1 if end >= 0 else off

    for raw in lines:
        if not raw.strip():
            continue
        try:
            e = json.loads(raw)
        except Exception:
            continue
        if not isinstance(e, dict):
            continue
        u = e.get("uuid")
        if not u or (agent_id is None and e.get("isSidechain")):
            continue
        if agent_id is not None and e.get("agentId") not in (None, agent_id):
            continue
        if e.get("subtype") == "compact_boundary" or e.get("isCompactSummary"):
            # Compaction restarts the conversation (and legitimately rebuilds
            # the cache), so recency resets — but waste already paid does not
            # un-happen. Carry the integral and the observed TTL across.
            prev = (lookup(e.get("parentUuid")) or lookup(leaf) or ROOT)
            fresh = ROOT[:]
            fresh[3] = prev[3]
            fresh[4] = prev[4]
            fresh[7] = prev[7]
            fresh[8] = prev[8]
            new[u] = fresh
            leaf = u
            continue
        pu = e.get("parentUuid")
        p = lookup(pu)
        if p is None:
            # A parentUuid naming a message this file does not contain (from
            # entries merged in from another session, say) continues the chain
            # that is already being followed. Only a null parent starts over.
            p = (lookup(leaf) if pu else None) or ROOT
        if e.get("type") == "assistant":
            m = e.get("message") or {}
            usage = m.get("usage") or {}
            mid = m.get("id") or ""
            cr = usage.get("cache_read_input_tokens") or 0
            cc = usage.get("cache_creation_input_tokens") or 0
            it = usage.get("input_tokens") or 0
            # Mid-stream lines carry input_tokens but no cache counts yet, so
            # reading one as a finished call scores the whole parent footprint
            # as lost. Wait for a line that reports the cache fields; the
            # closing lines of the same response carry them. Do not claim mid
            # here either, or the closing line looks already counted.
            if "cache_read_input_tokens" not in usage:
                new[u] = p[:]
                leaf = u
                continue
            # count one node per API response (content blocks share message.id);
            # all-zero usage = synthetic/error entry, pass through
            if (cr or cc or it) and mid != p[2]:
                total = p[0] + 1
                # redundancy: cached parent footprint the call failed to read
                # (and thus re-wrote). Measured against what this call could
                # have read: min() so a prompt trimmed below the parent
                # footprint (tool-result clearing) is not booked as waste.
                # Every call also leaves a benign residue — cache blocks are
                # 128-token quantized and the newest tail is never cached yet
                # (measured median 660, p90 ~1k per call) — so below 2048
                # tokens it is noise, not a bust, and does not accumulate.
                base = min(p[5], cr + cc + it)
                redundant = max(0, base - cr)
                if redundant < 2048:
                    redundant = 0
                redsum = p[4] + redundant
                # the count restarts only when more than half the footprint was lost
                bust = base > 0 and redundant * 2 > base
                lastrw = total if bust else p[1]
                call_ts = 0
                ts = e.get("timestamp")
                if ts:
                    try:
                        call_ts = int(datetime.fromisoformat(
                            ts.replace("Z", "+00:00")).timestamp())
                        last_ts = call_ts
                    except Exception:
                        pass
                bustts = p[7]
                if bust:
                    # Usage shows a missed prefix, not why it was missed. A
                    # missing event timestamp must not borrow the previous one.
                    bustts = call_ts
                b = usage.get("cache_creation") or {}
                e5 = b.get("ephemeral_5m_input_tokens") or 0
                e1 = b.get("ephemeral_1h_input_tokens") or 0
                ttl = "5m+1h" if (e5 and e1) else "5m" if e5 else "1h" if e1 else p[3]
                new[u] = [total, lastrw, mid, ttl, redsum, cr + cc + it,
                          call_ts or p[6], bustts, 0,
                          m.get("model") or p[9], context_tokens(usage)]
            else:
                new[u] = [p[0], p[1], mid or p[2]] + p[3:]
                if cr or cc or it:
                    new[u][9] = m.get("model") or p[9]
                    new[u][10] = context_tokens(usage)
        else:
            new[u] = p[:]
        leaf = u

    if new:
        db.executemany("INSERT OR REPLACE INTO nodes VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       [(tp, u) + tuple(v) for u, v in new.items()])
    db.execute("INSERT OR REPLACE INTO files VALUES(?,?,?,?,?,?,?)",
               (tp, new_off, leaf, last_ts, agent_id, st.st_dev, st.st_ino))
    db.commit()

    n = lookup(leaf)
    if n and n[0] > 0:
        live = n[0] - n[1] + 1
        rw = n[4] / n[5] * 100.0 if n[5] else 0.0
        # Mixed caches reach their first TTL boundary after five minutes.
        # Unknown TTLs provide no basis for an expiry estimate.
        window = {"1h": 3600, "5m": 300, "5m+1h": 300}.get(n[3])
        exp = last_ts + window if last_ts and window else 0
        return (live / n[0] * 100.0, n[3] or "?", rw, exp, n[7] or 0, n[8] or 0,
                n[10], n[9])
    return None


class Cache:
    # (size, mtime_ns)-keyed memo of engine results. The daemon keeps one of
    # these plus an open db for its lifetime; one-shot mode persists the memo
    # in a sidecar file instead, so unchanged-transcript renders skip sqlite.
    def __init__(self, persistent):
        self.persistent = persistent
        self.mem = {}
        self.db = None
        self.pstate_path = None  # resolved proxy-state file, for tick fingerprints
        if persistent:
            try:
                with open(SIDECAR) as f:
                    saved = json.load(f)
                if saved.get("version") == CACHE_MEMO_VERSION:
                    self.mem = saved.get("entries") or {}
            except Exception:
                self.mem = {}

    def values(self, tp, agent_id=None):
        try:
            st = os.stat(tp)
        except OSError:
            return None
        ent = self.mem.get(tp)
        stamp = [st.st_size, st.st_mtime_ns, st.st_dev, st.st_ino, st.st_ctime_ns, agent_id]
        if ent and ent[:6] == stamp:
            return tuple(ent[6]) if ent[6] else None
        if self.db is None:
            self.db = open_db()
        try:
            result = cache_engine(tp, self.db, agent_id)
        except Exception:
            return None
        self.mem[tp] = stamp + [list(result) if result else None]
        if self.persistent:
            try:
                tmp = SIDECAR + ".tmp.%d" % os.getpid()
                with open(tmp, "w") as f:
                    json.dump({"version": CACHE_MEMO_VERSION, "entries": self.mem}, f)
                os.replace(tmp, SIDECAR)
            except Exception:
                pass
            self.db.close()
            self.db = None
        return result

    def ingest_tasks(self, data):
        session_id, tp = session_identity(data)
        tasks = data.get("tasks")
        if not session_id or not tp or not isinstance(tasks, list):
            return
        if self.db is None:
            self.db = open_db()
        rows = []
        for task in tasks:
            if not isinstance(task, dict):
                continue
            agent_id = task.get("id")
            if not isinstance(agent_id, str) or not AGENT_ID_RE.fullmatch(agent_id):
                continue
            payload = {k: task.get(k) for k in (
                "model", "effort", "contextWindowSize", "name")}
            rows.append((session_id, tp, agent_id, json.dumps(payload)))
        if rows:
            self.db.executemany("INSERT OR REPLACE INTO task_metadata VALUES(?,?,?,?)", rows)
            self.db.commit()

    def task_info(self, session_id, tp, agent_id):
        if self.db is None:
            self.db = open_db()
        row = self.db.execute("SELECT payload FROM task_metadata"
                              " WHERE session_id=? AND path=? AND agent_id=?",
                              (session_id, tp, agent_id)).fetchone()
        try:
            value = json.loads(row[0]) if row else {}
            return value if isinstance(value, dict) else {}
        except (ValueError, TypeError):
            return {}


def session_identity(data):
    tp = data.get("transcript_path")
    tp = os.path.abspath(tp) if isinstance(tp, str) and tp else None
    session_id = data.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        session_id = os.path.basename(tp)[:-6] if tp and tp.endswith(".jsonl") else None
    return session_id, tp


def clean_label(value, limit=80):
    if not isinstance(value, str):
        return ""
    # Keep terminal controls, line breaks and bidi format controls out of names.
    import unicodedata
    return " ".join("".join(c for c in value
        if not unicodedata.category(c).startswith("C")).split())[:limit]


def positive_number(value):
    try:
        return (isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(value) and value > 0)
    except OverflowError:
        return False


def view_owner_alive(owner):
    if not isinstance(owner, str) or not OWNER_RE.fullmatch(owner):
        return False
    import fcntl
    try:
        fd = os.open(os.path.join(CLAUDE_DIR, owner), os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        except OSError:
            return False
        return False
    finally:
        os.close(fd)


def same_model(left, right):
    # A native task's explicit window belongs to its request model. The API
    # response omits the request's context selector, but keeps the model id.
    if not isinstance(left, str) or not isinstance(right, str):
        return False
    strip = lambda value: re.sub(r"\[(?:1m|200k)\]$", "", value)
    return bool(left and right and strip(left) == strip(right))


def selected_input(data, cache):
    if not isinstance(data, dict):
        return {}
    native = data.get("statusline_input")
    if not isinstance(native, dict):
        return data
    if data.get("statusline_task_metadata") is True:
        cache.ingest_tasks(native)
        return None
    view = data.get("statusline_view")
    try:
        view = json.loads(view) if isinstance(view, str) else view
    except (ValueError, TypeError):
        return native
    session_id, tp = session_identity(native)
    if (not isinstance(view, dict) or view.get("version") != 1 or not session_id
            or view.get("session_id") != session_id):
        return native
    if "expires_at" in view and (not positive_number(view["expires_at"])
                                 or view["expires_at"] <= time.time() * 1000):
        return native
    if "owner" in view and not view_owner_alive(view["owner"]):
        return native
    agent_id = view.get("agent_id")
    if (not isinstance(agent_id, str) or not AGENT_ID_RE.fullmatch(agent_id)
            or not tp or not tp.endswith(".jsonl")):
        return native
    agent_path = os.path.join(tp[:-6], "subagents", "agent-" + agent_id + ".jsonl")
    values = cache.values(agent_path, agent_id)
    try:
        task = cache.task_info(session_id, tp, agent_id)
    except Exception:
        task = {}
    model = clean_label(values[7] if values else view.get("model") or task.get("model"))
    selected = dict(native)
    selected.update(transcript_path=agent_path, model={"id": model}, effort={}, context_window={})
    selected["_selected_agent_id"] = agent_id
    selected["_selected_agent_name"] = clean_label(view.get("name") or task.get("name"))
    selected["_cache_values"] = values
    selected["_selected_missing"] = not os.path.isfile(agent_path)
    effort = view.get("effort") if same_model(view.get("model"), model) else None
    if effort is None and same_model(task.get("model"), model):
        effort = task.get("effort")
    if isinstance(effort, str):
        effort = clean_label(effort, 20)
    if effort is not None and isinstance(effort, (str, int, float)) and not isinstance(effort, bool):
        selected["effort"] = {"level": effort}
    window = view.get("context_window_size")
    if not same_model(view.get("model"), model):
        window = None
    if not positive_number(window) and same_model(task.get("model"), model):
        window = task.get("contextWindowSize")
    if values:
        context = {"total_input_tokens": values[6]}
        if positive_number(window):
            context.update(context_window_size=window,
                           used_percentage=max(0, min(100, values[6] / window * 100)))
        selected["context_window"] = context
    return selected


# Time-dependent fragments render into numbered slots instead of being
# inlined, so a clock tick rebuilds only those and splices them into an
# otherwise cached line.
SLOT = "\x00S%d\x00"

# Croatian weekday abbreviations, Monday first to match tm_wday; hardcoded so
# the stamp reads the same whatever locale the process happens to run under.
HR_DAYS = ["pon", "uto", "sri", "čet", "pet", "sub", "ned"]


def slot_str(kind, exp):
    # All three renewals read "→ <when>", with a live countdown in parens
    # when one is meaningful. Callers supply the leading space.
    if kind in ("warm", "warm_mixed"):
        # Transcript timestamps approximate the cache's minimum TTL boundary;
        # passing it does not establish that the server has removed the cache.
        if not exp or exp <= 0:
            return ""
        now = int(time.time())
        stamp = "→ ~%s" % time.strftime("%H:%M", time.localtime(exp))
        if now < exp:
            if COMPACT:
                return muted(stamp)
            rem = exp - now
            return muted("%s (%d:%02d)" % (stamp, rem // 60, rem % 60))
        label = "5m TTL elapsed" if kind == "warm_mixed" else "TTL elapsed"
        return "%s %s" % (muted(stamp), muted("(" + label + ")"))
    if kind == "reset_rel":
        # Window renewal within the day: wall clock plus countdown (H:MM:SS).
        stamp = "→ %s" % time.strftime("%H:%M", time.localtime(exp))
        if COMPACT:
            return muted(stamp)
        rem = max(0, exp - int(time.time()))
        return muted("%s (%d:%02d:%02d)" % (
            stamp, rem // 3600, rem % 3600 // 60, rem % 60))
    if kind == "reset_abs":
        # Renewal days out: weekday + date, where a countdown reads as noise.
        t = time.localtime(exp)
        return muted("→ %s %d.%d. %s" % (
            HR_DAYS[t.tm_wday], t.tm_mday, t.tm_mon,
            time.strftime("%H:%M", t)))
    if kind == "ago":
        # Elapsed time since an event (last cache bust): live like the
        # countdowns, so it keeps aging on clock ticks between requests.
        ago = max(0, int(time.time()) - int(exp))
        return ("%dm" % (ago // 60)) if ago < 3600 else ("%.1fh" % (ago / 3600.0))
    return ""


def fill(template, slots):
    for i, spec in enumerate(slots):
        template = template.replace(SLOT % i, slot_str(*spec))
    return template


def render_core(data, cache):
    # Returns (template, slots): the full line is fill(template, slots).
    # Holding the time-dependent fragments in slots lets the daemon serve pure
    # clock ticks by refilling a cached template instead of re-rendering.
    data = selected_input(data, cache)
    if data is None:
        return "", []
    def get(path, default=None):
        cur = data
        for k in path:
            if not isinstance(cur, dict) or k not in cur:
                return default
            cur = cur[k]
        return cur if cur is not None else default

    parts = []
    slots = []

    agent_id = get(["_selected_agent_id"])
    if agent_id:
        name = get(["_selected_agent_name"]) or agent_id
        parts.append("agent " + name)
        if get(["_selected_missing"]):
            parts.append(muted("transcript unavailable"))

    model = get(["model", "display_name"]) or get(["model", "id"]) or "unknown"
    parts.append(model[7:] if model.startswith("Claude ") else model)

    effort = get(["effort", "level"])
    if effort or (agent_id and effort == 0):
        parts.append("effort:%s" % effort)

    # Org and quota from the anthropic-proxy's state file, which the proxy
    # writes from the response headers of real inference calls, keyed by a hash
    # of the Authorization header. Falls back to the stdin values when the file
    # is not there.
    five_pct = get(["rate_limits", "five_hour", "used_percentage"])
    week_pct = get(["rate_limits", "seven_day", "used_percentage"])
    org_id = None
    # The token is read only to hash it into the proxy's per-credential
    # filename. It is never sent anywhere, and nothing happens at all when no
    # proxy-state file exists, which is the normal case.
    tok = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    if not tok:
        try:
            with open(CLAUDE_DIR + "/.credentials.json") as f:
                tok = (json.load(f).get("claudeAiOauth") or {}).get("accessToken")
        except Exception:
            tok = None
    if tok:
        import hashlib
        pstate = (CLAUDE_DIR + "/proxy-state/"
                  + hashlib.sha256(("Bearer " + tok).encode()).hexdigest()[:16] + ".json")
        cache.pstate_path = pstate
        try:
            with open(pstate) as f:
                ps = json.load(f)
            org_id = ps.get("org")
            # proxy stores fractions (0.5 = 50%); the bars want 0-100
            if ps.get("h5") is not None:
                five_pct = float(ps["h5"]) * 100
            if ps.get("d7") is not None:
                week_pct = float(ps["d7"]) * 100
        except Exception:
            pass
    if org_id:
        parts.append("org %s" % org_id.split("-")[0])

    used_pct = get(["context_window", "used_percentage"])
    used_tokens = get(["context_window", "total_input_tokens"])
    context_size = get(["context_window", "context_window_size"])
    if used_pct is not None:
        if used_tokens is not None and context_size:
            label = format_context_label(used_tokens, context_size)
            parts.append("ctx %s %s" % (make_bar(used_pct, label=label),
                                         format_pct(used_pct)))
        else:
            parts.append("ctx %s %s" % (make_bar(used_pct), format_pct(used_pct)))
    elif agent_id and used_tokens is not None:
        parts.append("ctx " + format_tokens(used_tokens).strip())

    # Cache health bar: ramp inverted, so a full bar reads azure and a
    # drained one red.
    tp = get(["transcript_path"])
    cv = (data.get("_cache_values") if agent_id else
          cache.values(tp) if tp and os.path.isfile(tp) else None)
    if cv:
        health, ttl, rw, exp = cv[:4]
        # Older memo entries may still be 4-tuples; they have no event time.
        bustts = cv[4] if len(cv) > 4 else 0
        # The TTL badge sits beside the label, dimmed for the normal 1h and
        # alarm-colored for 5m, which on a main session usually means overage.
        # All three segments that carry a time then read
        # "<label> <bar> <pct> → <renewal>", and the renewal goes in a slot so
        # clock ticks rebuild it on its own.
        badge = ""
        if ttl == "1h" or (agent_id and ttl == "5m"):
            badge = " " + muted(ttl)
        elif ttl and ttl != "?":
            badge = " " + paint(ttl, fg=ALARM_RGB)
        pct = format_pct(health)
        if health < CACHE_ALARM_PCT:
            pct = paint(pct, fg=ALARM_RGB)
        seg = "cache%s %s %s" % (badge, make_bar(health, 100 - health), pct)
        if exp and ttl in ("5m", "1h", "5m+1h"):
            seg += " " + SLOT % len(slots)
            slots.append(("warm_mixed" if ttl == "5m+1h" else "warm", exp))
        parts.append(seg)

        # Cumulative rewrite factor as a colored multiplier: tokens
        # redundantly re-cached over the current context footprint. 0.00x
        # azure = purely additive; 1.00x = re-cached once; can exceed 1.
        parts.append("recached " + paint("%.2fx" % (rw / 100), fg=ramp_rgb(rw)))

        # Last substantial cache miss. The usage counters establish diminished
        # reuse, not eviction or expiry. Keep its age explicit and live.
        if bustts:
            seg = "cache miss " + SLOT % len(slots) + " ago"
            slots.append(("ago", int(bustts)))
            parts.append(seg)

    cost = get(["cost", "total_cost_usd"])
    if cost is not None and cost > 0:
        parts.append(("session $%.2f" if agent_id else "$%.2f") % cost)

    # Each rate-limit bar carries when its window renews: the 5h window gets
    # wall clock plus a live countdown, the 7d window a dated stamp.
    if five_pct is not None:
        seg = "5h %s %s" % (make_bar(five_pct), format_pct(five_pct))
        r5 = get(["rate_limits", "five_hour", "resets_at"])
        if r5:
            seg += " " + SLOT % len(slots)
            slots.append(("reset_rel", int(r5)))
        parts.append(seg)
    if week_pct is not None:
        seg = "7d %s %s" % (make_bar(week_pct), format_pct(week_pct))
        r7 = get(["rate_limits", "seven_day", "resets_at"])
        if r7:
            seg += " " + SLOT % len(slots)
            slots.append(("reset_abs", int(r7)))
        parts.append(seg)

    # Session id (transcript filename without extension), dimmed
    if tp and not COMPACT:
        base = os.path.basename(tp)
        if base.endswith(".jsonl"):
            base = base[:-6]
        parts.append(muted(base))

    sep = (" %s " if COMPACT else "  %s  ") % muted("·")
    return sep.join(parts), slots


def render(data, cache):
    return fill(*render_core(data, cache))


def _fingerprint(data, cache):
    # The stdin fields the render actually uses, plus freshness stamps of the
    # two out-of-band inputs (transcript, proxy state). stdin also carries
    # volatile fields we never display (durations tick every request), so we
    # fingerprint the used subset rather than the raw bytes.
    def g(*ks):
        cur = data
        for k in ks:
            if not isinstance(cur, dict):
                return None
            cur = cur.get(k)
        return cur
    tp = g("transcript_path")
    try:
        st = os.stat(tp)
        tstat = (st.st_size, st.st_mtime_ns, st.st_dev, st.st_ino, st.st_ctime_ns)
    except (OSError, TypeError):
        tstat = None
    ps_mt = None
    if cache.pstate_path:
        try:
            ps_mt = os.stat(cache.pstate_path).st_mtime_ns
        except OSError:
            ps_mt = None
    sig = (g("model", "display_name"), g("model", "id"), g("effort", "level"),
           g("context_window", "used_percentage"),
           g("context_window", "total_input_tokens"),
           g("context_window", "context_window_size"),
           g("cost", "total_cost_usd"),
           g("rate_limits", "five_hour", "used_percentage"),
           g("rate_limits", "five_hour", "resets_at"),
           g("rate_limits", "seven_day", "used_percentage"),
           g("rate_limits", "seven_day", "resets_at"), tp,
           g("_selected_agent_id"), g("_selected_agent_name"),
           g("_selected_missing"), g("_cache_values"))
    return tp, sig, tstat, ps_mt


def serve(data, cache, memo):
    # Internally event-driven: a pure clock tick (nothing changed but time)
    # splices a fresh countdown into the cached line; any real change re-renders
    # and refreshes the memo.
    data = selected_input(data, cache)
    if data is None:
        return ""
    tp, sig, tstat, ps_mt = _fingerprint(data, cache)
    ent = memo.get(tp)
    if ent and ent[0] == sig and ent[1] == tstat and ent[2] == ps_mt:
        return fill(ent[3], ent[4])
    template, slots = render_core(data, cache)
    memo[tp] = (sig, tstat, ps_mt, template, slots)
    return fill(template, slots)


def read_all(sock):
    chunks = []
    while True:
        b = sock.recv(65536)
        if not b:
            return b"".join(chunks)
        chunks.append(b)


def daemon_main():
    import fcntl
    import socket
    # Singleton via connect-then-bind: a live daemon answers the connect and
    # we exit; a stale socket file refuses and gets unlinked. Probe, unlink
    # and bind happen under an exclusive flock: without it, two spawns racing
    # through this window each saw no listener, each unlinked the path, and
    # each bound its own inode — leaving an unreachable daemon listening on
    # an orphaned socket forever. The loser now waits on the lock, probes the
    # winner successfully, and exits.
    lockfd = os.open(SOCK + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(lockfd, fcntl.LOCK_EX)
    try:
        probe = socket.socket(socket.AF_UNIX)
        probe.settimeout(0.2)
        try:
            probe.connect(SOCK)
            probe.close()
            return  # someone already serves
        except OSError:
            pass
        try:
            os.unlink(SOCK)
        except OSError:
            pass
        srv = socket.socket(socket.AF_UNIX)
        try:
            srv.bind(SOCK)
        except OSError:
            return  # cannot bind at all (path/permissions); nothing to serve
        os.chmod(SOCK, 0o600)
        srv.listen(8)
    finally:
        os.close(lockfd)  # releases the flock; the socket is live past here
    srv.settimeout(IDLE_EXIT_S)

    src = os.path.abspath(__file__)
    src_mtime = os.stat(src).st_mtime_ns
    cache = Cache(persistent=False)
    memo = {}  # per-transcript (fingerprint, pre, exp, post) for tick splicing

    while True:
        try:
            conn, _ = srv.accept()
        except socket.timeout:
            break  # idle: free the RAM, a client will respawn us
        except OSError:
            break
        try:
            conn.settimeout(2.0)
            raw = read_all(conn)
            try:
                data = json.loads(raw.decode("utf-8", "replace")) if raw.strip() else {}
            except Exception:
                data = {}
            try:
                out = serve(data, cache, memo)
            except Exception:
                out = ""
            conn.sendall(out.encode())
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass
        # Serve, then exit if the source changed: the next client spawns
        # a fresh daemon running the edited code.
        try:
            if os.stat(src).st_mtime_ns != src_mtime:
                break
        except OSError:
            break
    try:
        os.unlink(SOCK)
    except OSError:
        pass


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--daemon":
        daemon_main()
        return
    try:
        data = json.load(sys.stdin)
    except Exception:
        data = {}
    sys.stdout.write(render(data, Cache(persistent=True)))


if __name__ == "__main__":
    main()
