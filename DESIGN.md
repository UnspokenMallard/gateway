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
