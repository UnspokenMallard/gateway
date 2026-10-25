# Next prompt: Gateway

**Built and cut over 2026-10-17/18 (DONE).** Every model call on this Mac goes through `~/.gateway`; `DESIGN.md` "As built"
has the layout, the call-site table, the rules a later edit could break and what differs from the design. The local model
was removed 2026-10-24; every surface runs on Claude.

## When to fire this

Earliest-useful dates, not deadlines; being late costs nothing (a broken call shows up as a failed job or HUD fallback,
and Jarvis alerts `gateway-unreachable` when the service is down).

| When | Step | Waits on |
|---|---|---|
| DONE 2026-10-18 11:50 | first-night check for the 10-18 night: all Claude jobs ran with `ok=1` through Gateway, no surface or CLI errors. Open: the weekly `self-improve` row (10-18 06:15, `ok=0`, model `none`) is not yet traced; reminder `aac1d5` (2026-10-19) rechecks the 10-19 night | the 10-18 night |
| with Phase 0 | A05: `--strict-mcp-config` in `command()` | Phase 0 approval |
| with Phase 3 | A30 cap-hit report | Phase 3 approval |

First-night check, read-only (historical; kept as a template for a new cutover):
- `sqlite3 ~/.budget/state/budget.db "SELECT datetime(ts,'unixepoch','localtime'), job, ok, model, detail FROM job_calls WHERE ts > strftime('%s','2026-10-18') ORDER BY ts"`:
  hud-audit-nightly, portfolio-advisor-*, nightly-reflection (and the Mind steps) ran with their usual models and `ok = 1`.
- No job log under `~/.jarvis/logs/` says `is not a Claude surface` or `claude CLI not found`.
- `~/.mind/state/sleep/` last report: `session-history`, `memory-capture`, `vault-capture` steps ok.
- `~/.gateway/bin/install-launchd status` running; `curl -s http://127.0.0.1:3027/health`.

## Audit 2026-10-21: your items

From the Jarvis audit (HUD task audit-jarvis-and-all-its-com-e5a9). The evidence, effort, risk and
done-criteria of each item are in `~/.jarvis/AUDIT-2026-10-21.md`; the phases and Anze's decisions
are in `~/.jarvis/NEXT-PROMPT.md`. Build an item only after Anze approves its phase. Prompt A below
covers these rows too. The rule stays: this component measures or knows; Jarvis alerts and acts.

- **A05 (Phase 0, do first)** `bifrost/agent.py` `command()` still does not pass
  `--strict-mcp-config` (checked 2026-10-21). Jarvis's own jobs add it through
  `jobcommon.MCP_OFF` and Analyst's through `JOB_FLAGS`, but Mind's `agentjob.run_claude_job`
  passes no flags, so nightly-reflection loads every claude.ai connector and thrashes on
  autocompact. Add the flag in `command()` for job sessions. Done: nightly-reflection's log no
  longer warns about claude.ai MCP servers, and a run's first turn is under 30k tokens.
  (Check first: `tests/test_bifrost.py` `test_job_session_strict_mcp` asserts the flag and passes 2026-10-24.)
- **A30 (Phase 3)** hud-audit-nightly hits `error_max_budget_usd` in 23 of 71 runs ($24.88 of its
  $46.41 in 14 days). The cap is set by the caller; Gateway should report cap-hits per surface so the
  caller can be fixed, and a capped run should keep its partial output.
- **A42 (Phase 4)** `~/.analyst/odin/common.py` and `~/.mind/mimir/agentjob.py` keep their own
  copies of `run_claude_job`; fold them into Gateway's client.

**Status 2026-10-25 (HUD task freyr-398c, merge with Jarvis):** A37 (a6ceb4f): `job:weekly-activity` routes to `claude:sonnet` again; Haiku failed 2 of 4 runs on tool denials.

| When (earliest useful, not a deadline) | Step | Waits on |
|---|---|---|
| 2026-10-26 after 07:45 | weekly-activity ran on Sonnet and exited 0 | the weekly run |
| with Phase 0 | A05: `--strict-mcp-config` in `command()` (check first, see above) | Phase 0 approval |
| with Phase 3 | A30 cap-hit report | Phase 3 approval |

## Sessions switch: tmux or ACP (built 2026-10-25, HUD task njord-ed21)

