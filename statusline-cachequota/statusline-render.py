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
ACTIVATION_RE = re.compile(r"v2_[a-f0-9]{32}\Z")
TEAM_PART_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}\Z")
PANE_RE = re.compile(r"%[0-9]+\Z")

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
    peer_cols = ["session_id", "pid", "proc_start", "team", "agent_id",
                 "parent_session_id", "pane", "payload", "origin_verified"]
    existing_peer_cols = [r[1] for r in db.execute("PRAGMA table_info(peer_frames)")]
    if existing_peer_cols and existing_peer_cols != peer_cols:
        db.execute("DROP TABLE peer_frames")
    db.execute("CREATE TABLE IF NOT EXISTS peer_frames("
               " session_id TEXT, pid INT, proc_start TEXT, team TEXT, agent_id TEXT,"
               " parent_session_id TEXT, pane TEXT, payload TEXT, origin_verified INT,"
               " PRIMARY KEY(session_id,pid,proc_start))")
    db.execute("CREATE INDEX IF NOT EXISTS peer_identity ON peer_frames(team,agent_id)")
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
        self.processes = {}  # argv identity is immutable for one kernel process
        self.frames = {}  # rendered-field snapshots, unchanged ticks do not write
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
            if (not isinstance(agent_id, str)
                    or not (AGENT_ID_RE.fullmatch(agent_id) or teammate_address(agent_id))):
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

    def register_frame(self, native, process, origin_pid=None):
        """Save the process's raw main frame, before its own view is applied."""
        session_id, tp = session_identity(native)
        pid = process.get("pid") if isinstance(process, dict) else None
        record = live_session(pid, session_id, allow_missing=True)
        if not record or not tp:
            return
        start = record["procStart"]
        if not process_descends_from(origin_pid, pid, start):
            return
        key = (pid, start)
        if key not in self.processes:
            self.processes[key] = process_agent_identity(pid)
        identity = self.processes[key]
        pane = process.get("pane") or tmux_pane(record.get("tmux"))
        pane = pane if isinstance(pane, str) and PANE_RE.fullmatch(pane) else ""
        recorded_pane = tmux_pane(record.get("tmux"))
        if pane and recorded_pane and pane != recorded_pane:
            return
        # Native durations advance on every refresh. Keep the metrics the peer
        # renderer uses, so dozens of idle sessions do not churn the WAL.
        frame = {k: native[k] for k in ("session_id", "transcript_path", "model", "effort",
                                        "context_window", "rate_limits") if k in native}
        cost = native.get("cost")
        if isinstance(cost, dict) and "total_cost_usd" in cost:
            frame["cost"] = {"total_cost_usd": cost["total_cost_usd"]}
        snapshot = (identity.get("team", ""), identity.get("agent_id", ""),
                    identity.get("parent_session_id", ""), pane,
                    json.dumps(frame, sort_keys=True, separators=(",", ":")))
        frame_key = (session_id, pid, start)
        if self.frames.get(frame_key) == snapshot:
            return
        if self.db is None:
            self.db = open_db()
        # /clear can change sessionId without changing pid or start time. A
        # reused pid also retires its former frame; neither may resolve later.
        self.db.execute("DELETE FROM peer_frames WHERE pid=? AND"
                        " (session_id<>? OR proc_start<>?)", (pid, session_id, start))
        self.db.execute("INSERT OR REPLACE INTO peer_frames VALUES(?,?,?,?,?,?,?,?,?)",
                        (session_id, pid, start) + snapshot + (1,))
        self.db.commit()
        self.frames = {key: value for key, value in self.frames.items()
                       if key[1] != pid or key == frame_key}
        self.frames[frame_key] = snapshot

    def peer_frame(self, native, process, address):
        """Resolve exactly one live roster member, scoped to this viewer's team."""
        parts = teammate_address(address)
        if not parts:
            return None
        name, team = parts
        roster = read_json_object(os.path.join(CLAUDE_DIR, "teams", team_directory(team), "config.json"))
        if not roster or roster.get("name", team) != team:
            return None
        members = roster.get("members")
        if not isinstance(members, list):
            return None
        matching = [m for m in members if isinstance(m, dict) and m.get("agentId") == address]
        if len(matching) != 1:
            return None
        member = matching[0]
        if member.get("name", name) != name or member.get("backendType") in ("in-process", "in_process"):
            return None
        viewer, _ = session_identity(native)
        if not viewer:
            return None
        if self.db is None:
            self.db = open_db()
        qualified = roster.get("leadSessionId") == viewer
        viewer_pid = process.get("pid") if isinstance(process, dict) else None
        viewer_record = live_session(viewer_pid, viewer, allow_missing=True)
        if not qualified and viewer_record:
            row = self.db.execute("SELECT agent_id,pane FROM peer_frames WHERE"
                                  " session_id=? AND pid=? AND proc_start=? AND team=? AND origin_verified=1",
                                  (viewer, viewer_pid, viewer_record["procStart"], team)).fetchone()
            if row:
                own_members = [m for m in members if isinstance(m, dict) and m.get("agentId") == row[0]]
                qualified = len(own_members) == 1 and pane_matches(own_members[0], row[1])
        candidates = []
        rows = self.db.execute("SELECT session_id,pid,proc_start,parent_session_id,pane,payload"
                               " FROM peer_frames WHERE team=? AND agent_id=? AND origin_verified=1",
                               (team, address))
        for sid, pid, start, parent, pane, payload in rows:
            # The running child names its parent directly. A roster can retain
            # the lead's previous session id after resume or /clear.
            if not qualified and parent != viewer:
                continue
            record = live_session(pid, sid, start, allow_missing=True)
            if not record or not pane_matches(member, pane):
                continue
            current_pane = tmux_pane(record.get("tmux"))
            if current_pane and pane and current_pane != pane:
                continue
            try:
                frame = json.loads(payload)
            except (ValueError, TypeError):
                continue
            if isinstance(frame, dict) and session_identity(frame)[0] == sid:
                candidates.append(frame)
        # Names are reused on respawn. Never select by newest timestamp when
        # two live processes claim the same current roster identity.
        return candidates[0] if len(candidates) == 1 else None


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


