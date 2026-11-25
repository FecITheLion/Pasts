"""HATEOAS application/hal+json envelope builders and REST link enrichment."""

from __future__ import annotations

import ast
import copy
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote

from .ast_core import build_blank_shell_ast, deserialize_ast_node, export_all_formats, find_called_names
from .deps import CircularDependencyError, resolve_package_name

if TYPE_CHECKING:
    from .linter import LintResult


def build_hal_response(
    self_href: str,
    data: dict[str, Any],
    links: dict[str, Any],
    embedded: dict[str, Any] | None = None,
) -> dict[str, Any]:
    envelope: dict[str, Any] = {
        "_media_type": "application/hal+json",
        "_links": {
            "self": {"href": self_href},
            **links,
        },
        **data,
    }
    if embedded:
        envelope["_embedded"] = embedded
    return envelope


def build_circular_dependency_hal_recovery(
    exc: CircularDependencyError,
    registry: dict[str, str],
    failed_tool: str,
) -> dict[str, Any]:
    unique_nodes: list[dict[str, Any]] = []
    seen: set[str] = set()
    for node_name in exc.cycle_path:
        if node_name not in seen and node_name in registry:
            seen.add(node_name)
            fmt = export_all_formats(node_name, registry[node_name])
            ast_node = deserialize_ast_node(registry[node_name])
            deps = sorted(n for n in find_called_names(ast_node) if n in registry and n != node_name)
            unique_nodes.append(
                {
                    "name": node_name,
                    "python_source": fmt["formats"]["python_source"],
                    "dependencies": deps,
                }
            )

    update_links = {
        f"update_{n['name']}": {
            "tool": "ast_update",
            "required_params": ["name", "code_or_ast"],
            "target": n["name"],
        }
        for n in unique_nodes
    }

    return build_hal_response(
        self_href=f"mcp://tool/{failed_tool}",
        data={
            "status": "error",
            "error_type": "circular_ast_dependency",
            "policy": "resolve_circular_ast_dependency",
            "message": str(exc),
            "cycle_path": exc.cycle_path,
            "cycle_nodes": unique_nodes,
        },
        links={
            "list_registry": {"tool": "ast_read", "arguments": {}},
            **update_links,
        },
    )


def build_missing_call_hal_recovery(
    missing_name: str,
    failed_tool: str,
    failed_params: dict[str, Any],
    reason: str,
) -> dict[str, Any]:
    shell_source, shell_serialized_ast = build_blank_shell_ast(missing_name)

    create_step = {
        "tool": "ast_create",
        "arguments": {
            "name": missing_name,
            "code_or_ast": shell_serialized_ast,
            "description": f"Blank shell for `{missing_name}`",
            "expose_as_tool": True,
        },
    }
    retry_step = {
        "tool": failed_tool,
        "arguments": failed_params,
    }

    return build_hal_response(
        self_href=f"mcp://tool/{failed_tool}",
        data={
            "status": "error",
            "error_type": "missing_ast_call",
            "policy": "create_blank_shell_then_retry",
            "missing_call": missing_name,
            "message": reason,
            "generated_blank_shell": {
                "name": missing_name,
                "python_source": shell_source,
                "serialized_ast": shell_serialized_ast,
            },
        },
        links={
            "create_shell": create_step,
            "retry": retry_step,
            "recovery_chain": {
                "tool": "ast_chain",
                "arguments": {"commands": [create_step, retry_step]},
            },
        },
    )


