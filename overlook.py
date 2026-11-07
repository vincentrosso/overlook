#!/usr/bin/env python3
"""Overlook — a local web dashboard for Claude Code background agents.

Status board for every background session, plus a searchable hub of every
agent definition on the machine, with one-click launch.

Zero dependencies: Python 3.9+ stdlib only. Binds to 127.0.0.1.

    python3 overlook.py            # http://127.0.0.1:7878
    python3 overlook.py --open     # ...and open the browser
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

VERSION = "0.2.0"
HOME = Path.home()
WEB = Path(__file__).resolve().parent / "web"
ID_RE = re.compile(r"^[A-Za-z0-9_-]{4,64}$")
AGENT_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
PERMISSION_MODES = {"default", "acceptEdits", "auto", "plan", "bypassPermissions"}


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


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


def claude_env() -> dict:
    env = dict(os.environ)
    dirs = config_dirs()
    # Background jobs live under whichever config dir has a jobs/ folder.
    if "CLAUDE_CONFIG_DIR" not in env:
        for d in dirs:
            if (d / "jobs").is_dir() and d.name != ".claude":
                env["CLAUDE_CONFIG_DIR"] = str(d)
                break
    return env


def run_claude(args: list[str], timeout: float = 20, cwd: str | None = None) -> tuple[int, str]:
    exe = claude_bin()
    if not exe:
        return 127, "claude CLI not found on PATH (set OVERLOOK_CLAUDE)"
    try:
        p = subprocess.run([exe, *args], capture_output=True, text=True,
                           timeout=timeout, cwd=cwd, env=claude_env())
        return p.returncode, (p.stdout + p.stderr)
    except subprocess.TimeoutExpired:
        return 124, "timed out"


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
            "dir": None,
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
    for a in out.values():
        a.pop("_mtime", None)
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
    model = str(body.get("model") or "").strip()
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


# ----------------------------------------------------------------------- http


class Handler(BaseHTTPRequestHandler):
    server_version = f"Overlook/{VERSION}"

    def log_message(self, fmt, *args):  # quiet; only errors and launches log
        pass

    # Only answer to our own origin: stops DNS-rebinding and drive-by POSTs
    # from random web pages to a server that can start agents.
    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("127.0.0.1", "localhost", "[::1]")

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        if origin is None:
            return self.headers.get("Sec-Fetch-Site", "same-origin") in ("same-origin", "none")
        o = urlparse(origin)
        return o.hostname in ("127.0.0.1", "localhost") and o.port == self.server.server_port

    def _send(self, code: int, payload, ctype="application/json"):
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, {"error": "bad host"})
        u = urlparse(self.path)
        p = u.path
        q = parse_qs(u.query)
        try:
            if p in ("/", "/index.html"):
                return self._send(200, (WEB / "index.html").read_bytes(), "text/html; charset=utf-8")
            if p == "/api/sessions":
                return self._send(200, {"sessions": sessions(), "now": time.time()})
            if p == "/api/agents":
                return self._send(200, {"agents": agents()})
            if p == "/api/dirs":
                return self._send(200, {"dirs": dirs()})
            if p == "/api/info":
                return self._send(200, {"version": VERSION, "claude": claude_bin(),
                                        "configDirs": [str(d) for d in config_dirs()]})
            m = re.match(r"^/api/sessions/([^/]+)/(timeline|logs)$", p)
            if m and ID_RE.match(m.group(1)):
                jid, what = m.groups()
                if what == "timeline":
                    return self._send(200, {"timeline": timeline(jid, int(q.get("limit", ["200"])[0]))})
                code, out = run_claude(["logs", jid], timeout=20)
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
        m = re.match(r"^/api/sessions/([^/]+)/stop$", p)
        if m and ID_RE.match(m.group(1)):
            log(f"stop {m.group(1)}")
            code, out = run_claude(["stop", m.group(1)], timeout=30)
            _live_cache["at"] = 0
            return self._send(200 if code == 0 else 500, {"ok": code == 0, "output": out.strip()})
        return self._send(404, {"error": "not found"})


def main():
    ap = argparse.ArgumentParser(description="Overlook: dashboard for Claude Code agents")
    ap.add_argument("--port", type=int, default=int(os.environ.get("OVERLOOK_PORT", 7878)))
    ap.add_argument("--open", action="store_true", help="open the browser")
    a = ap.parse_args()
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    url = f"http://127.0.0.1:{a.port}"
    log(f"Overlook {VERSION} at {url}")
    log(f"config dirs: {', '.join(str(d) for d in config_dirs()) or 'none found'}")
    log(f"claude: {claude_bin() or 'NOT FOUND'}")
    if a.open:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log("bye")


if __name__ == "__main__":
    sys.exit(main())