def read_json_object(path, limit=262144):
    try:
        with open(path) as stream:
            raw = stream.read(limit + 1)
        value = json.loads(raw) if len(raw) <= limit else None
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, TypeError):
        return None


def process_record(pid):
    try:
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            return None
        with open("/proc/%d/stat" % pid) as stream:
            raw = stream.read(65536)
        # The comm field may itself contain spaces and closing parentheses.
        fields = raw[raw.rfind(")") + 2:].split()
        return (fields[19], int(fields[1])) if fields[0] not in ("Z", "X") else None
    except (OSError, ValueError, IndexError, TypeError):
        return None


def process_start(pid):
    record = process_record(pid)
    return record[0] if record else None


def process_descends_from(origin_pid, claimed_pid, claimed_start):
    # origin_pid comes from SO_PEERCRED, or this one-shot renderer's own pid.
    # It is never supplied by the JSON payload or inherited environment.
    seen = set()
    pid = origin_pid
    for _ in range(16):
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0 or pid in seen:
            return False
        seen.add(pid)
        record = process_record(pid)
        if not record:
            return False
        if pid == claimed_pid:
            return record[0] == claimed_start
        pid = record[1]
    return False


def live_session(pid, session_id, expected_start=None, allow_missing=False):
    if (not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0
            or not isinstance(session_id, str) or not session_id):
        return None
    start = process_start(pid)
    if not start or expected_start is not None and start != expected_start:
        return None
    path = os.path.join(CLAUDE_DIR, "sessions", "%d.json" % pid)
    try:
        with open(path) as stream:
            raw = stream.read(65537)
        record = json.loads(raw) if len(raw) <= 65536 else None
    except FileNotFoundError:
        # External native teammates do not publish a sessions ledger. Only
        # ancestry-verified frames may be stored and later use this fallback.
        return {"pid": pid, "sessionId": session_id, "procStart": start} if allow_missing else None
    except (OSError, ValueError, TypeError):
        return None
    if (not isinstance(record, dict) or record.get("pid") != pid
            or record.get("sessionId") != session_id or record.get("procStart") != start):
        return None
    return record


