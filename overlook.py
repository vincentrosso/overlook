#!/usr/bin/env python3
"""Overlook — a local web dashboard for Claude Code background agents.

Status board for every background session, plus a searchable hub of every
agent definition on the machine, with one-click launch.

Zero dependencies: Python 3.9+ stdlib only. Binds to 127.0.0.1.

    python3 overlook.py            # http://overlook.localhost:7878
    python3 overlook.py --open     # ...and open the browser
"""
from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import json
import os
import pty
import re
import shutil
import signal
import struct
import subprocess
import sys
import termios
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

VERSION = "0.8.2"
HOME = Path.home()
WEB = Path(__file__).resolve().parent / "web"
ID_RE = re.compile(r"^[A-Za-z0-9_-]{4,64}$")
AGENT_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
PERMISSION_MODES = {"default", "acceptEdits", "auto", "plan", "bypassPermissions"}


MODELS = ["fable", "opus", "sonnet", "haiku"]
SETTINGS = Path(os.environ.get("OVERLOOK_SETTINGS", HOME / ".config" / "overlook" / "settings.json"))
_settings_lock = threading.Lock()


def load_settings() -> dict:
    d = read_json(SETTINGS) if SETTINGS.exists() else None
    d = d if isinstance(d, dict) else {}
    d.setdefault("agentModels", {})
    d.setdefault("autoYes", {})
    d.setdefault("names", {})   # session id -> display name set from the dashboard
    d.setdefault("colors", {})  # session id -> palette index override (click the dot to cycle)
    d.setdefault("slots", {})   # session id -> grid slot its terminal window was placed in
    d.setdefault("wrapped", {}) # session id -> epoch when /wrape was sent (Close is offered once it exits)
    return d


def save_settings(d: dict) -> None:
    SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    tmp = SETTINGS.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=2))
    tmp.replace(SETTINGS)


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# UI telemetry: the page batches every interaction (clicks, dead clicks, views, drawer open/close with
# dwell, shortcuts, toasts, API errors, terminal attach/detach) to /api/ui-log; one JSON line each.
# Read it with scripts/ui_report.py to see how the dashboard is actually used and where it hurts.
UI_LOG = Path(os.environ.get("OVERLOOK_UI_LOG", SETTINGS.parent / "ui-log.jsonl"))
_ui_lock = threading.Lock()


def ui_log(body: dict) -> tuple[int, dict]:
    evs = body.get("events")
    if not isinstance(evs, list):
        return 400, {"ok": False, "error": "need events[]"}
    sid, recv = str(body.get("sid") or "")[:12], round(time.time(), 3)
    rows = []
    for e in evs[:500]:
        if isinstance(e, dict):
            row = {k[:32]: (v if isinstance(v, (int, float, bool)) or v is None else str(v)[:300])
                   for k, v in list(e.items())[:24]}
            row.update(sid=sid, recv=recv)
            rows.append(json.dumps(row, ensure_ascii=False))
    with _ui_lock:
        UI_LOG.parent.mkdir(parents=True, exist_ok=True)
        if UI_LOG.exists() and UI_LOG.stat().st_size > 50_000_000:   # keep one rotated copy
            UI_LOG.replace(UI_LOG.with_suffix(".1.jsonl"))
        with UI_LOG.open("a", encoding="utf-8") as f:
            f.write("".join(r + "\n" for r in rows))
    return 200, {"ok": True, "n": len(rows)}


# ---------------------------------------------------------------- config dirs


def config_dirs() -> list[Path]:
    """Claude config dirs to read: $CLAUDE_CONFIG_DIR first, then ~/.claude."""
    out: list[Path] = []
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    for p in [env and Path(env).expanduser(), HOME / ".claude-home", HOME / ".claude"]:
        if p and p.is_dir() and p not in out:
            out.append(p)
    return out


def claude_bin() -> str | None:
    return os.environ.get("OVERLOOK_CLAUDE") or shutil.which("claude")


def claude_env(jid: str | None = None) -> dict:
    """Environment for a claude subprocess. With a job id, CLAUDE_CONFIG_DIR is
    the config dir that owns that job: several dirs can hold jobs/, and
    attach/logs/stop only see the one they are pointed at."""
    env = dict(os.environ)
    dirs = config_dirs()
    if jid:
        jd = find_job_dir(jid)
        if jd:
            env["CLAUDE_CONFIG_DIR"] = str(jd.parent.parent)
            return env
    # Background jobs live under whichever config dir has a jobs/ folder.
    if "CLAUDE_CONFIG_DIR" not in env:
        for d in dirs:
            if (d / "jobs").is_dir() and d.name != ".claude":
                env["CLAUDE_CONFIG_DIR"] = str(d)
                break
    return env


def run_claude(args: list[str], timeout: float = 20, cwd: str | None = None, jid: str | None = None) -> tuple[int, str]:
    exe = claude_bin()
    if not exe:
        return 127, "claude CLI not found on PATH (set OVERLOOK_CLAUDE)"
    try:
        p = subprocess.run([exe, *args], capture_output=True, text=True,
                           timeout=timeout, cwd=cwd, env=claude_env(jid))
        return p.returncode, (p.stdout + p.stderr)
    except subprocess.TimeoutExpired:
        return 124, "timed out"



# ------------------------------------------------------------------- progress

FRAC_RE = re.compile(r"(?<![\d/])(\d[\d,]*)\s*/\s*(\d[\d,]*)(?![\d/])")
PCT_RE = re.compile(r"(\d{1,3}(?:\.\d+)?)\s*%")
ETA_CLOCK_RE = re.compile(
    r"(?:ETA\s*[:~]?\s*~?\s*(\d{1,2}:\d{2})\s*(PT|PST|PDT|ET|UTC|Z)?)|"
    r"(?:~?(\d{1,2}:\d{2})\s*(PT|PST|PDT|ET|UTC|Z)?\s*ETA)", re.I)
ETA_DUR_RE = re.compile(r"(?:ETA|in|another|left|remaining)\s*~?\s*(\d+(?:\.\d+)?)\s*(h|hr|hrs|hours?|m|min|mins|minutes?)\b"
                        r"|~?(\d+(?:\.\d+)?)\s*(h|hr|hrs|hours?|m|min|mins|minutes?)\s*(?:left|remaining|to go)", re.I)
COUNT_RE = re.compile(r"(\d[\d,]*)\s*\(\+\s*(\d[\d,]*)\)")
TZS = {"PT": "America/Los_Angeles", "PST": "America/Los_Angeles", "PDT": "America/Los_Angeles",
       "ET": "America/New_York", "UTC": "UTC", "Z": "UTC"}


def _num(x: str) -> float:
    return float(x.replace(",", ""))


def _clock_to_epoch(hhmm: str, tz: str | None, ref: float) -> float | None:
    from datetime import datetime, timedelta
    try:
        from zoneinfo import ZoneInfo
        zone = ZoneInfo(TZS.get((tz or "").upper(), "")) if tz else None
    except Exception:
        zone = None
    h, m = map(int, hhmm.split(":"))
    if h > 23 or m > 59:
        return None
    base = datetime.fromtimestamp(ref, zone) if zone else datetime.fromtimestamp(ref)
    t = base.replace(hour=h, minute=m, second=0, microsecond=0)
    # Statuses mention the NEXT occurrence; tolerate up to 3h of "overdue".
    if t.timestamp() < ref - 3 * 3600:
        t += timedelta(days=1)
    return t.timestamp()


def _iso_epoch(iso: str | None) -> float | None:
    from datetime import datetime
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def parse_progress(text: str, said_at: float) -> dict:
    """Pull counters and ETAs out of a free-text status line."""
    out: dict = {}
    if not text:
        return out
    for m in FRAC_RE.finditer(text):
        num, den = _num(m.group(1)), _num(m.group(2))
        tail = text[m.end():m.end() + 1]
        # skip dates (11/06) and ratios that are not progress
        if den > 1 and 0 <= num <= den and not (den <= 31 and num <= 12 and tail in "/-"):
            out.update(num=num, den=den, pct=round(100 * num / den, 1))
            break
    if "pct" not in out:
        m = PCT_RE.search(text)
        if m and float(m.group(1)) <= 100:
            out["pct"] = float(m.group(1))
    m = COUNT_RE.search(text)
    if m:
        out.update(count=_num(m.group(1)), delta=_num(m.group(2)))
    m = ETA_CLOCK_RE.search(text)
    if m:
        hhmm, tz = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
        at = _clock_to_epoch(hhmm, tz, said_at)
        if at:
            out.update(etaAt=at, etaText=m.group(0).strip(), etaSource="stated")
    else:
        m = ETA_DUR_RE.search(text)
        if m:
            n, unit = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
            secs = float(n) * (3600 if unit.lower().startswith("h") else 60)
            out.update(etaAt=said_at + secs, etaText=m.group(0).strip(), etaSource="stated")
    return out


def estimate_rate(entries: list[tuple[float, str]], den: float) -> float | None:
    """Items/sec from timeline samples that report the same denominator."""
    pts = []
    for at, text in entries:
        p = parse_progress(text, at)
        if p.get("den") == den and "num" in p:
            pts.append((at, p["num"]))
    if len(pts) < 2:
        return None
    pts.sort()
    (t0, n0), (t1, n1) = pts[max(0, len(pts) - 8)], pts[-1]
    if t1 - t0 < 60 or n1 <= n0:
        return None
    return (n1 - n0) / (t1 - t0)


