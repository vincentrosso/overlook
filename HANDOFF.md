# Overlook handoff

Local web dashboard for Claude Code agents, built as a Conductor replacement.
v0.8.0, branch `main`, public repo `vincentrosso/overlook`.

## Layout

- `overlook.py`: stdlib-only server, binds 127.0.0.1:7878, answers to any `*.localhost` Host
  (browsers resolve those to loopback with no /etc/hosts) — canonical URL `http://overlook.localhost:7878`;
  `sudo scripts/port80.sh` adds a persistent pf `rdr` so `http://overlook.localhost` works too (a user
  process can't bind :80 on macOS — tested, EACCES — and the LaunchAgent must not run as root). Run `python3 overlook.py --open`.
- `web/index.html`: the whole UI (vanilla JS, polls every 4s).
- `scripts/install-launchd.sh`: run at login (`--uninstall` removes it). **Installed and KeepAlive:** `pkill -f overlook.py` restarts it on the new code; log `~/Library/Logs/overlook.log`.
- `~/.config/overlook/settings.json`: per-agent default models and per-session auto-yes.

## Where the data comes from

| What | Source |
|---|---|
| Background sessions | `$CLAUDE_CONFIG_DIR/jobs/*/state.json`, `timeline.jsonl` |
| All live processes, including terminal sessions | `sessions/<pid>.json`, kept if the pid is alive |
| Name, last ask, last reply for terminal sessions | tail of `projects/*/<sessionId>.jsonl` (`ai-title`, `last-prompt`, last assistant) |
| Pid and liveness | `claude agents --json --all`, cached 10s |
| Agent catalog | `agents/*.md` in user, project and plugin dirs, plus 4 hard-coded built-ins |

Config dir here is `~/.claude-home`, not `~/.claude`.

## How the non-obvious parts work

- **Needs input:** `stClass()` in the UI maps states by regex. Background jobs report
  `blocked`; terminal sessions get a synthetic `waiting` when the last turn ended.
- **Yes / Go (`poke`):** there is no `claude send`. It reads the screen with `claude logs`,
  picks keys (`yes_plan`), runs `claude attach` in a pty, sends keys, then Ctrl+Z to detach.
  Text and Enter must be separate writes, or the Enter is swallowed as paste.
- **Wrap (`POST /api/sessions/<id>/wrap`, v0.7):** `poke(jid, text=WRAP_TEXT)` types `/wrape `
  through the same pty path. The trailing space closes the slash picker so Enter submits the
  typed command, not the highlighted suggestion. Overlook does NOT stop the job: `/wrape`'s
  own last step is `claude stop` on itself, after the wrap report is written. Refused (409)
  when `yes_plan` sees a permission/menu prompt, because the keys would land in the picker.
- **Live terminal line on cards (`term_line`):** `term_line_loop` runs `claude logs` per alive background
  job every ~3s (sequential, 30s backoff on error, paused when no browser has hit `/api/sessions` for 15s),
  keeps the spinner/status row (or last real output line) in `_term_lines`; `sessions()` just reads it.
- **Colour + window grid (v0.7):** `session_color(jid)` = md5 of the id into `PALETTE`, overridable
  via `settings.colors[id]` (`POST …/color` cycles). `open_session` paints the real window with
  OSC 2 (title) + OSC 11 (background = `tint(color)`) + iTerm's OSC 6 tab colour, and Ghostty also
  gets `--title/--background`. **Ghostty 1.4 ignores `--window-position-*`/`--window-width/height`
  on these launches** (verified live), and `open -na` spawns a NEW ghostty process per window, so
  `_place_new_ghostty` diffs `pgrep` before/after and moves "window 1 of the process whose unix id
  is <pid>" via System Events (needs Accessibility permission for the python/launcher; it worked
  from a `--bg` job without prompting). Two traps found live: the process briefly shows a transient
  84×77 window (so target `first window whose subrole is "AXStandardWindow"`), and Ghostty re-applies
  its default size right after the window appears, so the set is repeated until a read-back agrees.
  AppleScript trap: `tell X to tell Y` followed by a block body mis-parses (-10006) — `_se()` sends
  each statement as its own complete one-liner. `_term_windows[jid]` = pid of the session's window;
  ↗ App raises that window if the process is still alive instead of opening another (the "doubling").
