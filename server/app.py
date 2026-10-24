"""Bifrost's HTTP service on 127.0.0.1:3027 (launchd com.gateway.server), for callers that are not
Python on this Mac: the HUD asks its judgments here. Python callers import the `bifrost` library
directly (same code, same config, same ledger).

Localhost only: the host check refuses DNS rebinding, and a write carrying an Origin header (any
browser page) is refused, so no web page can spend through Anze's browser. Bifrost makes the call
and reports what happened; it never alerts.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bifrost import ask, store, surfaces  # noqa: E402
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
    return JSONResponse({"ok": True, "db": str(store.DB_PATH), "claude": exe() is not None,
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


routes = [
    Route("/health", health),
    Route("/surfaces", list_surfaces),
    Route("/ask", ask_endpoint, methods=["POST"]),
]

app = Starlette(routes=routes, middleware=[Middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])])

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