def progress_for(job_dir: Path | None, detail: str, updated_iso: str | None) -> dict:
    said = _iso_epoch(updated_iso) or time.time()
    p = parse_progress(detail, said)
    if not p:
        return p
    p["saidAt"] = said
    if "den" in p and job_dir and (job_dir / "timeline.jsonl").exists():
        entries = []
        for ln in (job_dir / "timeline.jsonl").read_text(errors="replace").splitlines()[-300:]:
            try:
                e = json.loads(ln)
            except json.JSONDecodeError:
                continue
            at = _iso_epoch(e.get("at"))
            if at and e.get("detail"):
                entries.append((at, e["detail"]))
        rate = estimate_rate(entries, p["den"])
        if rate:
            p["ratePerMin"] = round(rate * 60, 2)
            if "etaAt" not in p:
                p.update(etaAt=said + (p["den"] - p["num"]) / rate, etaSource="estimated")
    return p

# ------------------------------------------------------------------- sessions

_live_cache: dict = {"at": 0.0, "data": {}}
_live_lock = threading.Lock()


def live_agents() -> dict:
    """`claude agents --json --all`, cached 10s (it spawns node)."""
    with _live_lock:
        if time.time() - _live_cache["at"] < 10:
            return _live_cache["data"]
        code, out = run_claude(["agents", "--json", "--all"], timeout=15)
        data = {}
        if code == 0:
            try:
                start = out.index("[")
                for row in json.loads(out[start:]):
                    data[row.get("id")] = row
            except (ValueError, json.JSONDecodeError):
                pass
        _live_cache.update(at=time.time(), data=data)
        return data


def read_json(p: Path):
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def pid_alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError, TypeError):
        return False


def registry() -> list[dict]:
    """Every live Claude Code process registers in <config>/sessions/<pid>.json,
    interactive terminals included. Keep only entries whose pid still exists."""
    out = []
    for cdir in config_dirs():
        sd = cdir / "sessions"
        if not sd.is_dir():
            continue
        for f in sd.glob("*.json"):
            r = read_json(f)
            if isinstance(r, dict) and r.get("sessionId") and pid_alive(r.get("pid")):
                r["_config"] = str(cdir)
                out.append(r)
    return out


_tx_cache: dict = {}


_CD_RE = re.compile(r"(?:^|&&|;|\|\|)\s*cd\s+(?:--\s+)?['\"]?(/[^\s'\";&|]+)")


def tool_dir(inp: dict) -> str | None:
    """Directory a tool call touched: file_path/path/cwd of Read/Edit/Write/Glob/Grep, or the
    last `cd /abs/path` in a Bash command. Absolute paths only; relative ones say nothing new."""
    for k in ("cwd", "file_path", "path", "notebook_path"):
        v = inp.get(k)
        if isinstance(v, str) and v.startswith("/"):
            return v if (k == "cwd" or k == "path") else str(Path(v).parent)
    cmd = inp.get("command")
    if isinstance(cmd, str):
        hits = _CD_RE.findall(cmd)
        if hits:
            return hits[-1].rstrip("/")
    return None


def project_of(path: str | None) -> str | None:
    """Project folder for a path: the child of a project root it sits under, else the nearest git
    root, else the path itself. Returned as the full directory."""
    if not path:
        return None
    for r in os.environ.get("OVERLOOK_ROOTS", str(HOME / "Dropbox" / "development")).split(":"):
        rp = str(Path(r).expanduser()).rstrip("/") + "/"
        if path.startswith(rp):
            rest = path[len(rp):].split("/")
            if rest and rest[0]:
                return rp + rest[0]
    p = Path(path)
    for q in [p, *p.parents]:
        if (q / ".git").exists():
            return str(q)
    return path


def transcript_info(config: str, session_id: str) -> dict:
    """Title, latest ask, last reply and activity from a session transcript.
    Reads only the tail; cached on mtime."""
    hits = list(Path(config, "projects").glob(f"*/{session_id}.jsonl"))
    if not hits:
        return {}
    f = hits[0]
    mtime = f.stat().st_mtime
    c = _tx_cache.get(str(f))
    if c and c[0] == mtime:
        return c[1]
    info: dict = {"mtime": mtime}
    work_at = None
    with open(f, "rb") as fh:
        size = fh.seek(0, 2)
        fh.seek(max(0, size - 1_500_000))
        lines = fh.read().decode("utf-8", "replace").splitlines()[1:] if size > 1_500_000 else \
            (fh.seek(0) or fh.read().decode("utf-8", "replace").splitlines())
    for ln in lines:
        try:
            r = json.loads(ln)
        except json.JSONDecodeError:
            continue
        t = r.get("type")
        if t == "ai-title" and r.get("aiTitle"):
            info["title"] = r["aiTitle"]
        elif t == "custom-title" and r.get("customTitle"):
            info["custom"] = r["customTitle"]
        elif t == "last-prompt" and r.get("lastPrompt"):
            info["prompt"] = r["lastPrompt"]
        elif t == "permission-mode":
            info["mode"] = r.get("permissionMode")
        elif t == "assistant":
            msg = r.get("message") or {}
            info["stop"] = msg.get("stop_reason")
            info["at"] = r.get("timestamp")
            text = next((x.get("text") for x in msg.get("content") or []
                         if isinstance(x, dict) and x.get("type") == "text" and x.get("text")), None)
            if text:
                info["reply"] = " ".join(text.split())[:300]
            for x in msg.get("content") or []:          # the dir a session is REALLY in = its latest tool path
                if isinstance(x, dict) and x.get("type") == "tool_use":
                    d = tool_dir(x.get("input") or {})
                    if d:
                        info["work"], work_at = d, r.get("timestamp")
            u = msg.get("usage") or {}
            info["tokens"] = sum(u.get(k) or 0 for k in ("input_tokens", "output_tokens",
                                 "cache_read_input_tokens", "cache_creation_input_tokens"))
        elif t == "user":
            info["stop"] = None  # a newer user turn means the model owes a reply
    _tx_cache[str(f)] = (mtime, info)
    return info


def sessions() -> list[dict]:
    live = live_agents()
    seen: dict[str, dict] = {}
    for cdir in config_dirs():
        jobs = cdir / "jobs"
        if not jobs.is_dir():
            continue
        for jd in jobs.iterdir():
            st = read_json(jd / "state.json")
            if not isinstance(st, dict):
                continue
            jid = st.get("daemonShort") or jd.name
            row = live.get(jid, {})
            seen[jid] = {
                "id": jid, "kind": "background",
                "name": st.get("name") or row.get("name") or jid,
                "state": st.get("state") or row.get("state") or "unknown",
                "status": row.get("status") or st.get("tempo"),
                "detail": st.get("detail") or "",
                "intent": st.get("intent") or "",
                "output": st.get("output"),
                "tokens": st.get("tokens") or 0,
                "cwd": st.get("cwd") or row.get("cwd") or "",
                "agent": (st.get("respawnFlags") or [None, None])[1]
                         if (st.get("respawnFlags") or [None])[0] == "--agent" else st.get("template"),
                "pid": row.get("pid"),
                "alive": bool(row.get("pid")),
                "createdAt": st.get("createdAt"),
                "updatedAt": st.get("updatedAt"),
                "cliVersion": st.get("cliVersion"),
                "sessionId": st.get("sessionId"),
                "children": st.get("children"),
                "dir": str(jd),
            }
            if st.get("sessionId"):
                tx = transcript_info(str(cdir), st["sessionId"])
                seen[jid]["work"] = project_of(tx.get("work")) or seen[jid]["cwd"]
    # Live processes with no readable job: interactive terminals, plus jobs
    # that have not written state.json yet.
    by_sid = {v.get("sessionId"): v for v in seen.values()}
    for r in registry():
        sid = r["sessionId"]
        if sid in by_sid:
            by_sid[sid].update(pid=r["pid"], alive=True)
            continue
        jid = sid[:8]
        tx = transcript_info(r["_config"], sid)
        recent = tx.get("mtime") and time.time() - tx["mtime"] < 90
        state = "working" if (recent and tx.get("stop") != "end_turn") else (
            "waiting" if tx.get("stop") == "end_turn" else ("working" if recent else "idle"))
        updated = _ms_iso((tx.get("mtime") or 0) * 1000) or _ms_iso(r.get("startedAt"))
        seen[jid] = {
            "id": jid, "kind": "background" if find_job_dir(jid) else "interactive",
            "name": tx.get("custom") or tx.get("title") or Path(r.get("cwd", "")).name or jid,
            "state": state, "status": None,
            "detail": tx.get("reply") or "", "intent": tx.get("prompt") or "",
            "output": None, "tokens": tx.get("tokens") or 0, "cwd": r.get("cwd", ""),
            "agent": r.get("agent") or "claude", "pid": r.get("pid"), "alive": True,
            "createdAt": _ms_iso(r.get("startedAt")), "updatedAt": updated,
            "cliVersion": r.get("version"), "sessionId": sid, "mode": tx.get("mode"),
            "dir": None, "work": project_of(tx.get("work")) or r.get("cwd", ""),
        }
    # Sessions the CLI knows about that have no job folder we can read.
    for jid, row in live.items():
        if jid and jid not in seen:
            seen[jid] = {
                "id": jid, "kind": "background", "name": row.get("name") or jid, "state": row.get("state", "unknown"),
                "status": row.get("status"), "detail": "", "intent": "", "tokens": 0,
                "cwd": row.get("cwd", ""), "agent": None, "pid": row.get("pid"),
                "alive": bool(row.get("pid")),
                "createdAt": _ms_iso(row.get("startedAt")), "updatedAt": _ms_iso(row.get("startedAt")),
                "sessionId": row.get("sessionId"), "dir": None,
            }
    st_ = load_settings()
    auto, names = st_.get("autoYes", {}), st_.get("names", {})
    for v in seen.values():
        v["autoYes"] = bool(auto.get(v["id"]))
        if names.get(v["id"]):
            v["name"], v["renamed"] = names[v["id"]], True
        v["color"] = session_color(v["id"], st_)
        v["wrapped"] = bool(st_.get("wrapped", {}).get(v["id"]))
        v["finished"] = is_finished(v)
        v["closing"] = v["id"] in _pending_close
        v["term_line"] = _term_lines.get(v["id"], "") if v.get("alive") else ""
        v.setdefault("work", v.get("cwd", ""))
        try:
            v["progress"] = progress_for(Path(v["dir"]) if v.get("dir") else None,
                                         v.get("detail") or "", v.get("updatedAt"))
        except Exception as e:  # a weird status line must never break the board
            log(f"progress parse failed for {v.get('id')}: {e!r}")
            v["progress"] = {}
    return sorted(seen.values(), key=lambda s: s.get("updatedAt") or "", reverse=True)


