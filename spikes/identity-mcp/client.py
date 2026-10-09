"""C03 spike client — a real MCP client process, not an in-process shortcut."""
from __future__ import annotations

import asyncio
import json
import pathlib
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

HERE = pathlib.Path(__file__).parent
ROOT = HERE.parents[1]


async def main() -> int:
    server = StdioServerParameters(
        command=sys.executable,
        args=[str(HERE / "server.py")],
        cwd=str(ROOT),
        env={"PYTHONPATH": str(ROOT / "src")},
    )
    async with stdio_client(server) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            print("server:", init.server_info.name, init.server_info.version)

            tools = await session.list_tools()
            names = sorted(t.name for t in tools.tools)
            print("tools exposed:", names)

            r1 = await session.call_tool("search_knowledge",
                                        {"query": "брендбук тон", "limit": 3})
            print("search_knowledge ->", r1.content[0].text[:160])

            r2 = await session.call_tool("whoami", {"user_id": "spoofed-principal"})
            print("whoami(spoofed) ->", r2.content[0].text[:200])

            print("RESULT_JSON:" + json.dumps({
                "tools": names,
                "call_ok": True,
            }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
