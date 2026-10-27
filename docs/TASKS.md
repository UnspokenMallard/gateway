# Bifrost tasks (from the 2026-10-27 audit)

- [x] (done 2026-10-27, 5588319) [medium] [improvement] Write the surface name on every `provider_call` and `job_call` row — Bifrost telemetry emitter — acceptance: a query groups 30 days of calls by surface and lists the 20 currently unseen surfaces with a count or zero.
- [x] (retracted: rows exist as `job_call`, job `promises`) [medium] [underperforming] Record cost and a row for `promises-extract` calls — `bifrost.ask` path used by `~/.jarvis/scripts/run-promises.py:132` — acceptance: after one run, `provider_call` has rows for `promises-extract` and the run summary shows a non-zero list price.
- [x] (done 2026-10-27, 5588319) [low] [improvement] Return the failure reason (`error_max_budget_usd`, timeout, denial) in the telemetry row and result — acceptance: the next failed `hud-audit-nightly` alert names its cause instead of `<empty>`.
- [x] (done 2026-10-27, 5588319) [low] [improvement] Log one line per failed call to `state/logs/server.log` — acceptance: a forced failure appears there.
- [x] (done 2026-10-27, 4294ef9) [low] [improvement] Inventory direct `claude` callers and mark routed / intentional / to migrate — list in this file's audit finding 6 — acceptance: each file has a verdict in `DESIGN.md`.
- [x] (done 2026-10-27, a9e1869) [low] [improvement] Make tests runnable by one documented command — `tests/` — acceptance: prints a pass count.
