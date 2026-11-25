#!/usr/bin/env python3
"""Entrypoint launcher for the refactored Dynamic AST + HATEOAS MCP Server."""

from ast_mcp.cli import create_server, main

__all__ = ["create_server", "main"]

if __name__ == "__main__":
    main()
