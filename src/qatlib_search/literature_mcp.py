"""Local stdio adapter using the official MCP SDK."""
from __future__ import annotations

from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from .literature import LiteratureError, Snapshot
from .literature_query import DESCRIPTIONS, QUERIES, RESULT, execute

TOOL_COMMANDS = {f"qatlib_{name.replace('-', '_')}": name for name in QUERIES}


async def serve(snapshot: Snapshot) -> None:
    server = Server("qatlib-literature")

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [
            types.Tool(name=name, description=DESCRIPTIONS[command],
                       inputSchema=QUERIES[command], outputSchema=RESULT,
                       annotations=types.ToolAnnotations(readOnlyHint=True, openWorldHint=False))
            for name, command in TOOL_COMMANDS.items()
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> dict:
        try:
            return execute(snapshot, TOOL_COMMANDS[name], arguments)
        except LiteratureError as error:
            raise ValueError(f"{error.code}: {error}") from error

    async with stdio_server() as (reader, writer):
        await server.run(reader, writer, server.create_initialization_options())