def _ms_iso(ms) -> str | None:
    if not ms:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(ms / 1000))


def find_job_dir(jid: str) -> Path | None:
    for cdir in config_dirs():
        p = cdir / "jobs" / jid
        if p.is_dir():
            return p
    return None


def timeline(jid: str, limit: int = 200) -> list[dict]:
    jd = find_job_dir(jid)
    if not jd or not (jd / "timeline.jsonl").exists():
        return []
    lines = (jd / "timeline.jsonl").read_text(errors="replace").splitlines()[-limit:]
    out = []
    for ln in lines:
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            pass
    return out


# --------------------------------------------------------------------- agents

BUILTINS = [
    ("claude", "Default Claude Code agent: general coding, all tools."),
    ("general-purpose", "Research complex questions, search code, run multi-step tasks."),
    ("Explore", "Read-only fan-out search across a codebase; returns conclusions."),
    ("Plan", "Software architect: designs step-by-step implementation plans."),
]


def parse_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end < 0:
        return {}, text
    head, body = text[3:end], text[end + 4:].lstrip("\n")
    meta: dict = {}
    lines = head.splitlines()
    i = 0
    while i < len(lines):
        m = re.match(r"^([A-Za-z_][\w-]*):\s?(.*)$", lines[i])
        i += 1
        if not m:
            continue
        key, val = m.group(1), m.group(2).strip()
        if val in ("|", ">", "|-", ">-", ""):
            block = []
            while i < len(lines) and (lines[i].startswith((" ", "\t")) or not lines[i].strip()):
                block.append(lines[i].strip())
                i += 1
            if block and all(b.startswith("- ") or not b for b in block):
                meta[key] = [b[2:].strip() for b in block if b]
                continue
            val = (" " if val.startswith(">") else "\n").join(block).strip()
        elif val.startswith('"'):
            try:
                val = json.loads(val)
            except json.JSONDecodeError:
                val = val.strip('"')
        elif val.startswith("'"):
            val = val.strip("'")
        elif val.startswith("["):
            val = [v.strip().strip("'\"") for v in val.strip("[]").split(",") if v.strip()]
        meta[key] = val
    return meta, body


def _plugin_name(agent_file: Path) -> str | None:
    root = agent_file.parent.parent
    pj = read_json(root / ".claude-plugin" / "plugin.json")
    if isinstance(pj, dict) and pj.get("name"):
        return pj["name"]
    return root.name.split("~")[0] or None


def _tools(meta: dict) -> list[str]:
    t = meta.get("tools")
    if isinstance(t, str):
        t = [x.strip() for x in t.split(",") if x.strip()]
    return t or []


def agent_dirs(extra_cwds: list[str]) -> list[tuple[Path, str]]:
    dirs: list[tuple[Path, str]] = []
    for cdir in config_dirs():
        dirs.append((cdir / "agents", "user"))
    for cwd in extra_cwds:
        dirs.append((Path(cwd) / ".claude" / "agents", "project"))
    return dirs


def agents() -> list[dict]:
    out: dict[str, dict] = {}
    for name, desc in BUILTINS:
        out[name] = {"id": name, "name": name, "description": desc, "source": "built-in",
                     "model": "inherit", "tools": ["*"], "color": None, "body": "", "path": None}

    def add(f: Path, source: str, plugin: str | None = None, project: str | None = None):
        try:
            meta, body = parse_frontmatter(f.read_text(errors="replace"))
        except OSError:
            return
        name = str(meta.get("name") or f.stem)
        qid = f"{plugin}:{name}" if plugin else name
        prev = out.get(qid)
        mtime = f.stat().st_mtime
        if prev and prev.get("_mtime", 0) >= mtime:
            return
        desc = str(meta.get("description") or "").replace("\\n", "\n")
        out[qid] = {
            "id": qid, "name": name, "description": desc,
            "summary": re.split(r"\n|<example>", desc)[0].strip(),
            "source": source, "plugin": plugin, "project": project,
            "model": meta.get("model") or "inherit", "color": meta.get("color"),
            "tools": _tools(meta), "maxTurns": meta.get("maxTurns"),
            "body": body, "path": str(f), "_mtime": mtime,
        }

    cwds = sorted({s["cwd"] for s in sessions() if s.get("cwd")} | set(project_roots()))
    for d, source in agent_dirs(cwds):
        if d.is_dir():
            for f in sorted(d.glob("*.md")):
                add(f, source, project=str(d.parent.parent) if source == "project" else None)
    for cdir in config_dirs():
        pdir = cdir / "plugins"
        if pdir.is_dir():
            for f in pdir.glob("**/agents/*.md"):
                add(f, "plugin", plugin=_plugin_name(f))
    overrides = load_settings()["agentModels"]
    for a in out.values():
        a.pop("_mtime", None)
        a["modelOverride"] = overrides.get(a["id"])
        a.setdefault("summary", a["description"])
    order = {"user": 0, "project": 1, "built-in": 2, "plugin": 3}
    return sorted(out.values(), key=lambda a: (order.get(a["source"], 9), a["id"].lower()))


def project_roots() -> list[str]:
    """Directories offered in the launch picker: $OVERLOOK_ROOTS children."""
    roots = os.environ.get("OVERLOOK_ROOTS", str(HOME / "Dropbox" / "development"))
    out = []
    for r in roots.split(":"):
        rp = Path(r).expanduser()
        if rp.is_dir():
            out += [str(c) for c in sorted(rp.iterdir())
                    if c.is_dir() and not c.name.startswith(".") and (c / ".git").exists()]
    return out


def dirs() -> list[str]:
    recent = []
    for s in sessions():
        c = s.get("cwd")
        if c and c not in recent and Path(c).is_dir():
            recent.append(c)
    return recent + [d for d in project_roots() if d not in recent]


# --------------------------------------------------------------------- launch


def launch(body: dict) -> tuple[int, dict]:
    agent = str(body.get("agent") or "claude")
    cwd = str(body.get("cwd") or "")
    prompt = str(body.get("prompt") or "").strip()
    name = str(body.get("name") or "").strip()[:80]
    mode = str(body.get("permissionMode") or "auto")
    model = str(body.get("model") or "").strip() or load_settings()["agentModels"].get(agent, "")
    if not AGENT_RE.match(agent):
        return 400, {"error": "bad agent name"}
    if not prompt:
        return 400, {"error": "prompt is required"}
    if not cwd or not Path(cwd).expanduser().is_dir():
        return 400, {"error": f"not a directory: {cwd}"}
    if mode not in PERMISSION_MODES:
        return 400, {"error": "bad permission mode"}
    args = ["--bg", "--agent", agent, "--permission-mode", mode]
    if name:
        args += ["--name", name]
    if model:
        if not AGENT_RE.match(model):
            return 400, {"error": "bad model"}
        args += ["--model", model]
    args += ["--", prompt]
    log(f"launch agent={agent} cwd={cwd} mode={mode}")
    code, out = run_claude(args, timeout=60, cwd=str(Path(cwd).expanduser()))
    _live_cache["at"] = 0
    m = re.search(r"\b([0-9a-f]{8})\b", out) if code == 0 else None
    return (200 if code == 0 else 500), {"ok": code == 0, "id": m.group(1) if m else None,
                                          "output": out.strip()[-2000:]}



