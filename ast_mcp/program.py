"""Core DynamicASTMcpProgram runtime, registry, compilation cache, and SSE broadcasting."""

from __future__ import annotations

import ast
import contextlib
import importlib
import importlib.util
import inspect
import io
import json
import keyword
import os
import queue
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import asdict
from typing import Any
from urllib.parse import unquote

from .analyzer import (
    build_dependency_graph,
    compute_function_metrics,
    compute_unified_diff,
    generate_default_test_cases,
    generate_pytest_snippet,
)
from .ast_core import (
    compile_function_from_ast,
    deserialize_ast_node,
    export_all_formats,
    find_called_names,
    parse_expression_or_ast,
    serialize_function,
    verify_and_roundtrip_ast,
)
from .console import ROUTE_META, SOURCE_COLORS, colorize
from .converter import convert_structure
from .deps import (
    CircularDependencyError,
    MissingASTCallError,
    MissingModuleDependencyError,
    extract_ast_import_requirements,
    generate_fallback_module_script,
    invalidate_package_cache,
    is_module_available,
    resolve_package_name,
    validate_package_name,
)
from .gemini_line import DEFAULT_GEMINI_MODEL, GeminiLineError, generate_line_with_gemini
from .hal import (
    build_circular_dependency_hal_recovery,
    build_hal_response,
    build_lint_hal_response,
    build_missing_call_hal_recovery,
    build_missing_module_hal_recovery,
    enrich_rest_hal,
)
from .linter import lint_and_autofix_input
from .signature import (
    OMITTED_SENTINEL,
    build_signature_mismatch_hal_recovery,
    check_and_build_missing_default_recovery,
    normalize_eval_call_args,
    parse_eval_args_with_omissions,
)

try:
    from mcp.server.fastmcp import Context, FastMCP

    HAS_MCP = True
    MCP_IMPORT_ERROR_MSG = ""
except Exception as _mcp_import_err:
    HAS_MCP = False
    MCP_IMPORT_ERROR_MSG = str(_mcp_import_err)
    Context = Any  # type: ignore[misc,assignment]

    class FastMCP:  # type: ignore[no-redef]
        """Fallback shim so the HTTP dashboard, REST API, and stdin bridge run even if `mcp` fails."""

        def __init__(self, name: str, **kwargs: Any) -> None:
            self.name = name
            self._tools: dict[str, Any] = {}

        def resource(self, uri: str):
            return lambda fn: fn

        def tool(self, name: str | None = None, description: str | None = None):
            def _decorator(fn: Any) -> Any:
                self._tools[name or fn.__name__] = fn
                return fn

            return _decorator

        def remove_tool(self, name: str) -> None:
            self._tools.pop(name, None)

        def run(self, transport: str = "streamable-http") -> None:
            print(f"[WARN] Official `mcp` SDK failed to load ({MCP_IMPORT_ERROR_MSG}).")
            print("[WARN] Running Live HTML/HTTP Dashboard, REST /api & stdin bridge only.")
            threading.Event().wait()


def _validate_function_name(name: str) -> str | None:
    if not isinstance(name, str) or not name.isidentifier() or keyword.iskeyword(name):
        return f"Function name `{name}` must be a valid non-keyword Python identifier."
    return None