Bifrost now runs long-lived Claude sessions two ways, chosen per kind in `config.toml` `[sessions.kinds]`: the TUI in tmux
(default, the HUD's old way) or the claude-agent-acp adapter (ACP). One or the other for a session, never both; the first
start pins the runner in `state/sessions.json`. `DESIGN.md` "Sessions" has the layout and routes. The HUD side is in
`~/.jarvis/hud` (`lib/sessions.js`, kind `hud-task`; README "Two runners"). Every kind is still `tmux`: nothing changes
until you flip one. Tests: `~/.gateway/.venv/bin/python ~/.gateway/tests/test_sessions.py`; HUD `node --test test/sessions.test.js`.
Both repos' branches are `hud/njord-ed21`; they reach `main` / `master` only when Anze says Done.

| When (earliest useful, not a deadline) | Step | Waits on |
|---|---|---|
| after Done of `njord-ed21` | `cd ~/.gateway/acp && npm install --ignore-scripts` (adapter 0.95.0, pinned in `package.json`), `bin/install-launchd`, restart the HUD; check `curl -s http://127.0.0.1:3027/sessions` shows `switch.kinds` empty | the merge |
| 2026-11-01 | re-read Anthropic help article 15036540 (2026-10-07: Agent SDK and `claude -p` still on the subscription, the June split paused). Do not flip a kind if that changed | a month since the last check |
| after the check above | one throwaway HUD task with `hud-task = "acp"`: a real prompt turn, a permission prompt, an `AskUserQuestion` form (including a typed-only answer), `/clear`, a `send` while busy, mode names, a Gateway restart then resume. Needs Anze's OK: it spends a real turn | billing check, Anze |
| after a clean trial | wire what the HUD skips for ACP: mode cycling, the preview window (`task.tmux:preview` breaks for ACP tasks), the orchestrator's `quiet_for` stall watchdog | the trial |

Verified 2026-10-25 only against `tests/fake_acp.py` and a real adapter handshake; no real model prompt went through the adapter.

## A: advance the work

> Read `~/.gateway/DESIGN.md` ("As built") and this file, then do the first row of the table above whose date has come and
> that is not done. Gateway never alerts, pings or files; brain keeps its caps and breaker; brain's isolated argv must stay
> byte-identical (G15). A model change is a `[surfaces]` edit, never a caller edit. Tests:
> `~/.gateway/.venv/bin/python ~/.gateway/tests/test_bifrost.py` and Jarvis's selftest. Stop and tell me if anything differs
> from what the design expects.

## B: status only

> Read `~/.gateway/DESIGN.md` and this file, run `gateway surfaces`, and report. Change nothing.

## C: something is broken

> A model call broke after the Gateway cutover. First, do not delete a state file, a log or an archived `*.db.archived-2026-10-24`.
> Run `~/.jarvis/.venv/bin/python ~/.jarvis/status.py`, `curl -s http://127.0.0.1:3027/health`,
> `tail -40 ~/.gateway/state/logs/server.log`, the failing job's log, and `gateway surfaces`; report before fixing. Rolling
> back is `git revert` of the cutover commits in `~/.jarvis` (6325365, b1fae1f), `~/.mind` (dd0c8e7) and `~/.forge` (f29a727)
> plus `~/.jarvis/restart.sh`.

## Sessions switch prompts

**A-S, advance.** > Read `~/.gateway/DESIGN.md` "Sessions" and the "Sessions switch" table in this file, then do the first row
that has come due and is not done. Never flip a kind to `acp` before the billing re-check and my OK. Existing tmux tasks and my
own tmux sessions stay untouched; the default stays `tmux`. Stop and tell me if anything differs from what the table expects.

**B-S, status only.** > Read the same two places, run `curl -s http://127.0.0.1:3027/sessions`, and report the switch and the
live sessions. Change nothing.

**C-S, broken.** > An ACP task misbehaves. Do not `forget` the session or delete `state/sessions.json` first: they
hold the runner pin and the resumable session id. Read `curl -s http://127.0.0.1:3027/session/<id>`, `tail -40
~/.gateway/state/logs/server.log` and the task in the HUD; report before fixing. The way back is setting the kind to `tmux`
in `config.toml`: new tasks use tmux, a pinned ACP task keeps its runner until it is forgotten.