- **UI telemetry (v0.8.2):** the page batches every interaction (clicks with a stable control name, DEAD clicks on
  things that look clickable but aren't, view switches, drawer open/close + dwell, shortcuts, toasts, API errors,
  terminal detach) to `POST /api/ui-log` → `~/.config/overlook/ui-log.jsonl` (rotates at 50 MB). `python3 scripts/ui_report.py`
  summarises it: top controls, dead clicks, rage clicks, bounce drawers, recurring toasts/errors. Use it before changing the UI.
- **◧ Arrange (2026-11-09, PRs #5-#7, verified live):** `POST /api/arrange` puts the dashboard on the left
  half, opens a Ghostty window for every running background session that lacks one, and STACKS them on the
  right half (`_split_slot`): each collapsed to its title bar (`STRIP` 36px; Ghostty accepts it), the focused
  one open in place with the remaining height. `focus_watch_loop` polls the frontmost app twice a second
  (`lsappinfo`, not osascript) and re-stacks when a terminal window comes forward. Mode persists as
  `settings.arrange` (`split`/`grid`) until ⊞ Align. `retile()` moves only windows whose cell changed
  (`_placed`), in parallel; the buttons pass `force=True`.
- **Window rediscovery:** `_discover_windows()` rebuilds `_term_windows` from `ps` (each ↗ App Ghostty process
  carries `claude attach '<id>'` on its command line), so a restart no longer orphans windows ("Aligned 0").
  A window of a finished session still matches and rides in the stack until closed.
- **Title colour chip (`CHIPS`, parallel to `PALETTE`):** Ghostty on macOS draws the title bar in the terminal
  background and has no separate title-bar colour (`window-titlebar-background` is GTK-only).
- **Terminal tab = one bar above the terminal** (`.termbar`, built in `renderDrawer`): hint, auto-yes, ↗ App,
  Yes/Go, Wrap, Stop, Delete, Exit, Reconnect. `#termbox` flexes to fill the drawer (`.bd:has(#termbox)`);
  never size it with `100vh - N` again, that is what hid the old bottom bar.
- **Terminals view (`#terms`, 2026-11-09):** the live terminals in the page, no terminal app. `renderStack()`
  draws one `.tbar` per running background session and a persistent `#stackbox` holding the single xterm
  (`mountTerm` now takes the box). Bars and box are ordered with CSS `order`, never moved in the DOM: moving
  the xterm node drops keyboard focus on every 4s refresh. A bar's innerHTML is rewritten only when it
  changed. The view is a flex column from `body` down (`body:has(#v-terms:not([hidden]))`), so the stack
  gets what the header leaves even when the header wraps to two rows at half-screen width. One xterm at a
  time, shared with the drawer: opening the drawer steals it, returning to the view remounts it.
  Switching bars was tested with injected fake sessions only (one real session was running).
- **Counts lag by design upstream:** card state comes from each job's `state.json`, which Claude Code writes
  after a turn ends; Overlook adds at most the 4s page poll (`claude agents` is cached 10s but only feeds
  pid/status).
- **⚠ A scratch server (`--port 7899`) shares `settings.json` and the same Ghostty windows with the live one.**
  Window-placement tests on a scratch port get undone by the live server re-tiling behind them. Test window
  code on the live server (merge, pull, `pkill`), or stop the live one first. Page-only changes are fine on a
  scratch port, and need no restart once pulled: `web/index.html` is read from disk per request.
- **⊞ Align (v0.8.1):** header button → `POST /api/align {cols, ids}`: sizes the dashboard's Chrome window
  (System Events, window named "Overlook") to the screen's visible frame and `retile()`s every open terminal
  window on demand — the layout POST only re-tiles on a CHANGE, so dragged windows never snapped back before.
- **Grid = the dashboard's grid (v0.8.0):** the page POSTs `/api/layout {cols, ids}` whenever its card
  grid changes (column count from `gridTemplateColumns`, ids in card order; debounced on resize), and
  `window_slot` gives a session's terminal window the cell its card has (rows = ceil(n/cols), capped 4;
  past the grid stacks on the last cell). A changed layout runs `retile()` over `_term_windows`, so
  the open windows follow the cards. Before any layout is posted the old lowest-free 2x2/3x2 +
  `settings.slots` scheme applies. `_place_window(pid, slot, tries)` is the shared mover.
