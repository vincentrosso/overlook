#!/usr/bin/env python3
"""What the Overlook UI is actually used for, and where it hurts — from ~/.config/overlook/ui-log.jsonl.

  python3 scripts/ui_report.py [--since 7d|24h|2026-11-01] [--sid XXXX] [PATH]

Sections: span + sessions · events by type · top controls clicked · DEAD clicks (clicked, nothing bound)
· RAGE clicks (≥3 on one control within 2 s) · drawers: dwell + bounce rate (<2 s) · toasts/errors ·
API errors · keyboard · views. Each is a hint, not a verdict: a dead click on a card title means people
expect it to open something; a bounce drawer means it did not show what they came for.
"""
import argparse, json, os, statistics, sys, time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

DEFAULT = Path(os.environ.get("OVERLOOK_UI_LOG", Path.home() / ".config" / "overlook" / "ui-log.jsonl"))


def since_epoch(s: str | None) -> float:
    if not s:
        return 0.0
    if s[-1] in "dh" and s[:-1].isdigit():
        return time.time() - int(s[:-1]) * (86400 if s[-1] == "d" else 3600)
    return datetime.fromisoformat(s).timestamp()


def load(path: Path, since: float, sid: str | None) -> list[dict]:
    rows = []
    for line in path.open(encoding="utf-8", errors="replace"):
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        t = (e.get("t") or 0) / 1000
        if t >= since and (not sid or e.get("sid") == sid):
            e["_t"] = t
            rows.append(e)
    rows.sort(key=lambda e: e["_t"])
    return rows


def hms(ms: float) -> str:
    s = ms / 1000
    return f"{s:.1f}s" if s < 90 else f"{s / 60:.1f}m"


def top(counter: Counter, n=12, width=44) -> str:
    return "\n".join(f"  {c:5}  {str(k)[:width]}" for k, c in counter.most_common(n)) or "  —"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("path", nargs="?", default=str(DEFAULT))
    ap.add_argument("--since", help="7d, 24h or an ISO date")
    ap.add_argument("--sid", help="one page session id")
    a = ap.parse_args()
    path = Path(a.path)
    if not path.exists():
        print(f"no log at {path} — open the dashboard and click around first"); return 1
    ev = load(path, since_epoch(a.since), a.sid)
    if not ev:
        print("no events in range"); return 1

    t0, t1 = ev[0]["_t"], ev[-1]["_t"]
    sids = Counter(e.get("sid") for e in ev)
    print(f"{len(ev)} events, {len(sids)} page sessions, {datetime.fromtimestamp(t0):%Y-%m-%d %H:%M} → {datetime.fromtimestamp(t1):%m-%d %H:%M}  ({path})")

    print("\nEVENTS BY TYPE"); print(top(Counter(e.get("ev") for e in ev)))

    clicks = [e for e in ev if e.get("ev") == "click"]
    print(f"\nTOP CONTROLS CLICKED ({len(clicks)})"); print(top(Counter(e.get("ctl") for e in clicks)))
    print("  by view:", dict(Counter(e.get("view") for e in clicks)))

    dead = [e for e in ev if e.get("ev") == "deadclick"]
    print(f"\nDEAD CLICKS ({len(dead)}) — looked clickable, nothing bound; the top ones want an action")
    print(top(Counter(f'{e.get("ctl")} “{e.get("text")}”' for e in dead), width=70))

    rage = Counter()
    bysid = defaultdict(list)
    for e in clicks:
        bysid[e.get("sid")].append(e)
    for rows in bysid.values():
        run = []
        for e in rows:
            if run and e.get("ctl") == run[-1].get("ctl") and e["_t"] - run[-1]["_t"] <= 2:
                run.append(e)
            else:
                if len(run) >= 3:
                    rage[run[0].get("ctl")] += 1
                run = [e]
        if len(run) >= 3:
            rage[run[0].get("ctl")] += 1
    print(f"\nRAGE CLICKS ({sum(rage.values())} bursts of ≥3 on one control within 2 s) — slow, unresponsive, or unclear it worked")
    print(top(rage))

    closes = [e for e in ev if e.get("ev") == "drawer-close" and isinstance(e.get("dwell"), (int, float))]
    print(f"\nDRAWERS ({len(closes)} closes)")
    for key in sorted({f'{e.get("kind")}:{e.get("tab")}' for e in closes}):
        ds = [e["dwell"] for e in closes if f'{e.get("kind")}:{e.get("tab")}' == key]
        bounce = sum(d < 2000 for d in ds)
        print(f"  {key:22} n={len(ds):4}  median {hms(statistics.median(ds)):>6}  bounce(<2s) {bounce / len(ds):4.0%}")
    opens = Counter(e.get("kind") for e in ev if e.get("ev") == "drawer-open")
    if opens:
        print("  opened by kind:", dict(opens))

    toasts = [e for e in ev if e.get("ev") == "toast"]
    errs = [e for e in toasts if str(e.get("msg", "")).lower().startswith(("couldn", "⚠", "error", "failed", "refused"))]
    print(f"\nTOASTS ({len(toasts)}, {len(errs)} error-ish)"); print(top(Counter(e.get("msg") for e in toasts), width=80))

    api = [e for e in ev if e.get("ev") == "api-error"]
    print(f"\nAPI ERRORS ({len(api)})"); print(top(Counter(f'{e.get("status")} {e.get("path")} — {e.get("err")}' for e in api), width=90))

    keys = [e for e in ev if e.get("ev") == "key"]
    print(f"\nKEYBOARD ({len(keys)})"); print(top(Counter(f'{e.get("k")}{" (in terminal)" if e.get("inTerm") else ""}' for e in keys)))

    views = [e for e in ev if e.get("ev") == "view"]
    print(f"\nVIEW SWITCHES ({len(views)})"); print(top(Counter(e.get("to") for e in views)))

    changes = [e for e in ev if e.get("ev") == "change"]
    if changes:
        print(f"\nTOGGLES ({len(changes)})"); print(top(Counter(f'{e.get("ctl")}={e.get("on")}' for e in changes)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
