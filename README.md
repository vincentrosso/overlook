# Overlook

A local web dashboard for **Claude Code background agents**: one board showing
what every agent is doing right now, and a searchable hub of every agent
definition on your machine, with one-click launch.

No dependencies. One Python file and one HTML file. Runs on `127.0.0.1` only.

```bash
git clone https://github.com/vincentrosso/overlook && cd overlook
python3 overlook.py --open        # http://127.0.0.1:7878
```

Run it at login on macOS: `scripts/install-launchd.sh` (undo with `--uninstall`).

## What it does

**Status.** Every live Claude Code session, background (`claude --bg`) or a plain `claude` in a terminal: state (working / needs input / done /
failed), its latest one-line status, the original ask, folder, tokens, pid,
and when it last moved. Click a card for the full timeline, recent logs,
attach/logs commands, and a Stop button. Refreshes every 4 seconds.

**Agent Hub.** Every agent Claude Code can use, in one place: your user
agents (`~/.claude/agents`), project agents (`.claude/agents` in any folder
you've run sessions in), plugin agents, and the built-ins. Search by text and
filter by source, model or plugin. Open a card to read its description and
full system prompt, then **Launch** it into any folder with a task, a
permission mode and an optional model override.

## One click for everything

- **✔ Yes / Go** on any session that needs you. Overlook reads its screen and
  picks the right answer: permission prompt → "Yes, and don't ask again";
  question menu → first option; plain "your turn" → types `yes, go ahead`.
- **Auto-yes** (per session, off by default): answers for you after it has been
  stuck 20s, at most 10 times an hour.
- **Open** puts the live session in a new terminal window (Ghostty, iTerm or
  Terminal; override with `OVERLOOK_TERMINAL`).
- Needs-you cards blink, the tab title and icon show a count, and 🔔 Alerts
  adds a desktop notification + chime.
- Progress counters (`165/1198`), `ETA ~21:50 PT` and `2,407 (+266)` in a
  status line become a bar, a live countdown and a big number. With a counter
  but no stated ETA, Overlook estimates one from the job's own timeline.
- Per-agent default model, chosen in the Hub (saved in
  `~/.config/overlook/settings.json`, never in the agent file).

Background sessions only for Yes/Open/Stop: an interactive `claude` in a
terminal can't be attached, so its card shows status and a "Go to terminal".

## How it works

It reads what Claude Code already writes to disk; nothing to configure.

| Data | Source |
|---|---|
| Background session state, status line, ask, tokens | `$CLAUDE_CONFIG_DIR/jobs/*/state.json` |
| Interactive (terminal) sessions | `sessions/<pid>.json` registry (pid checked alive) + tail of `projects/*/<sessionId>.jsonl` for title, last ask, last reply |
| Timeline | `jobs/*/timeline.jsonl` |
| Liveness (pid) | `claude agents --json --all` (cached 10s) |
| Logs / stop / launch | `claude logs`, `claude stop`, `claude --bg --agent …` |
| Agents | `agents/*.md` frontmatter in user, project and plugin dirs |

## Config

| Env var | Default | |
|---|---|---|
| `OVERLOOK_PORT` | `7878` | |
| `CLAUDE_CONFIG_DIR` | `~/.claude` | also checks `~/.claude-home` |
| `OVERLOOK_ROOTS` | `~/Dropbox/development` | colon-separated; git repos inside are offered in the launch folder picker |
| `OVERLOOK_CLAUDE` | `claude` on PATH | path to the CLI |

## Security

Overlook can start agents, so it only answers requests addressed to
`127.0.0.1`/`localhost` (blocks DNS rebinding), refuses cross-origin POSTs
(no web page can launch an agent behind your back), and accepts JSON only.
Don't put it behind a public proxy.

Launching into a folder Claude Code hasn't trusted yet fails with the CLI's
own message; run `claude` there once and accept the trust prompt.

## License

MIT
