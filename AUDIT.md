# Gateway (Bifrost) audit, 2026-11-01

Auditor: Njord (HUD task njord-93db, branch `hud/njord-93db`). Scope: `~/.gateway`, nothing else. Real model calls: 2 (`hud-whereami` on the personal and on the work account), cost about $0.0007 in total.

## What I checked

- Read `bifrost/` (`claude.py`, `agent.py`, `proc.py`, `store.py`, `_budget.py`, `cli.py`, `sessions/*`), `server/app.py`, `config.toml`, `DESIGN.md`, `NEXT-PROMPT.md`, the 2026-10-27 audit and the tests.
- Surfaces: all 32 in `config.toml` resolve to a model, including the new `hud-goal-plan` (Sonnet), `hud-goal-check` (Haiku) and `hud-finding-triage` (Haiku). Only `hud-*` surfaces are served over HTTP.
- `agent.command()` and `claude.command()` both pass `--strict-mcp-config`. The item "Mind's jobs may lack it" is not a fault: nightly-reflection costs about $0.12 a run, so it is in effect.
- Account selection: a live `hud-whereami` call on the personal scope and on the work scope both succeeded.
- Usage-limit handling and rate-limit deferral: traced `claude.call` and `agent.run_agent` against Fafnir's `limits.detect/record/note_success`, and compared the real CLI output of a limit hit with what Bifrost recorded (faults F2 below).
- Timeouts: `ask` has a 300 s default, `/ask` 90 s, but `run_agent` had none (F1).
- Merge notices ("Bifrost carries ... is merged"): they are produced by the HUD (`~/.jarvis/hud/lib/persona.js`, `OUTCOMES.merged`, fired by `onStatusChange`), not by Gateway. Of 127 such records none has an identical detail; each carries a different SHA. The "repeats" are god names reused from a pool of chat names, not duplicate notices. The notice for the still-running chat (Sol) fires at Done-merge time, before the tmux session is swept (grace in `tmuxsweep.js`). That is HUD code, so I only report it.
- Tests: `tests/run.py` gave 20 passed at the start and 25 passed now (5 new). `~/.jarvis/jarvis.sh doctor`: all components pass; gateway at c60da6e, :3027 http 200; archivist and scout "never run".

## Faults found and fixed (one commit each)

| # | Severity | Fault | Commit |
|---|---|---|---|
| F3 | low | `tmux.send` of a one-line message starting with `-` failed (`send-keys` took it as a flag, "invalid flag -"). Fix: `--` before the text. Test added. | f972d0b |
| F5 | low | `/ask` parsed `timeout_s` outside the `try`: a non-numeric value gave a 500. Now 400, and a non-positive value is refused. Test added. | 49d7f31 |
| F2 | medium | A usage-limit hit was recorded with the raw JSON line as evidence and `exit 1: {"duration_api_ms":0,"stop_reason":"stop_s…` as the failure reason. 133 `hud-whereami` rows (and a few other surfaces) over 14 days read this way, so the real cause ("You've hit your 5-hour limit") was invisible. Now the evidence and the reason carry the CLI `result` text (`usage_limit_reached: You've hit your 5-hour limit ...`); a limit is also detected when the CLI exits 0 with `is_error`; `note_success` runs only when the call is ok (it used to clear a limit window after a failed call); the result parse takes the last `{"type":"result"}` line, so a `{` in an earlier warning line cannot break it. Test added. | 23f1931 |
| F1 | medium | `run_agent` had no default timeout. Jarvis `jobcommon`, Mind `agentjob` and Analyst `common` call it without one, so a hung `claude -p` job would block for ever. Default is now 1800 s (the longest normal job is about 842 s; the implementer passes its own). An explicit value, including `None`, still wins. Test added. | 21575b4 |
| F4 | low | A malformed `config.toml` raised `TOMLDecodeError` through every `ask` and `/health`, against the "never raises" contract. `load_config` now logs one warning and returns the last good config (`{}` if none). Test added. | af37474 |

## Faults found, not fixed

- **Stale server (needs Anze).** The running server (pid 83967, started 2026-10-28 09:58) predates Fafnir's detector fix d1161c6 (2026-10-29, "Detect JSON-quoted api_error_status 429") and Gateway's d7d2cf0 (dead tmux session before start/resume). It also will not have the five commits above. Until it restarts (`bin/install-launchd`), the HUD keeps getting undetected limit hits and the old garbage reasons. I did not restart it: it is your call. Every session kind is tmux today, so a restart ends no ACP session.
- **Merge-notice wording and timing (HUD code).** `persona.js` `post()` does no key dedupe, and "is merged" can arrive while the tmux session of that chat is still alive (until the sweep grace passes). Reported for the HUD audit; not Gateway.

## Suggested improvements (ranked, none implemented)

1. **Show the running code version in `/health`** (git HEAD at start next to current HEAD) and have `jarvis.sh doctor` warn on a mismatch. A stale server hid two fixes for days.
2. **Fail fast on a known limit.** Gateway never checks `limits.blocked/should_defer` before spawning `claude`, so every call during a limit window starts a process and fails one by one. Check first and return the limit with no spawn.
3. **`tmux.count()` counts every session on the shared socket** (9 now against `max=10`), so moving the HUD to Gateway-started tmux sessions would block after one new chat. Count only registered sessions.
4. **Pass `--` before the positional initial prompt in the tmux `argv`** (a prompt starting with `-` may be read as a flag; untested).
5. **Cap and report per surface.** `hud-audit-nightly` keeps hitting the cap (A30); report cap-hits per surface. Put the surface name on every telemetry row (open since 2026-10-27, finding 1).
6. **`hud-whereami` volume** is about 107 calls a day. A cache or rate cap in the HUD would cut it.
7. **`proposer`** has 11 of 28 failures with an empty reason; with F2 the reason is filled in now, so re-check after the restart.
8. **ACP `request()` leaves entries in `calls` after a timeout** (small leak), and `sessions.start` holds `_starting` for the whole adapter handshake (30 s or more).
9. **Merge the duplicate `run_claude_job` copies** in Analyst and Mind (A42) into `bifrost.run_agent`.
10. **`gateway doctor` command**: every surface with last call, failure rate, 30-day cost, plus the server-version check from item 1.