- **Working dir (v0.8.0):** `transcript_info` keeps the latest tool path (`tool_dir`: file_path/path/
  cwd, or the last `cd /abs` in a Bash command) and `project_of` maps it to the project folder (child
  of an `OVERLOOK_ROOTS` root, else nearest `.git`). Served as `work`; the card shows `poker-history →
  overlook` when it differs from the launch cwd, the drawer header shows it, and the Ghostty/iTerm
  window title is `<name> · <project>`. Background jobs now read their transcript too (mtime-cached).
- **Everything that closes a session wraps first (v0.8.0):** `close_session` on a LIVE background
  session types `/wrape` (skipped if `settings.wrapped[id]` is <30 min old, e.g. the term tab just
  typed it), records it in `_pending_close`, returns **202 `{pending:"wrapping"}`**, and
  `pending_close_loop` (5s) `claude rm`s it once the daemon is gone — after 20 min still alive it
  stops then rms, so a Close is never silently dropped. Dead sessions rm at once (200). Interactive
  ones are refused (409). Cards show `closing` as a disabled "Closing — wrapping up first…" and hide
  the actions. `stop_session` does the same wrap-first dance (202) unless `{force:true}` — the UI's
  Stop button sends force on **Shift/Alt-click** — or the session is dead/just wrapped (<10 min).
  ⏻ Exit on the term tab is now: type `/wrape` into the live xterm, then `/close`; `finishExit` is gone,
  `refresh()` just drops the drawer and toasts when the card disappears. `close-finished` returns
  `closed` / `wrapping` / `failed`.
- **URL = state (v0.7.6):** `#status`, `#hub`, `#s/<id>/<tab>`, `#a/<agent>`; `syncHash()` on
  open/close/tab, `openFromHash()` after the first sessions load. `replaceState`, not `location.hash=`,
  so Back isn't flooded.
  `window_slot` hands out grid cells, remembered in `settings.slots[id]`, freed when the session
  dies. `screen_frame()` reads `NSScreen.visibleFrame` via JXA (0.25s, cached 60s), flipped to a
  top-left origin because AppKit's origin is bottom-left and the terminals' isn't.