# ------------------------------------------------------------------ yes / go
# There is no `claude send`; `claude attach` opens the live terminal of a
# background session, so we drive it in a pseudo-terminal: read the screen
# with `claude logs`, press the right keys, then Ctrl+Z to detach (the session
# keeps running).

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07|\x1b[()][A-Z0-9]")
YES_TEXT = os.environ.get("OVERLOOK_YES_TEXT", "yes, go ahead")
# Typed by the Wrap button. /wrape = /wrap (update CLAUDE.md, notes, memory) then stop the job itself,
# so the exit happens AFTER the wrap report is written rather than racing it from here.
WRAP_TEXT = os.environ.get("OVERLOOK_WRAP_TEXT", "/wrape")
_poke_locks: dict[str, threading.Lock] = {}
_poke_log: dict[str, list[float]] = {}


def screen_text(jid: str) -> str:
    code, out = run_claude(["logs", jid], timeout=20, jid=jid)
    t = ANSI_RE.sub("\n", out)
    lines = [" ".join(l.split()) for l in t.splitlines() if l.strip()]
    return "\n".join(lines[-60:])


# One live line per background session for its card: the spinner/status row of its
# screen ("✻ Thinking… (12s · esc to interrupt)", "⏺ Bash(…)"), else the last meaningful
# line above the input box. Polled by one thread, sequentially, only while a browser is
# actually looking (`claude logs` spawns node, ~0.8s each; several jobs share this laptop).
_term_lines: dict[str, str] = {}
_term_backoff: dict[str, float] = {}
_term_viewed = {"at": 0.0}
TERM_SGR_RE = re.compile(r"\x1b\[[0-9;]*m")
TERM_COL_RE = re.compile(r"\x1b\[[0-9;]*[CG]")
TERM_HINT_RE = re.compile(r"for shortcuts|bypass permissions|shift\+tab|ctrl\+|esc to cancel", re.I)
TERM_WORD_RE = re.compile(r"[A-Za-z]{2}")
SPINNERS = "✻✽✶✳✢·∗⏺"


def _whole_row(l: str) -> bool:
    """A diff redraw repaints only the TAIL of the status row after its timer ticks ("· thinking)",
    "ought for 24s)"); the whole row starts "<glyph> Capitalised…" and has balanced parentheses."""
    return l.count("(") == l.count(")") and (l[0] not in SPINNERS or (l[1:2] == " " and l[2:3].isupper()))


def term_line(jid: str) -> str:
    # `claude logs` is a raw pty replay drawn as DIFFS: every redraw starts with cursor-home
    # (ESC[H), and a spinner tick rewrites one digit, so the last 60 cleaned lines are mostly
    # "7", "8", "9". Colours are dropped, column moves become spaces (a word-by-word rewrite
    # of one row reads back as that row), other cursor moves become line breaks, noise
    # fragments go, then frames are walked newest-first for a spinner/status row.
    code, out = run_claude(["logs", jid], timeout=20, jid=jid)
    if code:
        raise RuntimeError(out.strip()[:120] or f"exit {code}")
    lines: list[str] = []
    for frame in reversed(re.split(r"\x1b\[(?:1;1)?H", out)):
        t = ANSI_RE.sub("\n", TERM_COL_RE.sub(" ", TERM_SGR_RE.sub("", frame))).replace("\r", "\n")
        ls = [" ".join(l.split()) for l in t.splitlines() if len(l.strip()) >= 4 and TERM_WORD_RE.search(l)]
        for l in reversed(ls):
            if len(l) >= 10 and ("esc to interrupt" in l or l[0] in SPINNERS) and _whole_row(l):
                return l
        lines = ls + lines
        if len(lines) >= 40:
            break
    for l in reversed(lines):                      # no spinner up: last real line of output
        if l[0] not in "─│╭╮╰╯❯>" and not TERM_HINT_RE.search(l) and _whole_row(l):
            return l
    return ""


def term_line_loop():
    while True:
        time.sleep(3)
        if time.time() - _term_viewed["at"] > 15:   # no open dashboard: don't spend the CPU
            continue
        try:
            for s_ in sessions():
                jid = s_["id"]
                if not s_.get("alive") or s_.get("kind") != "background":
                    _term_lines.pop(jid, None)
                    continue
                if _term_backoff.get(jid, 0) > time.time():
                    continue
                try:
                    line = term_line(jid)
                    if line:
                        _term_lines[jid] = line
                except Exception as e:
                    _term_backoff[jid] = time.time() + 30
                    log(f"term line {jid}: {e!r}")
        except Exception as e:
            log(f"term line loop error: {e!r}")


def yes_plan(screen: str) -> tuple[str, bytes]:
    tail = screen[-2500:].lower()
    if "don't ask again" in tail or "don\u2019t ask again" in tail:
        return "permission", b"\x1b[B\r"         # option 2: yes, and don't ask again
    if "enter to select" in tail or "enter to confirm" in tail:
        return "menu", b"\r"                     # first (highlighted) option
    return "text", YES_TEXT.encode() + b"\r"


def poke(jid: str, reason: str = "button", text: str | None = None) -> dict:
    """Attach to a background job's screen over a pty and type into it.

    Default: answer whatever prompt is up (see yes_plan). With `text`, type that
    command instead -- refused while a permission/menu prompt is up, since the
    keystrokes would land in the picker rather than the input box.
    """
    import pty as _pty, select, signal
    if not find_job_dir(jid):
        return {"ok": False, "error": "only background sessions can be answered (terminal sessions can't be attached)"}
    lock = _poke_locks.setdefault(jid, threading.Lock())
    if not lock.acquire(blocking=False):
        return {"ok": False, "error": "already answering this session"}
    try:
        kind, keys = yes_plan(screen_text(jid))
        if text is not None:
            if kind != "text":
                return {"ok": False, "error": f"a {kind} prompt is up -- answer it first (Yes / Go), then wrap"}
            # Trailing space closes the slash-command picker so Enter submits the
            # typed text rather than whichever suggestion is highlighted.
            kind, keys = "command", (text.rstrip() + " ").encode() + b"\r"
        exe = claude_bin()
        master, slave = _pty.openpty()
        import fcntl, struct, termios
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 200, 0, 0))
        env = claude_env(jid); env["TERM"] = "xterm-256color"
        proc = subprocess.Popen([exe, "attach", jid], stdin=slave, stdout=slave, stderr=slave,
                                env=env, start_new_session=True, close_fds=True)
        os.close(slave)

        def drain(sec):
            end, n = time.time() + sec, 0
            while time.time() < end:
                r, _, _ = select.select([master], [], [], 0.2)
                if r:
                    try:
                        n += len(os.read(master, 65536))
                    except OSError:
                        break
            return n
        rendered = drain(4)
        # Text and Enter as separate writes: a single burst reads as a paste
        # and the Enter lands inside the input instead of submitting it.
        body, enter = (keys[:-1], keys[-1:]) if keys.endswith(b"\r") else (keys, b"")
        if body:
            os.write(master, body)
            drain(0.6)
        if enter:
            os.write(master, enter)
        drain(2.5)
        os.write(master, b"\x1a")
        drain(1)
        for stop in (proc.terminate, proc.kill):
            if proc.poll() is not None:
                break
            try:
                stop()
                proc.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                pass
        os.close(master)
        _poke_log.setdefault(jid, []).append(time.time())
        log(f"{'wrap' if text else 'yes'} -> {jid} ({kind}, {reason}, rendered {rendered}B)")
        _live_cache["at"] = 0
        return {"ok": rendered > 0, "kind": kind}
    finally:
        lock.release()


def mark_wrapped(jid: str) -> None:
    with _settings_lock:
        st = load_settings()
        st["wrapped"][jid] = time.time()
        save_settings(st)


FINISHED_RE = re.compile(r"done|complete|finish|success|idle|stop|exit|fail|error|crash|kill", re.I)
BUSY_RE = re.compile(r"work|run|active|start|input|block|wait|question|permission", re.I)


def is_finished(s: dict) -> bool:
    """A card is 'dead' when its session has exited OR is a background job sitting in done/idle/
    failed: the daemon stays alive after the work ends, so `alive` alone never clears the board."""
    if s.get("kind") == "interactive":
        return False
    if not s.get("alive"):
        return True
    st = s.get("state") or ""
    return bool(FINISHED_RE.search(st)) and not BUSY_RE.search(st)


_pending_close: dict[str, float] = {}   # session id -> epoch Close was asked; rm'd by pending_close_loop once it exits


def _forget(jid: str) -> None:
    with _settings_lock:
        st = load_settings()
        for k in ("names", "colors", "slots", "wrapped", "autoYes"):
            st[k].pop(jid, None)
        save_settings(st)


def _rm(jid: str) -> tuple[int, dict]:
    """`claude rm`: delete the session and its worktree when safe, then forget what Overlook kept."""
    code, out = run_claude(["rm", jid], timeout=60, jid=jid)
    out = out.strip()
    if code != 0:
        log(f"close {jid} failed: {out[-300:]}")
        return 409, {"ok": False, "error": out[-600:] or f"claude rm exited {code}"}
    _forget(jid)
    _pending_close.pop(jid, None)
    _live_cache["at"] = 0
    log(f"close {jid}")
    return 200, {"ok": True, "output": out}


