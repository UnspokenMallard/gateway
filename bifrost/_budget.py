"""Fafnir's library (`~/.budget`, stdlib only): the gate, the ledger and the limit window."""
import sys
import time
import uuid
from pathlib import Path

try:
    from fafnir import ledger, limits  # noqa: F401
except ImportError:
    sys.path.insert(0, str(Path.home() / ".budget"))
    from fafnir import ledger, limits  # noqa: F401


def record_call(surface: str, ok: bool, duration_s: float, cost_usd: float | None, model: str, detail: str,
                scope: str, reason: str = "") -> None:
    """Fafnir's `provider_call` row plus `reason`. Never raises."""
    try:
        ledger.mr.append(ledger.SOURCE, ledger.CALLS, {
            "surface": surface, "model": model, "ok": 1 if ok else 0, "duration_s": duration_s, "cost_usd": cost_usd,
            "detail": detail[:500], "scope": scope, "reason": reason[:200]})
    except Exception:  # noqa: BLE001 - metering must not break the call
        pass


def record_job(job: str, surface: str, ok: bool, duration_s: float, cost_usd: float | None, model: str, detail: str,
               scope: str, reason: str = "", attempt: int | None = None) -> str | None:
    """Fafnir's `job_call` row (same fields as `ledger.record_job`) plus `surface`; returns its id.
    Written here because `ledger.record_job` has no `surface` argument."""
    try:
        now, uid = time.time(), uuid.uuid4().hex
        ledger.mr.append(ledger.SOURCE, ledger.JOBS, {
            "uid": uid, "job": job, "surface": surface, "model": model, "ok": 1 if ok else 0,
            "duration_s": duration_s, "cost_usd": cost_usd, "detail": detail[:500], "scope": scope,
            "reason": reason[:200], "attempt": attempt}, at=now, uid=uid)
        return uid
    except Exception:  # noqa: BLE001
        return None


def calls_by_surface(days: int = 30) -> dict[str, int]:
    """Call count per surface over `days`, from both streams. Job rows written before `surface` existed
    count under `job:<job>`; the larger of the two counts wins so a row is not counted twice."""
    since, out = time.time() - days * 86400, {}

    def agg(stream: str, by: str) -> dict:
        return ledger.mr.agg("count", stream=stream, by=by, since=since, source=ledger.SOURCE) or {}
    for stream in (ledger.CALLS, ledger.JOBS):
        for name, n in agg(stream, "surface").items():
            if name:
                out[str(name)] = out.get(str(name), 0) + int(n)
    for job, n in agg(ledger.JOBS, "job").items():
        if job:
            out["job:" + str(job)] = max(out.get("job:" + str(job), 0), int(n))
    return out