class DynamicASTMcpProgram:
    def __init__(
        self,
        mcp_server: FastMCP,
        initial_registry: dict[str, str] | None = None,
        auto_expose_keys: list[str] | None = None,
        stdout_is_mcp_stdio: bool = False,
        modules_dir: str | None = None,
    ) -> None:
        self.mcp = mcp_server
        self.registry: dict[str, str] = {}
        self.runtime_ns: dict[str, Any] = {"__builtins__": __builtins__}
        self._compiled: dict[str, dict[str, Any]] = {}
        self._exposed_tools: set[str] = set()
        self.stdout_is_mcp_stdio = stdout_is_mcp_stdio
        self.modules_dir = os.path.realpath(modules_dir or os.getcwd())

        self._lock = threading.RLock()
        self._pip_lock = threading.Lock()
        self._sse_subscribers: list[queue.Queue[str]] = []
        self.event_log: deque[dict[str, Any]] = deque(maxlen=100)
        self._next_event_id: int = 0
        # Cache of (serialized_ast, formats_dict, deps_list) keyed by function name
        self._export_cache: dict[str, tuple[str, dict[str, Any], list[str]]] = {}
        # Revision history & last execution outputs per AST function
        self._history: dict[str, deque[dict[str, Any]]] = {}
        self._version_counters: dict[str, int] = {}
        self._last_results: dict[str, Any] = {}

        for k, raw_ast in (initial_registry or {}).items():
            rt = verify_and_roundtrip_ast(k, raw_ast)
            self.registry[k] = rt["canonical_serialized_ast"]
            self._record_version(k, self.registry[k], note="initial seed")

        for key in auto_expose_keys or []:
            self._compile_needed(key)
            self._expose_on_mcp(key, self.runtime_ns[key])

    def _record_version(self, name: str, serialized_ast: str, note: str = "update") -> dict[str, Any]:
        buf = self._history.setdefault(name, deque(maxlen=20))
        next_ver = self._version_counters.get(name, 0) + 1
        self._version_counters[name] = next_ver
        node = deserialize_ast_node(serialized_ast)
        entry = {
            "version": next_ver,
            "timestamp": time.strftime("%H:%M:%S"),
            "note": note,
            "serialized_ast": serialized_ast,
            "python_source": ast.unparse(node),
        }
        buf.append(entry)
        return entry

    def _console_write(self, line: str) -> None:
        stream = sys.stderr if self.stdout_is_mcp_stdio else sys.stdout
        stream.write(line + "\n")
        stream.flush()

    def _get_cached_formats_and_deps(self, name: str) -> tuple[dict[str, Any], list[str]]:
        serialized = self.registry[name]
        cached = self._export_cache.get(name)
        if cached is not None and cached[0] == serialized:
            _, fmt_dict, cached_deps = cached
            deps = [d for d in cached_deps if d in self.registry and d != name]
            return fmt_dict, deps

        fmt_dict = export_all_formats(name, serialized)
        ast_node = deserialize_ast_node(serialized)
        all_called = sorted(n for n in find_called_names(ast_node) if n != name)
        self._export_cache[name] = (serialized, fmt_dict, all_called)
        deps = [d for d in all_called if d in self.registry]
        return fmt_dict, deps

    def record_and_broadcast(
        self,
        source: str,
        tool: str,
        arguments: Any,
        result: dict[str, Any],
    ) -> None:
        route_label, route_color = ROUTE_META.get(tool, ("CUSTOM_FN", "green"))
        rest_enriched_result = enrich_rest_hal(
            result,
            request_href=f"/api/command/{tool}",
            request_method="POST",
        )
        with self._lock:
            self._next_event_id += 1
            entry = {
                "id": self._next_event_id,
                "timestamp": time.strftime("%H:%M:%S"),
                "source": source,
                "route": route_label,
                "tool": tool,
                "arguments": arguments,
                "status": rest_enriched_result.get("status", "ok"),
                "result": rest_enriched_result,
            }
            self.event_log.append(entry)
            if self._sse_subscribers:
                payload = json.dumps(
                    {
                        "type": "state_update",
                        "event": entry,
                        "snapshot": self.get_dashboard_snapshot(),
                    }
                )
                dead: list[queue.Queue[str]] = []
                for q in self._sse_subscribers:
                    try:
                        q.put_nowait(payload)
                    except Exception:
                        dead.append(q)
                for q in dead:
                    self._sse_subscribers.remove(q)

        src_key = source.lower()
        src_color = SOURCE_COLORS.get(src_key, "white")
        status_str = str(entry["status"])
        status_color = "red" if status_str == "error" else "green"

        ts_part = colorize(f"[{entry['timestamp']}]", "dim")
        src_part = colorize(f"[SRC:{source.upper()}]", src_color)
        route_part = colorize(f"[ROUTE:{route_label}]", route_color)
        call_part = colorize(f"{tool}({json.dumps(arguments)})", route_color)
        status_part = colorize(f"status={status_str}", status_color)
        summary_val = result.get("result", result.get("message", result.get("status")))
        val_part = colorize(str(summary_val), status_color)

        self._console_write(
            f"{ts_part} {src_part} -> {route_part} {call_part} => {status_part} | {val_part}"
        )

    def subscribe_sse(self) -> queue.Queue[str]:
        q: queue.Queue[str] = queue.Queue(maxsize=64)
        with self._lock:
            self._sse_subscribers.append(q)
        return q

    def unsubscribe_sse(self, q: queue.Queue[str]) -> None:
        with self._lock:
            if q in self._sse_subscribers:
                self._sse_subscribers.remove(q)

    def get_dashboard_snapshot(self) -> dict[str, Any]:
        with self._lock:
            items = []
            for k in self.registry:
                fmt, deps = self._get_cached_formats_and_deps(k)
                items.append(
                    {
                        "name": k,
                        "compiled": k in self._compiled,
                        "exposed_as_tool": k in self._exposed_tools,
                        "dependencies": deps,
                        "compile_metadata": self._compiled.get(k),
                        "metrics": compute_function_metrics(k, self.registry[k]),
                        "history_count": len(self._history.get(k, ())),
                        "formats": fmt["formats"],
                        "_links": self._resource_links(k),
                    }
                )
            return {
                "status": "ok",
                "count": len(items),
                "items": items,
                "events": list(self.event_log)[-40:],
                "_embedded": {"items": items},
            }

    def _known_symbols(self) -> set[str]:
        builtins_obj = __builtins__ if isinstance(__builtins__, dict) else vars(__builtins__)
        return set(builtins_obj.keys()) | set(self.runtime_ns.keys()) | set(self.registry.keys())

    def _compile_needed(self, key: str, _stack: list[str] | None = None) -> tuple[Any, list[str]]:
        if key not in self.registry:
            raise MissingASTCallError(missing_name=key, caller_context="registry_lookup")

        stack = list(_stack) if _stack else []
        if key in stack:
            cycle_start = stack.index(key)
            cycle_path = stack[cycle_start:] + [key]
            raise CircularDependencyError(key=key, cycle_path=cycle_path)

        stack.append(key)
        compiled_chain: list[str] = []
        node = deserialize_ast_node(self.registry[key])

        import_reqs = extract_ast_import_requirements(node)
        for mod_name, symbols in import_reqs.items():
            if mod_name not in self._known_symbols() and not is_module_available(mod_name):
                raise MissingModuleDependencyError(
                    module_name=mod_name,
                    caller_context=key,
                    imported_symbols=symbols,
                )

        for dep_name in sorted(find_called_names(node)):
            if dep_name == key:
                continue
            if dep_name in self.registry:
                _, sub_chain = self._compile_needed(dep_name, stack)
                for item in sub_chain:
                    if item not in compiled_chain:
                        compiled_chain.append(item)
            elif dep_name not in self._known_symbols():
                raise MissingASTCallError(missing_name=dep_name, caller_context=key)

        if key not in self._compiled:
            try:
                fn, meta = compile_function_from_ast(key, self.registry[key], self.runtime_ns)
            except ModuleNotFoundError as exc:
                missing_mod = exc.name or "unknown"
                raise MissingModuleDependencyError(
                    module_name=missing_mod,
                    caller_context=key,
                    imported_symbols=import_reqs.get(missing_mod, []),
                ) from exc
            self._compiled[key] = meta
            setattr(self, key, staticmethod(fn).__get__(self, type(self)))
        else:
            fn = self.runtime_ns[key]

        if key not in compiled_chain:
            compiled_chain.append(key)

        return fn, compiled_chain

    def _handle_compile_error(
        self,
        exc: Exception,
        failed_tool: str,
        failed_params: dict[str, Any],
    ) -> dict[str, Any]:
        if isinstance(exc, CircularDependencyError):
            return build_circular_dependency_hal_recovery(
                exc=exc,
                registry=self.registry,
                failed_tool=failed_tool,
            )
        if isinstance(exc, MissingModuleDependencyError):
            return build_missing_module_hal_recovery(
                module_name=exc.module_name,
                failed_tool=failed_tool,
                failed_params=failed_params,
                caller_context=exc.caller_context,
                imported_symbols=exc.imported_symbols,
            )
        if isinstance(exc, MissingASTCallError):
            return build_missing_call_hal_recovery(
                missing_name=exc.missing_name,
                failed_tool=failed_tool,
                failed_params=failed_params,
                reason=str(exc),
            )
        raise exc

    async def _notify_tools_changed(self, ctx: Context | None) -> bool:
        if ctx is None:
            return False
        try:
            await ctx.session.send_tool_list_changed()
            return True
        except Exception:
            return False

    def _expose_on_mcp(self, name: str, fn: Any, description: str | None = None) -> None:
        if name in self._exposed_tools:
            try:
                self.mcp.remove_tool(name)
            except Exception:
                pass
        doc = description or inspect.getdoc(fn) or f"Dynamically compiled AST function `{name}`"
        self.mcp.tool(name=name, description=doc)(fn)
        self._exposed_tools.add(name)

    def _resource_links(self, name: str) -> dict[str, Any]:
        return {
            "resource_uri": {"href": f"ast://resource/{name}"},
            "read": {"tool": "ast_read", "arguments": {"name": name}},
            "update": {"tool": "ast_update", "required_params": ["name", "code_or_ast"], "target": name},
            "delete": {"tool": "ast_delete", "arguments": {"name": name}},
            "eval": {"tool": "eval_ast_call", "arguments": {"target": name, "args": []}},
            "convert_to_pydantic": {
                "tool": "ast_convert",
                "arguments": {"source_kind": "saved_ast", "target_kind": "pydantic", "payload": name},
            },
            "list": {"tool": "ast_read", "arguments": {}},
        }

    # --- CRUD: CREATE ---
    async def create(
        self,
        name: str,
        code_or_ast: str | dict[str, Any] | None = None,
        description: str | None = None,
        expose_as_tool: bool = True,
        overwrite: bool = False,
        skip_roundtrip: bool = False,
        ctx: Context | None = None,
        source: str = "mcp",
    ) -> dict[str, Any]:
        from .ast_core import build_blank_shell_ast

        with self._lock:
            failed_params = {
                "name": name,
                "code_or_ast": code_or_ast,
                "description": description or "",
                "expose_as_tool": expose_as_tool,
                "overwrite": overwrite,
                "skip_roundtrip": skip_roundtrip,
            }
            name_err = _validate_function_name(name)
            if name_err:
                res = build_hal_response(
                    self_href="ast://resource",
                    data={"status": "error", "error_type": "validation_error", "message": name_err},
                    links={"list_registry": {"tool": "ast_read", "arguments": {}}},
                )
                self.record_and_broadcast(source, "ast_create", failed_params, res)
                return res

            if name in self.registry and not overwrite:
                res = build_hal_response(
                    self_href=f"ast://resource/{name}",
                    data={
                        "status": "error",
                        "error_type": "already_exists",
                        "message": f"AST `{name}` already exists. Follow `_links.update` or set `overwrite=True`.",
                    },
                    links={
                        "update": {
                            "tool": "ast_update",
                            "arguments": {
                                "name": name,
                                "code_or_ast": code_or_ast or build_blank_shell_ast(name)[1],
                                "description": description or "",
                                "expose_as_tool": expose_as_tool,
                                "skip_roundtrip": skip_roundtrip,
                            },
                        },
                        "read": {"tool": "ast_read", "arguments": {"name": name}},
                        "delete": {"tool": "ast_delete", "arguments": {"name": name}},
                    },
                )
                self.record_and_broadcast(source, "ast_create", failed_params, res)
                return res

            lint_warnings: list[dict[str, Any]] = []
            if not code_or_ast:
                _, serialized = build_blank_shell_ast(name)
            else:
                lint_res = lint_and_autofix_input(name, code_or_ast, self._known_symbols())
                if not lint_res.valid:
                    res = build_lint_hal_response(
                        target_name=name,
                        lint_result=lint_res,
                        failed_tool="ast_create",
                        failed_params=failed_params,
                        exists_in_registry=(name in self.registry),
                    )
                    self.record_and_broadcast(source, "ast_create", failed_params, res)
                    return res
                assert lint_res.fixed_serialized_ast is not None
                serialized = lint_res.fixed_serialized_ast
                lint_warnings = [asdict(i) for i in lint_res.issues if i.severity == "warning"]

            roundtrip_stages: list[str] = []
            if not skip_roundtrip:
                try:
                    rt_report = verify_and_roundtrip_ast(name, serialized)
                    serialized = rt_report["canonical_serialized_ast"]
                    roundtrip_stages = rt_report["stages"]
                except Exception as exc:
                    res = build_hal_response(
                        self_href=f"ast://resource/{name}/roundtrip",
                        data={
                            "status": "error",
                            "error_type": "roundtrip_verification_failed",
                            "policy": "navigation_only_no_patch",
                            "target": name,
                            "message": str(exc),
                        },
                        links={
                            "bypass_and_create": {
                                "tool": "ast_create",
                                "arguments": {**failed_params, "skip_roundtrip": True},
                            },
                            "list_registry": {"tool": "ast_read", "arguments": {}},
                        },
                    )
                    self.record_and_broadcast(source, "ast_create", failed_params, res)
                    return res
            else:
                node = deserialize_ast_node(serialized)
                node.name = name
                serialized = serialize_function(ast.fix_missing_locations(node))

            prev_entry = self.registry.get(name)
            prev_meta = self._compiled.get(name)
            self.registry[name] = serialized
            self._compiled.pop(name, None)

            try:
                fn, chain = self._compile_needed(name)
            except (CircularDependencyError, MissingModuleDependencyError, MissingASTCallError) as exc:
                res = self._handle_compile_error(exc, "ast_create", failed_params)
                if prev_entry is None:
                    self.registry.pop(name, None)
                else:
                    self.registry[name] = prev_entry
                if prev_meta is not None:
                    self._compiled[name] = prev_meta
                self.record_and_broadcast(source, "ast_create", failed_params, res)
                return res

            notified = False
            if expose_as_tool:
                self._expose_on_mcp(name, fn, description)
                notified = await self._notify_tools_changed(ctx)

            rev = self._record_version(name, serialized, note="created")

            res_data: dict[str, Any] = {
                "status": "created",
                "name": name,
                "version": rev["version"],
                "roundtrip_checked": not skip_roundtrip,
                "roundtrip_verified_stages": roundtrip_stages,
                "compilation_order": chain,
                "exposed_as_tool": name in self._exposed_tools,
                "tool_list_changed_notified": notified,
                "compile_metadata": self._compiled[name],
                "metrics": compute_function_metrics(name, serialized),
            }
            if lint_warnings:
                res_data["lint_warnings"] = lint_warnings

            fmt, _ = self._get_cached_formats_and_deps(name)
            res = build_hal_response(
                self_href=f"ast://resource/{name}",
                data=res_data,
                links=self._resource_links(name),
                embedded={"resource": fmt},
            )
            self.record_and_broadcast(source, "ast_create", failed_params, res)
            return res

    # --- CRUD: READ ---
    def read(self, name: str | None = None, source: str = "mcp", log_event: bool = True) -> dict[str, Any]:
        with self._lock:
            if not name:
                items = [
                    {
                        "name": k,
                        "compiled": k in self._compiled,
                        "exposed_as_tool": k in self._exposed_tools,
                        "_links": self._resource_links(k),
                    }
                    for k in self.registry
                ]
                res = build_hal_response(
                    self_href="ast://resource",
                    data={"status": "ok", "count": len(items)},
                    links={
                        "create": {"tool": "ast_create", "required_params": ["name", "code_or_ast"]},
                        "chain": {"tool": "ast_chain", "required_params": ["commands"]},
                    },
                    embedded={"items": items},
                )
                if log_event:
                    self.record_and_broadcast(source, "ast_read", {"name": ""}, res)
                return res

            if name not in self.registry:
                res = build_missing_call_hal_recovery(
                    missing_name=name,
                    failed_tool="ast_read",
                    failed_params={"name": name},
                    reason=f"AST `{name}` does not exist in the registry.",
                )
                if log_event:
                    self.record_and_broadcast(source, "ast_read", {"name": name}, res)
                return res

            fmt, _ = self._get_cached_formats_and_deps(name)
            res = build_hal_response(
                self_href=f"ast://resource/{name}",
                data={
                    "status": "ok",
                    "name": name,
                    "compiled": name in self._compiled,
                    "exposed_as_tool": name in self._exposed_tools,
                    "compile_metadata": self._compiled.get(name),
                },
                links=self._resource_links(name),
                embedded={"resource": fmt},
            )
            if log_event:
                self.record_and_broadcast(source, "ast_read", {"name": name}, res)
            return res

    # --- CRUD: UPDATE ---
    async def update(
        self,
        name: str,
        code_or_ast: str | dict[str, Any],
        description: str | None = None,
        expose_as_tool: bool = True,
        skip_roundtrip: bool = False,
        ctx: Context | None = None,
        source: str = "mcp",
    ) -> dict[str, Any]:
        with self._lock:
            failed_params = {
                "name": name,
                "code_or_ast": code_or_ast,
                "description": description or "",
                "expose_as_tool": expose_as_tool,
                "skip_roundtrip": skip_roundtrip,
            }
            if name not in self.registry:
                res = build_missing_call_hal_recovery(
                    missing_name=name,
                    failed_tool="ast_update",
                    failed_params=failed_params,
                    reason=f"Cannot update `{name}` because it does not exist yet.",
                )
                self.record_and_broadcast(source, "ast_update", failed_params, res)
                return res

            lint_res = lint_and_autofix_input(name, code_or_ast, self._known_symbols())
            if not lint_res.valid:
                res = build_lint_hal_response(
                    target_name=name,
                    lint_result=lint_res,
                    failed_tool="ast_update",
                    failed_params=failed_params,
                    exists_in_registry=True,
                )
                self.record_and_broadcast(source, "ast_update", failed_params, res)
                return res

            assert lint_res.fixed_serialized_ast is not None
            serialized = lint_res.fixed_serialized_ast
            lint_warnings = [asdict(i) for i in lint_res.issues if i.severity == "warning"]

            roundtrip_stages: list[str] = []
            if not skip_roundtrip:
                try:
                    rt_report = verify_and_roundtrip_ast(name, serialized)
                    serialized = rt_report["canonical_serialized_ast"]
                    roundtrip_stages = rt_report["stages"]
                except Exception as exc:
                    res = build_hal_response(
                        self_href=f"ast://resource/{name}/roundtrip",
                        data={
                            "status": "error",
                            "error_type": "roundtrip_verification_failed",
                            "policy": "navigation_only_no_patch",
                            "target": name,
                            "message": str(exc),
                        },
                        links={
                            "bypass_and_update": {
                                "tool": "ast_update",
                                "arguments": {**failed_params, "skip_roundtrip": True},
                            },
                            "inspect_current": {"tool": "ast_read", "arguments": {"name": name}},
                        },
                    )
                    self.record_and_broadcast(source, "ast_update", failed_params, res)
                    return res
            else:
                node = deserialize_ast_node(serialized)
                node.name = name
                serialized = serialize_function(ast.fix_missing_locations(node))

            prev_serialized = self.registry[name]
            prev_compiled = dict(self._compiled)
            prev_ns_entry = self.runtime_ns.get(name)

            self.registry[name] = serialized
            self._compiled.pop(name, None)

            def _rollback() -> None:
                self.registry[name] = prev_serialized
                self._compiled = prev_compiled
                if prev_ns_entry is not None:
                    self.runtime_ns[name] = prev_ns_entry
                else:
                    self.runtime_ns.pop(name, None)

            try:
                fn, chain = self._compile_needed(name)
                for other_key in list(self._compiled.keys()):
                    if other_key != name:
                        self._compiled.pop(other_key, None)
                        self._compile_needed(other_key)
                        if other_key in self._exposed_tools:
                            self._expose_on_mcp(other_key, self.runtime_ns[other_key])
            except (CircularDependencyError, MissingModuleDependencyError, MissingASTCallError) as exc:
                res = self._handle_compile_error(exc, "ast_update", failed_params)
                _rollback()
                self.record_and_broadcast(source, "ast_update", failed_params, res)
                return res

            notified = False
            if expose_as_tool:
                self._expose_on_mcp(name, fn, description)
                notified = await self._notify_tools_changed(ctx)

            rev = self._record_version(name, serialized, note="updated")

            res_data: dict[str, Any] = {
                "status": "updated",
                "name": name,
                "version": rev["version"],
                "roundtrip_checked": not skip_roundtrip,
                "roundtrip_verified_stages": roundtrip_stages,
                "compilation_order": chain,
                "exposed_as_tool": name in self._exposed_tools,
                "tool_list_changed_notified": notified,
                "compile_metadata": self._compiled[name],
                "metrics": compute_function_metrics(name, serialized),
            }
            if lint_warnings:
                res_data["lint_warnings"] = lint_warnings

            fmt, _ = self._get_cached_formats_and_deps(name)
            res = build_hal_response(
                self_href=f"ast://resource/{name}",
                data=res_data,
                links=self._resource_links(name),
                embedded={"resource": fmt},
            )
            self.record_and_broadcast(source, "ast_update", failed_params, res)
            return res

    # --- CRUD: DELETE ---
    async def delete(self, name: str, ctx: Context | None = None, source: str = "mcp") -> dict[str, Any]:
        with self._lock:
            if name not in self.registry:
                res = build_missing_call_hal_recovery(
                    missing_name=name,
                    failed_tool="ast_delete",
                    failed_params={"name": name},
                    reason=f"Cannot delete `{name}` because it does not exist.",
                )
                self.record_and_broadcast(source, "ast_delete", {"name": name}, res)
                return res

            deleted_formats = export_all_formats(name, self.registry.pop(name))
            self._export_cache.pop(name, None)
            self._compiled.pop(name, None)
            self._last_results.pop(name, None)
            self.runtime_ns.pop(name, None)
            if hasattr(self, name):
                delattr(self, name)

            notified = False
            if name in self._exposed_tools:
                self._exposed_tools.discard(name)
                try:
                    self.mcp.remove_tool(name)
                    notified = await self._notify_tools_changed(ctx)
                except Exception:
                    pass

            res = build_hal_response(
                self_href=f"ast://resource/{name}",
                data={
                    "status": "deleted",
                    "name": name,
                    "tool_list_changed_notified": notified,
                },
                links={
                    "recreate": {
                        "tool": "ast_create",
                        "arguments": {
                            "name": name,
                            "code_or_ast": deleted_formats["formats"]["serialized_ast"],
                        },
                    },
                    "list": {"tool": "ast_read", "arguments": {}},
                },
                embedded={"deleted_resource": deleted_formats},
            )
            self.record_and_broadcast(source, "ast_delete", {"name": name}, res)
            return res

    # --- EVAL ---
    def eval_target(
        self,
        target: str,
        args: Any = None,
        kwargs: Any = None,
        source: str = "mcp",
    ) -> dict[str, Any]:
        with self._lock:
            omitted_pos: list[int] = []
            omitted_kws: list[str] = []

            if isinstance(args, str) and kwargs is None:
                norm_args, norm_kwargs, omitted_pos, omitted_kws = parse_eval_args_with_omissions(args)
            else:
                norm_args, norm_kwargs = normalize_eval_call_args(args, kwargs)
                for idx, val in enumerate(norm_args):
                    if val is OMITTED_SENTINEL:
                        omitted_pos.append(idx)

            clean_args = [a for a in norm_args if a is not OMITTED_SENTINEL]
            failed_params = {"target": target, "args": clean_args, "kwargs": norm_kwargs}

            if target in self.registry:
                if omitted_pos or omitted_kws:
                    missing_default_res = check_and_build_missing_default_recovery(
                        target_name=target,
                        serialized_ast=self.registry[target],
                        failed_params=failed_params,
                        call_args=norm_args,
                        call_kwargs=norm_kwargs,
                        omitted_pos_indices=omitted_pos,
                        omitted_kw_names=omitted_kws,
                    )
                    if missing_default_res is not None:
                        self.record_and_broadcast(source, "eval_ast_call", failed_params, missing_default_res)
                        return missing_default_res

                try:
                    fn, chain = self._compile_needed(target)
                except (CircularDependencyError, MissingModuleDependencyError, MissingASTCallError) as exc:
                    res = self._handle_compile_error(exc, "eval_ast_call", failed_params)
                    self.record_and_broadcast(source, "eval_ast_call", failed_params, res)
                    return res

                stdout_buf = io.StringIO()
                t0 = time.perf_counter()
                try:
                    with contextlib.redirect_stdout(stdout_buf):
                        result = fn(*clean_args, **norm_kwargs)
                except ModuleNotFoundError as exc:
                    missing_mod = exc.name or "unknown"
                    node = deserialize_ast_node(self.registry[target])
                    import_reqs = extract_ast_import_requirements(node)
                    res = build_missing_module_hal_recovery(
                        module_name=missing_mod,
                        failed_tool="eval_ast_call",
                        failed_params=failed_params,
                        caller_context=target,
                        imported_symbols=import_reqs.get(missing_mod, []),
                    )
                    self.record_and_broadcast(source, "eval_ast_call", failed_params, res)
                    return res
                except TypeError as exc:
                    err_text = str(exc)
                    sig_markers = (
                        "positional argument",
                        "keyword argument",
                        "missing",
                        "takes from",
                        "takes no arguments",
                    )
                    if any(marker in err_text for marker in sig_markers):
                        res = build_signature_mismatch_hal_recovery(
                            target_name=target,
                            serialized_ast=self.registry[target],
                            failed_params=failed_params,
                            call_args=clean_args,
                            call_kwargs=norm_kwargs,
                            type_error_msg=err_text,
                        )
                    else:
                        res = build_hal_response(
                            self_href=f"ast://resource/{target}/eval",
                            data={
                                "status": "error",
                                "error_type": "runtime_type_error",
                                "policy": "navigation_only_no_patch",
                                "target": target,
                                "message": err_text,
                            },
                            links=self._resource_links(target),
                        )
                    self.record_and_broadcast(source, "eval_ast_call", failed_params, res)
                    return res
                except Exception as exc:
                    duration_ms = round((time.perf_counter() - t0) * 1000.0, 3)
                    captured = stdout_buf.getvalue().strip()
                    err_data: dict[str, Any] = {
                        "status": "error",
                        "error_type": "runtime_error",
                        "policy": "navigation_only_no_patch",
                        "target": target,
                        "duration_ms": duration_ms,
                        "message": f"{type(exc).__name__}: {exc}",
                    }
                    if captured:
                        err_data["stdout"] = captured[:4000]
                    res = build_hal_response(
                        self_href=f"ast://resource/{target}/eval",
                        data=err_data,
                        links=self._resource_links(target),
                    )
                    self.record_and_broadcast(source, "eval_ast_call", failed_params, res)
                    return res

                duration_ms = round((time.perf_counter() - t0) * 1000.0, 3)
                captured_out = stdout_buf.getvalue().strip()
                self._last_results[target] = result

                eval_data: dict[str, Any] = {
                    "status": "evaluated_function",
                    "target": target,
                    "compilation_order": chain,
                    "args": clean_args,
                    "kwargs": norm_kwargs,
                    "result": result,
                    "duration_ms": duration_ms,
                }
                if captured_out:
                    eval_data["stdout"] = captured_out[:4000]

                fmt, _ = self._get_cached_formats_and_deps(target)
                res = build_hal_response(
                    self_href=f"ast://resource/{target}/eval",
                    data=eval_data,
                    links=self._resource_links(target),
                    embedded={"resource": fmt},
                )
                self.record_and_broadcast(source, "eval_ast_call", failed_params, res)
                return res

            try:
                expr_node = parse_expression_or_ast(target)
            except Exception as exc:
                if target.isidentifier():
                    res = build_missing_call_hal_recovery(
                        missing_name=target,
                        failed_tool="eval_ast_call",
                        failed_params=failed_params,
                        reason=f"Target `{target}` is not defined in the AST registry.",
                    )
                else:
                    res = build_hal_response(
                        self_href="mcp://tool/eval_ast_call",
                        data={
                            "status": "error",
                            "error_type": "lint_errors_unfixable",
                            "policy": "navigation_only_no_patch",
                            "message": str(exc),
                        },
                        links={"list_registry": {"tool": "ast_read", "arguments": {}}},
                    )
                self.record_and_broadcast(source, "eval_ast_call", failed_params, res)
                return res

            for called_name in sorted(find_called_names(expr_node)):
                if called_name in self.registry:
                    try:
                        self._compile_needed(called_name)
                    except (CircularDependencyError, MissingModuleDependencyError, MissingASTCallError) as exc:
                        res = self._handle_compile_error(exc, "eval_ast_call", failed_params)
                        self.record_and_broadcast(source, "eval_ast_call", failed_params, res)
                        return res
                elif called_name not in self._known_symbols():
                    res = build_missing_call_hal_recovery(
                        missing_name=called_name,
                        failed_tool="eval_ast_call",
                        failed_params=failed_params,
                        reason=f"Expression calls undefined AST function `{called_name}`.",
                    )
                    self.record_and_broadcast(source, "eval_ast_call", failed_params, res)
                    return res

            if isinstance(expr_node.body, ast.Name) and expr_node.body.id not in self._known_symbols():
                res = build_missing_call_hal_recovery(
                    missing_name=expr_node.body.id,
                    failed_tool="eval_ast_call",
                    failed_params=failed_params,
                    reason=f"Expression references undefined AST function `{expr_node.body.id}`.",
                )
                self.record_and_broadcast(source, "eval_ast_call", failed_params, res)
                return res

            expr_stdout = io.StringIO()
            t0 = time.perf_counter()
            try:
                code_obj = compile(expr_node, filename="<ast:eval>", mode="eval")
                with contextlib.redirect_stdout(expr_stdout):
                    result = eval(code_obj, self.runtime_ns)
            except Exception as exc:
                duration_ms = round((time.perf_counter() - t0) * 1000.0, 3)
                res = build_hal_response(
                    self_href="mcp://tool/eval_ast_call",
                    data={
                        "status": "error",
                        "error_type": "runtime_error",
                        "policy": "navigation_only_no_patch",
                        "duration_ms": duration_ms,
                        "message": f"{type(exc).__name__}: {exc}",
                    },
                    links={"list": {"tool": "ast_read", "arguments": {}}},
                )
                self.record_and_broadcast(source, "eval_ast_call", failed_params, res)
                return res

            duration_ms = round((time.perf_counter() - t0) * 1000.0, 3)
            expr_captured = expr_stdout.getvalue().strip()
            expr_data: dict[str, Any] = {
                "status": "evaluated_expression",
                "expression_source": ast.unparse(expr_node),
                "expression_serialized_ast": ast.dump(expr_node),
                "result": result,
                "duration_ms": duration_ms,
            }
            if expr_captured:
                expr_data["stdout"] = expr_captured[:4000]

            res = build_hal_response(
                self_href="mcp://tool/eval_ast_call",
                data=expr_data,
                links={"list": {"tool": "ast_read", "arguments": {}}},
            )
            self.record_and_broadcast(source, "eval_ast_call", failed_params, res)
            return res

    # --- MODULE INSTALL & FALLBACK LOCAL MODULE WRITER ---
    def install_module(
        self,
        module_name: str,
        package_name: str | None = None,
        exported_symbols: list[str] | None = None,
        caller_context: str = "ast_function",
        source: str = "mcp",
    ) -> dict[str, Any]:
        clean_mod = unquote(str(module_name)).strip()
        args_record = {
            "module_name": clean_mod,
            "package_name": package_name or "",
            "exported_symbols": exported_symbols or [],
        }
        if not clean_mod.isidentifier():
            res = build_hal_response(
                self_href="ast://env/module/invalid",
                data={
                    "status": "error",
                    "error_type": "invalid_module_identifier",
                    "message": f"Module name `{clean_mod}` must be a valid Python identifier.",
                },
                links={"list_registry": {"tool": "ast_read", "arguments": {}}},
            )
            self.record_and_broadcast(source, "ast_install_module", args_record, res)
            return res

        raw_pkg = package_name or resolve_package_name(clean_mod)
        try:
            pkg = validate_package_name(raw_pkg)
        except ValueError as exc:
            res = build_hal_response(
                self_href="ast://env/module/invalid",
                data={"status": "error", "error_type": "invalid_package_name", "message": str(exc)},
                links={"list_registry": {"tool": "ast_read", "arguments": {}}},
            )
            self.record_and_broadcast(source, "ast_install_module", args_record, res)
            return res

        args_record["package_name"] = pkg

        if is_module_available(clean_mod):
            res = build_hal_response(
                self_href=f"ast://env/module/{clean_mod}",
                data={
                    "status": "already_installed",
                    "module_name": clean_mod,
                    "package_name": pkg,
                },
                links={"list_registry": {"tool": "ast_read", "arguments": {}}},
            )
            self.record_and_broadcast(source, "ast_install_module", args_record, res)
            return res

        # Run pip outside self._lock so dashboard/read/eval requests are not blocked for up to 120s.
        with self._pip_lock:
            if is_module_available(clean_mod):
                installed_ok = True
                pip_stderr = ""
            else:
                try:
                    proc = subprocess.run(
                        [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", pkg],
                        capture_output=True,
                        text=True,
                        timeout=120.0,
                    )
                    importlib.invalidate_caches()
                    invalidate_package_cache()
                    installed_ok = proc.returncode == 0 and (
                        is_module_available(clean_mod)
                        or importlib.util.find_spec(pkg.replace("-", "_")) is not None
                    )
                    pip_stderr = (proc.stderr or proc.stdout or "").strip()[-600:]
                except Exception as exc:
                    installed_ok = False
                    pip_stderr = str(exc)

        if installed_ok:
            res = build_hal_response(
                self_href=f"ast://env/module/{clean_mod}",
                data={
                    "status": "installed",
                    "module_name": clean_mod,
                    "package_name": pkg,
                    "python_executable": sys.executable,
                },
                links={"list_registry": {"tool": "ast_read", "arguments": {}}},
            )
            self.record_and_broadcast(source, "ast_install_module", args_record, res)
            return res

        stub = generate_fallback_module_script(
            module_name=clean_mod,
            exported_symbols=exported_symbols,
            caller_context=caller_context,
        )
        create_file_step = {
            "tool": "ast_write_module_file",
            "arguments": {
                "filename": stub["filename"],
                "code": stub["python_source"],
                "overwrite": True,
            },
        }

        res = build_hal_response(
            self_href=f"ast://env/module/{clean_mod}/fallback",
            data={
                "status": "error",
                "error_type": "module_install_failed",
                "policy": "recommend_create_local_module_script",
                "module_name": clean_mod,
                "attempted_package": pkg,
                "pip_stderr": pip_stderr,
                "recommendation": (
                    f"Package `{pkg}` could not be installed via pip. Create local module "
                    f"`{stub['filename']}` exporting {stub['exports'] or ['__getattr__']} "
                    "by following `_links.create_local_module_file`."
                ),
                "recommended_module_script": stub,
            },
            links={
                "create_local_module_file": create_file_step,
                "list_registry": {"tool": "ast_read", "arguments": {}},
            },
        )
        self.record_and_broadcast(source, "ast_install_module", args_record, res)
        return res

    def write_local_module_file(
        self,
        filename: str,
        code: str,
        overwrite: bool = False,
        source: str = "mcp",
    ) -> dict[str, Any]:
        with self._lock:
            decoded_name = unquote(str(filename))
            safe_name = os.path.basename(decoded_name)
            if (
                not safe_name.endswith(".py")
                or "{" in safe_name
                or "}" in safe_name
                or not safe_name[:-3].isidentifier()
            ):
                res = build_hal_response(
                    self_href=f"ast://file/{safe_name}",
                    data={
                        "status": "error",
                        "error_type": "invalid_module_filename",
                        "message": f"Filename `{safe_name}` must be a valid Python identifier ending in `.py`.",
                    },
                    links={"list_registry": {"tool": "ast_read", "arguments": {}}},
                )
                self.record_and_broadcast(source, "ast_write_module_file", {"filename": safe_name}, res)
                return res

            target_path = os.path.realpath(os.path.join(self.modules_dir, safe_name))
            if not target_path.startswith(self.modules_dir + os.sep):
                res = build_hal_response(
                    self_href=f"ast://file/{safe_name}",
                    data={
                        "status": "error",
                        "error_type": "invalid_module_path",
                        "message": "Resolved path escapes configured modules directory.",
                    },
                    links={"list_registry": {"tool": "ast_read", "arguments": {}}},
                )
                self.record_and_broadcast(source, "ast_write_module_file", {"filename": safe_name}, res)
                return res

            if os.path.exists(target_path) and not overwrite:
                res = build_hal_response(
                    self_href=f"ast://file/{safe_name}",
                    data={
                        "status": "error",
                        "error_type": "file_already_exists",
                        "message": f"`{safe_name}` already exists. Set `overwrite=True` to replace it.",
                    },
                    links={
                        "overwrite_file": {
                            "tool": "ast_write_module_file",
                            "arguments": {"filename": safe_name, "code": code, "overwrite": True},
                        }
                    },
                )
                self.record_and_broadcast(source, "ast_write_module_file", {"filename": safe_name}, res)
                return res

            try:
                ast.parse(code, mode="exec")
            except SyntaxError as exc:
                res = build_hal_response(
                    self_href=f"ast://file/{safe_name}",
                    data={
                        "status": "error",
                        "error_type": "syntax_error",
                        "message": f"SyntaxError in `{safe_name}` (line {exc.lineno}): {exc.msg}",
                    },
                    links={"list_registry": {"tool": "ast_read", "arguments": {}}},
                )
                self.record_and_broadcast(source, "ast_write_module_file", {"filename": safe_name}, res)
                return res

            with open(target_path, "w", encoding="utf-8") as fh:
                fh.write(code)
            importlib.invalidate_caches()

            res = build_hal_response(
                self_href=f"ast://file/{safe_name}",
                data={
                    "status": "module_file_created",
                    "filename": safe_name,
                    "path": target_path,
                    "module_name": safe_name[:-3],
                },
                links={"list_registry": {"tool": "ast_read", "arguments": {}}},
            )
            self.record_and_broadcast(source, "ast_write_module_file", {"filename": safe_name}, res)
            return res

    # --- CONVERT ---
    def convert(
        self,
        source_kind: str,
        target_kind: str,
        payload: Any,
        model_name: str = "GeneratedModel",
        source: str = "mcp",
    ) -> dict[str, Any]:
        with self._lock:
            failed_params = {
                "source_kind": source_kind,
                "target_kind": target_kind,
                "payload": payload,
                "model_name": model_name,
            }
            if source_kind == "saved_ast" and str(payload) not in self.registry:
                res = build_missing_call_hal_recovery(
                    missing_name=str(payload),
                    failed_tool="ast_convert",
                    failed_params=failed_params,
                    reason=f"Saved AST `{payload}` does not exist in the registry.",
                )
                self.record_and_broadcast(source, "ast_convert", failed_params, res)
                return res

            try:
                converted = convert_structure(
                    source_kind=source_kind,
                    target_kind=target_kind,
                    payload=payload,
                    model_name=model_name,
                    saved_registry=self.registry,
                )
            except Exception as exc:
                res = build_hal_response(
                    self_href=f"ast://convert/{source_kind}-to-{target_kind}",
                    data={
                        "status": "error",
                        "error_type": "conversion_error",
                        "policy": "navigation_only_no_patch",
                        "message": str(exc),
                    },
                    links={"list_registry": {"tool": "ast_read", "arguments": {}}},
                )
                self.record_and_broadcast(source, "ast_convert", failed_params, res)
                return res

            links: dict[str, Any] = {
                "list_registry": {"tool": "ast_read", "arguments": {}},
            }
            if target_kind in ("ast", "function_ast"):
                fn_name = converted["function_name"]
                links["register_converted_ast"] = {
                    "tool": "ast_create",
                    "arguments": {
                        "name": fn_name,
                        "code_or_ast": converted["serialized_ast"],
                        "description": f"Converted from {source_kind} -> AST",
                        "expose_as_tool": True,
                        "overwrite": True,
                    },
                }
            elif target_kind == "pydantic":
                links["convert_to_dataclass"] = {
                    "tool": "ast_convert",
                    "arguments": {
                        "source_kind": "pydantic",
                        "target_kind": "dataclass",
                        "payload": converted["python_source"],
                        "model_name": converted["model_name"],
                    },
                }
                links["convert_to_ast"] = {
                    "tool": "ast_convert",
                    "arguments": {
                        "source_kind": "pydantic",
                        "target_kind": "ast",
                        "payload": converted["python_source"],
                        "model_name": converted["model_name"],
                    },
                }
            elif target_kind == "dataclass":
                links["convert_to_pydantic"] = {
                    "tool": "ast_convert",
                    "arguments": {
                        "source_kind": "dataclass",
                        "target_kind": "pydantic",
                        "payload": converted["python_source"],
                        "model_name": converted["model_name"],
                    },
                }
                links["convert_to_ast"] = {
                    "tool": "ast_convert",
                    "arguments": {
                        "source_kind": "dataclass",
                        "target_kind": "ast",
                        "payload": converted["python_source"],
                        "model_name": converted["model_name"],
                    },
                }

            res = build_hal_response(
                self_href=f"ast://convert/{source_kind}-to-{target_kind}",
                data={"status": "converted", **converted},
                links=links,
            )
            self.record_and_broadcast(source, "ast_convert", failed_params, res)
            return res

    # --- COMMAND CHAINING ---
    @staticmethod
    def _resolve_chain_refs(value: Any, step_results: list[dict[str, Any]]) -> Any:
        if isinstance(value, str):
            if value == "$prev" and step_results:
                last_out = step_results[-1].get("output", {})
                return last_out.get("result", last_out)
            if value.startswith("$steps."):
                parts = value.split(".")
                idx = int(parts[1])
                cur: Any = step_results[idx]
                for key in parts[2:]:
                    cur = cur[key]
                return cur
            return value
        if isinstance(value, list):
            return [DynamicASTMcpProgram._resolve_chain_refs(v, step_results) for v in value]
        if isinstance(value, dict):
            return {
                k: DynamicASTMcpProgram._resolve_chain_refs(v, step_results)
                for k, v in value.items()
            }
        return value

    async def execute_chain(
        self,
        commands: list[dict[str, Any]],
        ctx: Context | None = None,
        source: str = "mcp",
    ) -> dict[str, Any]:
        completed: list[dict[str, Any]] = []

        for idx, cmd in enumerate(commands):
            if not isinstance(cmd, dict):
                res = build_hal_response(
                    self_href="mcp://tool/ast_chain",
                    data={
                        "status": "error",
                        "error_type": "validation_error",
                        "message": f"Chain step {idx} must be a JSON object.",
                        "completed_steps": completed,
                        "failed_step_index": idx,
                    },
                    links={"list": {"tool": "ast_read", "arguments": {}}},
                )
                self.record_and_broadcast(source, "ast_chain", {"commands": commands}, res)
                return res

            tool_name = cmd.get("tool") or cmd.get("action")
            raw_args = cmd.get("arguments") or cmd.get("args") or {}
            try:
                resolved_args = self._resolve_chain_refs(raw_args, completed)
            except (IndexError, KeyError, ValueError, TypeError) as exc:
                res = build_hal_response(
                    self_href="mcp://tool/ast_chain",
                    data={
                        "status": "error",
                        "error_type": "chain_reference_error",
                        "message": f"Invalid reference in chain step {idx}: {exc}",
                        "completed_steps": completed,
                        "failed_step_index": idx,
                    },
                    links={"list": {"tool": "ast_read", "arguments": {}}},
                )
                self.record_and_broadcast(source, "ast_chain", {"commands": commands}, res)
                return res

            args_dict = resolved_args if isinstance(resolved_args, dict) else {}

            if tool_name in ("ast_create", "create"):
                out = await self.create(
                    name=str(args_dict.get("name", "")),
                    code_or_ast=args_dict.get("code_or_ast"),
                    description=args_dict.get("description"),
                    expose_as_tool=bool(args_dict.get("expose_as_tool", True)),
                    overwrite=bool(args_dict.get("overwrite", False)),
                    skip_roundtrip=bool(args_dict.get("skip_roundtrip", False)),
                    ctx=ctx,
                    source=source,
                )
            elif tool_name in ("ast_read", "read"):
                out = self.read(name=args_dict.get("name") or None, source=source)
            elif tool_name in ("ast_update", "update"):
                out = await self.update(
                    name=str(args_dict.get("name", "")),
                    code_or_ast=args_dict.get("code_or_ast", ""),
                    description=args_dict.get("description"),
                    expose_as_tool=bool(args_dict.get("expose_as_tool", True)),
                    skip_roundtrip=bool(args_dict.get("skip_roundtrip", False)),
                    ctx=ctx,
                    source=source,
                )
            elif tool_name in ("ast_delete", "delete"):
                out = await self.delete(name=str(args_dict.get("name", "")), ctx=ctx, source=source)
            elif tool_name in ("eval_ast_call", "eval"):
                if isinstance(resolved_args, dict) and ("target" in resolved_args or "expression" in resolved_args):
                    out = self.eval_target(
                        target=str(resolved_args.get("target") or resolved_args.get("expression") or ""),
                        args=resolved_args.get("args"),
                        kwargs=resolved_args.get("kwargs"),
                        source=source,
                    )
                else:
                    out = self.eval_target(target=str(resolved_args), source=source)
            elif tool_name in ("ast_convert", "convert"):
                out = self.convert(
                    source_kind=str(args_dict.get("source_kind", "saved_ast")),
                    target_kind=str(args_dict.get("target_kind", "pydantic")),
                    payload=args_dict.get("payload", ""),
                    model_name=str(args_dict.get("model_name", "GeneratedModel")),
                    source=source,
                )
            elif tool_name == "ast_install_module":
                out = self.install_module(
                    module_name=str(args_dict.get("module_name", "")),
                    package_name=args_dict.get("package_name"),
                    exported_symbols=args_dict.get("exported_symbols"),
                    caller_context=str(args_dict.get("caller_context", "ast_function")),
                    source=source,
                )
            elif tool_name == "ast_write_module_file":
                out = self.write_local_module_file(
                    filename=str(args_dict.get("filename", "")),
                    code=str(args_dict.get("code", "")),
                    overwrite=bool(args_dict.get("overwrite", False)),
                    source=source,
                )
            elif tool_name in self.registry:
                out = self.eval_target(target=tool_name, args=resolved_args, source=source)
            else:
                remaining_commands = [
                    {"tool": tool_name, "arguments": resolved_args},
                    *commands[idx + 1 :],
                ]
                recovery = build_missing_call_hal_recovery(
                    missing_name=str(tool_name),
                    failed_tool="ast_chain",
                    failed_params={"commands": remaining_commands},
                    reason=f"Chain step {idx} called unknown AST function `{tool_name}`.",
                )
                recovery["completed_steps"] = completed
                recovery["failed_step_index"] = idx
                self.record_and_broadcast(source, "ast_chain", {"commands": commands}, recovery)
                return recovery

            if out.get("status") == "error":
                remaining_commands = [
                    {"tool": tool_name, "arguments": resolved_args},
                    *commands[idx + 1 :],
                ]
                err_type = out.get("error_type")
                if err_type == "missing_ast_call":
                    create_step = out["_links"]["create_shell"]
                    out["_links"]["recovery_chain"] = {
                        "tool": "ast_chain",
                        "arguments": {"commands": [create_step, *remaining_commands]},
                    }
                elif err_type == "missing_module_dependency":
                    install_step = out["_links"]["install_module"]
                    out["_links"]["recovery_chain"] = {
                        "tool": "ast_chain",
                        "arguments": {"commands": [install_step, *remaining_commands]},
                    }
                elif err_type == "module_install_failed":
                    create_file_step = out["_links"]["create_local_module_file"]
                    out["_links"]["recovery_chain"] = {
                        "tool": "ast_chain",
                        "arguments": {"commands": [create_file_step, *remaining_commands]},
                    }
                elif err_type in ("lint_errors_autofixed", "signature_mismatch_autofixed", "missing_default_value"):
                    patch_step = out["_links"]["apply_patched_update"]
                    out["_links"]["recovery_chain"] = {
                        "tool": "ast_chain",
                        "arguments": {"commands": [patch_step, *commands[idx + 1 :]]},
                    }
                out["completed_steps"] = completed
                out["failed_step_index"] = idx
                return out

            completed.append({"step": idx, "tool": tool_name, "output": out})

        res = build_hal_response(
            self_href="mcp://tool/ast_chain",
            data={"status": "chain_completed", "steps_executed": len(completed)},
            links={
                "list": {"tool": "ast_read", "arguments": {}},
                "chain": {"tool": "ast_chain", "required_params": ["commands"]},
            },
            embedded={"steps": completed},
        )
        self.record_and_broadcast(source, "ast_chain", {"commands": commands}, res)
        return res

    # --- GEMINI LINE GENERATION ---
    def generate_line(
        self,
        prompt: str,
        prefix: str = "",
        suffix: str = "",
        target_name: str = "",
        language: str = "python",
        model: str = DEFAULT_GEMINI_MODEL,
        source: str = "mcp",
    ) -> dict[str, Any]:
        with self._lock:
            symbols = sorted(self.registry.keys())
        args_record = {
            "prompt": prompt,
            "target_name": target_name,
            "language": language,
        }
        try:
            generated = generate_line_with_gemini(
                prompt=prompt,
                prefix=prefix,
                suffix=suffix,
                target_name=target_name,
                language=language,
                registered_symbols=symbols,
                model=model,
            )
        except GeminiLineError as exc:
            res = build_hal_response(
                self_href="ast://gemini/generate-line",
                data={
                    "status": "error",
                    "error_type": exc.error_type,
                    "http_status": exc.status_code,
                    "message": str(exc),
                },
                links={"list_registry": {"tool": "ast_read", "arguments": {}}},
            )
            self.record_and_broadcast(source, "ast_generate_line", args_record, res)
            return res

        res = build_hal_response(
            self_href="ast://gemini/generate-line",
            data={
                "status": "line_generated",
                "model": generated["model"],
                "line": generated["line"],
                "explanation": generated["explanation"],
                "target_name": target_name,
            },
            links={
                "list_registry": {"tool": "ast_read", "arguments": {}},
                **(
                    {"inspect_target": {"tool": "ast_read", "arguments": {"name": target_name}}}
                    if target_name and target_name in symbols
                    else {}
                ),
            },
        )
        self.record_and_broadcast(source, "ast_generate_line", args_record, res)
        return res

    # --- DEVELOPER TOOLS: TEST RUNNER, ANALYZER, DIFF, GRAPH, HISTORY & ROLLBACK ---
    def get_graph(self) -> dict[str, Any]:
        with self._lock:
            graph = build_dependency_graph(self.registry)
            return {
                "status": "ok",
                **graph,
                "history_counts": {k: len(v) for k, v in self._history.items()},
            }

    def get_history(self, name: str, source: str = "mcp", log_event: bool = False) -> dict[str, Any]:
        with self._lock:
            if name not in self.registry:
                res = build_missing_call_hal_recovery(
                    missing_name=name,
                    failed_tool="ast_read",
                    failed_params={"name": name},
                    reason=f"AST `{name}` does not exist in the registry.",
                )
                if log_event:
                    self.record_and_broadcast(source, "ast_analyze", {"name": name}, res)
                return res
            revs = list(self._history.get(name, ()))
            res = build_hal_response(
                self_href=f"ast://resource/{name}/history",
                data={
                    "status": "ok",
                    "name": name,
                    "count": len(revs),
                    "versions": revs,
                },
                links={
                    **self._resource_links(name),
                    "rollback": {"tool": "ast_rollback", "arguments": {"name": name}},
                },
            )
            if log_event:
                self.record_and_broadcast(source, "ast_analyze", {"name": name}, res)
            return res

    def analyze(
        self,
        name: str | None = None,
        candidate_code: str | None = None,
        source: str = "mcp",
    ) -> dict[str, Any]:
        with self._lock:
            if not name:
                graph = self.get_graph()
                res = build_hal_response(
                    self_href="ast://graph",
                    data=graph,
                    links={"list": {"tool": "ast_read", "arguments": {}}},
                )
                self.record_and_broadcast(source, "ast_analyze", {"name": ""}, res)
                return res

            if name not in self.registry:
                res = build_missing_call_hal_recovery(
                    missing_name=name,
                    failed_tool="ast_analyze",
                    failed_params={"name": name},
                    reason=f"AST `{name}` does not exist in the registry.",
                )
                self.record_and_broadcast(source, "ast_analyze", {"name": name}, res)
                return res

            serialized = self.registry[name]
            current_source = ast.unparse(deserialize_ast_node(serialized))
            graph = build_dependency_graph(self.registry)
            node_info = next((n for n in graph["nodes"] if n["name"] == name), {})
            metrics = compute_function_metrics(name, serialized)
            sample_out = self._last_results.get(name)
            pytest_src = generate_pytest_snippet(name, serialized, sample_output=sample_out)
            revs = [
                {k: v for k, v in rev.items() if k != "serialized_ast"}
                for rev in self._history.get(name, ())
            ]

            data: dict[str, Any] = {
                "status": "analyzed",
                "name": name,
                "metrics": metrics,
                "calls": node_info.get("calls", []),
                "called_by": node_info.get("called_by", []),
                "history_count": len(revs),
                "history": revs,
                "python_source": pytest_src,
                "pytest_source": pytest_src,
            }

            if candidate_code is not None:
                lint_preview = lint_and_autofix_input(name, candidate_code, self._known_symbols())
                candidate_src = lint_preview.fixed_source or candidate_code
                diff_text = compute_unified_diff(name, current_source, candidate_src)
                data["status"] = "diff_preview"
                data["unified_diff"] = diff_text or "(no changes compared to current version)"
                data["has_changes"] = bool(diff_text)
                data["lint_valid"] = lint_preview.valid
                data["lint_issues"] = [asdict(i) for i in lint_preview.issues]
                data["candidate_source"] = candidate_src

            res = build_hal_response(
                self_href=f"ast://resource/{name}/analyze",
                data=data,
                links={
                    **self._resource_links(name),
                    "run_tests": {"tool": "ast_run_tests", "arguments": {"name": name}},
                    "rollback": {"tool": "ast_rollback", "arguments": {"name": name}},
                },
            )
            self.record_and_broadcast(source, "ast_analyze", {"name": name, "has_candidate": candidate_code is not None}, res)
            return res

    def run_tests(
        self,
        name: str,
        cases: list[dict[str, Any]] | None = None,
        source: str = "mcp",
    ) -> dict[str, Any]:
        with self._lock:
            failed_params = {"name": name, "cases": cases}
            if name not in self.registry:
                res = build_missing_call_hal_recovery(
                    missing_name=name,
                    failed_tool="ast_run_tests",
                    failed_params=failed_params,
                    reason=f"AST `{name}` does not exist in the registry.",
                )
                self.record_and_broadcast(source, "ast_run_tests", failed_params, res)
                return res

            try:
                fn, chain = self._compile_needed(name)
            except (CircularDependencyError, MissingModuleDependencyError, MissingASTCallError) as exc:
                res = self._handle_compile_error(exc, "ast_run_tests", failed_params)
                self.record_and_broadcast(source, "ast_run_tests", failed_params, res)
                return res

            auto_generated = not cases
            test_cases = list(cases) if cases else generate_default_test_cases(name, self.registry[name])
            results: list[dict[str, Any]] = []
            passed_count = 0

            for idx, raw_case in enumerate(test_cases):
                case = raw_case if isinstance(raw_case, dict) else {"args": [raw_case]}
                case_name = str(case.get("name") or f"case_{idx + 1}")
                raw_args = case.get("args", [])
                c_args = list(raw_args) if isinstance(raw_args, list) else ([raw_args] if raw_args is not None else [])
                c_kwargs = case.get("kwargs") if isinstance(case.get("kwargs"), dict) else {}

                buf = io.StringIO()
                t0 = time.perf_counter()
                actual: Any = None
                exc_raised: Exception | None = None
                try:
                    with contextlib.redirect_stdout(buf):
                        actual = fn(*c_args, **c_kwargs)
                except Exception as exc:
                    exc_raised = exc
                duration_ms = round((time.perf_counter() - t0) * 1000.0, 3)
                captured = buf.getvalue().strip()

                expected_err = case.get("expected_error")
                has_expected = "expected" in case
                case_ok = False
                failure_reason = ""

                if expected_err:
                    if exc_raised is not None and (
                        str(expected_err).lower() in type(exc_raised).__name__.lower()
                        or str(expected_err).lower() in str(exc_raised).lower()
                    ):
                        case_ok = True
                    elif exc_raised is None:
                        failure_reason = f"Expected error `{expected_err}`, but got {actual!r}"
                    else:
                        failure_reason = f"Expected `{expected_err}`, got {type(exc_raised).__name__}: {exc_raised}"
                elif exc_raised is not None:
                    failure_reason = f"{type(exc_raised).__name__}: {exc_raised}"
                elif has_expected:
                    if actual == case["expected"]:
                        case_ok = True
                    else:
                        failure_reason = f"Expected {case['expected']!r}, got {actual!r}"
                else:
                    case_ok = True

                if case_ok:
                    passed_count += 1
                    if exc_raised is None:
                        self._last_results[name] = actual

                entry: dict[str, Any] = {
                    "name": case_name,
                    "passed": case_ok,
                    "args": c_args,
                    "kwargs": c_kwargs,
                    "duration_ms": duration_ms,
                }
                if exc_raised is None:
                    entry["actual"] = actual
                else:
                    entry["error"] = f"{type(exc_raised).__name__}: {exc_raised}"
                if has_expected:
                    entry["expected"] = case["expected"]
                if expected_err:
                    entry["expected_error"] = expected_err
                if captured:
                    entry["stdout"] = captured[:1000]
                if failure_reason:
                    entry["reason"] = failure_reason
                results.append(entry)

            total = len(results)
            failed_count = total - passed_count
            status_str = "tests_passed" if failed_count == 0 else "tests_failed"
            pytest_src = generate_pytest_snippet(name, self.registry[name], sample_output=self._last_results.get(name))

            res = build_hal_response(
                self_href=f"ast://resource/{name}/tests",
                data={
                    "status": status_str,
                    "name": name,
                    "passed": passed_count,
                    "failed": failed_count,
                    "total": total,
                    "auto_generated": auto_generated,
                    "compilation_order": chain,
                    "results": results,
                    "pytest_source": pytest_src,
                },
                links={
                    **self._resource_links(name),
                    "analyze": {"tool": "ast_analyze", "arguments": {"name": name}},
                    "rollback": {"tool": "ast_rollback", "arguments": {"name": name}},
                },
            )
            self.record_and_broadcast(source, "ast_run_tests", {"name": name, "passed": passed_count, "total": total}, res)
            return res

    async def rollback(
        self,
        name: str,
        version: int | None = None,
        ctx: Context | None = None,
        source: str = "mcp",
    ) -> dict[str, Any]:
        with self._lock:
            failed_params = {"name": name, "version": version}
            if name not in self.registry:
                res = build_missing_call_hal_recovery(
                    missing_name=name,
                    failed_tool="ast_rollback",
                    failed_params=failed_params,
                    reason=f"AST `{name}` does not exist in the registry.",
                )
                self.record_and_broadcast(source, "ast_rollback", failed_params, res)
                return res

            revs = list(self._history.get(name, ()))
            if len(revs) < 2 and version is None:
                res = build_hal_response(
                    self_href=f"ast://resource/{name}/rollback",
                    data={
                        "status": "error",
                        "error_type": "no_previous_version",
                        "message": f"AST `{name}` only has {len(revs)} recorded version(s); update it first before rolling back.",
                    },
                    links=self._resource_links(name),
                )
                self.record_and_broadcast(source, "ast_rollback", failed_params, res)
                return res

            if version is None:
                target_rev = revs[-2]
            else:
                target_rev = next((r for r in revs if r["version"] == version), None)
                if target_rev is None:
                    res = build_hal_response(
                        self_href=f"ast://resource/{name}/rollback",
                        data={
                            "status": "error",
                            "error_type": "version_not_found",
                            "message": f"Version v{version} not found for `{name}`. Available: {[r['version'] for r in revs]}.",
                        },
                        links=self._resource_links(name),
                    )
                    self.record_and_broadcast(source, "ast_rollback", failed_params, res)
                    return res

            prev_serialized = self.registry[name]
            prev_compiled = dict(self._compiled)
            prev_ns_entry = self.runtime_ns.get(name)

            restored_serialized = target_rev["serialized_ast"]
            self.registry[name] = restored_serialized
            self._compiled.pop(name, None)

            def _restore_prev() -> None:
                self.registry[name] = prev_serialized
                self._compiled = prev_compiled
                if prev_ns_entry is not None:
                    self.runtime_ns[name] = prev_ns_entry
                else:
                    self.runtime_ns.pop(name, None)

            try:
                fn, chain = self._compile_needed(name)
                for other_key in list(self._compiled.keys()):
                    if other_key != name:
                        self._compiled.pop(other_key, None)
                        self._compile_needed(other_key)
                        if other_key in self._exposed_tools:
                            self._expose_on_mcp(other_key, self.runtime_ns[other_key])
            except (CircularDependencyError, MissingModuleDependencyError, MissingASTCallError) as exc:
                res = self._handle_compile_error(exc, "ast_rollback", failed_params)
                _restore_prev()
                self.record_and_broadcast(source, "ast_rollback", failed_params, res)
                return res

            notified = False
            if name in self._exposed_tools:
                self._expose_on_mcp(name, fn)
                notified = await self._notify_tools_changed(ctx)

            new_rev = self._record_version(
                name,
                restored_serialized,
                note=f"rollback to v{target_rev['version']}",
            )
            fmt, _ = self._get_cached_formats_and_deps(name)
            res = build_hal_response(
                self_href=f"ast://resource/{name}",
                data={
                    "status": "rolled_back",
                    "name": name,
                    "restored_from_version": target_rev["version"],
                    "version": new_rev["version"],
                    "compilation_order": chain,
                    "tool_list_changed_notified": notified,
                    "python_source": target_rev["python_source"],
                    "metrics": compute_function_metrics(name, restored_serialized),
                },
                links=self._resource_links(name),
                embedded={"resource": fmt},
            )
            self.record_and_broadcast(source, "ast_rollback", {"name": name, "version": target_rev["version"]}, res)
            return res

    @staticmethod
    def _annotation_to_json_schema(ann: Any) -> dict[str, Any]:
        if ann is int:
            return {"type": "integer"}
        if ann is float:
            return {"type": "number"}
        if ann is bool:
            return {"type": "boolean"}
        if ann is str:
            return {"type": "string"}
        if ann is list or getattr(ann, "__origin__", None) is list:
            return {"type": "array"}
        if ann is dict or getattr(ann, "__origin__", None) is dict:
            return {"type": "object"}
        return {"type": "string"}

    def list_mcp_tools(self) -> list[dict[str, Any]]:
        """Return live MCP server tool definitions (including dynamic exposed AST functions) as JSON schemas."""
        tools_out: list[dict[str, Any]] = []
        raw_tools: dict[str, Any] = {}
        tool_mgr = getattr(self.mcp, "_tool_manager", None)
        if tool_mgr is not None and isinstance(getattr(tool_mgr, "_tools", None), dict):
            raw_tools = dict(tool_mgr._tools)
        elif isinstance(getattr(self.mcp, "_tools", None), dict):
            raw_tools = dict(self.mcp._tools)

        for tool_name, entry in raw_tools.items():
            fn = getattr(entry, "fn", entry)
            desc = (getattr(entry, "description", None) or inspect.getdoc(fn) or f"MCP tool `{tool_name}`").strip()
            params_schema: dict[str, Any] | None = getattr(entry, "parameters", None)
            if not isinstance(params_schema, dict) and callable(fn):
                sig = inspect.signature(fn)
                props: dict[str, Any] = {}
                required: list[str] = []
                for p_name, param in sig.parameters.items():
                    if p_name == "ctx":
                        continue
                    props[p_name] = self._annotation_to_json_schema(param.annotation)
                    if param.default is inspect.Parameter.empty:
                        required.append(p_name)
                params_schema = {
                    "type": "object",
                    "properties": props,
                }
                if required:
                    params_schema["required"] = required
            tools_out.append(
                {
                    "name": tool_name,
                    "description": desc,
                    "dynamic_ast_function": tool_name in self._exposed_tools,
                    "parameters": params_schema or {"type": "object", "properties": {}},
                }
            )
        return tools_out