- **⏻ Exit on the term tab (v0.7.3):** client-side only. `termExit(id)` sends the wrap (websocket,
  else `/wrap`), puts the id in `EXITING`, and `refresh()` calls `finishExit` → `/close` the first
  time it sees the session not alive. A 5s interval keeps polling while anything is exiting, since
  the normal 4s refresh pauses when the tab is hidden. Lost on page reload (it's a JS map).
- **Close (`POST /api/sessions/<id>/close`, v0.7):** `is_finished()` = not alive, OR alive in a
  done/idle/failed state (a `--bg` daemon keeps its pid after the work ends, so "dead cards" are
  almost always alive=true). Finished-but-alive gets `claude stop` first, then `claude rm`; a
  working/blocked one is refused with 409. `POST /api/sessions/close-finished` loops it over every
  finished session (the "✕ Close finished" button). `claude rm` itself then forgets names/colors/slots/wrapped/autoYes for it. `rm` itself
  refuses when the worktree has unpushed commits; its message is passed through as the error.
  `settings.wrapped[id]` is set by `/wrap` and by the terminal tab (`/wrapped`), only to style
  Close as primary once the session exits. The terminal tab is not re-rendered on refresh (that
  would kill the xterm), so refresh() toggles `#termclose` in place.
- **Rename (`POST /api/sessions/<id>/rename`, v0.7):** `settings.names[id]` overrides `name` in
  `sessions()` (`renamed: true`). Not pushed into the CLI: `state.json` is daemon-owned and
  `/rename` via pty would cost an attach per edit.
- **Wrap & exit on the term tab (v0.7):** `termWrap()` writes `/wrape ` then, 400ms later, `\r`
  over the live websocket (no second attach). Bound to Ctrl+Shift+W via
  `attachCustomKeyEventHandler`; Cmd+Shift+W is Chrome's close-window, so not that.
- **Terminal input mid-panel (v0.7):** `.xt` is `--term-h` (54%) of `#termbox`, top-anchored.
  Claude draws its input box at the bottom of its screen, so a half-height screen puts the
  typing line at the middle of the panel. The lower half is deliberately empty.
- **Auto-yes:** opt-in per session, stuck 20s+, 90s between answers, max 10/hour.
  It accepts permission prompts with "don't ask again", so treat it as a real risk surface.
- **Progress:** `parse_progress` regexes the status line for fractions, percent, ETAs and
  `N (+M)`. If there is a fraction and no ETA, the rate comes from `timeline.jsonl`.
- **Terminal tab (v0.6):** `GET /ws/term/<id>` is a hand-rolled WebSocket (handshake,
  framing, ping/pong) in `Handler._ws_terminal`; `spawn_terminal` runs `claude attach <id>`
  on a pty (own session + controlling tty so ^C/^Z work). Binary frames = pty bytes both
  ways; text frames are JSON `{"resize":[cols,rows]}` / `{"input":...}` / `{"exit":code}`.
  Closing the socket SIGHUPs (then SIGKILLs) only the attach client. xterm.js + fit addon
  come from jsdelivr (`@xterm/xterm@5.5.0`, `@xterm/addon-fit@0.10.0`) — offline, the tab
  says so and ↗ App still works. The xterm object lives in a global `TERM` and survives
  drawer re-renders (refresh only re-renders the overview tab).
- **CLAUDE_CONFIG_DIR per job:** three config dirs here hold `jobs/`. `claude_env(jid)`
  points attach/logs/stop at the dir that owns the job; before this, attach of a
  `.claude-home` job said "No job matching" because the env picked `.claude-nit`.
- **↗ App:** new Ghostty/iTerm/Terminal window running `claude attach`; terminal sessions
  only get the app brought forward (`claude attach` refuses interactive session ids).
- **`--app`:** opens the dashboard as a Chrome app window (`open -na "Google Chrome" --args --app=URL`).
- **Security:** Host check on all requests; Origin/Sec-Fetch and JSON-only on POST.

## Verified vs not

Verified: API routes, cross-origin refusal, real launch, Yes on a question menu and on a
plain prompt, parser on sample strings, Ghostty launching a command. **v0.6: the UI was
viewed in a real browser (Playwright):** status list and the session drawer render, the
Terminal tab attached to a live background job, showed its full screen, and Ctrl+Z
detached cleanly with no `claude attach` process left behind; a foreign Origin on the
WebSocket gets 403.

Not verified:
- Blink, countdown, Hub drawer and launch form in a browser (only Status + session drawer were looked at).
- Typing into the web terminal from a real keyboard (bytes were sent programmatically).
- ↗ App on a real session.
- The permission-prompt path (down arrow + Enter, chooses option 2).
- Auto-yes loop firing on a real stuck session.
- Per-agent/launch `--model`: one test launched with `--model haiku` in plan mode showed
  Sonnet 5.5 in its status bar. Cause unknown; check whether the flag is honored.

No automated tests exist.

## Known loose ends

- README Status paragraph still mentions logs and attach commands; the Logs tab and copy
  buttons are gone from the session panel. `/api/sessions/<id>/logs` still exists.
- Dead code: `data-copy` click handler, `.summary`/`.stat`/`.chips` CSS.
- Yes/Stop are background-only. Answering a terminal session has no mechanism yet.
- `OVERLOOK_ROOTS` defaults to `~/Dropbox/development`, which is personal to this machine.
- Each test session costs usage and appears in the owner's list; name it obviously and
  `claude rm` it afterwards.

## Owner's preferences for this tool

- At most about 5 big buttons on any screen.
- Every action is one click; never "copy this and run it".
- Show any ETA or counter available.
- No full test suites or heavy builds on the laptop.
