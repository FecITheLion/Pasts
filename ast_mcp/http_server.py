"""HTTP Dashboard, SSE live event stream, and HATEOAS REST `/api` server."""

from __future__ import annotations

import asyncio
import json
import queue
import sys
import threading
from functools import lru_cache
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from .gemini_line import GeminiLineError, chat_with_gemini_mcp_tools
from .hal import build_hal_response, build_missing_call_hal_recovery, enrich_rest_hal
from .program import DynamicASTMcpProgram

MAX_BODY_BYTES = 2 * 1024 * 1024  # 2 MiB upper bound per HTTP JSON request
DEFAULT_HTTP_TIMEOUT = 150.0


class PayloadTooLargeError(ValueError):
    """Raised when an incoming HTTP request Content-Length exceeds MAX_BODY_BYTES."""


class QuietThreadingHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


@lru_cache(maxsize=1)
def load_dashboard_html() -> bytes:
    """Read and cache the standalone dashboard HTML document as UTF-8 bytes."""
    html_path = Path(__file__).with_name("dashboard.html")
    return html_path.read_text(encoding="utf-8").encode("utf-8")


def parse_stdin_line(line: str) -> dict[str, Any]:
    """Parse an interactive CLI / STDIN command line into a normalized tool dispatch dict."""
    stripped = line.strip()
    if not stripped:
        return {"tool": "ast_read", "arguments": {"name": ""}, "source": "stdin"}

    if stripped.startswith("{"):
        parsed = json.loads(stripped)
        if not isinstance(parsed, dict):
            raise ValueError("JSON command line must decode to an object.")
        parsed.setdefault("source", "stdin")
        return parsed

    parts = stripped.split(None, 2)
    cmd = parts[0].lower()

    def _extract_name_body_and_flag(raw_line: str) -> tuple[str, str, bool]:
        found_flag = False
        rest = raw_line[len(parts[0]) :].lstrip()
        if rest == "--no-roundtrip" or rest.startswith("--no-roundtrip "):
            found_flag = True
            rest = rest[len("--no-roundtrip") :].lstrip()
        sub = rest.split(None, 1)
        if not sub:
            return "", "", found_flag
        target_name = sub[0]
        body_text = sub[1] if len(sub) > 1 else ""
        if body_text == "--no-roundtrip" or body_text.startswith("--no-roundtrip "):
            found_flag = True
            body_text = body_text[len("--no-roundtrip") :].lstrip()
        elif body_text.endswith(" --no-roundtrip"):
            found_flag = True
            body_text = body_text[: -len(" --no-roundtrip")].rstrip()
        return target_name, body_text, found_flag

    if cmd == "read":
        return {
            "tool": "ast_read",
            "arguments": {"name": parts[1] if len(parts) > 1 else ""},
            "source": "stdin",
        }
    if cmd == "delete" and len(parts) >= 2:
        return {"tool": "ast_delete", "arguments": {"name": parts[1]}, "source": "stdin"}
    if cmd == "create" and len(parts) >= 2:
        target_name, body_text, skip_rt = _extract_name_body_and_flag(stripped)
        if target_name:
            return {
                "tool": "ast_create",
                "arguments": {
                    "name": target_name,
                    "code_or_ast": body_text,
                    "overwrite": True,
                    "skip_roundtrip": skip_rt,
                },
                "source": "stdin",
            }
    if cmd == "update" and len(parts) >= 3:
        target_name, body_text, skip_rt = _extract_name_body_and_flag(stripped)
        if target_name and body_text:
            return {
                "tool": "ast_update",
                "arguments": {
                    "name": target_name,
                    "code_or_ast": body_text,
                    "skip_roundtrip": skip_rt,
                },
                "source": "stdin",
            }
    if cmd == "convert" and len(parts) >= 3:
        return {
            "tool": "ast_convert",
            "arguments": {"source_kind": parts[1], "target_kind": "pydantic", "payload": parts[2]},
            "source": "stdin",
        }
    if cmd == "install" and len(parts) >= 2:
        return {
            "tool": "ast_install_module",
            "arguments": {"module_name": parts[1]},
            "source": "stdin",
        }
    if cmd == "eval" and len(parts) >= 2:
        target = parts[1]
        eval_payload: Any = []
        if len(parts) == 3:
            try:
                eval_payload = json.loads(parts[2])
            except Exception:
                eval_payload = parts[2]
        return {
            "tool": "eval_ast_call",
            "arguments": {"target": target, "args": eval_payload},
            "source": "stdin",
        }

    return {"tool": "eval_ast_call", "arguments": {"target": stripped, "args": []}, "source": "stdin"}


