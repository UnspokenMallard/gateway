# Gateway

Every model call on this Mac goes through one place: which model answers (a local model or
Claude), under which account, inside which budget, recorded where, and what happens on a usage
limit or a failure. Designed 2026-10-17, after Mind, Budget, Sentinel, Herald and Forge were split
out of Jarvis.

## Decisions (Anze, 2026-10-17)

| Question | Decision |
|---|---|
| Home | `~/.gateway`, own git repo, code and `state/` together, own venv (like the others) |
| Routing | Gateway decides, per *surface*, from its config: `off`, `shadow`, `local`, `claude:haiku`, `claude:sonnet`, ... A caller names its surface and its task, never a model |
| Process | (following the earlier rule) library for Python callers; an HTTP service on `127.0.0.1:3027` (launchd `com.gateway.server`) for the HUD, which is Node |
| Order | Built before Analyst (Analyst's jobs run on it), and before the local model goes live (Ollama is due 2026-10-21 on Jarvis's list) |

## Why

About 20 places start `claude -p` themselves (Jarvis's brain and job scripts, the HUD's three Haiku
calls, Mind's session summaries, drafts and improve evals, Forge's implementer and criteria scoring,
the check-ups). Each repeats the account selection, the Budget gate, the cost parsing, the ledger
row and the usage-limit check, and each would grow its own local-model path once Ollama is in. The
local model's client (`daemon/local_llm.py`) lives in Jarvis, and Mind's contradiction check
imports it straight from `~/.jarvis/daemon`: the one place a component still reaches into Jarvis's code.

## Boundary

Gateway **makes the call it is asked for and reports what happened**. Budget stays the ledger and
the gate ("may I spend": Gateway asks it before every Claude call and records every call there).
Jarvis, Mind, Forge and the HUD stay the deciders of *what* to ask and what to do with the answer.
Gateway never alerts, pings or files anything: a usage limit is recorded in Budget's window and
returned to the caller, and Jarvis raises the alert as today (`core.record_limit` splits: Budget
records, Jarvis alerts).

## Two kinds of call

1. **`ask(surface, prompt, schema=None, scope=None)`**: a small judgment. No tools, no session
   files. Routed by the surface's mode:
   - `off`: returns None (the caller does what it did before).
   - `local`: the local model answers. Any failure, memory pressure on the host, or a "nothing to
     see" answer on text the keyword rules flag (`needs_second_opinion`) falls back to Claude Haiku:
     the local model may skip an escalation, never suppress one (TRIAGE-MODEL-DESIGN.md).
   - `shadow`: Claude answers and is used; the local model answers next to it; both are stored for
     the agreement gate (`agreement()`).
   - `claude:<model>`: Claude only, `--tools ''`, `--setting-sources ''`, no MCP, JSON output.
   Returns `{answer, model, cost_usd, duration_s, limited}`; never raises.
2. **`run_agent(job, prompt, tools, allow, model, budget_usd, cwd=None, scope=None, optional=False)`**:
   a tool-using `claude -p` session (today `jobcommon.run_claude_job`): `dontAsk` permissions, the
   explicit tool list and allow rules, the shared deny list (secrets, credentials, transcripts), the
   per-run cap, the Budget gate before every attempt (optional jobs stop at 80% of the week or the
   daily cap), retries only on transient network errors, the result parsed and recorded as a job run.

Both pick the account (`personal` or `work`, the Claude config dir and its environment) from the
caller's scope; the scope rules (`daemon/scope.py`: which repo is work) stay in Jarvis and are passed in.

## What moves here

