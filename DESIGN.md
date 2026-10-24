# Bifrost (Gateway)

Every model call on this Mac goes through one place: which model answers, under which account,
inside which budget, recorded where, and what happens on a usage limit or a failure. Designed
2026-10-17, after Mimir, Fafnir, Heimdall, Hermod and Brokkr were split out of Jarvis.

## Decisions (Anze, 2026-10-17)

| Question | Decision |
|---|---|
| Home | `~/.gateway`, own git repo, code and `state/` together, own venv (like the others) |
| Routing | Bifrost decides, per *surface*, from its config: `off`, `claude:haiku`, `claude:sonnet`, ... A caller names its surface and its task, never a model |
| Process | (following the earlier rule) library for Python callers; an HTTP service on `127.0.0.1:3027` (launchd `com.gateway.server`) for the HUD, which is Node |

## Why

About 20 places start `claude -p` themselves (Jarvis's brain and job scripts, the HUD's three Haiku
calls, Mimir's session summaries, drafts and improve evals, Brokkr's implementer and criteria scoring,
the check-ups). Each repeats the account selection, the Fafnir gate, the cost parsing, the ledger
row and the usage-limit check. Bifrost does these once.

## Boundary

Bifrost **makes the call it is asked for and reports what happened**. Fafnir stays the ledger and
the gate ("may I spend": Bifrost asks it before every Claude call and records every call there).
Jarvis, Mimir, Brokkr and the HUD stay the deciders of *what* to ask and what to do with the answer.
Bifrost never alerts, pings or files anything: a usage limit is recorded in Fafnir's window and
returned to the caller, and Jarvis raises the alert as today (`core.record_limit` splits: Fafnir
records, Jarvis alerts).

## Two kinds of call

1. **`ask(surface, prompt, schema=None, scope=None)`**: a small judgment. No tools, no session
   files. `off` returns None (the caller does what it did before). `claude:<model>` runs Claude
   only: `--tools ''`, `--setting-sources ''`, no MCP, JSON output. Returns
   `{answer, model, cost_usd, duration_s, limited}`; never raises.
2. **`run_agent(job, prompt, tools, allow, model, budget_usd, cwd=None, scope=None, optional=False)`**:
   a tool-using `claude -p` session (today `jobcommon.run_claude_job`): `dontAsk` permissions, the
   explicit tool list and allow rules, the shared deny list (secrets, credentials, transcripts), the
   per-run cap, the Fafnir gate before every attempt (optional jobs stop at 80% of the week or the
   daily cap), retries only on transient network errors, the result parsed and recorded as a job run.

Both pick the account (`personal` or `work`, the Claude config dir and its environment) from the
caller's scope; the scope rules (`daemon/scope.py`: which repo is work) stay in Jarvis and are passed in.

Both also take `auth` (2026-10-23): `None` (the environment as it is), `login` (the subscription: `ANTHROPIC_API_KEY`
and `ANTHROPIC_AUTH_TOKEN` are stripped, since `claude -p` prefers an API key over OAuth). There is no API-key mode
(removed 2026-10-23): Bifrost never reads a Console key. Used by Mimir's Proposer.

## What moved here

| From | Here |
|---|---|
| `scripts/jobcommon.py` `run_claude_job`, `claude_exe`, `JOB_DENY_TOOLS`, the account environment (`claude_scope`) | `bifrost/agent.py`; `jobcommon` keeps the job lock, logs and artifact checks and calls Bifrost |
| brain's `claude -p` call (`brain.py` `_call`) | `ask("escalation")` and its tiers; brain keeps its caps, breaker and `brain_calls` |
| HUD `whereami.js`, `merge.js` (commit message), `tasks.js` (classify) | `POST /ask` with surfaces `hud-whereami`, `hud-commit-message`, `hud-task-classify` |
| Mimir's `session-history`, `memory-capture`, `vault-capture`, `improve` calls | `ask` / `run_agent` with their own surfaces |
| Brokkr's implementer and criteria scoring | `run_agent` and `ask` |

Each surface keeps its Claude model, so the move changed no behavior. Changing a surface's model is a
one-line edit in `config.toml`, read at call time.

## Interface

