# Next prompt: Gateway

Designed 2026-10-17 (`~/.gateway/DESIGN.md`, decisions at its top); not built yet. One component
build at a time: each edits `~/.jarvis/config.toml`, the selftest and the daemon. Before 2026-10-21 if possible: Ollama goes in then, and every local surface should come through Gateway.

## When to fire this

Earliest-useful dates, not deadlines; being late costs nothing (Jarvis keeps running as is).

| When | Step | Waits on |
|---|---|---|
| when now (no other component build is running) | build Gateway from its DESIGN.md | the previous build's cutover |

## A: advance the work

> Build Gateway. Read `~/.gateway/DESIGN.md`, `~/.jarvis/NEXT-PROMPT.md` (load-bearing rules), and the
> "As built" sections of `~/.budget`, `~/.sentinel`, `~/.herald` and `~/.forge` DESIGN.md: same layout,
> same cutover discipline. Work in the main checkouts, not a worktree (the cutover restarts the live
> daemon: `~/.jarvis/restart.sh` only; the HUD with `launchctl kickstart -k gui/$(id -u)/com.jarvis.hud`).
> Rules: Gateway never alerts, pings or files; Jarvis triggers it and acts. No behavior change. Back up
> `jarvis.db` with `sqlite3 .backup` before moving any table; copy rows, never drop a table or delete a
> backup without Anze's confirmation. Redirect its data in `selftest.py` before anything imports it.
> Selftest, HUD tests and every component's tests pass before and after. Ask Anze the design's open
> questions first (AskUserQuestion, one batch). When done: an "As built" section in DESIGN.md, this file
> marked DONE with its first-night check row, a dated section in `~/.jarvis/JOURNAL.md`; stop and tell
> me if anything differs from the design.

## B: status only

> Read `~/.gateway/DESIGN.md` and this file and report whether Gateway is built. Change nothing.

## C: something is broken

> Gateway or the daemon broke during the cutover. First, do not delete `jarvis.db`, a backup or a
> state file. Run `~/.jarvis/.venv/bin/python ~/.jarvis/status.py` and the selftest, read the daemon
> log, and report before fixing.
