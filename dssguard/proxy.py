"""DSS-Guard as a drop-in MCP stdio proxy.

    python -m dssguard --root work=/path/to/dir:tree -- node server.js /path/to/dir

The client talks to the proxy exactly as it would to the server. Tools are
listed unchanged (annotations included). Every call goes through Guard.call.
A blocked call returns isError=true with an explanation; nothing else changes.

Root syntax:  NAME=PATH:KIND[:FILE]   KIND in tree | git | memory | sqlite | raw
              memory and sqlite take the file name inside PATH.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

import mcp.types as types
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server

from .guard import ADDITIVE, LOSSLESS, Guard, explain
from .state import View


def parse_root(spec: str, scratch: tuple[str, ...]) -> View:
    name, rest = spec.split("=", 1)
    parts = rest.rsplit(":", 2) if rest.count(":") >= 2 else rest.rsplit(":", 1)
    path, kind = parts[0], parts[1]
    file = parts[2] if len(parts) > 2 else None
    if kind in ("memory", "sqlite") and not file:
        raise SystemExit(f"--root {spec}: {kind} needs a file name (NAME=PATH:{kind}:FILE)")
    return View(name, Path(path).resolve(), kind, file, scratch)


async def list_all_tools(client: ClientSession) -> list[types.Tool]:
    """Every page of tools/list. A proxy that reads only the first page would
    expose a partial tool set and could forward later tools without their
    declarations."""
    res = await client.list_tools()
    tools = list(res.tools)
    while res.nextCursor:
        res = await client.list_tools(cursor=res.nextCursor)
        tools += res.tools
    return tools


async def serve(downstream: StdioServerParameters, guard: Guard):
    state: dict = {"stale": False}

    async def on_message(msg):
        # the server changed its tools (and possibly their annotations)
        if isinstance(msg, types.ServerNotification) and isinstance(msg.root, types.ToolListChangedNotification):
            state["stale"] = True

    async with stdio_client(downstream, errlog=sys.stderr) as (r, w):
        async with ClientSession(r, w, message_handler=on_message) as client:
            init = await client.initialize()
            tools = await list_all_tools(client)
            by_name = {t.name: t for t in tools}

            async def refresh():
                nonlocal tools, by_name
                tools = await list_all_tools(client)
                by_name = {t.name: t for t in tools}
                state["stale"] = False

            server = Server(f"dss-guard({init.serverInfo.name})",
                            instructions=getattr(init, "instructions", None))

            @server.list_tools()
            async def _list() -> list[types.Tool]:
                if state["stale"]:
                    await refresh()
                return tools

            @server.call_tool(validate_input=False)
            async def _call(name: str, arguments: dict):
                if state["stale"] or name not in by_name:
                    await refresh()
                tool = by_name.get(name)
                # unknown tool: no declaration, so the spec's defaults apply (no promise)
                ann = tool.annotations if tool else None
                result, d = await guard.call(name, ann, arguments or {},
                                             lambda: client.call_tool(name, arguments or {}))
                if d.verdict == "blocked" and guard.enforce:
                    return types.CallToolResult(
                        content=[types.TextContent(type="text", text=explain(d))],
                        isError=True)
                return result

            async with stdio_server() as (sr, sw):
                await server.run(sr, sw, InitializationOptions(
                    server_name=server.name, server_version="0.1.0",
                    capabilities=server.get_capabilities(NotificationOptions(tools_changed=True), {})))


def main(argv: list[str] | None = None):
    argv = sys.argv[1:] if argv is None else argv
    if "--" not in argv:
        raise SystemExit("usage: python -m dssguard [options] -- <server command> [args]")
    i = argv.index("--")
    ap = argparse.ArgumentParser(prog="dssguard")
    ap.add_argument("--root", action="append", default=[], required=True)
    ap.add_argument("--scratch", action="append", default=[],
                    help="relative path the server owns inside every root (declared, logged)")
    ap.add_argument("--nondestructive", choices=[LOSSLESS, ADDITIVE], default=LOSSLESS)
    ap.add_argument("--observe", action="store_true", help="log decisions, never roll back")
    ap.add_argument("--log")
    ap.add_argument("--store", help="checkpoint directory (same volume as the roots)")
    opts = ap.parse_args(argv[:i])
    cmd = argv[i + 1:]
    scratch = tuple(opts.scratch)
    views = [parse_root(s, scratch) for s in opts.root]
    guard = Guard(views, nondestructive=opts.nondestructive, store=opts.store,
                  log=opts.log, enforce=not opts.observe)
    env = dict(os.environ)
    asyncio.run(serve(StdioServerParameters(command=cmd[0], args=cmd[1:], env=env,
                                            cwd=os.getcwd()), guard))