- Library `gateway` (stdlib only, like Fafnir's), `ask()`, `run_agent()`, `mode()`.
- Service: `GET /health`, `POST /ask` (HUD), `GET /surfaces`. Same guard as Fafnir's: loopback hosts only,
  writes refused when they carry an `Origin` header.
- CLI `gateway ask <surface> <prompt>`, `gateway surfaces`.

## As built (2026-10-17/18)

Built and cut over in one session (Anze: "everything in one session"). Anze's build-time answers: the HUD keeps its
existing fallbacks when the service is down and Jarvis alerts `gateway-unreachable`; job models come from Bifrost's config,
one surface per job; Bifrost takes brain's whole call (argv, run, parse, limit record, ledger row) while brain keeps its caps,
breaker and `brain_calls`.

Layout as Fafnir's: `bifrost/` library (paths from `BIFROST_HOME`, default `~/.gateway`; tests set it to scratch):
`store.py` (paths and `config.toml`; no database), `claude.py` (`command`, `call`, `ask`, accounts, surfaces), `agent.py`
(`run_agent`, the shared deny list, `command`, `RUN`), `proc.py` (the file-backed, tree-killing process runner, copied from
`core._run_process_blocking`), `cli.py` behind `bin/gateway`, `server/app.py` (launchd `com.gateway.server`,
`bin/install-launchd [status]`, log `state/logs/server.log`), `config.toml`, `tests/test_bifrost.py`. Own venv (starlette,
uvicorn, httpx for the tests).

### Call sites and their surfaces

| Caller | Surface | Model | Kind |
|---|---|---|---|
| Jarvis `daemon/inbound.py` Asana task triage | `inbound-task-triage` | haiku | `brain.work` |
| Jarvis `daemon/briefing.py` mail and chat triage | `inbound-message-triage` | haiku | `brain.work` |
| Jarvis `daemon/briefing.py` reply drafts | `inbound-draft` | sonnet | `brain.work` |
| Jarvis `daemon/reports.py` spoken line | `report-narrate` | haiku | `brain.work` |
| Jarvis `brain.answer_escalation` | `escalation` | sonnet | `brain.work`, Read/Grep/Glob in the repo |
| Brokkr `originate.py` proposer | `originate` | sonnet (= `proposer.MODEL`, checked) | `brain.work` via the host |
| Brokkr `delegation.py` criteria scoring | `criteria-score` | haiku | `ask` |
| Brokkr `implementer.py` | `job:implementer` | sonnet | `run_agent`, one attempt |
| HUD `whereami.js`, `merge.js`, `tasks.js` | `hud-whereami`, `hud-commit-message`, `hud-task-classify` | haiku | `POST /ask` |
| Mimir `jobs/session-history.py`, `memory-capture.py`, `vault-capture.py` | `mind-session-history`, `mind-memory-capture`, `mind-vault-capture` | haiku | `ask`, recorded as their jobs |
| Mimir `improve.py` drafts, subject, judge | `mind-improve-draft`, `mind-improve-subject`, `mind-improve-judge` | haiku, sonnet, haiku | `ask` |
| Jarvis `scripts/run-*.py` through `jobcommon.run_claude_job` | `job:nightly-reflection`, `job:portfolio-advisor`, `job:consultant-digest`, `job:weekly-activity`, `job:weekly-introspect`, `job:hud-audit`, `job:issue-triage`, `job:checkups` | as before | `run_agent` |

### Rules a later edit could break

- **Bifrost makes the call and reports what happened.** It records the usage-limit window in Fafnir (`limits.record`) and
  returns the hit as `limit`; Jarvis raises the alert (`core.alert_limit`, the alert half of `core.record_limit`; Brokkr reaches it
  through the host binding `alert_limit`). Bifrost never alerts, pings or files.
- **brain keeps its policy.** `brain.work(prompt, surface=..., purpose=..., isolated=..., scope=...)` checks `allowed()` (caps,
  breaker, limit, brake), computes the per-call budget, calls `bifrost.acall`, then writes `brain_calls` and moves the breaker.
  Bifrost writes the provider-ledger row (`provider_calls.surface` is now the Bifrost surface, not `work`), so brain no longer
  writes it.
- **Brain's isolated argv is byte-identical to before** (`--safe-mode`, no `--setting-sources`): G15 measured the proposer on it.
  `ask()` adds `bare` (`--setting-sources ''`, `--no-session-persistence`), as the HUD's and Mimir's calls had. The selftest checks
  `bifrost.model_for("originate") == brokkr.proposer.MODEL`, and `originate` refuses to call when they differ.
- **Ledger.** `ask(..., job=...)` records a `job_calls` row (the weekly job budget); without it a `provider_calls` row under the
  surface. Mimir's three jobs keep their job names. `run_agent` records every attempt as its job.
- **The service serves only `hud-*` surfaces**, refuses a write carrying `Origin`, and answers only on loopback hosts.
- The selftest asserts no Jarvis, Brokkr or Mimir module builds its own `claude -p --model` line.

### Moved

- The HUD's `lib/ledger.js` became `lib/gateway.js` (the client).
- `[implementer] model` (Jarvis `config.toml`) became `job:implementer` here.

### Differs from the design above

- A third primitive, `call()`, carries brain's tool-using, cwd-bound, per-call-budgeted calls.
- Changed on purpose, small: criteria scoring was a non-isolated call with `--output-format text` (full profile, no cost);
  it is now an isolated `ask` with JSON output, so its cost is recorded. A Claude result with `is_error` is now a failed call
  for brain (it was counted ok when the exit code was 0). Mimir's three jobs record ok from the call, not from their own parse.
  The implementer's job detail gained `turns=` and `attempt=`. Job prompts go on stdin instead of argv.
- Not moved: `g15-replay.py` (run by hand when the proposer prompt changes; it builds its argv with `bifrost.command` through
  `brain.command` and adds `--session-id`) and `brain.triage()` (the Messages API tier, still unbuilt).
- `gateway-unreachable` comes from a Jarvis job `gateway-check` (every 5 min, `daemon/handlers/bifrost_check.py`), resolved by
  evidence when `/health` answers; `health-check.py` also probes `/health`.

### Checked

Selftest 657/0 before, 659/0 after (two new checks); HUD 148/0 before and after; Fafnir, Hermod 18, Brokkr, Heimdall 9, Mimir 3
green before and after; Bifrost's tests green. Live after the 00:11 restart: `/health` answers (28 surfaces), one
`hud-commit-message` call through the service ($0.0001, ledger row written), one Mimir `summarize()` through the library
($0.0002), one `run_agent` session accepted by the CLI (stopped at its $0.10 test cap: job sessions load the full profile,
about 170k cache-creation tokens, as before), `gateway-check` running on the new path, `gateway-unreachable` #59 (raised
before the service was installed) resolved by evidence.