def build_missing_module_hal_recovery(
    module_name: str,
    failed_tool: str,
    failed_params: dict[str, Any],
    caller_context: str,
    imported_symbols: list[str] | None = None,
) -> dict[str, Any]:
    import sys

    clean_mod = unquote(module_name).strip()
    if not clean_mod.isidentifier():
        return build_hal_response(
            self_href=f"mcp://tool/{failed_tool}",
            data={
                "status": "error",
                "error_type": "invalid_module_identifier",
                "policy": "navigation_only_no_patch",
                "missing_module": module_name,
                "message": f"Module name `{module_name}` is not a valid Python identifier.",
            },
            links={"list_registry": {"tool": "ast_read", "arguments": {}}},
        )

    pkg_name = resolve_package_name(clean_mod)
    install_step = {
        "tool": "ast_install_module",
        "arguments": {
            "module_name": clean_mod,
            "package_name": pkg_name,
            "exported_symbols": imported_symbols or [],
            "caller_context": caller_context,
        },
    }
    retry_step = {
        "tool": failed_tool,
        "arguments": failed_params,
    }

    return build_hal_response(
        self_href=f"mcp://tool/{failed_tool}",
        data={
            "status": "error",
            "error_type": "missing_module_dependency",
            "policy": "install_module_then_retry",
            "missing_module": clean_mod,
            "resolved_package": pkg_name,
            "imported_symbols": imported_symbols or [],
            "message": (
                f"Compilation of `{caller_context}` failed because module `{clean_mod}` "
                f"(package `{pkg_name}`) is not installed in `{sys.executable}`."
            ),
        },
        links={
            "install_module": install_step,
            "retry": retry_step,
            "recovery_chain": {
                "tool": "ast_chain",
                "arguments": {"commands": [install_step, retry_step]},
            },
        },
    )


def build_lint_hal_response(
    target_name: str,
    lint_result: LintResult,
    failed_tool: str,
    failed_params: dict[str, Any],
    exists_in_registry: bool,
) -> dict[str, Any]:
    from dataclasses import asdict

    serialized_issues = [asdict(i) for i in lint_result.issues]

    if lint_result.modified and lint_result.fixed_serialized_ast and lint_result.fixed_source:
        mutation_tool = "ast_update" if exists_in_registry else "ast_create"
        patched_args: dict[str, Any] = {
            "name": target_name,
            "code_or_ast": lint_result.fixed_serialized_ast,
            "description": failed_params.get("description", ""),
            "expose_as_tool": failed_params.get("expose_as_tool", True),
            "skip_roundtrip": failed_params.get("skip_roundtrip", False),
        }
        if mutation_tool == "ast_create":
            patched_args["overwrite"] = failed_params.get("overwrite", False)

        apply_patch_step = {
            "tool": mutation_tool,
            "arguments": patched_args,
        }

        if failed_tool in ("ast_create", "ast_update"):
            chain_commands = [apply_patch_step]
        else:
            chain_commands = [apply_patch_step, {"tool": failed_tool, "arguments": failed_params}]

        return build_hal_response(
            self_href=f"ast://resource/{target_name}/lint",
            data={
                "status": "error",
                "error_type": "lint_errors_autofixed",
                "policy": "autofixed_patch_ready",
                "target": target_name,
                "diagnostics": serialized_issues,
                "patched_resource": export_all_formats(target_name, lint_result.fixed_serialized_ast),
            },
            links={
                "apply_patched_update": apply_patch_step,
                "recovery_chain": {
                    "tool": "ast_chain",
                    "arguments": {"commands": chain_commands},
                },
                "inspect_existing": {"tool": "ast_read", "arguments": {"name": target_name}},
            },
        )

    nav_links: dict[str, Any] = {
        "list_registry": {"tool": "ast_read", "arguments": {}},
    }
    if exists_in_registry:
        nav_links["inspect_current"] = {"tool": "ast_read", "arguments": {"name": target_name}}
        nav_links["update_call"] = {
            "tool": "ast_update",
            "required_params": ["name", "code_or_ast"],
            "target": target_name,
        }
    else:
        nav_links["create_call"] = {
            "tool": "ast_create",
            "required_params": ["name", "code_or_ast"],
            "target": target_name,
        }
        nav_links["create_blank_shell_call"] = {
            "tool": "ast_create",
            "arguments": {"name": target_name},
        }

    return build_hal_response(
        self_href=f"ast://resource/{target_name}/lint",
        data={
            "status": "error",
            "error_type": "lint_errors_unfixable",
            "policy": "navigation_only_no_patch",
            "target": target_name,
            "diagnostics": serialized_issues,
        },
        links=nav_links,
    )


