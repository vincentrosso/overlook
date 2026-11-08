# Overlook handoff

Local web dashboard for Claude Code agents, built as a Conductor replacement.
v0.7.0, branch `main`, public repo `vincentrosso/overlook`.

## Layout

- `overlook.py`: stdlib-only server, binds 127.0.0.1:7878. Run `python3 overlook.py --open`.
- `web/index.html`: the whole UI (vanilla JS, polls every 4s).
- `scripts/install-launchd.sh`: run at login (`--uninstall` removes it).
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