def teammate_address(value):
    if not isinstance(value, str) or value.count("@") != 1:
        return None
    name, team = value.split("@")
    return (name, team) if TEAM_PART_RE.fullmatch(name) and TEAM_PART_RE.fullmatch(team) else None


def team_directory(team):
    # Native team directories normalize punctuation and case, while roster
    # names and teammate addresses keep their exact spelling.
    return re.sub(r"[^a-zA-Z0-9]", "-", team).lower()


def process_agent_identity(pid):
    # Read argv once per (pid, kernel start), never once per statusline client.
    # Keep only native teammate routing flags; prompt text is discarded.
    try:
        with open("/proc/%d/cmdline" % pid, "rb") as stream:
            raw = stream.read(1048577)
        if len(raw) > 1048576:
            return {}
        argv = raw.decode("utf-8", "replace").split("\0")[1:]
    except OSError:
        return {}
    flags = {"--agent-id": "agent_id", "--agent-name": "name",
             "--team-name": "team", "--parent-session-id": "parent_session_id"}
    found = {}
    at = 0
    while at < len(argv):
        arg = argv[at]
        if arg == "--":
            break
        flag, sep, value = arg.partition("=")
        if flag in flags:
            if not sep:
                at += 1
                if at >= len(argv) or argv[at].startswith("--"):
                    return {}
                value = argv[at]
            key = flags[flag]
            if not value or len(value) > 400 or key in found and found[key] != value:
                return {}
            found[key] = value
        at += 1
    address = teammate_address(found.get("agent_id"))
    if (not address or found.get("team") != address[1]
            or found.get("name", address[0]) != address[0]):
        return {}
    return found


def tmux_pane(value):
    if not isinstance(value, str):
        return ""
    pane = value.rsplit(".", 1)[-1]
    return pane if PANE_RE.fullmatch(pane) else ""


def pane_matches(member, pane):
    expected = member.get("tmuxPaneId")
    # A newly reserved address has no pane yet. It must not select the older
    # process that formerly held that name while the replacement starts.
    return bool(tmux_pane(expected) and tmux_pane(expected) == pane)


def same_model(left, right):
    # A native task's explicit window belongs to its request model. The API
    # response omits the request's context selector, but keeps the model id.
    if not isinstance(left, str) or not isinstance(right, str):
        return False
    strip = lambda value: re.sub(r"\[(?:1m|200k)\]$", "", value)
    return bool(left and right and strip(left) == strip(right))


def selected_input(data, cache, origin_pid=None):
    if not isinstance(data, dict):
        return {}
    native = data.get("statusline_input")
    if not isinstance(native, dict):
        return data
    if data.get("statusline_task_metadata") is True:
        cache.ingest_tasks(native)
        return None
    try:
        cache.register_frame(native, data.get("statusline_process"), origin_pid)
    except Exception:
        pass  # A peer registry problem must not suppress this process's line.
    view = data.get("statusline_view")
    try:
        view = json.loads(view) if isinstance(view, str) else view
    except (ValueError, TypeError):
        return native
    session_id, tp = session_identity(native)
    activation = data.get("statusline_activation")
    if (not isinstance(activation, str) or not ACTIVATION_RE.fullmatch(activation)
            or not isinstance(view, dict) or view.get("version") != 2
            or view.get("activation") != activation or not session_id
            or view.get("session_id") != session_id):
        return native
    agent_id = view.get("agent_id")
    if (not isinstance(agent_id, str)
            or not (AGENT_ID_RE.fullmatch(agent_id) or teammate_address(agent_id))):
        return native
    agent_path = (os.path.join(tp[:-6], "subagents", "agent-" + agent_id + ".jsonl")
                  if tp and tp.endswith(".jsonl") and AGENT_ID_RE.fullmatch(agent_id) else None)
    address = view.get("teammate_id") or agent_id
    if teammate_address(address) and (not agent_path or not os.path.isfile(agent_path)):
        try:
            peer = cache.peer_frame(native, data.get("statusline_process"), address)
        except Exception:
            peer = None
        return selected_peer_input(native, view, peer, cache)
    if not agent_path:
        return native
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