def _tool_link_to_rest_link(link_obj: Any, base_prefix: str = "/api") -> Any:
    if not isinstance(link_obj, dict):
        return link_obj

    enriched = dict(link_obj)
    tool = link_obj.get("tool")
    args = link_obj.get("arguments") or {}
    target = link_obj.get("target") or args.get("name") or args.get("target")

    if tool == "ast_read":
        name = args.get("name")
        enriched["method"] = "GET"
        enriched["href"] = f"{base_prefix}/ast/{name}" if name else f"{base_prefix}/ast"
    elif tool == "ast_create":
        enriched["method"] = "POST"
        enriched["href"] = f"{base_prefix}/ast"
        if args:
            enriched["body"] = args
    elif tool == "ast_update":
        name = args.get("name") or target
        enriched["method"] = "PUT"
        if name:
            enriched["href"] = f"{base_prefix}/ast/{name}"
        else:
            enriched["href"] = f"{base_prefix}/ast"
            enriched["templated"] = True
        if args:
            enriched["body"] = args
    elif tool == "ast_delete":
        name = args.get("name") or target
        enriched["method"] = "DELETE"
        if name:
            enriched["href"] = f"{base_prefix}/ast/{name}"
        else:
            enriched["href"] = f"{base_prefix}/ast"
            enriched["templated"] = True
    elif tool == "eval_ast_call":
        eval_target = args.get("target")
        enriched["method"] = "POST"
        if isinstance(eval_target, str) and eval_target.isidentifier():
            enriched["href"] = f"{base_prefix}/ast/{eval_target}/eval"
            enriched["body"] = {"args": args.get("args", []), "kwargs": args.get("kwargs", {})}
        else:
            enriched["href"] = f"{base_prefix}/eval"
            enriched["body"] = args
    elif tool == "ast_chain":
        enriched["method"] = "POST"
        enriched["href"] = f"{base_prefix}/chain"
        if args:
            enriched["body"] = args
    elif tool == "ast_convert":
        enriched["method"] = "POST"
        enriched["href"] = f"{base_prefix}/convert"
        if args:
            enriched["body"] = args
    elif tool == "ast_install_module":
        mod = args.get("module_name")
        enriched["method"] = "POST"
        if isinstance(mod, str) and mod.isidentifier():
            enriched["href"] = f"{base_prefix}/modules/{mod}/install"
        else:
            enriched["href"] = f"{base_prefix}/modules/install"
            enriched["templated"] = True
        if args:
            enriched["body"] = args
    elif tool == "ast_write_module_file":
        enriched["method"] = "POST"
        enriched["href"] = f"{base_prefix}/modules/files"
        if args:
            enriched["body"] = args

    return enriched


def enrich_rest_hal(
    payload: dict[str, Any],
    request_href: str,
    request_method: str = "GET",
    base_prefix: str = "/api",
) -> dict[str, Any]:
    out = copy.deepcopy(payload)
    out["_media_type"] = "application/hal+json"

    raw_links = out.get("_links") or {}
    rest_links: dict[str, Any] = {
        "self": {"href": request_href, "method": request_method},
        "api_root": {"href": base_prefix, "method": "GET"},
        "ast_collection": {"href": f"{base_prefix}/ast", "method": "GET"},
        "cli": {"href": f"{base_prefix}/cli", "method": "POST"},
    }

    for rel, link_val in raw_links.items():
        if rel == "self":
            rest_links["resource_self"] = link_val
            continue
        rest_links[rel] = _tool_link_to_rest_link(link_val, base_prefix=base_prefix)

    out["_links"] = rest_links

    embedded = out.get("_embedded")
    if isinstance(embedded, dict) and isinstance(embedded.get("items"), list):
        for item in embedded["items"]:
            if isinstance(item, dict) and isinstance(item.get("_links"), dict):
                item_name = item.get("name", "")
                item["_links"] = {
                    "self": {"href": f"{base_prefix}/ast/{item_name}", "method": "GET"},
                    **{
                        k: _tool_link_to_rest_link(v, base_prefix=base_prefix)
                        for k, v in item["_links"].items()
                    },
                }

    return out