def _http_status_for_hal(res: dict[str, Any], default_ok: int = 200) -> int:
    if res.get("status") != "error":
        return default_ok
    if isinstance(res.get("http_status"), int) and 400 <= res["http_status"] < 600:
        return res["http_status"]
    err_type = res.get("error_type", "")
    if err_type == "missing_ast_call":
        return 404
    if err_type in ("already_exists", "file_already_exists"):
        return 409
    if err_type in ("validation_error", "invalid_json", "missing_cli_command"):
        return 400
    return 422


async def dispatch_tool(
    prog: DynamicASTMcpProgram,
    tool: str,
    args: Any,
    source: str = "rest",
) -> dict[str, Any]:
    """Explicitly dispatch a validated command/tool payload without raw kwarg unpacking."""
    arg_map = args if isinstance(args, dict) else {}

    if tool == "ast_create":
        return await prog.create(
            name=str(arg_map.get("name", "")).strip(),
            code_or_ast=arg_map.get("code_or_ast"),
            description=arg_map.get("description"),
            expose_as_tool=bool(arg_map.get("expose_as_tool", True)),
            overwrite=bool(arg_map.get("overwrite", False)),
            skip_roundtrip=bool(arg_map.get("skip_roundtrip", False)),
            source=source,
        )
    if tool == "ast_read":
        raw_name = arg_map.get("name")
        return prog.read(name=(str(raw_name).strip() if raw_name else None), source=source)
    if tool == "ast_update":
        return await prog.update(
            name=str(arg_map.get("name", "")).strip(),
            code_or_ast=arg_map.get("code_or_ast", ""),
            description=arg_map.get("description"),
            expose_as_tool=bool(arg_map.get("expose_as_tool", True)),
            skip_roundtrip=bool(arg_map.get("skip_roundtrip", False)),
            source=source,
        )
    if tool == "ast_delete":
        return await prog.delete(name=str(arg_map.get("name", "")).strip(), source=source)
    if tool == "eval_ast_call":
        if isinstance(args, dict) and ("target" in args or "expression" in args):
            return prog.eval_target(
                target=str(args.get("target") or args.get("expression") or ""),
                args=args.get("args"),
                kwargs=args.get("kwargs"),
                source=source,
            )
        return prog.eval_target(target=str(args), source=source)
    if tool == "ast_chain":
        commands = arg_map.get("commands")
        if not isinstance(commands, list):
            commands = []
        return await prog.execute_chain(commands=commands, source=source)
    if tool == "ast_convert":
        return prog.convert(
            source_kind=str(arg_map.get("source_kind", "saved_ast")),
            target_kind=str(arg_map.get("target_kind", "pydantic")),
            payload=arg_map.get("payload", ""),
            model_name=str(arg_map.get("model_name", "GeneratedModel")),
            source=source,
        )
    if tool == "ast_install_module":
        return prog.install_module(
            module_name=str(arg_map.get("module_name", "")),
            package_name=arg_map.get("package_name"),
            exported_symbols=arg_map.get("exported_symbols"),
            caller_context=str(arg_map.get("caller_context", "ast_function")),
            source=source,
        )
    if tool == "ast_write_module_file":
        return prog.write_local_module_file(
            filename=str(arg_map.get("filename", "")),
            code=str(arg_map.get("code", "")),
            overwrite=bool(arg_map.get("overwrite", False)),
            source=source,
        )
    if tool == "ast_generate_line":
        return prog.generate_line(
            prompt=str(arg_map.get("prompt", "")),
            prefix=str(arg_map.get("prefix", "")),
            suffix=str(arg_map.get("suffix", "")),
            target_name=str(arg_map.get("target_name", "")),
            language=str(arg_map.get("language", "python")),
            source=source,
        )
    if tool == "ast_run_tests":
        cases = arg_map.get("cases")
        return prog.run_tests(
            name=str(arg_map.get("name", "")).strip(),
            cases=(cases if isinstance(cases, list) else None),
            source=source,
        )
    if tool == "ast_analyze":
        raw_name = arg_map.get("name")
        cand = arg_map.get("candidate_code")
        return prog.analyze(
            name=(str(raw_name).strip() if raw_name else None),
            candidate_code=(str(cand) if cand is not None and str(cand) != "" else None),
            source=source,
        )
    if tool == "ast_rollback":
        ver = arg_map.get("version")
        return await prog.rollback(
            name=str(arg_map.get("name", "")).strip(),
            version=(int(ver) if isinstance(ver, int) else None),
            source=source,
        )
    if tool == "ast_list_mcp_tools":
        tools = prog.list_mcp_tools()
        return build_hal_response(
            self_href="ast://mcp/tools",
            data={"status": "ok", "count": len(tools), "tools": tools},
            links={"list": {"tool": "ast_read", "arguments": {}}},
        )
    if tool in prog.registry:
        return prog.eval_target(target=tool, args=args, source=source)
    return build_missing_call_hal_recovery(
        missing_name=str(tool),
        failed_tool="eval_ast_call",
        failed_params={"target": tool, "args": []},
        reason=f"Unknown command/function `{tool}`.",
    )