def selected_peer_input(native, view, peer, cache):
    # Peer metrics belong to that process's raw main frame even if its own UI
    # is viewing a subagent. The original session guard was checked above.
    selected = dict(peer or native)
    selected["_selected_agent_id"] = view["agent_id"]
    selected["_selected_agent_name"] = clean_label(view.get("name") or view.get("teammate_id"))
    selected["_selected_peer"] = True
    selected["rate_limits"] = peer.get("rate_limits", {}) if peer is not None else {}
    if peer is None:
        selected.update(transcript_path=None, model={}, effort={}, context_window={}, cost={})
        selected["_selected_missing"] = True
        selected["_cache_values"] = None
        return selected
    _, tp = session_identity(peer)
    values = cache.values(tp) if tp else None
    selected["_cache_values"] = values
    selected["_selected_missing"] = not tp or not os.path.isfile(tp)
    native_context = peer.get("context_window")
    context = dict(native_context) if isinstance(native_context, dict) else {}
    usage = context.get("current_usage")
    used = values[6] if values else context_tokens(usage) if isinstance(usage, dict) else None
    native_model = peer.get("model")
    native_model = native_model if isinstance(native_model, dict) else {}
    if values and not same_model(native_model.get("id"), values[7]):
        # An unfocused pane can stop refreshing its statusline frame while its
        # transcript grows or changes model. Never lend the older frame's
        # model, effort, or context limit to the latest complete response.
        selected["model"] = {"id": clean_label(values[7])}
        selected["effort"] = {}
        context.pop("context_window_size", None)
        context.pop("used_percentage", None)
    # total_input_tokens in native frames can be cumulative across turns.
    # Current usage or the latest complete transcript response supplies fill.
    if used is not None:
        context["total_input_tokens"] = used
        window = context.get("context_window_size")
        if positive_number(window):
            context["used_percentage"] = max(0, min(100, used / window * 100))
    else:
        context.pop("total_input_tokens", None)
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


def render_core(data, cache, origin_pid=None):
    # Returns (template, slots): the full line is fill(template, slots).
    # Holding the time-dependent fragments in slots lets the daemon serve pure
    # clock ticks by refilling a cached template instead of re-rendering.
    data = selected_input(data, cache, origin_pid)
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
            parts.append(muted("teammate unavailable" if get(["_selected_peer"]) and not get(["transcript_path"])
                               else "transcript unavailable"))

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
    # A selected peer may use a different account. Its native frame is the
    # quota authority; this daemon's token and shared credentials identify no
    # account for that peer.
    tok = None
    if not get(["_selected_peer"]):
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
        if ttl == "1h" or (agent_id and not get(["_selected_peer"]) and ttl == "5m"):
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


def render(data, cache, origin_pid=None):
    return fill(*render_core(data, cache, origin_pid))


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
    if cache.pstate_path and not g("_selected_peer"):
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
           g("_selected_missing"), g("_selected_peer"), g("_cache_values"))
    return tp, sig, tstat, ps_mt


def serve(data, cache, memo, origin_pid=None):
    # Internally event-driven: a pure clock tick (nothing changed but time)
    # splices a fresh countdown into the cached line; any real change re-renders
    # and refreshes the memo.
    data = selected_input(data, cache, origin_pid)
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


def socket_peer_pid(conn):
    import socket
    import struct
    try:
        pid, uid, _ = struct.unpack("3i", conn.getsockopt(
            socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
        return pid if pid > 0 and uid == os.getuid() else None
    except (AttributeError, OSError, ValueError, struct.error):
        return None


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
            origin_pid = socket_peer_pid(conn)
            raw = read_all(conn)
            try:
                data = json.loads(raw.decode("utf-8", "replace")) if raw.strip() else {}
            except Exception:
                data = {}
            try:
                out = serve(data, cache, memo, origin_pid=origin_pid)
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
    sys.stdout.write(render(data, Cache(persistent=True), origin_pid=os.getpid()))


if __name__ == "__main__":
    main()