def _wrapped_recently(jid: str, within: float = 1800) -> bool:
    return time.time() - (load_settings().get("wrapped", {}).get(jid) or 0) < within


def close_session(jid: str) -> tuple[int, dict]:
    """Anything that closes a session wraps it first. Dead already: `claude rm` now. Alive: type
    /wrape (unless it was wrapped in the last 30 min), remember the id, and pending_close_loop
    rm's it once the daemon has exited (/wrape's last step is `claude stop` on itself)."""
    s_ = next((x for x in sessions() if x["id"] == jid), None)
    if not s_ or not s_.get("alive"):
        return _rm(jid)
    if s_.get("kind") == "interactive":
        return 409, {"ok": False, "error": "a terminal session — wrap and exit it from its own window"}
    if jid in _pending_close:
        return 202, {"ok": True, "pending": "wrapping", "note": "already closing"}
    if not _wrapped_recently(jid):
        res = poke(jid, "close", text=WRAP_TEXT)
        if not res.get("ok"):
            return 409, {"ok": False, "error": res.get("error") or "couldn't type /wrape into it"}
        mark_wrapped(jid)
    _pending_close[jid] = time.time()
    log(f"close {jid}: wrapping first, rm when it exits")
    return 202, {"ok": True, "pending": "wrapping"}


def stop_session(jid: str, force: bool = False) -> tuple[int, dict]:
    """Stop wraps first too: /wrape ends with `claude stop` on itself. `force` is the old raw stop,
    for a session that is hung or ignoring its input."""
    s_ = next((x for x in sessions() if x["id"] == jid), None)
    if force or not s_ or not s_.get("alive") or _wrapped_recently(jid, 600):
        log(f"stop {jid}{' (force)' if force else ''}")
        code, out = run_claude(["stop", jid], timeout=30, jid=jid)
        _live_cache["at"] = 0
        return (200 if code == 0 else 500), {"ok": code == 0, "output": out.strip()}
    res = poke(jid, "stop", text=WRAP_TEXT)
    if not res.get("ok"):
        return 409, {"ok": False, "error": res.get("error") or "couldn't type /wrape into it — Stop again with force"}
    mark_wrapped(jid)
    log(f"stop {jid}: wrapping first")
    return 202, {"ok": True, "pending": "wrapping"}


def pending_close_loop():
    """rm each pending session once its daemon is gone. A session that is still alive after 20 min
    gets a plain stop, then rm (the wrap hung or was ignored); the Close is never just forgotten."""
    while True:
        time.sleep(5)
        if not _pending_close:
            continue
        try:
            _live_cache["at"] = 0
            live = {s["id"]: s for s in sessions()}
            for jid, since in list(_pending_close.items()):
                s_ = live.get(jid)
                if s_ and s_.get("alive") and time.time() - since < 1200:
                    continue
                if s_ and s_.get("alive"):
                    log(f"close {jid}: still alive after 20 min — stopping")
                    run_claude(["stop", jid], timeout=30, jid=jid)
                _rm(jid)
                _pending_close.pop(jid, None)      # a failed rm is logged; don't retry every 5s
        except Exception as e:
            log(f"pending close: {e!r}")


def auto_yes_loop():
    while True:
        time.sleep(20)
        try:
            on = {k for k, v in load_settings().get("autoYes", {}).items() if v}
            if not on:
                continue
            for s_ in sessions():
                jid = s_["id"]
                if jid not in on or not s_.get("alive") or s_.get("kind") != "background":
                    continue
                if not re.search(r"block|input|wait|question|permission", (s_.get("state") or "").lower()):
                    continue
                if time.time() - (_iso_epoch(s_.get("updatedAt")) or 0) < 20:
                    continue
                recent = [t for t in _poke_log.get(jid, []) if time.time() - t < 3600]
                if (recent and time.time() - recent[-1] < 90) or len(recent) >= 10:
                    continue
                threading.Thread(target=poke, args=(jid, "auto"), daemon=True).start()
        except Exception as e:
            log(f"auto-yes loop error: {e!r}")


# ---------------------------------------------------------- open (one click)


# One colour per session, the same on its card, its Terminal tab and the real
# terminal window ↗ App opens, so you can tell the windows apart at a glance.
PALETTE = ["#3fb4f0", "#f2b84b", "#6cd47a", "#e36ba0", "#b48cf2", "#f08a5d", "#4fd1c5", "#d9c54b"]


# Ghostty on macOS paints its title bar in the terminal's background colour (already the session
# tint) and has no separate title-bar colour, so the title itself carries a chip in the palette colour.
CHIPS = ["🔵", "🟧", "🟢", "🩷", "🟣", "🟠", "🩵", "🟡"]


def session_color(jid: str, st: dict | None = None) -> str:
    st = st or load_settings()
    i = st.get("colors", {}).get(jid)
    if not isinstance(i, int):
        i = int(hashlib.md5(jid.encode()).hexdigest(), 16) % len(PALETTE)
    return PALETTE[i % len(PALETTE)]


