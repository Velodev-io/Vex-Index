import sys
import asyncio
import logging
from typing import Optional
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

logger = logging.getLogger(__name__)

from vexindex.config import settings
from vexindex.db import (
    init_db,
    get_all_projects,
    search_hybrid,
    get_symbol_definitions,
    get_symbol_relations,
    get_symbol_callers,
    get_symbol_callees
)
from vexindex.indexer import index_project

server = Server("vexindex-mcp")

@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="codebase_search",
            description="Search the indexed codebase chunks using hybrid FTS5 and vector search, merging results with RRF",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search term or concept to find inside the codebase"
                    },
                    "project_id": {
                        "type": "string",
                        "description": "Optional UUID of project directory filter"
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum search results to return (default: 10)",
                        "default": 10
                    },
                    "alpha": {
                        "type": "number",
                        "description": "Optional FTS5 weight in [0.0, 1.0]. 1.0 = pure lexical FTS5, 0.0 = pure vector semantic search, 0.6 = hybrid default",
                    }
                },
                "required": ["query"]
            }
        ),
        Tool(
            name="list_projects",
            description="List all registered codebase projects, their paths, file counts, and chunk counts",
            inputSchema={"type": "object"}
        ),
        Tool(
            name="find_symbol_definition",
            description="Locate class/function/method definitions for a symbol name across the codebase",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Symbol name to resolve"
                    },
                    "project_id": {
                        "type": "string",
                        "description": "Optional UUID of project filter"
                    }
                },
                "required": ["name"]
            }
        ),
        Tool(
            name="get_symbol_relations",
            description="Retrieve all incoming and outgoing relations (IMPORTS, CALLS, INHERITS) for a symbol",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Symbol name"
                    },
                    "project_id": {
                        "type": "string",
                        "description": "Optional UUID of project filter"
                    }
                },
                "required": ["name"]
            }
        ),
        Tool(
            name="find_callers",
            description="Find all locations and caller symbols that call the specified symbol",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Symbol name"
                    },
                    "project_id": {
                        "type": "string",
                        "description": "Optional UUID of project filter"
                    }
                },
                "required": ["name"]
            }
        ),
        Tool(
            name="find_callees",
            description="Find all symbols called by the specified symbol",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Caller symbol name"
                    },
                    "project_id": {
                        "type": "string",
                        "description": "Optional UUID of project filter"
                    }
                },
                "required": ["name"]
            }
        )
    ]
 
@server.call_tool()
async def call_tool(name: str, arguments: dict | None) -> list[TextContent]:
    conn = await init_db(settings.db_path_abs)
    try:
        if name == "codebase_search":
            args = arguments or {}
            query = args.get("query")
            project_id = args.get("project_id")
            limit = args.get("limit", 10)
            alpha = args.get("alpha")
            
            if not query:
                return [TextContent(type="text", text="Error: Query parameter is required.")]
                
            results = await search_hybrid(conn, query, project_id, limit, alpha=alpha)
            
            lines = []
            for r in results:
                lines.append(f"### {r['file_path']} (Lines {r['start_line']}-{r['end_line']}) - Type: {r['chunk_type']}")
                if r.get("name"):
                    lines.append(f"*Name: {r['name']}*")
                lines.append(f"```\n{r['content']}\n```")
                lines.append("\n---")
                
            text = "\n".join(lines) if lines else "No matching code chunks found."
            return [TextContent(type="text", text=text)]
            
        elif name == "list_projects":
            from vexindex.db import get_file_and_chunk_counts
            projects = await get_all_projects(conn)
            lines = ["# Registered Projects:\n"]
            for p in projects:
                file_count, chunk_count = await get_file_and_chunk_counts(conn, p["id"])
                lines.append(f"- **{p['name']}** (ID: {p['id']})")
                lines.append(f"  - Path: `{p['root_path']}`")
                lines.append(f"  - Files: {file_count} | Chunks: {chunk_count}")
                if p['last_indexed']:
                    lines.append(f"  - Last Indexed: {p['last_indexed']}")
            text = "\n".join(lines) if projects else "No projects registered yet."
            return [TextContent(type="text", text=text)]
            
        elif name == "find_symbol_definition":
            args = arguments or {}
            sym_name = args.get("name")
            project_id = args.get("project_id")
            if not sym_name:
                return [TextContent(type="text", text="Error: Name parameter is required.")]
            results = await get_symbol_definitions(conn, sym_name, project_id)
            lines = []
            for r in results:
                lines.append(f"### {r['file_path']} (Line {r['line']}) - Kind: {r['kind']}")
                if r.get("scope"):
                    lines.append(f"*Scope: {r['scope']}*")
                if r.get("content"):
                    lines.append(f"```\n{r['content']}\n```")
                lines.append("\n---")
            text = "\n".join(lines) if lines else "Symbol definition not found."
            return [TextContent(type="text", text=text)]
            
        elif name == "get_symbol_relations":
            args = arguments or {}
            sym_name = args.get("name")
            project_id = args.get("project_id")
            if not sym_name:
                return [TextContent(type="text", text="Error: Name parameter is required.")]
            results = await get_symbol_relations(conn, sym_name, project_id)
            lines = []
            for r in results:
                lines.append(f"- **{r['source_symbol'] or 'File'}** --[{r['relation_type']}]--> **{r['target_symbol']}** (at {r['file_path']}:{r['line']})")
            text = "\n".join(lines) if lines else "No relations found for symbol."
            return [TextContent(type="text", text=text)]
            
        elif name == "find_callers":
            args = arguments or {}
            sym_name = args.get("name")
            project_id = args.get("project_id")
            if not sym_name:
                return [TextContent(type="text", text="Error: Name parameter is required.")]
            results = await get_symbol_callers(conn, sym_name, project_id)
            lines = []
            for r in results:
                lines.append(f"- **{r['source_symbol'] or 'File'}** (at {r['file_path']}:{r['line']})")
            text = "\n".join(lines) if lines else "No callers found for symbol."
            return [TextContent(type="text", text=text)]
            
        elif name == "find_callees":
            args = arguments or {}
            sym_name = args.get("name")
            project_id = args.get("project_id")
            if not sym_name:
                return [TextContent(type="text", text="Error: Name parameter is required.")]
            results = await get_symbol_callees(conn, sym_name, project_id)
            lines = []
            for r in results:
                lines.append(f"- **{r['target_symbol']}** (at {r['file_path']}:{r['line']})")
            text = "\n".join(lines) if lines else "No callees found for symbol."
            return [TextContent(type="text", text=text)]
            
        else:
            raise ValueError(f"Unknown tool: {name}")
    except Exception as e:
        logger.exception(f"MCP Server Error during execution of tool '{name}': {e}")
        return [TextContent(type="text", text=f"Error executing tool '{name}': {e}")]
    finally:
        await conn.close()

async def main():
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options()
            )
    except Exception as e:
        logger.exception(f"MCP Server failed to start: {e}")
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())
