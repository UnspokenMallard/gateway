"""Bifrost's HTTP service on 127.0.0.1:3027 (launchd com.gateway.server), for callers that are not
Python on this Mac: the HUD asks its judgments here and runs its Claude sessions here (`bifrost.sessions`).
Python callers import the `bifrost` library directly (same code, same config, same ledger). An ACP session lives in
the process that started it, so the HUD's sessions are started through this service and die with it.

Localhost only: the host check refuses DNS rebinding, and a write carrying an Origin header (any
browser page) is refused, so no web page can spend through Anze's browser. Bifrost makes the call
and reports what happened; it never alerts.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bifrost import ask, sessions, surfaces  # noqa: E402
from bifrost.claude import SCOPES, exe  # noqa: E402
from starlette.applications import Starlette  # noqa: E402
from starlette.middleware import Middleware  # noqa: E402
from starlette.middleware.trustedhost import TrustedHostMiddleware  # noqa: E402
from starlette.requests import Request  # noqa: E402
from starlette.responses import JSONResponse  # noqa: E402
from starlette.routing import Route  # noqa: E402

HOST, PORT = "127.0.0.1", 3027
# Only the HUD's surfaces are served over HTTP; Python callers use the library.
HTTP_SURFACES = ("hud-",)


def health(_r: Request) -> JSONResponse:
    return JSONResponse({"ok": True, "claude": exe() is not None,
                         "surfaces": len(surfaces())})


def list_surfaces(_r: Request) -> JSONResponse:
    return JSONResponse(surfaces())


async def ask_endpoint(r: Request) -> JSONResponse:
    if r.headers.get("origin"):
        return JSONResponse({"error": "writes are not accepted from a browser page"}, status_code=403)
    try:
        body = await r.json()
        surface, prompt = str(body["surface"]), str(body["prompt"])
    except (ValueError, KeyError, TypeError) as e:
        return JSONResponse({"error": f"bad request: {e}"}, status_code=400)
    if not surface.startswith(HTTP_SURFACES):
        return JSONResponse({"error": f"surface {surface!r} is not served over HTTP"}, status_code=403)
    scope = body.get("scope") if body.get("scope") in SCOPES else "personal"
    out = await asyncio.to_thread(ask, surface, prompt, None, scope, system=body.get("system") or None,
                                  timeout_s=float(body.get("timeout_s") or 90))
    if out is None:
        return JSONResponse({"ok": False, "answer": None, "reason": "surface is off"})
    return JSONResponse({k: out.get(k) for k in ("ok", "answer", "text", "model", "cost_usd", "duration_s", "reason")}
                        | {"limited": out.get("limit") is not None})


def _start(_sid, b, _q):
    keys = ("scope", "model", "title", "prompt", "session_id", "resume", "name", "trust", "mode")
    return sessions.start(str(b["id"]), str(b["kind"]), str(b["cwd"]), **{k: b[k] for k in keys if b.get(k) is not None})


# What each session route does: (session id, JSON body, query) -> the JSON to return, or None.
def _answer(sid, b, _q):
    pick: dict = {"n": int(b["n"])} if b.get("n") is not None else {}
    if b.get("ns") is not None:
        pick["ns"] = [int(n) for n in b["ns"]]
    sessions.answer(sid, text=b.get("text"), label=b.get("label"), cancel=bool(b.get("cancel")),
                    ask_id=b.get("ask_id"), **pick)


async def _session(r: Request, write: bool, do) -> JSONResponse:
    """Run one session action off the event loop. A SessionError is the answer, with its own status."""
    if write and r.headers.get("origin"):
        return JSONResponse({"ok": False, "reason": "writes are not accepted from a browser page"}, status_code=403)
    try:
        body = {}
        if write and await r.body():
            body = await r.json()
            if not isinstance(body, dict):
                raise sessions.BadRequest("the body must be a JSON object")
        out = await asyncio.to_thread(do, r.path_params.get("id"), body, r.query_params)
    except sessions.SessionError as e:
        return JSONResponse({"ok": False, "reason": str(e)}, status_code=e.status)
    except (ValueError, KeyError, TypeError) as e:
        return JSONResponse({"ok": False, "reason": f"bad request: {e!r}"}, status_code=400)
    return JSONResponse({"ok": True, **(out or {})})


def _route(path: str, method: str, do) -> Route:
    async def endpoint(r: Request) -> JSONResponse:
        return await _session(r, method != "GET", do)
    return Route(path, endpoint, methods=[method])


SESSION_ROUTES = [
    _route("/session", "POST", _start),
    _route("/sessions", "GET", lambda _sid, _b, _q: {"switch": sessions.switch(), "sessions": sessions.list_sessions()}),
    _route("/session/{id}", "GET", lambda sid, _b, _q: sessions.state(sid)),
    _route("/session/{id}", "DELETE", lambda sid, _b, _q: sessions.forget(sid)),
    _route("/session/{id}/screen", "GET", lambda sid, _b, q: sessions.screen(sid, int(q["lines"]) if q.get("lines") else None)),
    _route("/session/{id}/events", "GET", lambda sid, _b, q: sessions.events(sid, int(q.get("after") or 0))),
    _route("/session/{id}/send", "POST", lambda sid, b, _q: sessions.send(sid, b["text"])),
    _route("/session/{id}/press", "POST", lambda sid, b, _q: sessions.press(sid, str(b["key"]))),
    _route("/session/{id}/answer", "POST", _answer),
    _route("/session/{id}/cancel", "POST", lambda sid, _b, _q: sessions.cancel(sid)),
    _route("/session/{id}/mode", "POST", lambda sid, b, _q: sessions.set_mode(sid, str(b["mode"]))),
    _route("/session/{id}/stop", "POST", lambda sid, _b, _q: sessions.stop(sid)),
]

routes = [
    Route("/health", health),
    Route("/surfaces", list_surfaces),
    Route("/ask", ask_endpoint, methods=["POST"]),
    *SESSION_ROUTES,
]

app = Starlette(routes=routes, middleware=[Middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])])

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