def start_web_dashboard(
    prog: DynamicASTMcpProgram,
    loop: asyncio.AbstractEventLoop,
    host: str = "0.0.0.0",
    port: int = 8080,
) -> QuietThreadingHTTPServer:
    """Start the HTTP server serving the Live UI, SSE stream, and HATEOAS REST `/api`."""

    class DashboardHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _send_bytes(self, body: bytes, content_type: str, status: int = 200) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, PATCH, DELETE, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Accept")
            self.end_headers()
            if body:
                self.wfile.write(body)
            self.wfile.flush()
            self.close_connection = True

        def _send_hal(
            self,
            payload: dict[str, Any],
            request_href: str,
            request_method: str,
            status: int = 200,
        ) -> None:
            enriched = enrich_rest_hal(payload, request_href=request_href, request_method=request_method)
            self._send_bytes(
                json.dumps(enriched, indent=2).encode("utf-8"),
                "application/hal+json; charset=utf-8",
                status=status,
            )

        def _read_any_json_body(self) -> Any:
            raw_len = self.headers.get("Content-Length")
            if raw_len is None or raw_len == "":
                return None
            try:
                length = int(raw_len)
            except ValueError as exc:
                raise ValueError(f"Invalid Content-Length header: {raw_len!r}") from exc
            if length < 0:
                raise ValueError("Content-Length must be non-negative.")
            if length > MAX_BODY_BYTES:
                raise PayloadTooLargeError(
                    f"Request body ({length} bytes) exceeds limit of {MAX_BODY_BYTES} bytes."
                )
            if length == 0:
                return None
            raw = self.rfile.read(length).decode("utf-8").strip()
            if not raw:
                return None
            return json.loads(raw)

        def _read_json_object_body(self) -> dict[str, Any]:
            parsed = self._read_any_json_body()
            if parsed is None:
                return {}
            if not isinstance(parsed, dict):
                raise ValueError("Request JSON body must be a JSON object.")
            return parsed

        def _read_body_or_send_error(self, path: str, method: str, require_object: bool = True) -> tuple[bool, Any]:
            try:
                body = self._read_json_object_body() if require_object else self._read_any_json_body()
                return True, body
            except PayloadTooLargeError as exc:
                self._send_hal(
                    {"status": "error", "error_type": "payload_too_large", "message": str(exc)},
                    request_href=path,
                    request_method=method,
                    status=413,
                )
                return False, None
            except Exception as exc:
                self._send_hal(
                    {"status": "error", "error_type": "invalid_json", "message": str(exc)},
                    request_href=path,
                    request_method=method,
                    status=400,
                )
                return False, None

        def _run_coro(self, coro: Any) -> dict[str, Any]:
            fut = asyncio.run_coroutine_threadsafe(coro, loop)
            return fut.result(timeout=DEFAULT_HTTP_TIMEOUT)

        def do_OPTIONS(self) -> None:
            self._send_bytes(b"", "text/plain", status=204)

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"

            if path == "/":
                self._send_bytes(load_dashboard_html(), "text/html; charset=utf-8")
                return

            if path == "/api/snapshot":
                self._send_hal(prog.get_dashboard_snapshot(), request_href="/api/snapshot", request_method="GET")
                return

            if path == "/api/events":
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache, no-transform")
                self.send_header("Connection", "keep-alive")
                self.send_header("X-Accel-Buffering", "no")
                self.end_headers()
                self.wfile.flush()
                self.close_connection = False

                q = prog.subscribe_sse()
                try:
                    init_msg = json.dumps({"type": "init", "snapshot": prog.get_dashboard_snapshot()})
                    self.wfile.write(f"data: {init_msg}\n\n".encode("utf-8"))
                    self.wfile.flush()
                    while True:
                        try:
                            msg = q.get(timeout=15.0)
                            self.wfile.write(f"data: {msg}\n\n".encode("utf-8"))
                            self.wfile.flush()
                        except queue.Empty:
                            self.wfile.write(b": keepalive\n\n")
                            self.wfile.flush()
                except Exception:
                    pass
                finally:
                    prog.unsubscribe_sse(q)
                    self.close_connection = True
                return

            if path == "/api":
                registry_listing = prog.read(None, source="rest", log_event=False)
                root_doc = {
                    "status": "ok",
                    "service": "Dynamic AST HATEOAS REST API",
                    "cli_commands": [
                        "read [name]",
                        "create <name> [code_or_ast] [--no-roundtrip]",
                        "update <name> <code_or_ast> [--no-roundtrip]",
                        "delete <name>",
                        "eval <target> [literal | list | list_then_object | object]",
                        "convert <source_kind> <payload>",
                        "install <module_name>",
                    ],
                    "_links": {
                        "list_ast": {"href": "/api/ast", "method": "GET", "cli": "read"},
                        "create_ast": {
                            "href": "/api/ast",
                            "method": "POST",
                            "cli": "create <name> <code_or_ast>",
                            "body_template": {
                                "name": "fn_name",
                                "code_or_ast": "def fn_name(x: int) -> int:\n    return x + 1",
                                "expose_as_tool": True,
                                "overwrite": False,
                                "skip_roundtrip": False,
                            },
                        },
                        "eval": {
                            "href": "/api/eval",
                            "method": "POST",
                            "cli": "eval <target> [args]",
                            "body_template": {"target": "entrypoint", "args": [3], "kwargs": {}},
                        },
                        "chain": {
                            "href": "/api/chain",
                            "method": "POST",
                            "body_template": {"commands": []},
                        },
                        "convert": {
                            "href": "/api/convert",
                            "method": "POST",
                            "cli": "convert <source_kind> <payload>",
                            "body_template": {
                                "source_kind": "saved_ast",
                                "target_kind": "pydantic",
                                "payload": "entrypoint",
                                "model_name": "GeneratedModel",
                            },
                        },
                        "install_module": {
                            "href": "/api/modules/install",
                            "href_template": "/api/modules/{module_name}/install",
                            "templated": True,
                            "method": "POST",
                            "cli": "install <module_name>",
                            "body_template": {"module_name": "requests"},
                        },
                        "write_module_file": {
                            "href": "/api/modules/files",
                            "method": "POST",
                            "body_template": {"filename": "custom_mod.py", "code": "", "overwrite": True},
                        },
                        "cli_execute": {
                            "href": "/api/cli",
                            "method": "POST",
                            "body_template": {"command": "eval entrypoint [3]"},
                        },
                        "generate_line": {
                            "href": "/api/gemini/generate-line",
                            "method": "POST",
                            "body_template": {
                                "prompt": "return x * 2 + 1",
                                "prefix": "def fn(x: int) -> int:\n    ",
                                "suffix": "",
                                "target_name": "fn",
                                "language": "python",
                            },
                        },
                        "gemini_chat": {
                            "href": "/api/gemini/chat",
                            "method": "POST",
                            "body_template": {
                                "message": "Inspect the registry, evaluate entrypoint(3), and summarize the complexity metrics.",
                                "history": [],
                            },
                        },
                        "mcp_tools": {"href": "/api/mcp/tools", "method": "GET"},
                        "graph": {"href": "/api/graph", "method": "GET"},
                        "snapshot": {"href": "/api/snapshot", "method": "GET"},
                        "events_sse": {"href": "/api/events", "method": "GET"},
                    },
                    "_embedded": registry_listing.get("_embedded", {}),
                }
                self._send_hal(root_doc, request_href="/api", request_method="GET")
                return

            if path == "/api/graph":
                self._send_hal(prog.get_graph(), request_href="/api/graph", request_method="GET")
                return

            if path == "/api/mcp/tools":
                tools = prog.list_mcp_tools()
                self._send_hal(
                    {"status": "ok", "count": len(tools), "tools": tools},
                    request_href="/api/mcp/tools",
                    request_method="GET",
                )
                return

            if path == "/api/ast":
                res = prog.read(None, source="rest")
                self._send_hal(res, request_href="/api/ast", request_method="GET", status=_http_status_for_hal(res))
                return

            if path.startswith("/api/ast/") and path.endswith("/analyze"):
                name = unquote(path[len("/api/ast/") : -len("/analyze")])
                res = prog.analyze(name=name, source="rest")
                self._send_hal(
                    res,
                    request_href=f"/api/ast/{name}/analyze",
                    request_method="GET",
                    status=_http_status_for_hal(res),
                )
                return

            if path.startswith("/api/ast/") and path.endswith("/history"):
                name = unquote(path[len("/api/ast/") : -len("/history")])
                res = prog.get_history(name=name, source="rest")
                self._send_hal(
                    res,
                    request_href=f"/api/ast/{name}/history",
                    request_method="GET",
                    status=_http_status_for_hal(res),
                )
                return

            if path.startswith("/api/ast/") and "/" not in path[len("/api/ast/") :]:
                name = unquote(path[len("/api/ast/") :])
                res = prog.read(name, source="rest")
                self._send_hal(
                    res,
                    request_href=f"/api/ast/{name}",
                    request_method="GET",
                    status=_http_status_for_hal(res),
                )
                return

            self._send_hal(
                {
                    "status": "error",
                    "error_type": "route_not_found",
                    "message": f"Unknown GET route `{path}`. Follow `_links.api_root` for valid endpoints.",
                },
                request_href=path,
                request_method="GET",
                status=404,
            )

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"

            if path.startswith("/api/ast/") and path.endswith("/eval"):
                name = unquote(path[len("/api/ast/") : -len("/eval")])
                ok, raw_payload = self._read_body_or_send_error(path, "POST", require_object=False)
                if not ok:
                    return
                res = prog.eval_target(target=name, args=raw_payload, source="rest")
                self._send_hal(
                    res,
                    request_href=f"/api/ast/{name}/eval",
                    request_method="POST",
                    status=_http_status_for_hal(res),
                )
                return

            ok, body = self._read_body_or_send_error(path, "POST", require_object=True)
            if not ok:
                return

            if path == "/api/command":
                tool = str(body.get("tool", ""))
                args = body.get("arguments") if "arguments" in body else body.get("args", {})
                source = str(body.get("source", "http"))
                res = self._run_coro(dispatch_tool(prog, tool, args, source=source))
                self._send_hal(
                    res,
                    request_href="/api/command",
                    request_method="POST",
                    status=_http_status_for_hal(res),
                )
                return

            if path == "/api/cli":
                raw_cmd = str(body.get("command") or body.get("line") or "").strip()
                if not raw_cmd:
                    self._send_hal(
                        {
                            "status": "error",
                            "error_type": "missing_cli_command",
                            "message": 'Provide `{"command": "<cli command>"}` in the JSON body.',
                        },
                        request_href="/api/cli",
                        request_method="POST",
                        status=400,
                    )
                    return
                try:
                    parsed_cli = parse_stdin_line(raw_cmd)
                except Exception as exc:
                    self._send_hal(
                        {"status": "error", "error_type": "invalid_cli_command", "message": str(exc)},
                        request_href="/api/cli",
                        request_method="POST",
                        status=400,
                    )
                    return
                res = self._run_coro(
                    dispatch_tool(
                        prog,
                        str(parsed_cli.get("tool", "")),
                        parsed_cli.get("arguments") or {},
                        source=str(body.get("source", "rest_cli")),
                    )
                )
                res["parsed_cli"] = parsed_cli
                self._send_hal(
                    res,
                    request_href="/api/cli",
                    request_method="POST",
                    status=_http_status_for_hal(res),
                )
                return

            if path == "/api/ast":
                name = str(body.get("name", "")).strip()
                if not name:
                    self._send_hal(
                        {
                            "status": "error",
                            "error_type": "validation_error",
                            "message": "`name` is required in POST /api/ast.",
                        },
                        request_href="/api/ast",
                        request_method="POST",
                        status=400,
                    )
                    return
                res = self._run_coro(
                    prog.create(
                        name=name,
                        code_or_ast=body.get("code_or_ast"),
                        description=body.get("description"),
                        expose_as_tool=bool(body.get("expose_as_tool", True)),
                        overwrite=bool(body.get("overwrite", False)),
                        skip_roundtrip=bool(body.get("skip_roundtrip", False)),
                        source="rest",
                    )
                )
                self._send_hal(
                    res,
                    request_href="/api/ast",
                    request_method="POST",
                    status=_http_status_for_hal(res, default_ok=201),
                )
                return

            if path == "/api/eval":
                target = str(body.get("target") or body.get("expression") or "").strip()
                extra_named = {
                    k: v
                    for k, v in body.items()
                    if k not in ("target", "expression", "args", "kwargs")
                }
                merged_kwargs: dict[str, Any] = {}
                if isinstance(body.get("kwargs"), dict):
                    merged_kwargs.update(body["kwargs"])
                merged_kwargs.update(extra_named)

                res = prog.eval_target(
                    target=target,
                    args=body.get("args"),
                    kwargs=merged_kwargs or None,
                    source="rest",
                )
                self._send_hal(
                    res,
                    request_href="/api/eval",
                    request_method="POST",
                    status=_http_status_for_hal(res),
                )
                return

            if path == "/api/chain":
                commands = body.get("commands")
                res = self._run_coro(
                    prog.execute_chain(commands=(commands if isinstance(commands, list) else []), source="rest")
                )
                self._send_hal(
                    res,
                    request_href="/api/chain",
                    request_method="POST",
                    status=_http_status_for_hal(res),
                )
                return

            if path == "/api/convert":
                res = prog.convert(
                    source_kind=str(body.get("source_kind", "saved_ast")),
                    target_kind=str(body.get("target_kind", "pydantic")),
                    payload=body.get("payload", ""),
                    model_name=str(body.get("model_name", "GeneratedModel")),
                    source="rest",
                )
                self._send_hal(
                    res,
                    request_href="/api/convert",
                    request_method="POST",
                    status=_http_status_for_hal(res),
                )
                return

            if path == "/api/modules/install" or (path.startswith("/api/modules/") and path.endswith("/install")):
                if path == "/api/modules/install":
                    mod_name = unquote(str(body.get("module_name", ""))).strip()
                else:
                    mod_name = unquote(path[len("/api/modules/") : -len("/install")]).strip()
                res = prog.install_module(
                    module_name=mod_name,
                    package_name=body.get("package_name"),
                    exported_symbols=body.get("exported_symbols"),
                    caller_context=str(body.get("caller_context", "ast_function")),
                    source="rest",
                )
                self._send_hal(
                    res,
                    request_href=f"/api/modules/{mod_name}/install" if mod_name else "/api/modules/install",
                    request_method="POST",
                    status=_http_status_for_hal(res),
                )
                return

            if path == "/api/modules/files":
                res = prog.write_local_module_file(
                    filename=str(body.get("filename", "")),
                    code=str(body.get("code", "")),
                    overwrite=bool(body.get("overwrite", False)),
                    source="rest",
                )
                self._send_hal(
                    res,
                    request_href="/api/modules/files",
                    request_method="POST",
                    status=_http_status_for_hal(res, default_ok=201),
                )
                return

            if path == "/api/gemini/generate-line":
                res = prog.generate_line(
                    prompt=str(body.get("prompt", "")),
                    prefix=str(body.get("prefix", "")),
                    suffix=str(body.get("suffix", "")),
                    target_name=str(body.get("target_name", "")),
                    language=str(body.get("language", "python")),
                    source="rest",
                )
                self._send_hal(
                    res,
                    request_href="/api/gemini/generate-line",
                    request_method="POST",
                    status=_http_status_for_hal(res),
                )
                return

            if path == "/api/gemini/chat":
                message = str(body.get("message", "")).strip()
                raw_history = body.get("history") if isinstance(body.get("history"), list) else []
                try:
                    chat_out = chat_with_gemini_mcp_tools(
                        message=message,
                        mcp_tools=prog.list_mcp_tools(),
                        tool_executor=lambda t_name, t_args: self._run_coro(
                            dispatch_tool(prog, t_name, t_args, source="gemini_mcp")
                        ),
                        history=raw_history,
                    )
                    res = {
                        "status": "ok",
                        "model": chat_out["model"],
                        "reply": chat_out["reply"],
                        "tool_traces": chat_out["tool_traces"],
                    }
                except GeminiLineError as exc:
                    res = {
                        "status": "error",
                        "error_type": exc.error_type,
                        "http_status": exc.status_code,
                        "message": str(exc),
                    }
                self._send_hal(
                    res,
                    request_href="/api/gemini/chat",
                    request_method="POST",
                    status=_http_status_for_hal(res),
                )
                return

            if path.startswith("/api/ast/") and path.endswith("/diff"):
                name = unquote(path[len("/api/ast/") : -len("/diff")])
                cand = body.get("candidate_code") if "candidate_code" in body else body.get("code_or_ast", "")
                res = prog.analyze(name=name, candidate_code=str(cand or ""), source="rest")
                self._send_hal(
                    res,
                    request_href=f"/api/ast/{name}/diff",
                    request_method="POST",
                    status=_http_status_for_hal(res),
                )
                return

            if path.startswith("/api/ast/") and path.endswith("/test"):
                name = unquote(path[len("/api/ast/") : -len("/test")])
                cases = body.get("cases") if isinstance(body.get("cases"), list) else None
                res = prog.run_tests(name=name, cases=cases, source="rest")
                self._send_hal(
                    res,
                    request_href=f"/api/ast/{name}/test",
                    request_method="POST",
                    status=_http_status_for_hal(res),
                )
                return

            if path.startswith("/api/ast/") and path.endswith("/rollback"):
                name = unquote(path[len("/api/ast/") : -len("/rollback")])
                ver = body.get("version")
                res = self._run_coro(
                    prog.rollback(
                        name=name,
                        version=(int(ver) if isinstance(ver, int) else None),
                        source="rest",
                    )
                )
                self._send_hal(
                    res,
                    request_href=f"/api/ast/{name}/rollback",
                    request_method="POST",
                    status=_http_status_for_hal(res),
                )
                return

            self._send_hal(
                {
                    "status": "error",
                    "error_type": "route_not_found",
                    "message": f"Unknown POST route `{path}`. Follow `_links.api_root` for valid endpoints.",
                },
                request_href=path,
                request_method="POST",
                status=404,
            )

        def do_PUT(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"

            if path.startswith("/api/ast/") and "/" not in path[len("/api/ast/") :]:
                name = unquote(path[len("/api/ast/") :])
                ok, body = self._read_body_or_send_error(path, "PUT", require_object=True)
                if not ok:
                    return

                res = self._run_coro(
                    prog.update(
                        name=name,
                        code_or_ast=body.get("code_or_ast", ""),
                        description=body.get("description"),
                        expose_as_tool=bool(body.get("expose_as_tool", True)),
                        skip_roundtrip=bool(body.get("skip_roundtrip", False)),
                        source="rest",
                    )
                )
                self._send_hal(
                    res,
                    request_href=f"/api/ast/{name}",
                    request_method="PUT",
                    status=_http_status_for_hal(res),
                )
                return

            self._send_hal(
                {
                    "status": "error",
                    "error_type": "route_not_found",
                    "message": f"Unknown PUT route `{path}`.",
                },
                request_href=path,
                request_method="PUT",
                status=404,
            )

        def do_PATCH(self) -> None:
            self.do_PUT()

        def do_DELETE(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"

            if path.startswith("/api/ast/") and "/" not in path[len("/api/ast/") :]:
                name = unquote(path[len("/api/ast/") :])
                res = self._run_coro(prog.delete(name=name, source="rest"))
                self._send_hal(
                    res,
                    request_href=f"/api/ast/{name}",
                    request_method="DELETE",
                    status=_http_status_for_hal(res),
                )
                return

            self._send_hal(
                {
                    "status": "error",
                    "error_type": "route_not_found",
                    "message": f"Unknown DELETE route `{path}`.",
                },
                request_href=path,
                request_method="DELETE",
                status=404,
            )

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = QuietThreadingHTTPServer((host, port), DashboardHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server
