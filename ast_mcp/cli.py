"""CLI entrypoint, FastMCP server factory, and STDIN-to-HTTP command bridge."""

from __future__ import annotations

import warnings

# Optional compatibility shim for older pydantic / pydantic_settings installations.
warnings.filterwarnings(
    "ignore",
    message=r".*eval_type_backport.*",
    category=UserWarning,
    module=r"pydantic_settings.*",
)

try:
    import pydantic._internal._typing_extra as _pte

    _orig_eval_type_backport = getattr(_pte, "eval_type_backport", None)
    if _orig_eval_type_backport is not None:

        def _safe_eval_type_backport(value: object, globalns: object = None, localns: object = None, type_params: object = None):
            try:
                return _orig_eval_type_backport(value, globalns, localns, type_params)
            except TypeError:
                return _orig_eval_type_backport(value, globalns, localns)

        _pte.eval_type_backport = _safe_eval_type_backport  # type: ignore[assignment]
except Exception:
    pass

import argparse
import asyncio
import json
import os
import sys
import threading
from typing import Any
from urllib.request import Request, urlopen

from .console import colorize
from .http_server import parse_stdin_line, start_web_dashboard
from .linter import normalize_to_serialized_ast
from .program import Context, DynamicASTMcpProgram, FastMCP


def start_stdin_to_http_bridge(http_base_url: str) -> threading.Thread:
    """Start daemon thread reading STDIN commands and forwarding them to `/api/command`."""

    def _worker() -> None:
        while True:
            try:
                line = sys.stdin.readline()
            except Exception:
                break
            if not line:
                break
            if not line.strip():
                continue
            try:
                payload = parse_stdin_line(line)
                req = Request(
                    f"{http_base_url}/api/command",
                    data=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urlopen(req, timeout=150.0) as resp:
                    resp.read()
            except Exception as exc:
                sys.stdout.write(colorize(f"[STDIN -> HTTP ERROR] {exc}\n", "red"))
                sys.stdout.flush()

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    return thread


def create_server(
    host: str = "0.0.0.0",
    port: int = 8000,
    streamable_http_path: str = "/mcp",
    stdout_is_mcp_stdio: bool = False,
) -> tuple[FastMCP, DynamicASTMcpProgram]:
    """Create and configure the FastMCP server and seed initial AST functions."""
    server = FastMCP(
        "dynamic-ast-server",
        host=host,
        port=port,
        streamable_http_path=streamable_http_path,
    )

    initial_registry: dict[str, str] = {
        "ast1": normalize_to_serialized_ast(
            """
def helper_one(x: int) -> int:
    return x * 2 + 1
"""
        ),
        "ast2": normalize_to_serialized_ast(
            """
def helper_two(y: int) -> int:
    return ast1(y) ** 2
"""
        ),
        "entrypoint": normalize_to_serialized_ast(
            """
def entrypoint(n: int = 3) -> int:
    '''Default entrypoint compiled and exposed at startup.'''
    return ast2(n) + 10
"""
        ),
    }

    prog = DynamicASTMcpProgram(
        mcp_server=server,
        initial_registry=initial_registry,
        auto_expose_keys=["entrypoint"],
        stdout_is_mcp_stdio=stdout_is_mcp_stdio,
    )

    @server.resource("ast://resource/{name}")
    def read_ast_resource(name: str) -> str:
        return json.dumps(prog.read(name, source="mcp_resource"), indent=2)

    @server.tool(
        name="ast_create",
        description=(
            "CREATE a new AST function, blank shell, or full-page Python script "
            "(auto-wrapped into a callable function with dynamic imports/classes preserved) "
            "with HATEOAS hal+json recovery."
        ),
    )
    async def ast_create(
        name: str,
        code_or_ast: str = "",
        description: str = "",
        expose_as_tool: bool = True,
        overwrite: bool = False,
        skip_roundtrip: bool = False,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        return await prog.create(
            name=name,
            code_or_ast=code_or_ast or None,
            description=description or None,
            expose_as_tool=expose_as_tool,
            overwrite=overwrite,
            skip_roundtrip=skip_roundtrip,
            ctx=ctx,
            source="mcp",
        )

    @server.tool(
        name="ast_read",
        description="READ one AST resource (or list all) in `application/hal+json` format.",
    )
    def ast_read(name: str = "") -> dict[str, Any]:
        return prog.read(name or None, source="mcp")

    @server.tool(
        name="ast_update",
        description=(
            "UPDATE an existing AST entry (single function or full-page Python script) "
            "with auto-fix lint pipeline, roundtrip check, or HATEOAS navigation."
        ),
    )
    async def ast_update(
        name: str,
        code_or_ast: str,
        description: str = "",
        expose_as_tool: bool = True,
        skip_roundtrip: bool = False,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        return await prog.update(
            name=name,
            code_or_ast=code_or_ast,
            description=description or None,
            expose_as_tool=expose_as_tool,
            skip_roundtrip=skip_roundtrip,
            ctx=ctx,
            source="mcp",
        )

    @server.tool(
        name="ast_delete",
        description="DELETE an AST resource and unexpose its MCP tool, returning `hal+json`.",
    )
    async def ast_delete(name: str, ctx: Context | None = None) -> dict[str, Any]:
        return await prog.delete(name=name, ctx=ctx, source="mcp")

    @server.tool(
        name="eval_ast_call",
        description=(
            "Evaluate a registered AST function or expression. Accepts `args` as a single literal, "
            "a list of literals, a list of literals followed by a kwargs object, or a kwargs object. "
            "On signature mismatch, returns a HATEOAS `ast_update` patch with `_new_[A-Z0-9]` parameter names."
        ),
    )
    def eval_ast_call(
        target: str,
        args: Any = None,
        kwargs: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return prog.eval_target(target=target, args=args, kwargs=kwargs, source="mcp")

    @server.tool(
        name="ast_convert",
        description=(
            "Convert between `saved_ast` (by registry key), `ast`, `json`, `schema`, `pydantic`, "
            "and `dataclass`. Returns Python source, serialized AST, pretty AST, and HATEOAS links."
        ),
    )
    def ast_convert(
        source_kind: str,
        target_kind: str,
        payload: str,
        model_name: str = "GeneratedModel",
    ) -> dict[str, Any]:
        return prog.convert(
            source_kind=source_kind,
            target_kind=target_kind,
            payload=payload,
            model_name=model_name,
            source="mcp",
        )

    @server.tool(
        name="ast_install_module",
        description=(
            "HATEOAS recovery tool: install a missing Python package into the active environment; "
            "if pip installation fails, generates a local Python stub module script with the required exports."
        ),
    )
    def ast_install_module(
        module_name: str,
        package_name: str = "",
        exported_symbols: list[str] | None = None,
        caller_context: str = "ast_function",
    ) -> dict[str, Any]:
        return prog.install_module(
            module_name=module_name,
            package_name=package_name or None,
            exported_symbols=exported_symbols,
            caller_context=caller_context,
            source="mcp",
        )

    @server.tool(
        name="ast_write_module_file",
        description="Write a recommended local fallback Python module (`<module_name>.py`) to the working directory.",
    )
    def ast_write_module_file(
        filename: str,
        code: str,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        return prog.write_local_module_file(
            filename=filename,
            code=code,
            overwrite=overwrite,
            source="mcp",
        )

    @server.tool(
        name="ast_chain",
        description="Execute a sequential chain of CRUD / eval / convert / install / dynamic AST commands with `hal+json` recovery.",
    )
    async def ast_chain(
        commands: list[dict[str, Any]],
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        return await prog.execute_chain(commands=commands, ctx=ctx, source="mcp")

    @server.tool(
        name="ast_generate_line",
        description="Use Gemini to generate a single context-aware Python/JSON line for an AST function.",
    )
    def ast_generate_line(
        prompt: str,
        prefix: str = "",
        suffix: str = "",
        target_name: str = "",
        language: str = "python",
    ) -> dict[str, Any]:
        return prog.generate_line(
            prompt=prompt,
            prefix=prefix,
            suffix=suffix,
            target_name=target_name,
            language=language,
            source="mcp",
        )

    @server.tool(
        name="ast_run_tests",
        description=(
            "Run unit/smoke test cases against a compiled AST function (auto-generates type-inferred "
            "smoke tests if `cases` is omitted) and return pass/fail assertions, duration_ms, stdout, and pytest export."
        ),
    )
    def ast_run_tests(
        name: str,
        cases: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        return prog.run_tests(name=name, cases=cases, source="mcp")

    @server.tool(
        name="ast_analyze",
        description=(
            "Inspect cyclomatic complexity, nesting depth, type coverage, call-graph impact (calls & called_by), "
            "pytest stub, and optional unified diff preview for a candidate code change."
        ),
    )
    def ast_analyze(
        name: str = "",
        candidate_code: str = "",
    ) -> dict[str, Any]:
        return prog.analyze(
            name=(name.strip() or None),
            candidate_code=(candidate_code if candidate_code else None),
            source="mcp",
        )

    @server.tool(
        name="ast_rollback",
        description="Atomically roll back an AST function to its previous (or specified) version with dependency re-verification.",
    )
    async def ast_rollback(
        name: str,
        version: int | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        return await prog.rollback(name=name, version=version, ctx=ctx, source="mcp")

    return server, prog


def _resolve_disabled_servers(
    raw_items: list[str] | None,
    no_mcp: bool,
    no_ui: bool,
    no_stdin: bool,
) -> set[str]:
    disabled: set[str] = set()
    alias_map = {
        "mcp": "mcp",
        "mcp_server": "mcp",
        "fastmcp": "mcp",
        "ui": "ui",
        "http": "ui",
        "rest": "ui",
        "dashboard": "ui",
        "web": "ui",
        "stdin": "stdin",
        "cli": "stdin",
        "bridge": "stdin",
    }
    for entry in raw_items or []:
        for token in str(entry).split(","):
            norm = token.strip().lower()
            if not norm:
                continue
            if norm == "all":
                disabled.update({"mcp", "ui", "stdin"})
            elif norm in alias_map:
                disabled.add(alias_map[norm])
            else:
                raise ValueError(
                    f"Unknown server identifier `{token}` for --disable-server. "
                    "Valid options: mcp, ui (or http/rest/dashboard), stdin (or cli), all."
                )
    if no_mcp:
        disabled.add("mcp")
    if no_ui:
        disabled.add("ui")
    if no_stdin:
        disabled.add("stdin")
    return disabled


def main() -> None:
    parser = argparse.ArgumentParser(description="Full Dynamic AST + HATEOAS REST API + Live UI MCP Server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http"],
        default=os.getenv("MCP_TRANSPORT", "streamable-http"),
        help="MCP transport protocol (default: streamable-http).",
    )
    parser.add_argument(
        "--host",
        "--mcp-host",
        dest="mcp_host",
        default=os.getenv("MCP_HOST", "0.0.0.0"),
        help="Bind host for the MCP server (default: 0.0.0.0).",
    )
    parser.add_argument(
        "--port",
        "--mcp-port",
        dest="mcp_port",
        type=int,
        default=int(os.getenv("MCP_PORT", "8000")),
        help="Bind port for the MCP streamable-http server (default: 8000).",
    )
    parser.add_argument(
        "--path",
        "--mcp-path",
        dest="mcp_path",
        default=os.getenv("MCP_PATH", "/mcp"),
        help="HTTP endpoint path for MCP streamable-http (default: /mcp).",
    )
    parser.add_argument(
        "--ui-host",
        dest="ui_host",
        default=os.getenv("UI_HOST", ""),
        help="Bind host for the Live UI & HATEOAS REST API server (defaults to --mcp-host).",
    )
    parser.add_argument(
        "--ui-port",
        "--http-port",
        dest="ui_port",
        type=int,
        default=int(os.getenv("UI_PORT", "8080")),
        help="Bind port for the Live UI & HATEOAS REST API server (default: 8080).",
    )
    parser.add_argument(
        "--disable-server",
        "--disable",
        dest="disable_servers",
        action="append",
        default=[],
        help="Disable one or more servers (comma-separated or repeated): mcp, ui (http/rest), stdin (cli), all.",
    )
    parser.add_argument("--no-mcp", action="store_true", help="Disable the MCP server.")
    parser.add_argument("--no-ui", "--no-http", action="store_true", help="Disable the Live UI & REST HTTP server.")
    parser.add_argument("--no-stdin", action="store_true", help="Disable the STDIN-to-HTTP interactive CLI bridge.")

    args = parser.parse_args()
    ui_host = args.ui_host.strip() or args.mcp_host

    env_disabled = [os.getenv("DISABLE_SERVERS", "")]
    disabled = _resolve_disabled_servers(
        raw_items=[*env_disabled, *args.disable_servers],
        no_mcp=args.no_mcp,
        no_ui=args.no_ui,
        no_stdin=args.no_stdin,
    )

    mcp_enabled = "mcp" not in disabled
    ui_enabled = "ui" not in disabled
    stdin_enabled = ("stdin" not in disabled) and ui_enabled and (args.transport != "stdio" or not mcp_enabled)

    if not mcp_enabled and not ui_enabled:
        print("[INFO] Both MCP and HTTP/UI servers are disabled (`--disable-server`). Exiting cleanly.")
        return

    is_stdio = mcp_enabled and (args.transport == "stdio")
    mcp, prog = create_server(
        host=args.mcp_host,
        port=args.mcp_port,
        streamable_http_path=args.mcp_path,
        stdout_is_mcp_stdio=is_stdio,
    )

    bg_loop = asyncio.new_event_loop()
    bg_thread = threading.Thread(target=bg_loop.run_forever, daemon=True)
    bg_thread.start()

    httpd = None
    ui_url = f"http://{ui_host}:{args.ui_port}"
    try:
        if ui_enabled:
            httpd = start_web_dashboard(prog=prog, loop=bg_loop, host=ui_host, port=args.ui_port)

        if not is_stdio:
            if stdin_enabled:
                start_stdin_to_http_bridge(ui_url)
            if ui_enabled:
                print(f"Live HTML/HTTP Dashboard : {colorize(ui_url + '/', 'cyan')}")
                print(f"HATEOAS REST API Root    : {colorize(ui_url + '/api', 'cyan')}")
            else:
                print(f"Live HTML/HTTP Dashboard : {colorize('DISABLED (--disable-server ui)', 'dim')}")
            if mcp_enabled:
                print(
                    f"MCP Streamable HTTP URL  : "
                    f"{colorize(f'http://{args.mcp_host}:{args.mcp_port}{args.mcp_path}', 'magenta')}"
                )
            else:
                print(f"MCP Server               : {colorize('DISABLED (--disable-server mcp)', 'dim')}")
            print(
                colorize("Legend:", "bold")
                + f" Sources=[{colorize('STDIN', 'yellow')} | {colorize('REST/HTTP', 'cyan')} | {colorize('MCP', 'magenta')}]"
                + f" Routes=[{colorize('AST_REGISTRY', 'blue')} | {colorize('EVAL_RUNTIME', 'green')} | {colorize('CONVERTER', 'teal')} | {colorize('CHAIN_ORCHESTRATOR', 'purple')} | {colorize('MODULE_ENV', 'white')}]"
            )
            if stdin_enabled:
                print("Type commands in stdin (e.g. `eval entrypoint 3`, `eval ast1 [3, 4]`, `read`):")
            if mcp_enabled:
                mcp.run(transport="streamable-http")
            else:
                threading.Event().wait()
        else:
            if ui_enabled:
                sys.stderr.write(f"Live HTML/HTTP Dashboard : {ui_url}/\n")
                sys.stderr.write(f"HATEOAS REST API Root    : {ui_url}/api\n")
                sys.stderr.flush()
            mcp.run(transport="stdio")
    except KeyboardInterrupt:
        pass
    finally:
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
        if bg_loop.is_running():
            bg_loop.call_soon_threadsafe(bg_loop.stop)
        bg_thread.join(timeout=2.0)
        bg_loop.close()