| From | Here |
|---|---|
| `~/.jarvis/daemon/local_llm.py` (modes, pressure check, Ollama client, shadow table, agreement) | `gateway/local.py`, table `local_llm_shadow` into `state/gateway.db` (copied with rows) |
| `[local_llm]` in Jarvis's config.toml (host, model, surfaces) | `~/.gateway/config.toml` `[local]` and `[surfaces]` |
| `scripts/local-llm-perf.py` (the host's memory pressure and model busy time, every 5 min) | `gateway/jobs/` (still triggered by Jarvis) |
| `scripts/jobcommon.py` `run_claude_job`, `claude_exe`, `JOB_DENY_TOOLS`, the account environment (`claude_scope`) | `gateway/agent.py`; `jobcommon` keeps the job lock, logs and artifact checks and calls Gateway |
| brain's `claude -p` call (`brain.py` `_call`) | `ask("brain-escalation" / its tiers)`; brain keeps its caps, breaker and `brain_calls` |
| HUD `whereami.js`, `merge.js` (commit message), `tasks.js` (classify) | `POST /ask` with surfaces `hud-whereami`, `hud-commit-message`, `hud-task-classify` |
| Mind's `session-history`, `memory-capture`, `vault-capture`, `improve` calls, `contradict.py` | `ask` / `run_agent` with their own surfaces |
| Forge's implementer and criteria scoring | `run_agent` and `ask` |

Surfaces today: `inbound-critical`, `briefing-triage`, `memory-contradict` (local-model candidates,
all `off`), plus every Claude caller above. Each keeps its current model, so the split changes no
behavior; moving a surface to `local` is a later one-line config change after its shadow gate passes.

## Interface

- Library `gateway` (stdlib only, like Budget's), `ask()`, `run_agent()`, `mode()`, `agreement()`.
- Service: `GET /health`, `POST /ask` (HUD), `GET /surfaces`, `GET /agreement?days=14`. Same guard as
  Budget's: loopback hosts only, writes refused when they carry an `Origin` header.
- CLI `gateway ask <surface> <prompt>`, `gateway surfaces`, `gateway agreement`.

## First steps for the build session

1. Read `TRIAGE-MODEL-DESIGN.md` and `daemon/local_llm.py` in `~/.jarvis`, `~/.budget/DESIGN.md`
   and `~/.forge/DESIGN.md` (its host binding: `env_for`, `brain_work`).
2. Map every `claude -p` call site (`grep -rn '"-p"'` across the six homes and the HUD) and give
   each a surface name in a table in this file before moving anything.
3. Move `local.py` and the shadow table first (nothing calls a local model yet: all surfaces `off`),
   then `run_agent` (jobcommon), then the `ask` callers one family at a time, the HUD last.
4. Behavior must not change: same models, same caps, same ledger rows (surface names may be new;
   say so in Budget's ledger notes). Selftest and every component's tests before and after.

## As built (2026-10-17/18)

Built and cut over in one session (Anze: "everything in one session"). Anze's build-time answers: the HUD keeps its
existing fallbacks when the service is down and Jarvis alerts `gateway-unreachable`; job models come from Gateway's config,
one surface per job; Gateway takes brain's whole call (argv, run, parse, limit record, ledger row) while brain keeps its caps,
breaker and `brain_calls`.

Layout as Budget's: `gateway/` library (paths from `GATEWAY_HOME`, default `~/.gateway`; tests set it to scratch):
`store.py` (`state/gateway.db`, `config.toml`), `claude.py` (`command`, `call`, `ask`, accounts, surfaces), `agent.py`
(`run_agent`, the shared deny list, `command`, `RUN`), `local.py` (the local model, moved from `~/.jarvis/daemon/local_llm.py`),
`proc.py` (the file-backed, tree-killing process runner, copied from `core._run_process_blocking`), `cli.py` behind
`bin/gateway`, `jobs/local-llm-perf.py` (moved from `~/.jarvis/scripts`, still triggered by Jarvis every 5 min),
`server/app.py` (launchd `com.gateway.server`, `bin/install-launchd [status]`, log `state/logs/server.log`), `config.toml`,
`tests/test_gateway.py`. Own venv (starlette, uvicorn, httpx for the tests).

### Call sites and their surfaces

| Caller | Surface | Model | Kind |
|---|---|---|---|
| Jarvis `daemon/inbound.py` Asana task triage (local first) | `inbound-critical` / `inbound-task-triage` | off / haiku | local, then `brain.work` |
| Jarvis `daemon/briefing.py` mail and chat triage (local first) | `briefing-triage` / `inbound-message-triage` | off / haiku | local, then `brain.work` |
| Jarvis `daemon/briefing.py` reply drafts | `inbound-draft` | sonnet | `brain.work` |
| Jarvis `daemon/reports.py` spoken line | `report-narrate` | haiku | `brain.work` |
| Jarvis `brain.answer_escalation` | `escalation` | sonnet | `brain.work`, Read/Grep/Glob in the repo |
| Forge `originate.py` proposer | `originate` | sonnet (= `proposer.MODEL`, checked) | `brain.work` via the host |
| Forge `delegation.py` criteria scoring | `criteria-score` | haiku | `ask` |
| Forge `implementer.py` | `job:implementer` | sonnet | `run_agent`, one attempt |
| HUD `whereami.js`, `merge.js`, `tasks.js` | `hud-whereami`, `hud-commit-message`, `hud-task-classify` | haiku | `POST /ask` |
| Mind `jobs/session-history.py`, `memory-capture.py`, `vault-capture.py` | `mind-session-history`, `mind-memory-capture`, `mind-vault-capture` | haiku | `ask`, recorded as their jobs |
| Mind `improve.py` drafts, subject, judge | `mind-improve-draft`, `mind-improve-subject`, `mind-improve-judge` | haiku, sonnet, haiku | `ask` |
| Mind `contradict.py` | `memory-contradict` | off | local only |
| Jarvis `scripts/run-*.py` through `jobcommon.run_claude_job` | `job:nightly-reflection`, `job:portfolio-advisor`, `job:consultant-digest`, `job:weekly-activity`, `job:weekly-introspect`, `job:hud-audit`, `job:issue-triage`, `job:checkups` | as before | `run_agent` |

Every model is the one the caller used before. Moving a local-model surface to `shadow`/`local` or a Claude surface to
another model is a one-line edit in `config.toml`, read at call time (no restart).

### Rules a later edit could break

- **Gateway makes the call and reports what happened.** It records the usage-limit window in Budget (`limits.record`) and
  returns the hit as `limit`; Jarvis raises the alert (`core.alert_limit`, the alert half of `core.record_limit`; Forge reaches it
  through the host binding `alert_limit`). Gateway never alerts, pings or files.
- **brain keeps its policy.** `brain.work(prompt, surface=..., purpose=..., isolated=..., scope=...)` checks `allowed()` (caps,
  breaker, limit, brake), computes the per-call budget, calls `gateway.acall`, then writes `brain_calls` and moves the breaker.
  Gateway writes the provider-ledger row (`provider_calls.surface` is now the Gateway surface, not `work`), so brain no longer
  writes it.
- **Brain's isolated argv is byte-identical to before** (`--safe-mode`, no `--setting-sources`): G15 measured the proposer on it.
  `ask()` adds `bare` (`--setting-sources ''`, `--no-session-persistence`), as the HUD's and Mind's calls had. The selftest checks
  `gateway.model_for("originate") == forge.proposer.MODEL`, and `originate` refuses to call when they differ.
- **Local-model surfaces keep the caller's logic.** `ask()` on `off|shadow|local` returns the local answer or None; the caller
  decides the fallback and records shadow rows (`gateway.local.record_shadow`), as it did with `local_llm`.
- **Ledger.** `ask(..., job=...)` records a `job_calls` row (the weekly job budget); without it a `provider_calls` row under the
  surface. Mind's three jobs keep their job names. `run_agent` records every attempt as its job.
- **The service serves only `hud-*` surfaces**, refuses a write carrying `Origin`, and answers only on loopback hosts.
- The selftest asserts no Jarvis, Forge or Mind module builds its own `claude -p --model` line.

### Moved

- `local_llm_shadow`: no table to move: it never existed in `jarvis.db` (no surface ever left `off`), so no backup was taken.
  It is created in `state/gateway.db` on first use.
- `state/local-llm-perf.json(l)` copied to `~/.gateway/state/` (320 samples; one written by the old process after the copy was
  merged in by timestamp). The Jarvis copies stay until Anze confirms.
- `[local_llm]` (Jarvis `config.toml`) became `[local]` and `[surfaces]` here; `[implementer] model` became `job:implementer`.
- Deleted from Jarvis (tracked, now here): `daemon/local_llm.py`, `scripts/local-llm-perf.py`; the HUD's `lib/ledger.js` became
  `lib/gateway.js` (the client).

### Differs from the design above

- `ask()` on a local surface does not fall back to Haiku itself: each caller keeps its own fallback and shadow comparison (they
  compare different fields), so behaviour stayed the same. A third primitive, `call()`, carries brain's tool-using, cwd-bound,
  per-call-budgeted calls.
- Changed on purpose, small: criteria scoring was a non-isolated call with `--output-format text` (full profile, no cost);
  it is now an isolated `ask` with JSON output, so its cost is recorded. A Claude result with `is_error` is now a failed call
  for brain (it was counted ok when the exit code was 0). Mind's three jobs record ok from the call, not from their own parse.
  The implementer's job detail gained `turns=` and `attempt=`. Job prompts go on stdin instead of argv.
- Not moved: `g15-replay.py` (run by hand when the proposer prompt changes; it builds its argv with `gateway.command` through
  `brain.command` and adds `--session-id`) and `brain.triage()` (the Messages API tier, still unbuilt).
- `gateway-unreachable` comes from a Jarvis job `gateway-check` (every 5 min, `daemon/handlers/gateway_check.py`), resolved by
  evidence when `/health` answers; `health-check.py` also probes `/health`.

### Checked

Selftest 657/0 before, 659/0 after (two new checks); HUD 148/0 before and after; Budget, Herald 18, Forge, Sentinel 9, Mind 3
green before and after; Gateway's tests green. Live after the 00:11 restart: `/health` answers (28 surfaces), one
`hud-commit-message` call through the service ($0.0001, ledger row written), one Mind `summarize()` through the library
($0.0002), one `run_agent` session accepted by the CLI (stopped at its $0.10 test cap: job sessions load the full profile,
about 170k cache-creation tokens, as before), `gateway-check` and `local-llm-perf` running on the new paths,
`gateway-unreachable` #59 (raised before the service was installed) resolved by evidence.

## Local-model watchdog (2026-10-21)

`gateway/jobs/local-llm-watchdog.py`, triggered every 5 min by a Jarvis job (not yet added: see below). Every run sends
5 one-token probes to the model and takes their p95. Baseline = median of the first 6 check p95s (about 30 min after
the first runs; measured here, so start it while the host is normal). A check is degraded at p95 > 2x baseline; 3 in a
row trip it: `[local] enabled = false` in `config.toml` (stops shadow for every surface), `ollama stop <model>` (model
stays installed), exit 1 with the reason, so Jarvis alerts from the failed job. Swap and memory level are recorded in
each check and named in the alert; they are not a trip signal of their own. An unloaded model is not probed (the
check is skipped, the streak kept). Failed or timed-out probes count as `timeout_s`.
State: `state/local-llm-watchdog.json` (baseline, streak, last 50 checks, `tripped`). A tripped watchdog exits 0 until
`local-llm-watchdog.py --reset` (also clears the baseline); then set `enabled = true` again by hand.
Tests: `tests/test_watchdog.py` (injected timings). Not verified: a live degradation, the real probe against Ollama,
the Jarvis alert text.

Jarvis job to add (`~/.jarvis/config.toml`, next to `local-llm-perf`):
```toml
[[jobs]]
name = "local-llm-watchdog"
kind = "script"
schedule = "every:300"
enabled = true
payload = { script = '/Users/anzes/.gateway/gateway/jobs/local-llm-watchdog.py', artifact = '/Users/anzes/.gateway/state/local-llm-watchdog.json', timeout_s = 90, artifact_max_age_s = 900 }
```
