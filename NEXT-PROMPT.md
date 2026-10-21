# Next prompt: Gateway

**Built and cut over 2026-10-17/18 (DONE).** Every model call on this Mac goes through `~/.gateway`; `DESIGN.md` "As built"
has the layout, the call-site table, the rules a later edit could break and what differs from the design.

## When to fire this

Earliest-useful dates, not deadlines; being late costs nothing (a broken call shows up as a failed job or HUD fallback,
and Jarvis alerts `gateway-unreachable` when the service is down).

| When | Step | Waits on |
|---|---|---|
| DONE 2026-10-18 11:50 | first-night check for the 10-18 night: all Claude jobs ran with `ok=1` through Gateway, no surface or CLI errors. Open: the weekly `self-improve` row (10-18 06:15, `ok=0`, model `none`) is not yet traced; reminder `aac1d5` (2026-10-19) rechecks the 10-19 night | the 10-18 night |
| 2026-10-21 | install Ollama, then set `[local] model`, `enabled = true` and a surface to `"shadow"` in `config.toml` here (no longer Jarvis's `[local_llm]`) | Jarvis's Ollama row (TRIAGE-MODEL-DESIGN.md) |
| DONE 2026-10-18 | Jarvis's copies of `state/local-llm-perf.json(l)` deleted (this copy is the one kept) | Anze's go-ahead |

First-night check, read-only:
- `sqlite3 ~/.budget/state/budget.db "SELECT datetime(ts,'unixepoch','localtime'), job, ok, model, detail FROM job_calls WHERE ts > strftime('%s','2026-10-18') ORDER BY ts"`:
  hud-audit-nightly, portfolio-advisor-*, nightly-reflection (and the Mind steps) ran with their usual models and `ok = 1`.
- No job log under `~/.jarvis/logs/` says `is not a Claude surface` or `claude CLI not found`.
- `~/.mind/state/sleep/` last report: `session-history`, `memory-capture`, `vault-capture` steps ok.
- `provider_calls` rows since the restart carry the new surface names (`escalation`, `inbound-*`, `report-narrate`, `hud-*`),
  none under `work`.
- `~/.gateway/bin/install-launchd status` running; `curl -s http://127.0.0.1:3027/health`.

## Audit 2026-10-21: your items

From the Jarvis audit (HUD task audit-jarvis-and-all-its-com-e5a9). The evidence, effort, risk and
done-criteria of each item are in `~/.jarvis/AUDIT-2026-10-21.md`; the phases and Anze's decisions
are in `~/.jarvis/NEXT-PROMPT.md`. Build an item only after Anze approves its phase. Prompt A below
covers these rows too. The rule stays: this component measures or knows; Jarvis alerts and acts.

- **A05 (Phase 0, do first)** `gateway/agent.py` `command()` still does not pass
  `--strict-mcp-config` (checked 2026-10-21). Jarvis's own jobs add it through
  `jobcommon.MCP_OFF` and Analyst's through `JOB_FLAGS`, but Mind's `agentjob.run_claude_job`
  passes no flags, so nightly-reflection loads every claude.ai connector and thrashes on
  autocompact. Add the flag in `command()` for job sessions. Done: nightly-reflection's log no
  longer warns about claude.ai MCP servers, and a run's first turn is under 30k tokens.
- **A30 (Phase 3)** hud-audit-nightly hits `error_max_budget_usd` in 23 of 71 runs ($24.88 of its
  $46.41 in 14 days). The cap is set by the caller; Gateway should report cap-hits per surface so the
  caller can be fixed, and a capped run should keep its partial output.
- **A36 (Phase 3)** The local shadow has no data, and the surfaces it would
  replace cost about $0.31 in 14 days. Decided 2026-10-21 (D10): finish it. **Step 1 done 2026-10-21:**
  Ollama 0.40.7 runs (brew service, 127.0.0.1) and the bench is in `tools/local-bench/` (RESULTS.md);
  qwen3.5:4b picked; `[local]` enabled and `inbound-critical`/`briefing-triage` set to shadow in the branch config (live after merge); then at least 2 weeks of shadow before any surface flips to `local` (the gate in
  `~/.jarvis/NEXT-PROMPT.md`).
- **A42 (Phase 4)** `~/.analyst/analyst/common.py` and `~/.mind/mind/agentjob.py` keep their own
  copies of `run_claude_job`; fold them into Gateway's client.

| When (earliest useful, not a deadline) | Step | Waits on |
|---|---|---|
| with Phase 0 | A05: `--strict-mcp-config` in `command()` | Phase 0 approval |
| after Anze picks a model | A36: set `[local]`/`[surfaces]` shadow (design step 2) | Anze's model pick from `tools/local-bench/RESULTS.md` |
| with Phase 3 | A30 cap-hit report | Phase 3 approval |

## A: advance the work

> Read `~/.gateway/DESIGN.md` ("As built") and this file, then do the first row of the table above whose date has come and
> that is not done. Gateway never alerts, pings or files; brain keeps its caps and breaker; brain's isolated argv must stay
> byte-identical (G15). A model change is a `[surfaces]` edit, never a caller edit. Tests:
> `~/.gateway/.venv/bin/python ~/.gateway/tests/test_gateway.py` and Jarvis's selftest. Stop and tell me if anything differs
> from what the design expects.

## B: status only

> Read `~/.gateway/DESIGN.md` and this file, run the first-night check list above and `gateway surfaces`, and report. Change nothing.

## C: something is broken

> A model call broke after the Gateway cutover. First, do not delete `gateway.db`, `budget.db`, a state file or a log.
> Run `~/.jarvis/.venv/bin/python ~/.jarvis/status.py`, `curl -s http://127.0.0.1:3027/health`,
> `tail -40 ~/.gateway/state/logs/server.log`, the failing job's log, and `gateway surfaces`; report before fixing. Rolling
> back is `git revert` of the cutover commits in `~/.jarvis` (6325365, b1fae1f), `~/.mind` (dd0c8e7) and `~/.forge` (f29a727)
> plus `~/.jarvis/restart.sh`.