def tint(hex_color: str, base: str = "#0b0d11", amount: float = 0.16) -> str:
    """Blend a little of the session colour into the terminal's dark background."""
    c = [int(hex_color[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(base[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(bb + (cc - bb) * amount):02x}" for cc, bb in zip(c, b))


_screen_cache: dict = {"at": 0.0, "frame": None}


def screen_frame() -> tuple[int, int, int, int] | None:
    """(x, y, w, h) of the main screen's visible area in points, top-left origin. Cached 60s."""
    if time.time() - _screen_cache["at"] < 60:
        return _screen_cache["frame"]
    frame = None
    try:
        js = ('ObjC.import("AppKit"); const f=$.NSScreen.mainScreen.visibleFrame, s=$.NSScreen.mainScreen.frame;'
              'JSON.stringify([f.origin.x, s.size.height-f.origin.y-f.size.height, f.size.width, f.size.height])')
        out = subprocess.run(["osascript", "-l", "JavaScript", "-e", js], capture_output=True, text=True, timeout=5).stdout
        x, y, w, h = json.loads(out.strip())
        frame = (int(x), int(y), int(w), int(h))
    except Exception as e:
        log(f"screen frame: {e!r}")
    _screen_cache.update(at=time.time(), frame=frame)
    return frame


_layout: dict = {"cols": 0, "ids": [], "at": 0.0}   # the dashboard's card grid, POSTed by the page whenever it changes


def set_layout(body: dict, tile: bool = True) -> tuple[int, dict]:
    """The page reports its card grid: column count and the visible session ids in card order.
    Terminal windows take the same cell their card has; a changed order re-tiles the open ones."""
    ids = [str(i) for i in (body.get("ids") or []) if isinstance(i, str) and ID_RE.match(i)][:64]
    cols = int(body.get("cols") or 0)
    if not ids or cols < 1:
        return 400, {"ok": False, "error": "need cols and ids"}
    changed = (ids, cols) != (_layout["ids"], _layout["cols"])
    _layout.update(ids=ids, cols=cols, at=time.time())
    if tile and changed and _term_windows:
        threading.Thread(target=retile, daemon=True).start()
    return 200, {"ok": True, "changed": changed}


def _place_dash(frame: tuple[int, int, int, int]) -> bool:
    """Size the dashboard's own browser window (Chrome-family app window, or the tab in front) to the
    screen's visible frame, so its card grid lines up with the terminal cells."""
    x, y, w, h = frame
    win = '(first window whose name contains "Overlook")'
    for app in ("Google Chrome", "Brave Browser", "Microsoft Edge", "Chromium"):
        pre = f'tell application "System Events" to tell process "{app}" to '
        try:
            r = subprocess.run(["osascript", "-e", pre + f"set position of {win} to {{{x}, {y}}}",
                                "-e", pre + f"set size of {win} to {{{w}, {h}}}"], capture_output=True, text=True, timeout=10)
        except subprocess.TimeoutExpired:
            continue
        if r.returncode == 0:
            return True
    return False


def align(body: dict) -> tuple[int, dict]:
    """The ⊞ Align button: take the page's current grid, size the dashboard window to the screen and
    put every open terminal window back on its card's cell (the layout POST only re-tiles on CHANGE;
    this does it on demand, e.g. after windows were dragged or a display came and went)."""
    if body.get("ids"):
        code, r = set_layout(body, tile=False)
        if code != 200:
            return code, r
    _set_arrange("grid")
    _screen_cache["at"] = 0.0                 # re-read the screen: it may have changed
    frame = screen_frame()
    dash = _place_dash(frame) if frame else False
    retile(force=True)
    return 200, {"ok": True, "terminals": len(_term_windows), "dash": dash}


def _set_arrange(mode: str) -> None:
    with _settings_lock:
        st = load_settings()
        st["arrange"] = mode
        save_settings(st)


def arrange(body: dict) -> tuple[int, dict]:
    """The ◧ Arrange button: the dashboard takes the left half of the screen, the terminal windows
    stack on the right half (title bars, with the clicked one open: see _split_slot). The mode sticks (settings.arrange), so windows opened or re-tiled later
    keep to their half until ⊞ Align puts them back on the card grid."""
    if body.get("ids"):
        code, r = set_layout(body, tile=False)
        if code != 200:
            return code, r
    _set_arrange("split")
    _screen_cache["at"] = 0.0
    frame = screen_frame()
    dash = False
    if frame:
        x, y, w, h = frame
        dash = _place_dash((x, y, w - w // 2, h))
    retile(force=True)
    have = len(_term_windows)
    # every running background session gets a window on the right, not only the ones already open
    want = [s_["id"] for s_ in sessions()
            if s_.get("alive") and s_.get("kind") != "interactive" and s_["id"] not in _term_windows][:8]   # alive, incl. done/idle
    opened = 0
    if _terminal_app() == "Ghostty":
        for jid in want:
            opened += 1 if open_session(jid).get("ok") else 0
    return 200, {"ok": True, "terminals": have, "opened": opened, "dash": dash}


STRIP = 36          # height of a collapsed terminal window: its title bar and nothing else
_focus: dict = {"jid": None}   # the terminal showing full in the stack (the last one clicked)


def _split_slot(jid: str, frame: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    """Stack for the right half of the screen: every terminal window is collapsed to its title bar,
    in card order, except the focused one, which opens in place and takes all the height left.
    Click any title bar (or ↗ App) and focus_watch_loop makes that one the open one."""
    x0, y0, w, h = frame
    order = [i for i in _layout["ids"] if i in _term_windows] + sorted(i for i in _term_windows if i not in _layout["ids"])
    if jid not in order:
        order.append(jid)
    n, hw = len(order), w // 2
    k = order.index(_focus["jid"]) if _focus["jid"] in order else 0
    strip = min(STRIP, h // (2 * n))
    full = h - (n - 1) * strip
    i = order.index(jid)
    y = y0 + i * strip + (full - strip if i > k else 0)
    return (x0 + w - hw, y, hw, full if i == k else strip)


def _front_pid() -> int | None:
    """Pid of the frontmost app. lsappinfo, not osascript: this runs twice a second."""
    try:
        asn = subprocess.run(["lsappinfo", "front"], capture_output=True, text=True, timeout=3).stdout.strip()
        out = subprocess.run(["lsappinfo", "info", "-only", "pid", asn], capture_output=True, text=True, timeout=3).stdout
        return int(out.strip().split("=")[1])
    except Exception:
        return None


def focus_watch_loop() -> None:
    """While arranged (settings.arrange == split): when a terminal window comes to the front, it
    becomes the open one in the stack and the previous one folds back to its title bar."""
    mode_at, mode = 0.0, "grid"
    while True:
        time.sleep(0.5)
        try:
            if time.time() - mode_at > 3:
                mode_at, mode = time.time(), load_settings().get("arrange")
            if mode != "split" or len(_term_windows) < 2:
                continue
            front = _front_pid()
            jid = next((j for j, p_ in _term_windows.items() if p_ == front), None)
            if jid and jid != _focus["jid"]:
                _focus["jid"] = jid
                retile()
        except Exception as e:
            log(f"focus watch: {e!r}")


def window_slot(jid: str, st: dict) -> tuple[int, int, int, int] | None:
    """Grid cell for this session's terminal window = the cell its card has on the dashboard
    (same column count, same order). Before the page has reported a layout: the old scheme,
    lowest free cell of a 2x2 / 3x2, remembered in settings.slots."""
    frame = screen_frame()
    if not frame:
        return None
    x0, y0, w, h = frame
    if st.get("arrange") == "split":
        return _split_slot(jid, frame)
    if jid in _layout["ids"]:
        cols = max(1, min(_layout["cols"], 6))
        rows = max(1, min(-(-len(_layout["ids"]) // cols), 4))
        i = _layout["ids"].index(jid)
        if i >= cols * rows:                      # past the visible grid: stack on the last cell
            i = cols * rows - 1
    else:
        cols, rows = (3 if w >= 2400 else 2), 2
        slots = st.setdefault("slots", {})
        live = {s["id"] for s in sessions() if s.get("alive")}
        for k in [k for k in slots if k not in live and k != jid]:
            slots.pop(k)
        if jid not in slots:
            taken = set(slots.values())
            slots[jid] = next((i for i in range(cols * rows) if i not in taken), len(slots) % (cols * rows))
        i = slots[jid] % (cols * rows)
    cw, ch = w // cols, h // rows
    return (x0 + (i % cols) * cw, y0 + (i // cols) * ch, cw, ch)


_retile_lock = threading.Lock()


_placed: dict[int, tuple] = {}   # ghostty pid -> the cell we last put its window in


def retile(force: bool = False) -> None:
    """Move every terminal window Overlook opened to the cell its card now occupies. `force` also
    re-places windows whose cell has not changed (the buttons: they may have been dragged)."""
    with _retile_lock:
        _retile(force)


def _retile(force: bool = False) -> None:
    alive = _discover_windows()
    for jid, pid in list(_term_windows.items()):
        if pid not in alive:
            _term_windows.pop(jid, None)
    st, jobs = load_settings(), []
    for jid, pid in list(_term_windows.items()):
        slot = window_slot(jid, st)
        if slot and (force or _placed.get(pid) != slot):   # a focus change moves only the windows whose cell changed
            _placed[pid] = slot
            jobs.append(threading.Thread(target=_place_window, args=(pid, slot, 6), daemon=True))
    for pid in [p_ for p_ in _placed if p_ not in alive]:
        _placed.pop(pid, None)
    for t in jobs:
        t.start()
    for t in jobs:
        t.join()


def _ghostty_pids() -> set[int]:
    out = subprocess.run(["pgrep", "-f", "Ghostty.app/Contents/MacOS/ghostty"], capture_output=True, text=True).stdout
    return {int(p) for p in out.split()}


_term_windows: dict[str, int] = {}   # session id -> pid of the ghostty process showing it (↗ App reuses it)
_ATTACH_RE = re.compile(r" attach '([A-Za-z0-9_-]{4,64})'")


def _discover_windows() -> set[int]:
    """Re-learn which Ghostty process shows which session; returns the live Ghostty pids.
    _term_windows is memory only, so a server restart used to orphan every open window (Align then
    moved 0 of them). Each window ↗ App opens is its own process whose command line carries
    `claude attach '<id>'`, so `ps` can always rebuild the map."""
    out = subprocess.run(["ps", "-axwwo", "pid=,command="], capture_output=True, text=True).stdout
    pids: set[int] = set()
    for line in out.splitlines():
        pid, _, cmd = line.strip().partition(" ")
        if not pid.isdigit() or not cmd.split(" --", 1)[0].endswith("Ghostty.app/Contents/MacOS/ghostty"):
            continue
        pids.add(int(pid))
        m = _ATTACH_RE.search(cmd)
        if m:
            _term_windows[m.group(1)] = int(pid)
    return pids


def _se(pid: int, *stmts: str) -> subprocess.CompletedProcess:
    """Run System Events statements against the process with this unix id. One -e per statement,
    each a complete one-liner: AppleScript mis-parses a block body after `tell X to tell Y`."""
    pre = f'tell application "System Events" to tell (first process whose unix id is {pid}) to '
    args = []
    for st in stmts:
        args += ["-e", pre + st]
    return subprocess.run(["osascript", *args], capture_output=True, text=True, timeout=15)


WIN = '(first window whose subrole is "AXStandardWindow")'   # not Ghostty's transient 84x77 startup window


def _focus_ghostty(pid: int) -> bool:
    r = _se(pid, "set frontmost to true", f'perform action "AXRaise" of {WIN}')
    return r.returncode == 0


def _track_new_ghostty(jid: str, before: set[int], slot: tuple[int, int, int, int] | None) -> None:
    """Find the ghostty process `open -na` spawned, remember it for this session, and (if a slot
    was given) move/resize its window into the slot. Ghostty re-applies its default size just after
    the window appears, so the set is repeated until a read-back agrees."""
    pid = None
    for _ in range(40):                       # up to ~8s for the process to show up
        time.sleep(0.2)
        _discover_windows()                   # several may open at once: the command line says whose is whose
        pid = _term_windows.get(jid)
        if pid:
            break
    if pid is None:
        new = _ghostty_pids() - before
        pid = min(new) if new else None
    if pid is None:
        log("ghostty: no new process"); return
    _term_windows[jid] = pid
    if load_settings().get("arrange") == "split":
        _focus["jid"] = jid                   # the window just opened is the open one in the stack
        retile()
    elif slot:
        _place_window(pid, slot)


def _place_window(pid: int, slot: tuple[int, int, int, int], tries: int = 25) -> None:
    x, y, w, h = slot
    r = None
    for _ in range(tries):
        r = _se(pid, f"set position of {WIN} to {{{x}, {y}}}", f"set size of {WIN} to {{{w}, {h}}}",
                "delay 0.3", f"get (position of {WIN}) & (size of {WIN})")
        if r.returncode == 0:
            try:
                got = [int(v) for v in r.stdout.strip().split(", ")]
            except ValueError:
                got = []
            if got and all(abs(a - b) <= 4 for a, b in zip(got, (x, y, w, h))):
                return
        time.sleep(0.3)
    log(f"ghostty place: gave up on pid {pid}: {(r.stderr or r.stdout).strip()[-120:] if r else ''}")


# Browsers resolve every *.localhost name to loopback without /etc/hosts, so the dashboard
# answers to http://overlook.localhost:7878 (and http://overlook.localhost after port80.sh).
HOST_NAME = os.environ.get("OVERLOOK_HOST", "overlook.localhost")


def _loopback_name(host: str) -> bool:
    host = host.lower()
    return host in ("127.0.0.1", "localhost", "::1") or host.endswith(".localhost")


def _terminal_app() -> str:
    want = os.environ.get("OVERLOOK_TERMINAL")
    if want:
        return want
    for app in ("Ghostty", "iTerm", "Terminal"):
        if Path(f"/Applications/{app}.app").exists():
            return app
    return "Terminal"


def open_session(jid: str) -> dict:
    """Background session: new terminal window attached to it.
    Interactive session: bring the terminal app forward."""
    app = _terminal_app()
    s_ = next((x for x in sessions() if x["id"] == jid), None)
    if not s_:
        return {"ok": False, "error": "no such session"}
    if s_.get("kind") == "interactive":
        subprocess.run(["open", "-a", app], timeout=10)
        return {"ok": True, "how": f"focused {app}"}
    exe = claude_bin() or "claude"
    cfg = claude_env(jid).get("CLAUDE_CONFIG_DIR", "")
    cwd = s_.get("cwd") or str(HOME)
    with _settings_lock:
        st = load_settings()
        color, title = session_color(jid, st), s_["name"]
        proj = Path(s_.get("work") or s_.get("cwd") or "").name
        if proj and proj not in title:
            title = f"{title} · {proj}"
        title = f"{CHIPS[PALETTE.index(color)]} {title}"
        slot = window_slot(jid, st)
        save_settings(st)
    bg = tint(color)
    q = lambda v: "'" + str(v).replace("'", "'\\''") + "'"
    # OSC 2 = window title, OSC 11 = background colour (Ghostty, iTerm, Terminal all honour it);
    # OSC 6 is iTerm's tab colour. Claude later rewrites the title; the colours stick.
    r, g, b = (int(color[i:i + 2], 16) for i in (1, 3, 5))
    paint = (f"printf '\\e]2;%s\\a\\e]11;%s\\a' {q(title)} {q(bg)}; "
             f"printf '\\e]6;1;bg;red;brightness;{r}\\a\\e]6;1;bg;green;brightness;{g}\\a\\e]6;1;bg;blue;brightness;{b}\\a'; ")
    cmd = f"cd {q(cwd)}; " + (f"export CLAUDE_CONFIG_DIR={q(cfg)}; " if cfg else "") + paint + f"{q(exe)} attach {q(jid)}; exec $SHELL -l"
    if app == "Ghostty":
        # Each `open -na Ghostty` is its own process, so the new window is "window 1 of the process
        # whose unix id is <new pid>". Ghostty honours --title/--background but IGNORES
        # --window-position-*/--window-width/height for these launches (verified 1.4.0), so the
        # window is placed afterwards through System Events.
        # One window per session: if the one we opened earlier is still there, bring it forward.
        live = _discover_windows()
        pid = _term_windows.get(jid)
        if pid and pid in live and _focus_ghostty(pid):
            log(f"open {jid}: focused existing Ghostty window (pid {pid})")
            return {"ok": True, "how": "focused its Ghostty window", "color": color}
        _term_windows.pop(jid, None)
        before = _ghostty_pids()
        subprocess.Popen(["open", "-na", "Ghostty", "--args", f"--title={title}", f"--background={bg}",
                          f"--unfocused-split-fill={bg}", "-e", "/bin/zsh", "-lc", cmd])
        threading.Thread(target=_track_new_ghostty, args=(jid, before, slot), daemon=True).start()
    elif app == "iTerm":
        script = [f'tell application "iTerm" to create window with default profile command "/bin/zsh -lc " & quoted form of {json.dumps(cmd)}']
        if slot:
            x, y, w, h = slot
            script.append(f'tell application "iTerm" to set bounds of front window to {{{x}, {y}, {x + w}, {y + h}}}')
        script.append('tell application "iTerm" to activate')
        subprocess.Popen(["osascript", *sum((["-e", s] for s in script), [])])
    else:
        script = [f'tell application "Terminal" to do script {json.dumps(cmd)}']
        if slot:
            x, y, w, h = slot
            script.append(f'tell application "Terminal" to set bounds of front window to {{{x}, {y}, {x + w}, {y + h}}}')
        script.append('tell application "Terminal" to activate')
        subprocess.Popen(["osascript", *sum((["-e", s] for s in script), [])])
    log(f"open {jid} in {app} ({color}, slot {slot})")
    return {"ok": True, "how": f"attached in {app}", "color": color}


# ---------------------------------------------------------------- web terminal
# A pty running `claude attach <id>` streamed to xterm.js over a WebSocket.
# stdlib only: the handshake, framing and pty plumbing are all here.

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def ws_recv(rf) -> tuple[int, bytes] | None:
    """One client frame as (opcode, payload); None when the socket is gone."""
    h = rf.read(2)
    if len(h) < 2:
        return None
    op, ln = h[0] & 0x0F, h[1] & 0x7F
    if ln == 126:
        ln = struct.unpack(">H", rf.read(2))[0]
    elif ln == 127:
        ln = struct.unpack(">Q", rf.read(8))[0]
    mask = rf.read(4) if h[1] & 0x80 else None
    data = rf.read(ln)
    if mask:
        data = bytes(c ^ mask[i & 3] for i, c in enumerate(data))
    return op, data


def ws_frame(op: int, data: bytes) -> bytes:
    n = len(data)
    if n < 126:
        return bytes([0x80 | op, n]) + data
    if n < 65536:
        return bytes([0x80 | op, 126]) + struct.pack(">H", n) + data
    return bytes([0x80 | op, 127]) + struct.pack(">Q", n) + data


def set_winsize(fd: int, cols: int, rows: int) -> None:
    cols, rows = max(20, min(int(cols), 500)), max(5, min(int(rows), 200))
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def spawn_terminal(jid: str, cols: int, rows: int) -> tuple[subprocess.Popen, int]:
    """`claude attach <id>` on its own pty. Ctrl+Z inside detaches; the session keeps running."""
    s_ = next((x for x in sessions() if x["id"] == jid), None)
    if not s_:
        raise FileNotFoundError("no such session")
    exe = claude_bin() or "claude"
    env = claude_env(jid)
    env.update(TERM="xterm-256color", COLORTERM="truecolor", LANG=env.get("LANG") or "en_US.UTF-8")
    master, slave = pty.openpty()
    set_winsize(slave, cols, rows)

    def child():  # own session + this pty as the controlling terminal, so ^C/^Z reach claude
        os.setsid()
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)

    proc = subprocess.Popen([exe, "attach", jid], stdin=slave, stdout=slave, stderr=slave,
                            cwd=s_.get("cwd") if s_.get("cwd") and Path(s_["cwd"]).is_dir() else str(HOME),
                            env=env, preexec_fn=child, close_fds=True)
    os.close(slave)
    log(f"terminal {jid} pid {proc.pid} ({cols}x{rows})")
    return proc, master


# ----------------------------------------------------------------------- http


class Handler(BaseHTTPRequestHandler):
    server_version = f"Overlook/{VERSION}"

    def log_message(self, fmt, *args):  # quiet; only errors and launches log
        pass

    # Only answer to our own origin: stops DNS-rebinding and drive-by POSTs
    # from random web pages to a server that can start agents.
    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
        return _loopback_name(host)

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        if origin is None:
            return self.headers.get("Sec-Fetch-Site", "same-origin") in ("same-origin", "none")
        o = urlparse(origin)
        # Same port, or the portless form when scripts/port80.sh redirects :80 here.
        return _loopback_name(o.hostname or "") and (o.port or 80) in (self.server.server_port, 80)

    def _send(self, code: int, payload, ctype="application/json"):
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)


    def _ws_terminal(self, jid: str, q: dict) -> None:
        key = self.headers.get("Sec-WebSocket-Key")
        if not key or (self.headers.get("Upgrade") or "").lower() != "websocket":
            return self._send(400, {"error": "websocket expected"})
        if not self._origin_ok():
            return self._send(403, {"error": "bad origin"})
        try:
            proc, fd = spawn_terminal(jid, int(q.get("cols", ["100"])[0]), int(q.get("rows", ["30"])[0]))
        except Exception as e:
            return self._send(404, {"error": str(e)})
        accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
        self.wfile.write(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                          f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode())
        self.close_connection = True
        wlock = threading.Lock()

        def send(op: int, data: bytes) -> None:
            with wlock:
                self.wfile.write(ws_frame(op, data))

        def pump() -> None:  # pty -> browser
            while True:
                try:
                    data = os.read(fd, 65536)
                except OSError:
                    data = b""
                if not data:
                    break
                try:
                    send(2, data)
                except OSError:
                    break
            try:
                code = proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                code = None
            try:
                send(1, json.dumps({"exit": code}).encode())
                send(8, struct.pack(">H", 1000))
            except OSError:
                pass

        threading.Thread(target=pump, daemon=True).start()
        try:
            while True:  # browser -> pty
                fr = ws_recv(self.rfile)
                if fr is None or fr[0] == 8:
                    break
                op, data = fr
                if op == 9:
                    send(10, data)
                elif op == 2:
                    os.write(fd, data)
                elif op == 1:
                    try:
                        m = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(m.get("resize"), list) and len(m["resize"]) == 2:
                        set_winsize(fd, *m["resize"])
                    elif isinstance(m.get("input"), str):
                        os.write(fd, m["input"].encode())
        except (OSError, ValueError):
            pass
        finally:
            if proc.poll() is None:  # the attach client only; the session itself keeps running
                try:
                    os.killpg(proc.pid, signal.SIGHUP)
                    proc.wait(timeout=2)
                except (OSError, subprocess.TimeoutExpired):
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except OSError:
                        pass
            try:
                os.close(fd)
            except OSError:
                pass
            log(f"terminal {jid} closed (exit {proc.poll()})")

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, {"error": "bad host"})
        u = urlparse(self.path)
        p = u.path
        q = parse_qs(u.query)
        try:
            if p in ("/", "/index.html"):
                return self._send(200, (WEB / "index.html").read_bytes(), "text/html; charset=utf-8")
            m = re.match(r"^/ws/term/([^/]+)$", p)
            if m and ID_RE.match(m.group(1)):
                return self._ws_terminal(m.group(1), q)
            if p == "/api/sessions":
                _term_viewed["at"] = time.time()
                return self._send(200, {"sessions": sessions(), "now": time.time()})
            if p == "/api/agents":
                return self._send(200, {"agents": agents()})
            if p == "/api/dirs":
                return self._send(200, {"dirs": dirs()})
            if p == "/api/settings":
                return self._send(200, {"settings": load_settings(), "models": MODELS, "path": str(SETTINGS)})
            if p == "/api/info":
                return self._send(200, {"version": VERSION, "claude": claude_bin(), "models": MODELS,
                                        "configDirs": [str(d) for d in config_dirs()]})
            m = re.match(r"^/api/sessions/([^/]+)/(timeline|logs)$", p)
            if m and ID_RE.match(m.group(1)):
                jid, what = m.groups()
                if what == "timeline":
                    return self._send(200, {"timeline": timeline(jid, int(q.get("limit", ["200"])[0]))})
                code, out = run_claude(["logs", jid], timeout=20, jid=jid)
                return self._send(200, {"ok": code == 0, "logs": out[-60000:]})
            return self._send(404, {"error": "not found"})
        except Exception as e:  # keep the dashboard up whatever one route does
            log(f"GET {p} failed: {e!r}")
            return self._send(500, {"error": str(e)})

    def do_POST(self):
        if not (self._host_ok() and self._origin_ok()):
            return self._send(403, {"error": "cross-origin request refused"})
        if "application/json" not in (self.headers.get("Content-Type") or ""):
            return self._send(415, {"error": "json only"})
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(min(n, 200_000)) or b"{}")
        except json.JSONDecodeError:
            return self._send(400, {"error": "bad json"})
        p = urlparse(self.path).path
        if p == "/api/launch":
            code, res = launch(body)
            return self._send(code, res)
        if p == "/api/settings/agent-model":
            agent, model = str(body.get("agent") or ""), str(body.get("model") or "").strip()
            if not AGENT_RE.match(agent) or (model and not AGENT_RE.match(model)):
                return self._send(400, {"error": "bad agent or model"})
            with _settings_lock:
                st = load_settings()
                if model:
                    st["agentModels"][agent] = model
                else:
                    st["agentModels"].pop(agent, None)
                save_settings(st)
            log(f"model for {agent} -> {model or 'agent default'}")
            return self._send(200, {"ok": True, "settings": st})
        m = re.match(r"^/api/sessions/([^/]+)/open$", p)
        if m and ID_RE.match(m.group(1)):
            res = open_session(m.group(1))
            return self._send(200 if res.get("ok") else 404, res)
        m = re.match(r"^/api/sessions/([^/]+)/(yes|auto)$", p)
        if m and ID_RE.match(m.group(1)):
            jid, what = m.groups()
            if what == "yes":
                res = poke(jid)
                return self._send(200 if res.get("ok") else 409, res)
            with _settings_lock:
                st = load_settings()
                st["autoYes"][jid] = bool(body.get("on"))
                save_settings(st)
            log(f"auto-yes {jid} -> {bool(body.get('on'))}")
            return self._send(200, {"ok": True, "on": bool(body.get("on"))})
        m = re.match(r"^/api/sessions/([^/]+)/rename$", p)
        if m and ID_RE.match(m.group(1)):
            # The CLI has no rename; the name is an Overlook-side override. Empty clears it.
            name = " ".join(str(body.get("name") or "").split())[:80]
            with _settings_lock:
                st = load_settings()
                if name:
                    st["names"][m.group(1)] = name
                else:
                    st["names"].pop(m.group(1), None)
                save_settings(st)
            log(f"rename {m.group(1)} -> {name!r}")
            return self._send(200, {"ok": True, "name": name})
        m = re.match(r"^/api/sessions/([^/]+)/color$", p)
        if m and ID_RE.match(m.group(1)):
            with _settings_lock:
                st = load_settings()
                cur = PALETTE.index(session_color(m.group(1), st))
                idx = body.get("index")
                st["colors"][m.group(1)] = int(idx) % len(PALETTE) if isinstance(idx, int) else (cur + 1) % len(PALETTE)
                save_settings(st)
                color = session_color(m.group(1), st)
            return self._send(200, {"ok": True, "color": color})
        m = re.match(r"^/api/sessions/([^/]+)/wrap$", p)
        if m and ID_RE.match(m.group(1)):
            res = poke(m.group(1), "wrap", text=WRAP_TEXT)
            if res.get("ok"):
                mark_wrapped(m.group(1))
            return self._send(200 if res.get("ok") else 409, res)
        m = re.match(r"^/api/sessions/([^/]+)/wrapped$", p)   # the terminal tab typed /wrape itself
        if m and ID_RE.match(m.group(1)):
            mark_wrapped(m.group(1))
            return self._send(200, {"ok": True})
        if p == "/api/sessions/close-finished":
            done, failed = [], []
            for s_ in [x for x in sessions() if x.get("finished")]:
                code, res = close_session(s_["id"])
                (done if code < 300 else failed).append({"id": s_["id"], "name": s_["name"], **({"pending": True} if code == 202 else {}), **({} if code < 300 else {"error": res.get("error")})})
            pending = [x for x in done if x.pop("pending", None)]
            log(f"close-finished: {len(done) - len(pending)} closed, {len(pending)} wrapping, {len(failed)} failed")
            return self._send(200, {"ok": not failed, "closed": [x for x in done if x["id"] not in _pending_close], "wrapping": [x for x in done if x["id"] in _pending_close], "failed": failed})
        m = re.match(r"^/api/sessions/([^/]+)/close$", p)
        if m and ID_RE.match(m.group(1)):
            return self._send(*close_session(m.group(1)))
        m = re.match(r"^/api/sessions/([^/]+)/stop$", p)
        if m and ID_RE.match(m.group(1)):
            return self._send(*stop_session(m.group(1), force=bool(body.get("force"))))
        if p == "/api/layout":
            return self._send(*set_layout(body))
        if p == "/api/align":
            return self._send(*align(body))
        if p == "/api/arrange":
            return self._send(*arrange(body))
        if p == "/api/ui-log":
            return self._send(*ui_log(body))
        return self._send(404, {"error": "not found"})


def open_app_window(url: str) -> None:
    """Dashboard as its own window (Chrome/Edge/Brave --app mode), browser tab otherwise."""
    for app in ("Google Chrome", "Brave Browser", "Microsoft Edge", "Chromium"):
        if Path(f"/Applications/{app}.app").exists():
            subprocess.Popen(["open", "-na", app, "--args", f"--app={url}"])
            return
    webbrowser.open(url)


def main():
    ap = argparse.ArgumentParser(description="Overlook: dashboard for Claude Code agents")
    ap.add_argument("--port", type=int, default=int(os.environ.get("OVERLOOK_PORT", 7878)))
    ap.add_argument("--open", action="store_true", help="open the browser")
    ap.add_argument("--app", action="store_true", help="open as a standalone Chrome app window")
    a = ap.parse_args()
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    url = f"http://{HOST_NAME}" + ("" if a.port == 80 else f":{a.port}")
    log(f"Overlook {VERSION} at {url}")
    log(f"config dirs: {', '.join(str(d) for d in config_dirs()) or 'none found'}")
    log(f"claude: {claude_bin() or 'NOT FOUND'}")
    threading.Thread(target=auto_yes_loop, daemon=True).start()
    threading.Thread(target=pending_close_loop, daemon=True).start()
    threading.Thread(target=term_line_loop, daemon=True).start()
    threading.Thread(target=focus_watch_loop, daemon=True).start()
    if a.app:
        threading.Timer(0.5, lambda: open_app_window(url)).start()
    elif a.open:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log("bye")


if __name__ == "__main__":
    sys.exit(main())
