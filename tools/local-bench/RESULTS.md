# Local triage model bench (A36, design step 1), 2026-10-21

Host: M5 Max 36 GB, Ollama 0.40.7 (brew service, 127.0.0.1:11434). `bench.py` runs one model at a time, one request at a time.
Corpus `corpus.json`: 50 items, 14 CRITICAL / 36 NORMAL. 24 are real Asana backlog titles (labels are Claude's, by the
`inbound.py` rubric, not Anze's), 26 are hand-written. Prompt = `inbound.py` triage_prompt. Temperature 0, `think:false`,
`keep_alive` 10m, JSON-schema enum `format` where Ollama allows it.

Baseline (work hours, 2026-10-16..21, n=300): swap p95 6951 MB, memlevel p5 45, HUD probe p95 0.13 s, `mem search` p95 0.34 s.

| Model | Acc | Real / synth | C->N | N->C | Warm p50 / p95 | Cold (max, 3 runs) | Peak RSS | Schema `format` |
|---|---|---|---|---|---|---|---|---|
| qwen3:4b-instruct (2507) | 0.98 | 0.96 / 1.00 | 1 | 0 | 0.15 / 0.18 s | 0.72 s | 5184 MB | yes |
| qwen3.5:4b | 0.96 | 0.96 / 0.96 | 2 | 0 | 0.07 / 0.09 s | 1.12 s | 3982 MB | **no** (501 "structured output is unavailable") |
| gemma3:4b | 0.88 | 0.79 / 0.96 | 1 | 5 | 0.21 / 0.26 s | 2.47 s | 4130 MB | yes |
| llama3.2:3b | 0.88 | 0.83 / 0.92 | 3 | 3 | 0.11 / 0.15 s | 2.27 s | 4143 MB | yes |
| qwen3:4b (thinking, `think:false`) | 0.28 | | 0 | 36 | | | 5193 MB | answers CRITICAL to everything; with thinking on it takes ~900 tokens. Out. |

Notes:
- RSS is `ps` over all `ollama` processes (the perf job's method). It includes memory-mapped weights, so it is an upper bound on private memory.
  Only qwen3.5:4b is under the 4 GB budget; gemma3 and llama3.2 miss it by about 130 MB; qwen3:4b-instruct by 1.2 GB.
- Reference probes: every candidate stayed within baseline x 1.2 (HUD p95 <= 0.25 s idle/burst noise, `mem search` p95 0.26-0.42 s vs 0.34 s baseline),
  but the burst lasts only 5-10 s, so only 3-9 probes fell inside it. Treat as "no visible effect", not a tight number.
- Missed CRITICAL: item 8 ("on edge users unable to subscribe", a real task I labelled CRITICAL) was missed by qwen3.5, qwen3-instruct and llama.
  The label is arguable (browser-specific). Item 34 (24 h deadline) and 35 (disk full) were missed by llama3.2 only; item 36 (Slovene payment error) by qwen3.5 only.
- qwen3.5:4b without `format` returned a clean one-word answer on all 50 items, but Gateway's `judge_sync` always sends `format`; it would need a fallback.
- gemma4:e2b / e4b exist (Ollama library, 3 weeks old) but weigh 4.6-6.6 GB on disk, so they cannot meet the 4 GB RSS budget.
