"""`gateway ask <surface> <prompt> [--scope work]`, `gateway surfaces`."""
from __future__ import annotations

import argparse
import json
import sys

from . import ask, surfaces


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="gateway")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("ask", help="one judgment through a surface; prints the result as JSON")
    a.add_argument("surface")
    a.add_argument("prompt", help="the prompt, or - for stdin")
    a.add_argument("--scope", default="personal", choices=("personal", "work"))
    sub.add_parser("surfaces", help="every configured surface and its mode")
    args = p.parse_args(argv)

    if args.cmd == "ask":
        prompt = sys.stdin.read() if args.prompt == "-" else args.prompt
        out = ask(args.surface, prompt, scope=args.scope)
        if out is None:
            print(json.dumps({"answer": None, "reason": "surface is off"}))
            return 0
        print(json.dumps({k: v for k, v in out.items() if k not in ("output", "limit")}
                         | {"limit": getattr(out.get("limit"), "evidence", None)}, indent=2))
        return 0 if out["ok"] else 1
    if args.cmd == "surfaces":
        for name, how in surfaces().items():
            print(f"{name:28} {how}")
        return 0
    return 0
