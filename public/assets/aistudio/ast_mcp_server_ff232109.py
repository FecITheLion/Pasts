import argparse
import warnings as _warnings

try:
    from pydantic_settings.sources.utils import IncompleteFieldDefinitionWarning as _IFDW

    _warnings.filterwarnings("ignore", category=_IFDW)
except Exception:
    _warnings.filterwarnings("ignore", message=".*has an incomplete definition.*")

import ast
import asyncio
import copy
import importlib
import importlib.util
import inspect
import io
import json
import os
import queue
import random
import re
import string
import subprocess
import sys
import threading
import time
import tokenize
from dataclasses import asdict, dataclass, field, is_dataclass, make_dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.metadata import packages_distributions
from typing import Any
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen

try:
    import pydantic._internal._typing_extra as _pte

    if not hasattr(_pte, "eval_type_backport"):
        _pte.eval_type_backport = (
            lambda v, g=None, l=None, tp=None: eval(v, g, l) if isinstance(v, str) else v
        )
except Exception:
    pass

try:
    from mcp.server.fastmcp import Context, FastMCP

    HAS_MCP = True
    _MCP_IMPORT_ERROR_MSG = ""
except Exception as _mcp_import_err:
    HAS_MCP = False
    _MCP_IMPORT_ERROR_MSG = str(_mcp_import_err)
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
            print(f"[WARN] Official `mcp` SDK failed to load ({_MCP_IMPORT_ERROR_MSG}).")
            print("[WARN] Running Live HTML/HTTP Dashboard, REST /api & stdin bridge only.")
            threading.Event().wait()


try:
    from pydantic import BaseModel, create_model

    HAS_PYDANTIC = True
except Exception:
    BaseModel = None  # type: ignore[assignment]
    create_model = None  # type: ignore[assignment]
    HAS_PYDANTIC = False


_AST_EVAL_NAMESPACE: dict[str, Any] = {
    "ast": ast,
    **{name: getattr(ast, name) for name in dir(ast) if not name.startswith("_")},
}


# ---------------------------------------------------------------------------
# Cross-Platform ANSI Color Formatting (Source + Route Color-Coded)
# ---------------------------------------------------------------------------
def _enable_windows_ansi() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.name != "nt":
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        for handle_id in (-11, -12):  # STD_OUTPUT_HANDLE, STD_ERROR_HANDLE
            h = kernel32.GetStdHandle(handle_id)
            mode = ctypes.c_uint32()
            if kernel32.GetConsoleMode(h, ctypes.byref(mode)):
                kernel32.SetConsoleMode(h, mode.value | 0x0004)
        return True
    except Exception:
        return False


_ANSI_ENABLED = _enable_windows_ansi()

_ANSI: dict[str, str] = {
    "reset": "\033[0m",
    "bold": "\033[1m",
    "dim": "\033[2m",
    "red": "\033[1;91m",
    "green": "\033[92m",
    "yellow": "\033[93m",
    "blue": "\033[94m",
    "magenta": "\033[95m",
    "cyan": "\033[96m",
    "teal": "\033[36m",
    "purple": "\033[35m",
    "white": "\033[97m",
}

_SOURCE_COLORS: dict[str, str] = {
    "stdin": "yellow",
    "rest": "cyan",
    "rest_cli": "cyan",
    "http": "cyan",
    "mcp": "magenta",
    "mcp_resource": "magenta",
}

_ROUTE_META: dict[str, tuple[str, str]] = {
    "ast_create": ("AST_REGISTRY", "blue"),
    "ast_read": ("AST_REGISTRY", "blue"),
    "ast_update": ("AST_REGISTRY", "blue"),
    "ast_delete": ("AST_REGISTRY", "blue"),
    "eval_ast_call": ("EVAL_RUNTIME", "green"),
    "ast_convert": ("CONVERTER", "teal"),
    "ast_chain": ("CHAIN_ORCHESTRATOR", "purple"),
    "ast_install_module": ("MODULE_ENV", "white"),
    "ast_write_module_file": ("MODULE_ENV", "white"),
}


def _colorize(text: str, color_key: str) -> str:
    if not _ANSI_ENABLED or color_key not in _ANSI:
        return text
    return f"{_ANSI[color_key]}{text}{_ANSI['reset']}"


# ---------------------------------------------------------------------------
# Data Models for Lint Diagnostics & Policy Output
# ---------------------------------------------------------------------------
@dataclass
class LintIssue:
    code: str
    severity: str
    message: str
    line: int | None = None
    col: int | None = None
    fixable: bool = False
    fix_applied: str | None = None


@dataclass
class LintResult:
    valid: bool
    modified: bool
    issues: list[LintIssue]
    original_source: str
    fixed_source: str | None = None
    fixed_serialized_ast: str | None = None


# ---------------------------------------------------------------------------
# AST Serialization, Roundtrip Gate & Blank Shell Helpers
# ---------------------------------------------------------------------------
def serialize_function(func_def: ast.FunctionDef) -> str:
    return ast.dump(func_def)


def build_blank_shell_ast(name: str, arg_names: list[str] | None = None) -> tuple[str, str]:
    if arg_names:
        args_node = ast.arguments(
            posonlyargs=[],
            args=[ast.arg(arg=a) for a in arg_names],
            vararg=None,
            kwonlyargs=[],
            kw_defaults=[],
            kwarg=None,
            defaults=[],
        )
    else:
        args_node = ast.arguments(
            posonlyargs=[],
            args=[],
            vararg=ast.arg(arg="args"),
            kwonlyargs=[],
            kw_defaults=[],
            kwarg=ast.arg(arg="kwargs"),
            defaults=[],
        )

    fn_node = ast.FunctionDef(
        name=name,
        args=args_node,
        body=[
            ast.Expr(
                value=ast.Constant(
                    value=f"Auto-generated blank shell for `{name}`. Update via `ast_update`."
                )
            ),
            ast.Return(value=ast.Constant(value=None)),
        ],
        decorator_list=[],
        returns=None,
    )
    ast.fix_missing_locations(fn_node)
    return ast.unparse(fn_node), serialize_function(fn_node)


def _ast_to_dict(node: Any) -> Any:
    if isinstance(node, ast.AST):
        result: dict[str, Any] = {"_type": type(node).__name__}
        for field_name in node._fields:
            result[field_name] = _ast_to_dict(getattr(node, field_name))
        return result
    if isinstance(node, list):
        return [_ast_to_dict(item) for item in node]
    return node


def _dict_to_ast(data: Any) -> Any:
    if isinstance(data, list):
        return [_dict_to_ast(item) for item in data]
    if isinstance(data, dict) and "_type" in data:
        node_cls = getattr(ast, data["_type"], None)
        if node_cls is None or not issubclass(node_cls, ast.AST):
            raise ValueError(f"Unknown AST node type: {data['_type']}")
        fields = {k: _dict_to_ast(v) for k, v in data.items() if k != "_type"}
        return node_cls(**fields)
    return data


def deserialize_ast_node(serialized_ast: str) -> ast.FunctionDef:
    expr_ast = ast.parse(serialized_ast, mode="eval")
    node = eval(compile(expr_ast, filename="<serialized-ast>", mode="eval"), _AST_EVAL_NAMESPACE)
    if not isinstance(node, ast.FunctionDef):
        raise TypeError(f"Expected ast.FunctionDef, got {type(node).__name__}")
    return ast.fix_missing_locations(node)


def export_all_formats(name: str, serialized_ast: str) -> dict[str, Any]:
    node = deserialize_ast_node(serialized_ast)
    node.name = name
    ast.fix_missing_locations(node)
    return {
        "name": name,
        "formats": {
            "python_source": ast.unparse(node),
            "serialized_ast": ast.dump(node),
            "pretty_ast": ast.dump(node, indent=2),
            "json_ast": _ast_to_dict(node),
        },
    }


def verify_and_roundtrip_ast(name: str, serialized_ast: str) -> dict[str, Any]:
    canonical = deserialize_ast_node(serialized_ast)
    canonical.name = name
    ast.fix_missing_locations(canonical)
    baseline_dump = ast.dump(canonical)

    formats = export_all_formats(name, baseline_dump)["formats"]
    stages_verified: list[str] = []

    parsed_mod = ast.parse(formats["python_source"], mode="exec")
    if len(parsed_mod.body) != 1 or not isinstance(parsed_mod.body[0], ast.FunctionDef):
        raise ValueError("Roundtrip failed at `python_source`: did not parse back to a single FunctionDef.")
    from_source = ast.fix_missing_locations(parsed_mod.body[0])
    if ast.dump(from_source) != baseline_dump:
        raise ValueError("Roundtrip mismatch after `python_source` (`ast.unparse` -> `ast.parse`).")
    stages_verified.append("python_source")

    from_serialized = deserialize_ast_node(formats["serialized_ast"])
    if ast.dump(from_serialized) != baseline_dump:
        raise ValueError("Roundtrip mismatch after `serialized_ast` (`ast.dump`).")
    stages_verified.append("serialized_ast")

    from_pretty = deserialize_ast_node(formats["pretty_ast"])
    if ast.dump(from_pretty) != baseline_dump:
        raise ValueError("Roundtrip mismatch after `pretty_ast` (`ast.dump(indent=2)`).")
    stages_verified.append("pretty_ast")

    json_blob = json.dumps(formats["json_ast"])
    from_json = _dict_to_ast(json.loads(json_blob))
    if not isinstance(from_json, ast.FunctionDef):
        raise ValueError("Roundtrip failed at `json_ast`: did not reconstruct a FunctionDef.")
    ast.fix_missing_locations(from_json)
    if ast.dump(from_json) != baseline_dump:
        raise ValueError("Roundtrip mismatch after `json_ast` (`_ast_to_dict` -> `_dict_to_ast`).")
    stages_verified.append("json_ast")

    return {
        "name": name,
        "verified": True,
        "stages": stages_verified,
        "canonical_serialized_ast": baseline_dump,
        "formats": formats,
    }


def _find_called_names(node: ast.AST) -> set[str]:
    called: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name):
            called.add(sub.func.id)
    return called


# ---------------------------------------------------------------------------
# Flexible Call Argument Normalizer & Signature Patching Helpers
# ---------------------------------------------------------------------------
def normalize_eval_call_args(
    raw_args: Any = None,
    raw_kwargs: Any = None,
) -> tuple[list[Any], dict[str, Any]]:
    norm_args: list[Any] = []
    norm_kwargs: dict[str, Any] = {}

    if isinstance(raw_kwargs, dict):
        norm_kwargs.update(raw_kwargs)

    if raw_args is None:
        return norm_args, norm_kwargs

    if isinstance(raw_args, dict):
        if ("args" in raw_args or "kwargs" in raw_args) and set(raw_args.keys()).issubset(
            {"args", "kwargs", "target", "expression"}
        ):
            sub_args, sub_kwargs = normalize_eval_call_args(
                raw_args.get("args"),
                raw_args.get("kwargs"),
            )
            norm_args.extend(sub_args)
            norm_kwargs.update(sub_kwargs)
        else:
            norm_kwargs.update(raw_args)
        return norm_args, norm_kwargs

    if isinstance(raw_args, (list, tuple)):
        items = list(raw_args)
        if not items:
            return norm_args, norm_kwargs
        if len(items) == 2 and isinstance(items[0], (list, tuple)) and isinstance(items[1], dict):
            norm_args.extend(items[0])
            norm_kwargs.update(items[1])
            return norm_args, norm_kwargs
        if isinstance(items[-1], dict):
            norm_args.extend(items[:-1])
            norm_kwargs.update(items[-1])
            return norm_args, norm_kwargs
        norm_args.extend(items)
        return norm_args, norm_kwargs

    norm_args.append(raw_args)
    return norm_args, norm_kwargs


def _collect_all_identifiers(node: ast.AST) -> set[str]:
    used: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name):
            used.add(sub.id)
        elif isinstance(sub, ast.arg):
            used.add(sub.arg)
        elif isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            used.add(sub.name)
        elif isinstance(sub, (ast.Import, ast.ImportFrom)):
            for alias in sub.names:
                used.add(alias.asname or alias.name.split(".")[0])
    return used


def _generate_new_param_name(used_names: set[str], index_hint: int = 1) -> str:
    alphabet = string.ascii_uppercase + string.digits
    for ch in alphabet:
        candidate = f"_new_{ch}"
        if candidate not in used_names:
            used_names.add(candidate)
            return candidate
    counter = max(1, index_hint)
    while True:
        suffix = "".join(random.choices(alphabet, k=3))
        candidate = f"_new_{suffix}_{counter}"
        if candidate not in used_names:
            used_names.add(candidate)
            return candidate
        counter += 1


def _literal_ast_for_value(val: Any) -> ast.expr:
    if isinstance(val, (int, float, str, bool)) or val is None:
        return ast.Constant(value=val)
    if isinstance(val, list):
        return ast.List(elts=[_literal_ast_for_value(x) for x in val], ctx=ast.Load())
    if isinstance(val, dict):
        return ast.Dict(
            keys=[_literal_ast_for_value(k) for k in val.keys()],
            values=[_literal_ast_for_value(v) for v in val.values()],
        )
    return ast.Constant(value=None)


def _infer_type_annotation_expr(val: Any) -> ast.expr | None:
    if isinstance(val, bool):
        return ast.Name(id="bool", ctx=ast.Load())
    if isinstance(val, int):
        return ast.Name(id="int", ctx=ast.Load())
    if isinstance(val, float):
        return ast.Name(id="float", ctx=ast.Load())
    if isinstance(val, str):
        return ast.Name(id="str", ctx=ast.Load())
    if isinstance(val, list):
        return ast.Name(id="list", ctx=ast.Load())
    if isinstance(val, dict):
        return ast.Name(id="dict", ctx=ast.Load())
    return None


def _default_expr_for_annotation(ann: ast.expr | None) -> ast.expr:
    if isinstance(ann, ast.Name):
        if ann.id == "int":
            return ast.Constant(value=0)
        if ann.id == "float":
            return ast.Constant(value=0.0)
        if ann.id == "str":
            return ast.Constant(value="")
        if ann.id == "bool":
            return ast.Constant(value=False)
    return ast.Constant(value=None)


OMITTED_SENTINEL = object()


def parse_eval_args_with_omissions(raw_input: Any) -> tuple[list[Any], dict[str, Any], list[int], list[str]]:
    """
    Parses standard JSON or relaxed shorthand such as:
      - '[, {"k": 1}]'   -> positional index 0 omitted
      - '[3, {"k": }]'   -> keyword 'k' omitted
      - '[3, {"k": 1}]'  -> args=[3], kwargs={'k': 1}
    Returns (call_args, call_kwargs, omitted_pos_indices, omitted_kw_names).
    """
    omitted_pos: list[int] = []
    omitted_kws: list[str] = []

    if isinstance(raw_input, str):
        text = raw_input.strip()
        # Detect omitted keyword values like {"k": } or {"k":,}
        def _replace_empty_kw(match: re.Match[str]) -> str:
            kw_key = match.group(1)
            if kw_key not in omitted_kws:
                omitted_kws.append(kw_key)
            return f'"{kw_key}": "__OMITTED_ARG__"'

        normalized = re.sub(r'"([A-Za-z_][A-Za-z0-9_]*)"\s*:\s*(?=[,}])', _replace_empty_kw, text)

        # Detect omitted leading/middle positional slots like [, {"k": 1}] or [3, , 4]
        if normalized.startswith("[") and normalized.endswith("]"):
            inner = normalized[1:-1]
            # Replace leading comma or consecutive commas outside braces/quotes
            normalized = re.sub(r'^\s*,', '"__OMITTED_ARG__",', inner)
            normalized = re.sub(r',\s*(?=,)', ', "__OMITTED_ARG__"', normalized)
            normalized = f"[{normalized}]"

        try:
            parsed = json.loads(normalized)
        except json.JSONDecodeError:
            parsed = raw_input
    else:
        parsed = raw_input

    call_args: list[Any] = []
    call_kwargs: dict[str, Any] = {}

    if isinstance(parsed, list):
        # Trailing dict in list acts as kwargs (e.g. [3, {"k": 1}])
        items = list(parsed)
        if items and isinstance(items[-1], dict):
            raw_kw = items.pop()
            for k, v in raw_kw.items():
                if v == "__OMITTED_ARG__" or v is None:
                    if k not in omitted_kws:
                        omitted_kws.append(k)
                else:
                    call_kwargs[k] = v
        for idx, val in enumerate(items):
            if val == "__OMITTED_ARG__":
                omitted_pos.append(idx)
                call_args.append(OMITTED_SENTINEL)
            else:
                call_args.append(val)
    elif isinstance(parsed, dict):
        if "args" in parsed or "kwargs" in parsed:
            raw_a = parsed.get("args", [])
            raw_k = parsed.get("kwargs", {})
            for idx, val in enumerate(raw_a if isinstance(raw_a, list) else [raw_a]):
                if val == "__OMITTED_ARG__":
                    omitted_pos.append(idx)
                    call_args.append(OMITTED_SENTINEL)
                else:
                    call_args.append(val)
            if isinstance(raw_k, dict):
                for k, v in raw_k.items():
                    if v == "__OMITTED_ARG__" or v is None:
                        if k not in omitted_kws:
                            omitted_kws.append(k)
                    else:
                        call_kwargs[k] = v
        else:
            for k, v in parsed.items():
                if v == "__OMITTED_ARG__" or v is None:
                    if k not in omitted_kws:
                        omitted_kws.append(k)
                else:
                    call_kwargs[k] = v
    elif parsed == "__OMITTED_ARG__":
        omitted_pos.append(0)
        call_args.append(OMITTED_SENTINEL)
    else:
        call_args.append(parsed)

    return call_args, call_kwargs, omitted_pos, omitted_kws


def generate_updated_signature_ast(
    name: str,
    serialized_ast: str,
    call_args: list[Any],
    call_kwargs: dict[str, Any],
    default_overrides: dict[str, Any] | None = None,
) -> tuple[str, str, list[str]]:
    """
    Updates a function signature to match a call WITHOUT inventing default values
    for parameters that were explicitly supplied in call_args / call_kwargs.
    """
    node = deserialize_ast_node(serialized_ast)
    node.name = name
    default_overrides = default_overrides or {}
    used_names = _collect_all_identifiers(node)
    added_params: list[str] = []

    pos_params = list(node.args.posonlyargs) + list(node.args.args)
    kwonly_params = list(node.args.kwonlyargs)
    existing_param_names = {p.arg for p in pos_params} | {p.arg for p in kwonly_params}
    if node.args.vararg:
        existing_param_names.add(node.args.vararg.arg)
    if node.args.kwarg:
        existing_param_names.add(node.args.kwarg.arg)

    # Apply explicit default overrides (from set-default modal) if provided
    if default_overrides:
        num_pos = len(node.args.args)
        num_def = len(node.args.defaults)
        first_def_idx = num_pos - num_def
        for idx, arg_node in enumerate(node.args.args):
            if arg_node.arg in default_overrides:
                lit = _literal_ast_for_value(default_overrides[arg_node.arg])
                if idx >= first_def_idx:
                    node.args.defaults[idx - first_def_idx] = lit
                else:
                    # Backfill defaults only if needed to reach idx
                    while len(node.args.defaults) < (num_pos - idx):
                        fill_idx = num_pos - len(node.args.defaults) - 1
                        fill_arg = node.args.args[fill_idx]
                        fill_val = default_overrides.get(fill_arg.arg, 0)
                        node.args.defaults.insert(0, _literal_ast_for_value(fill_val))
                added_params.append(f"{arg_node.arg}={ast.unparse(lit)}")

        for kw_idx, kw_node in enumerate(node.args.kwonlyargs):
            if kw_node.arg in default_overrides:
                lit = _literal_ast_for_value(default_overrides[kw_node.arg])
                node.args.kw_defaults[kw_idx] = lit
                added_params.append(f"{kw_node.arg}={ast.unparse(lit)}")

    # 1. Extra positional arguments -> add typed parameters WITHOUT defaults
    #    (unless preceding positional parameters already have defaults)
    extra_pos_count = max(0, len(call_args) - len(pos_params))
    if node.args.vararg is None and extra_pos_count > 0:
        has_existing_defaults = len(node.args.defaults) > 0
        for offset in range(extra_pos_count):
            sample_val = call_args[len(pos_params) + offset]
            if sample_val is OMITTED_SENTINEL:
                sample_val = 0
            new_param_name = _generate_new_param_name(used_names, index_hint=offset + 1)
            ann_expr = _infer_type_annotation_expr(sample_val)
            node.args.args.append(ast.arg(arg=new_param_name, annotation=ann_expr))
            if has_existing_defaults:
                default_expr = _literal_ast_for_value(sample_val)
                node.args.defaults.append(default_expr)
                added_params.append(f"{new_param_name}={ast.unparse(default_expr)}")
            else:
                added_params.append(new_param_name)
            existing_param_names.add(new_param_name)
            used_names.add(new_param_name)

    # 2. Unexpected keyword arguments (e.g. {"k": 1}) -> add named parameter WITHOUT a default
    #    so def ast1(x: int) -> int becomes def ast1(x: int, k: int) -> int
    if node.args.kwarg is None and call_kwargs:
        has_existing_pos_defaults = len(node.args.defaults) > 0
        for kw_name, kw_val in call_kwargs.items():
            if not isinstance(kw_name, str) or not kw_name.isidentifier():
                continue
            if kw_name in existing_param_names:
                continue
            ann_expr = _infer_type_annotation_expr(kw_val)
            if has_existing_pos_defaults or node.args.vararg is not None or node.args.kwonlyargs:
                # Keyword-only parameter allows no default even when positional params have defaults
                node.args.kwonlyargs.append(ast.arg(arg=kw_name, annotation=ann_expr))
                node.args.kw_defaults.append(None)
            else:
                # Positional-or-keyword parameter with NO default
                node.args.args.append(ast.arg(arg=kw_name, annotation=ann_expr))
            existing_param_names.add(kw_name)
            used_names.add(kw_name)
            added_params.append(kw_name)

    ast.fix_missing_locations(node)
    return ast.unparse(node), serialize_function(node), added_params


def check_and_build_missing_default_recovery(
    target_name: str,
    serialized_ast: str,
    failed_params: dict[str, Any],
    call_args: list[Any],
    call_kwargs: dict[str, Any],
    omitted_pos_indices: list[int],
    omitted_kw_names: list[str],
) -> dict[str, Any] | None:
    """
    Checks if any omitted positional slot (e.g. [, {"k": 1}]) or omitted keyword slot
    (e.g. [3, {"k": }]) targets a parameter that currently has NO default value.
    If so, returns a HAL response triggering the Set-Default modal.
    """
    node = deserialize_ast_node(serialized_ast)
    pos_args = list(node.args.posonlyargs) + list(node.args.args)
    num_pos = len(pos_args)
    first_default_idx = num_pos - len(node.args.defaults)

    params_needing_default: list[dict[str, Any]] = []

    for pos_idx in omitted_pos_indices:
        if 0 <= pos_idx < num_pos:
            has_default = pos_idx >= first_default_idx
            if not has_default:
                p_node = pos_args[pos_idx]
                ann_str = ast.unparse(p_node.annotation) if p_node.annotation else "Any"
                params_needing_default.append({
                    "name": p_node.arg,
                    "kind": "positional",
                    "index": pos_idx,
                    "annotation": ann_str,
                    "suggested_default": 0 if ann_str == "int" else (0.0 if ann_str == "float" else ""),
                })

    kw_map: dict[str, tuple[ast.arg, bool]] = {}
    for idx, p_node in enumerate(pos_args):
        kw_map[p_node.arg] = (p_node, idx >= first_default_idx)
    for kw_idx, kw_node in enumerate(node.args.kwonlyargs):
        has_kw_def = kw_idx < len(node.args.kw_defaults) and node.args.kw_defaults[kw_idx] is not None
        kw_map[kw_node.arg] = (kw_node, has_kw_def)

    for kw_name in omitted_kw_names:
        if kw_name in kw_map:
            p_node, has_default = kw_map[kw_name]
            if not has_default:
                ann_str = ast.unparse(p_node.annotation) if p_node.annotation else "Any"
                params_needing_default.append({
                    "name": kw_node.arg if hasattr(p_node, "arg") else kw_name,
                    "kind": "keyword",
                    "annotation": ann_str,
                    "suggested_default": 0 if ann_str == "int" else (0.0 if ann_str == "float" else ""),
                })
        else:
            # Keyword parameter doesn't exist yet AND its value was omitted (e.g. [3, {"k": }])
            params_needing_default.append({
                "name": kw_name,
                "kind": "new_keyword",
                "annotation": "int",
                "suggested_default": 0,
            })

    if not params_needing_default:
        return None

    # Build initial suggested signature using suggested_default values (editable in modal)
    seed_overrides = {p["name"]: p["suggested_default"] for p in params_needing_default}
    # Ensure any new_keyword is also added to AST before setting its default
    temp_kwargs = dict(call_kwargs)
    for p in params_needing_default:
        if p["kind"] == "new_keyword" and p["name"] not in temp_kwargs:
            temp_kwargs[p["name"]] = p["suggested_default"]

    updated_source, updated_serialized_ast, added_params = generate_updated_signature_ast(
        name=target_name,
        serialized_ast=serialized_ast,
        call_args=[0 if a is OMITTED_SENTINEL else a for a in call_args],
        call_kwargs=temp_kwargs,
        default_overrides=seed_overrides,
    )

    cleaned_retry_args = [a for a in call_args if a is not OMITTED_SENTINEL]
    apply_patch_step = {
        "tool": "ast_update",
        "arguments": {
            "name": target_name,
            "code_or_ast": updated_source,
            "expose_as_tool": True,
            "skip_roundtrip": False,
        },
    }
    retry_step = {
        "tool": "eval_ast_call",
        "arguments": {
            "target": target_name,
            "args": cleaned_retry_args,
            "kwargs": call_kwargs,
        },
    }

    missing_names = [p["name"] for p in params_needing_default]
    return build_hal_response(
        self_href=f"ast://resource/{target_name}/missing-default",
        data={
            "status": "error",
            "error_type": "missing_default_value",
            "policy": "set_default_value",
            "target": target_name,
            "missing_default_params": params_needing_default,
            "added_parameters": added_params,
            "message": (
                f"Parameter(s) {missing_names} on `{target_name}` have no default value. "
                f"Set default value(s) below and execute."
            ),
            "python_source": updated_source,
        },
        links={
            "set_default_and_retry": {
                "tool": "ast_chain",
                "arguments": {"commands": [apply_patch_step, retry_step]},
            },
            "apply_patched_update": apply_patch_step,
            "retry": retry_step,
        },
    )


# ---------------------------------------------------------------------------
# Compile-Time Dependency Inspection & Fallback Module Stub Generator
# ---------------------------------------------------------------------------
COMMON_IMPORT_TO_PACKAGE: dict[str, str] = {
    "bs4": "beautifulsoup4",
    "cv2": "opencv-python",
    "dateutil": "python-dateutil",
    "dotenv": "python-dotenv",
    "fitz": "PyMuPDF",
    "jwt": "PyJWT",
    "PIL": "Pillow",
    "pydantic": "pydantic",
    "sklearn": "scikit-learn",
    "yaml": "PyYAML",
}


class MissingModuleDependencyError(Exception):
    def __init__(self, module_name: str, caller_context: str, imported_symbols: list[str] | None = None):
        super().__init__(f"Missing Python module '{module_name}' required by '{caller_context}'")
        self.module_name = module_name
        self.caller_context = caller_context
        self.imported_symbols = imported_symbols or []


class CircularDependencyError(ValueError):
    def __init__(self, key: str, cycle_path: list[str]):
        self.key = key
        self.cycle_path = cycle_path
        cycle_str = " -> ".join(cycle_path)
        super().__init__(f"Circular AST dependency detected involving '{key}': {cycle_str}")


def resolve_package_name(module_name: str) -> str:
    pkg_map = packages_distributions()
    if module_name in pkg_map and pkg_map[module_name]:
        return pkg_map[module_name][0]
    return COMMON_IMPORT_TO_PACKAGE.get(module_name, module_name)


def is_module_available(module_name: str) -> bool:
    if module_name in sys.stdlib_module_names or module_name in sys.modules:
        return True
    return importlib.util.find_spec(module_name) is not None


def extract_ast_import_requirements(node: ast.AST) -> dict[str, list[str]]:
    reqs: dict[str, set[str]] = {}
    for sub in ast.walk(node):
        if isinstance(sub, ast.Import):
            for alias in sub.names:
                top = alias.name.split(".")[0].strip()
                if top:
                    reqs.setdefault(top, set())
        elif isinstance(sub, ast.ImportFrom):
            if getattr(sub, "level", 0) == 0 and sub.module:
                top = sub.module.split(".")[0].strip()
                if top:
                    sym_set = reqs.setdefault(top, set())
                    for alias in sub.names:
                        if alias.name != "*":
                            sym_set.add(alias.name)
    return {mod: sorted(syms) for mod, syms in sorted(reqs.items())}


def generate_fallback_module_script(
    module_name: str,
    exported_symbols: list[str] | None = None,
    caller_context: str = "ast_function",
) -> dict[str, Any]:
    clean_mod = module_name.strip()
    if not clean_mod.isidentifier():
        raise ValueError(f"Invalid Python module identifier `{module_name}`.")

    symbols = [s for s in dict.fromkeys(exported_symbols or []) if isinstance(s, str) and s.isidentifier()]
    all_exports = [repr(s) for s in symbols]
    all_line = f"__all__ = [{', '.join(all_exports)}]" if symbols else "__all__: list[str] = []"

    lines = [
        f'"""Auto-generated local fallback module `{clean_mod}.py` for `{caller_context}`."""',
        "",
        "from typing import Any",
        "",
        all_line,
        "",
        "",
        "class _StubExport:",
        "    def __init__(self, symbol_name: str) -> None:",
        "        self._symbol_name = symbol_name",
        "",
        "    def __call__(self, *args: Any, **kwargs: Any) -> dict[str, Any]:",
        "        return {",
        f'            "module": "{clean_mod}",',
        '            "symbol": self._symbol_name,',
        '            "status": "stub_executed",',
        '            "args": args,',
        '            "kwargs": kwargs,',
        "        }",
        "",
        "    def __getattr__(self, attr: str) -> '_StubExport':",
        '        return _StubExport(f"{self._symbol_name}.{attr}")',
        "",
        "    def __repr__(self) -> str:",
        '        return f"<StubExport {self._symbol_name}>"',
        "",
    ]

    for sym in symbols:
        lines.extend(
            [
                "",
                f"def {sym}(*args: Any, **kwargs: Any) -> Any:",
                f'    """Local fallback export `{sym}` for `{clean_mod}`."""',
                f'    return _StubExport("{sym}")(*args, **kwargs)',
            ]
        )

    lines.extend(
        [
            "",
            "",
            "def __getattr__(name: str) -> _StubExport:",
            "    return _StubExport(name)",
            "",
        ]
    )

    script_source = "\n".join(lines)
    ast.parse(script_source, mode="exec")

    return {
        "filename": f"{clean_mod}.py",
        "module_name": clean_mod,
        "exports": symbols,
        "python_source": script_source,
    }


# ---------------------------------------------------------------------------
# Full-Page Module Script Wrapper + AST & Source Linter + Auto-Fix Transformer
# ---------------------------------------------------------------------------
def _wrap_module_script_as_function(
    name: str,
    mod: ast.Module,
    issues: list[LintIssue],
) -> ast.FunctionDef:
    wrapper_body: list[ast.stmt] = []
    defined_callables: list[str] = []
    defined_vars: list[str] = []
    has_matching_entry_fn = False

    for stmt in mod.body:
        if isinstance(stmt, ast.ImportFrom) and stmt.module == "__future__":
            continue

        if (
            isinstance(stmt, ast.If)
            and isinstance(stmt.test, ast.Compare)
            and isinstance(stmt.test.left, ast.Name)
            and stmt.test.left.id == "__name__"
        ):
            wrapper_body.extend(stmt.body)
            continue

        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if stmt.name == name:
                has_matching_entry_fn = True
            defined_callables.append(stmt.name)
            wrapper_body.append(stmt)
        elif isinstance(stmt, ast.ClassDef):
            defined_callables.append(stmt.name)
            wrapper_body.append(stmt)
        elif isinstance(stmt, ast.Assign):
            for t in stmt.targets:
                if isinstance(t, ast.Name) and not t.id.startswith("_"):
                    defined_vars.append(t.id)
            wrapper_body.append(stmt)
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            if not stmt.target.id.startswith("_"):
                defined_vars.append(stmt.target.id)
            wrapper_body.append(stmt)
        else:
            wrapper_body.append(stmt)

    if has_matching_entry_fn:
        dispatch_stmt = ast.Return(
            value=ast.Call(
                func=ast.Name(id=name, ctx=ast.Load()),
                args=[ast.Starred(value=ast.Name(id="args", ctx=ast.Load()), ctx=ast.Load())],
                keywords=[ast.keyword(arg=None, value=ast.Name(id="kwargs", ctx=ast.Load()))],
            )
        )
        wrapper_body.append(dispatch_stmt)
    else:
        exported_names = list(dict.fromkeys([*defined_callables, *defined_vars]))
        if exported_names:
            dict_keys: list[ast.expr | None] = [ast.Constant(value=k) for k in exported_names]
            dict_vals: list[ast.expr] = [ast.Name(id=k, ctx=ast.Load()) for k in exported_names]
            wrapper_body.append(ast.Return(value=ast.Dict(keys=dict_keys, values=dict_vals)))
        else:
            wrapper_body.append(ast.Return(value=ast.Constant(value=None)))

    wrapped_fn = ast.FunctionDef(
        name=name,
        args=ast.arguments(
            posonlyargs=[],
            args=[],
            vararg=ast.arg(arg="args"),
            kwonlyargs=[],
            kw_defaults=[],
            kwarg=ast.arg(arg="kwargs"),
            defaults=[],
        ),
        body=wrapper_body,
        decorator_list=[],
        returns=None,
    )
    ast.fix_missing_locations(wrapped_fn)

    issues.append(
        LintIssue(
            code="W0105_MODULE_SCRIPT_WRAPPED",
            severity="warning",
            message="Full-page Python script automatically wrapped into a top-level callable function.",
            line=1,
            fixable=True,
            fix_applied=f"Wrapped {len(mod.body)} top-level module statement(s) into `def {name}(*args, **kwargs)`.",
        )
    )
    return wrapped_fn


class _ASTAutoFixTransformer(ast.NodeTransformer):
    def __init__(self, issues: list[LintIssue]) -> None:
        self.issues = issues

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.FunctionDef:
        all_arg_names = [a.arg for a in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)]
        if node.args.vararg:
            all_arg_names.append(node.args.vararg.arg)
        if node.args.kwarg:
            all_arg_names.append(node.args.kwarg.arg)

        seen_args: set[str] = set()
        for arg_name in all_arg_names:
            if arg_name in seen_args:
                self.issues.append(
                    LintIssue(
                        code="E901_DUPLICATE_ARG",
                        severity="error",
                        message=f"Duplicate parameter name `{arg_name}` in `{node.name}`.",
                        line=getattr(node, "lineno", None),
                        fixable=False,
                    )
                )
            seen_args.add(arg_name)

        injected_guards: list[ast.stmt] = []
        pos_args = list(node.args.posonlyargs) + list(node.args.args)
        num_defaults = len(node.args.defaults)
        if num_defaults:
            offset = len(pos_args) - num_defaults
            for idx, default_expr in enumerate(list(node.args.defaults)):
                if isinstance(default_expr, (ast.List, ast.Dict, ast.Set)):
                    param_name = pos_args[offset + idx].arg
                    empty_copy = copy.deepcopy(default_expr)
                    node.args.defaults[idx] = ast.Constant(value=None)
                    injected_guards.append(
                        ast.If(
                            test=ast.Compare(
                                left=ast.Name(id=param_name, ctx=ast.Load()),
                                ops=[ast.Is()],
                                comparators=[ast.Constant(value=None)],
                            ),
                            body=[
                                ast.Assign(
                                    targets=[ast.Name(id=param_name, ctx=ast.Store())],
                                    value=empty_copy,
                                )
                            ],
                            orelse=[],
                        )
                    )
                    self.issues.append(
                        LintIssue(
                            code="B006_MUTABLE_DEFAULT",
                            severity="error",
                            message=f"Mutable default argument on `{param_name}`.",
                            line=getattr(default_expr, "lineno", None),
                            fixable=True,
                            fix_applied=f"Replaced mutable default on `{param_name}` with `None` + lazy init.",
                        )
                    )

        self.generic_visit(node)

        if injected_guards:
            node.body = injected_guards + node.body

        if not node.body:
            node.body = [ast.Return(value=ast.Constant(value=None))]
            self.issues.append(
                LintIssue(
                    code="E701_EMPTY_BODY",
                    severity="error",
                    message=f"Function `{node.name}` has an empty body.",
                    line=getattr(node, "lineno", None),
                    fixable=True,
                    fix_applied="Inserted `return None` into empty function body.",
                )
            )

        trimmed_body: list[ast.stmt] = []
        unreachable_count = 0
        for stmt in node.body:
            trimmed_body.append(stmt)
            if isinstance(stmt, (ast.Return, ast.Raise)):
                unreachable_count = len(node.body) - len(trimmed_body)
                break
        if unreachable_count > 0:
            node.body = trimmed_body
            self.issues.append(
                LintIssue(
                    code="W0101_UNREACHABLE_CODE",
                    severity="error",
                    message=f"Removed {unreachable_count} unreachable statement(s) after `return`/`raise`.",
                    line=getattr(trimmed_body[-1], "lineno", None),
                    fixable=True,
                    fix_applied=f"Pruned {unreachable_count} unreachable statement(s).",
                )
            )

        has_return_or_yield = any(
            isinstance(sub, (ast.Return, ast.Yield, ast.YieldFrom))
            for sub in ast.walk(node)
        )
        if (
            not has_return_or_yield
            and node.body
            and isinstance(node.body[-1], ast.Expr)
            and not isinstance(node.body[-1].value, ast.Constant)
        ):
            expr_stmt = node.body[-1]
            node.body[-1] = ast.Return(value=expr_stmt.value)
            self.issues.append(
                LintIssue(
                    code="W0104_IMPLICIT_RETURN",
                    severity="error",
                    message=f"Trailing expression in `{node.name}` has no effect without `return`.",
                    line=getattr(expr_stmt, "lineno", None),
                    fixable=True,
                    fix_applied="Converted trailing expression into explicit `return` statement.",
                )
            )

        loaded_names = {
            sub.id
            for sub in ast.walk(node)
            if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load)
        }
        cleaned_body: list[ast.stmt] = []
        for stmt in node.body:
            if (
                isinstance(stmt, ast.Assign)
                and len(stmt.targets) == 1
                and isinstance(stmt.targets[0], ast.Name)
                and isinstance(stmt.value, ast.Constant)
            ):
                var_name = stmt.targets[0].id
                if var_name not in loaded_names and not var_name.startswith("_"):
                    self.issues.append(
                        LintIssue(
                            code="F841_UNUSED_VARIABLE",
                            severity="error",
                            message=f"Local variable `{var_name}` is assigned to a constant but never used.",
                            line=getattr(stmt, "lineno", None),
                            fixable=True,
                            fix_applied=f"Removed dead constant assignment to `{var_name}`.",
                        )
                    )
                    continue
            cleaned_body.append(stmt)
        if cleaned_body:
            node.body = cleaned_body

        return node

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> ast.ExceptHandler:
        self.generic_visit(node)
        if node.type is None:
            node.type = ast.Name(id="Exception", ctx=ast.Load())
            self.issues.append(
                LintIssue(
                    code="E722_BARE_EXCEPT",
                    severity="error",
                    message="Bare `except:` catches BaseException/SystemExit.",
                    line=getattr(node, "lineno", None),
                    fixable=True,
                    fix_applied="Upgraded bare `except:` to `except Exception:`.",
                )
            )
        return node

    def visit_Compare(self, node: ast.Compare) -> ast.Compare:
        self.generic_visit(node)
        new_ops: list[ast.cmpop] = []
        changed = False
        for op, comp in zip(node.ops, node.comparators):
            if isinstance(comp, ast.Constant) and comp.value in (None, True, False):
                if isinstance(op, ast.Eq):
                    new_ops.append(ast.Is())
                    changed = True
                    continue
                if isinstance(op, ast.NotEq):
                    new_ops.append(ast.IsNot())
                    changed = True
                    continue
            new_ops.append(op)
        if changed:
            node.ops = new_ops
            self.issues.append(
                LintIssue(
                    code="E711_SINGLETON_COMPARE",
                    severity="error",
                    message="Comparison to `None`/`True`/`False` should use `is` / `is not`.",
                    line=getattr(node, "lineno", None),
                    fixable=True,
                    fix_applied="Replaced `==`/`!=` singleton comparison with `is`/`is not`.",
                )
            )
        return node

    def visit_Dict(self, node: ast.Dict) -> ast.Dict:
        self.generic_visit(node)
        seen_keys: dict[Any, int] = {}
        new_keys: list[ast.expr | None] = []
        new_vals: list[ast.expr] = []
        dup_found = False
        for k, v in zip(node.keys, node.values):
            if isinstance(k, ast.Constant) and k.value in seen_keys:
                prev_idx = seen_keys[k.value]
                new_keys[prev_idx] = k
                new_vals[prev_idx] = v
                dup_found = True
            else:
                if isinstance(k, ast.Constant):
                    seen_keys[k.value] = len(new_keys)
                new_keys.append(k)
                new_vals.append(v)
        if dup_found:
            node.keys = new_keys
            node.values = new_vals
            self.issues.append(
                LintIssue(
                    code="F601_DUPLICATE_DICT_KEY",
                    severity="error",
                    message="Dictionary literal contains duplicate constant keys.",
                    line=getattr(node, "lineno", None),
                    fixable=True,
                    fix_applied="Deduplicated dictionary keys keeping the final override.",
                )
            )
        return node


def _repair_common_syntax_issues(source: str, issues: list[LintIssue]) -> str:
    fixed = source
    if "\t" in fixed:
        fixed = fixed.expandtabs(4)
        issues.append(
            LintIssue(
                code="W191_TAB_INDENTATION",
                severity="error",
                message="Tab indentation detected.",
                fixable=True,
                fix_applied="Expanded tabs to 4 spaces.",
            )
        )

    lines = fixed.splitlines()
    block_header_re = re.compile(
        r"^(\s*(?:def|class|if|elif|else|for|while|try|except|finally|with)\b[^:#\n]*?)(\s*(?:#.*)?)$"
    )
    new_lines: list[str] = []
    for lineno, line in enumerate(lines, start=1):
        stripped = line.strip()
        if (
            stripped
            and not stripped.endswith((":", "\\", "(", "[", "{", ","))
            and block_header_re.match(line)
        ):
            m = block_header_re.match(line)
            assert m is not None
            line = f"{m.group(1)}:{m.group(2)}"
            issues.append(
                LintIssue(
                    code="E999_MISSING_COLON",
                    severity="error",
                    message=f"Missing `:` at end of block header on line {lineno}.",
                    line=lineno,
                    fixable=True,
                    fix_applied=f"Appended missing `:` on line {lineno}.",
                )
            )
        cond_assign_re = re.compile(r"^(\s*(?:if|elif|while)\s+[^=:<>\n]+?)\s*=\s*([^=:\n]+:.*)$")
        cm = cond_assign_re.match(line)
        if cm:
            line = f"{cm.group(1)} == {cm.group(2)}"
            issues.append(
                LintIssue(
                    code="E999_ASSIGN_IN_CONDITION",
                    severity="error",
                    message=f"Assignment `=` used inside conditional test on line {lineno}.",
                    line=lineno,
                    fixable=True,
                    fix_applied=f"Replaced `=` with `==` in condition on line {lineno}.",
                )
            )
        new_lines.append(line)

    fixed = "\n".join(new_lines)

    stack: list[str] = []
    pairs = {"(": ")", "[": "]", "{": "}"}
    try:
        for tok in tokenize.generate_tokens(io.StringIO(fixed).readline):
            if tok.type == tokenize.OP and tok.string in pairs:
                stack.append(pairs[tok.string])
            elif tok.type == tokenize.OP and tok.string in pairs.values():
                if stack and stack[-1] == tok.string:
                    stack.pop()
    except tokenize.TokenError:
        pass

    if stack:
        closing = "".join(reversed(stack))
        fixed = fixed + closing
        issues.append(
            LintIssue(
                code="E999_UNCLOSED_DELIMITER",
                severity="error",
                message=f"Unclosed delimiter(s) at end of input; expected `{closing}`.",
                fixable=True,
                fix_applied=f"Appended `{closing}` to close open delimiter(s).",
            )
        )

    return fixed


def _check_undefined_local_names(
    fn_node: ast.FunctionDef,
    known_symbols: set[str],
    issues: list[LintIssue],
) -> None:
    bound_names: set[str] = set()
    for a in (*fn_node.args.posonlyargs, *fn_node.args.args, *fn_node.args.kwonlyargs):
        bound_names.add(a.arg)
    if fn_node.args.vararg:
        bound_names.add(fn_node.args.vararg.arg)
    if fn_node.args.kwarg:
        bound_names.add(fn_node.args.kwarg.arg)

    called_func_names = _find_called_names(fn_node)

    for sub in ast.walk(fn_node):
        if isinstance(sub, ast.Name) and isinstance(sub.ctx, (ast.Store, ast.Param)):
            bound_names.add(sub.id)
        elif isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound_names.add(sub.name)
            if not isinstance(sub, ast.ClassDef):
                for a in (*sub.args.posonlyargs, *sub.args.args, *sub.args.kwonlyargs):
                    bound_names.add(a.arg)
        elif isinstance(sub, (ast.Import, ast.ImportFrom)):
            for alias in sub.names:
                bound_names.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(sub, ast.ExceptHandler) and sub.name:
            bound_names.add(sub.name)
        elif isinstance(sub, ast.arg):
            bound_names.add(sub.arg)

    for sub in ast.walk(fn_node):
        if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load):
            ident = sub.id
            if (
                ident not in bound_names
                and ident not in known_symbols
                and ident not in called_func_names
            ):
                issues.append(
                    LintIssue(
                        code="F821_UNDEFINED_NAME",
                        severity="error",
                        message=f"Undefined variable `{ident}` referenced in `{fn_node.name}`.",
                        line=getattr(sub, "lineno", None),
                        col=getattr(sub, "col_offset", None),
                        fixable=False,
                    )
                )


def lint_and_autofix_input(
    name: str,
    payload: str | dict[str, Any],
    known_symbols: set[str],
) -> LintResult:
    issues: list[LintIssue] = []
    fn_node: ast.FunctionDef | None = None
    original_source = ""
    was_module_wrapped = False

    if isinstance(payload, dict):
        node = _dict_to_ast(payload)
        if isinstance(node, ast.Module):
            fn_node = _wrap_module_script_as_function(name, node, issues)
            was_module_wrapped = True
            original_source = ast.unparse(node)
        elif isinstance(node, ast.FunctionDef):
            fn_node = ast.fix_missing_locations(node)
            original_source = ast.unparse(fn_node)
        else:
            issues.append(
                LintIssue(
                    code="E900_NOT_FUNCTION_DEF",
                    severity="error",
                    message=f"Expected FunctionDef or Module AST, got {type(node).__name__}.",
                    fixable=False,
                )
            )
            return LintResult(valid=False, modified=False, issues=issues, original_source=json.dumps(payload))
    else:
        if not isinstance(payload, str) or not payload.strip():
            issues.append(
                LintIssue(
                    code="E900_EMPTY_INPUT",
                    severity="error",
                    message="Input payload must be non-empty Python code or AST.",
                    fixable=False,
                )
            )
            return LintResult(valid=False, modified=False, issues=issues, original_source=str(payload))

        stripped = payload.strip()
        original_source = stripped

        if stripped.startswith("{"):
            try:
                return lint_and_autofix_input(name, json.loads(stripped), known_symbols)
            except Exception as exc:
                issues.append(
                    LintIssue(
                        code="E900_INVALID_JSON_AST",
                        severity="error",
                        message=f"Malformed JSON AST: {exc}",
                        fixable=False,
                    )
                )
                return LintResult(valid=False, modified=False, issues=issues, original_source=original_source)

        if stripped.startswith(("FunctionDef(", "Module(")):
            try:
                expr_ast = ast.parse(stripped, mode="eval")
                node = eval(compile(expr_ast, filename="<serialized-ast>", mode="eval"), _AST_EVAL_NAMESPACE)
                if isinstance(node, ast.FunctionDef):
                    fn_node = ast.fix_missing_locations(node)
                    original_source = ast.unparse(fn_node)
                elif isinstance(node, ast.Module):
                    fn_node = _wrap_module_script_as_function(name, node, issues)
                    was_module_wrapped = True
                    original_source = ast.unparse(node)
            except Exception as exc:
                issues.append(
                    LintIssue(
                        code="E900_INVALID_AST_DUMP",
                        severity="error",
                        message=f"Failed to deserialize `ast.dump()` string: {exc}",
                        fixable=False,
                    )
                )
                return LintResult(valid=False, modified=False, issues=issues, original_source=original_source)

        if fn_node is None:
            candidate_source = stripped
            try:
                mod = ast.parse(candidate_source, mode="exec")
            except SyntaxError:
                candidate_source = _repair_common_syntax_issues(stripped, issues)
                try:
                    mod = ast.parse(candidate_source, mode="exec")
                except SyntaxError as exc:
                    issues.append(
                        LintIssue(
                            code="E999_SYNTAX_ERROR",
                            severity="error",
                            message=f"SyntaxError: {exc.msg}",
                            line=exc.lineno,
                            col=exc.offset,
                            fixable=False,
                        )
                    )
                    return LintResult(
                        valid=False,
                        modified=False,
                        issues=issues,
                        original_source=original_source,
                    )

            if not mod.body:
                issues.append(
                    LintIssue(
                        code="E900_EMPTY_INPUT",
                        severity="error",
                        message="Python script contains no executable statements.",
                        fixable=False,
                    )
                )
                return LintResult(
                    valid=False,
                    modified=False,
                    issues=issues,
                    original_source=original_source,
                )

            if len(mod.body) == 1 and isinstance(mod.body[0], ast.FunctionDef):
                fn_node = mod.body[0]
            else:
                fn_node = _wrap_module_script_as_function(name, mod, issues)
                was_module_wrapped = True

    assert fn_node is not None
    before_dump = ast.dump(fn_node)

    transformer = _ASTAutoFixTransformer(issues)
    fixed_node = transformer.visit(fn_node)
    assert isinstance(fixed_node, ast.FunctionDef)
    fixed_node.name = name
    ast.fix_missing_locations(fixed_node)

    _check_undefined_local_names(fixed_node, known_symbols, issues)

    after_dump = ast.dump(fixed_node)
    has_errors = any(i.severity == "error" for i in issues)
    has_unfixable = any(not i.fixable for i in issues)
    modified = was_module_wrapped or (before_dump != after_dump) or any(i.fixable for i in issues)

    if not has_errors:
        return LintResult(
            valid=True,
            modified=modified,
            issues=issues,
            original_source=original_source,
            fixed_source=ast.unparse(fixed_node),
            fixed_serialized_ast=after_dump,
        )

    return LintResult(
        valid=False,
        modified=modified and not has_unfixable,
        issues=issues,
        original_source=original_source,
        fixed_source=ast.unparse(fixed_node) if (modified and not has_unfixable) else None,
        fixed_serialized_ast=after_dump if (modified and not has_unfixable) else None,
    )


def normalize_to_serialized_ast(payload: str | dict[str, Any], fallback_name: str = "module_entry") -> str:
    if isinstance(payload, dict):
        node = _dict_to_ast(payload)
        if isinstance(node, ast.Module):
            wrapped = _wrap_module_script_as_function(fallback_name, node, [])
            return serialize_function(ast.fix_missing_locations(wrapped))
        if not isinstance(node, ast.FunctionDef):
            raise ValueError("Dict AST must represent an ast.FunctionDef or ast.Module")
        return serialize_function(ast.fix_missing_locations(node))
    stripped = payload.strip()
    if stripped.startswith("{"):
        return normalize_to_serialized_ast(json.loads(stripped), fallback_name=fallback_name)
    try:
        mod = ast.parse(stripped, mode="exec")
        if len(mod.body) == 1 and isinstance(mod.body[0], ast.FunctionDef):
            return serialize_function(mod.body[0])
        if mod.body:
            wrapped = _wrap_module_script_as_function(fallback_name, mod, [])
            return serialize_function(ast.fix_missing_locations(wrapped))
    except SyntaxError:
        pass
    expr_ast = ast.parse(stripped, mode="eval")
    node = eval(compile(expr_ast, filename="<serialized-ast>", mode="eval"), _AST_EVAL_NAMESPACE)
    if isinstance(node, ast.Module):
        wrapped = _wrap_module_script_as_function(fallback_name, node, [])
        return serialize_function(ast.fix_missing_locations(wrapped))
    if not isinstance(node, ast.FunctionDef):
        raise ValueError("Input did not resolve to a valid FunctionDef")
    return serialize_function(ast.fix_missing_locations(node))


def parse_expression_or_ast(expr_or_ast: str) -> ast.Expression:
    stripped = expr_or_ast.strip()
    if stripped.startswith("{"):
        node = _dict_to_ast(json.loads(stripped))
    else:
        parsed = ast.parse(stripped, mode="eval")
        if (
            isinstance(parsed.body, ast.Call)
            and isinstance(parsed.body.func, ast.Name)
            and hasattr(ast, parsed.body.func.id)
        ):
            node = eval(compile(parsed, filename="<eval-ast-dump>", mode="eval"), _AST_EVAL_NAMESPACE)
        else:
            return ast.fix_missing_locations(parsed)

    if isinstance(node, ast.Expression):
        return ast.fix_missing_locations(node)
    if isinstance(node, ast.expr):
        return ast.fix_missing_locations(ast.Expression(body=node))
    raise ValueError(f"Expected an expression AST node, got {type(node).__name__}")


def compile_function_from_ast(
    name: str,
    serialized_ast: str,
    namespace: dict[str, Any],
) -> tuple[Any, dict[str, Any]]:
    node = deserialize_ast_node(serialized_ast)
    node.name = name
    module = ast.Module(body=[node], type_ignores=[])
    ast.fix_missing_locations(module)

    code_obj = compile(module, filename=f"<ast:{name}>", mode="exec")
    exec(code_obj, namespace)
    fn = namespace[name]
    fn_code = fn.__code__

    compile_info = {
        "name": name,
        "signature": str(inspect.signature(fn)),
        "argcount": fn_code.co_argcount,
        "varnames": list(fn_code.co_varnames),
        "consts": [repr(c) for c in fn_code.co_consts],
        "bytecode_bytes": len(fn_code.co_code),
    }
    return fn, compile_info


# ---------------------------------------------------------------------------
# Bidirectional Structure Converter (AST / JSON / Schema / Pydantic / Dataclass)
# ---------------------------------------------------------------------------
def _py_type_from_str(type_str: str) -> Any:
    mapping: dict[str, Any] = {
        "int": int,
        "float": float,
        "str": str,
        "bool": bool,
        "dict": dict,
        "list": list,
        "Any": Any,
        "None": type(None),
    }
    return mapping.get(type_str, Any)


def _type_name_from_annotation(ann: Any) -> str:
    if ann is inspect.Parameter.empty or ann is None:
        return "Any"
    if isinstance(ann, str):
        return ann
    if ann is type(None):
        return "None"
    if hasattr(ann, "__name__"):
        return ann.__name__
    return str(ann).replace("typing.", "")


def _infer_type_str_from_value(val: Any) -> str:
    if val is None:
        return "Optional[Any]"
    if isinstance(val, bool):
        return "bool"
    if isinstance(val, int):
        return "int"
    if isinstance(val, float):
        return "float"
    if isinstance(val, str):
        return "str"
    if isinstance(val, list):
        if not val:
            return "list[Any]"
        elem_types = sorted({_infer_type_str_from_value(x) for x in val})
        inner = elem_types[0] if len(elem_types) == 1 else f"Union[{', '.join(elem_types)}]"
        return f"list[{inner}]"
    if isinstance(val, dict):
        return "dict[str, Any]"
    return "Any"


def _schema_prop_to_type_str(prop: dict[str, Any], required: bool = True) -> str:
    t = prop.get("type")
    if isinstance(t, list):
        non_null = [x for x in t if x != "null"]
        has_null = "null" in t
        base = _schema_prop_to_type_str(
            {**prop, "type": non_null[0] if len(non_null) == 1 else "Any"},
            required=True,
        )
        return f"Optional[{base}]" if (has_null or not required) else base

    type_map = {
        "string": "str",
        "integer": "int",
        "number": "float",
        "boolean": "bool",
        "object": "dict[str, Any]",
    }
    if t == "array":
        items = prop.get("items")
        inner = _schema_prop_to_type_str(items, required=True) if isinstance(items, dict) else "Any"
        base = f"list[{inner}]"
    else:
        base = type_map.get(str(t), "Any")

    return base if required else f"Optional[{base}]"


def _parse_any_ast_input(payload: str | dict[str, Any] | ast.AST) -> ast.AST:
    if isinstance(payload, ast.AST):
        return ast.fix_missing_locations(payload)
    if isinstance(payload, dict):
        return ast.fix_missing_locations(_dict_to_ast(payload))
    stripped = payload.strip()
    if stripped.startswith("{"):
        return ast.fix_missing_locations(_dict_to_ast(json.loads(stripped)))
    if stripped.startswith(("FunctionDef(", "ClassDef(", "Module(")):
        expr = ast.parse(stripped, mode="eval")
        node = eval(compile(expr, "<ast-dump>", "eval"), _AST_EVAL_NAMESPACE)
        return ast.fix_missing_locations(node)
    mod = ast.parse(stripped, mode="exec")
    return ast.fix_missing_locations(mod.body[0] if len(mod.body) == 1 else mod)


def extract_field_specs(
    source_kind: str,
    payload: Any,
    model_name: str = "GeneratedModel",
) -> tuple[str, list[dict[str, Any]]]:
    fields: list[dict[str, Any]] = []

    if source_kind in ("ast", "pydantic", "dataclass"):
        if isinstance(payload, str):
            node = _parse_any_ast_input(payload)
            if isinstance(node, ast.Module) and node.body:
                target_def = next(
                    (stmt for stmt in node.body if isinstance(stmt, (ast.ClassDef, ast.FunctionDef))),
                    node.body[0],
                )
                node = target_def

            if isinstance(node, ast.FunctionDef):
                resolved_name = (
                    model_name
                    if model_name != "GeneratedModel"
                    else f"{node.name.title().replace('_', '')}Input"
                )
                pos_args = list(node.args.posonlyargs) + list(node.args.args)
                defaults = list(node.args.defaults)
                default_offset = len(pos_args) - len(defaults)

                for idx, arg in enumerate(pos_args):
                    type_str = ast.unparse(arg.annotation) if arg.annotation else "Any"
                    has_def = idx >= default_offset
                    def_val = None
                    if has_def:
                        try:
                            def_val = ast.literal_eval(defaults[idx - default_offset])
                        except Exception:
                            def_val = None
                    fields.append(
                        {
                            "name": arg.arg,
                            "type_str": type_str,
                            "required": not has_def,
                            "has_default": has_def,
                            "default": def_val,
                        }
                    )
                return resolved_name, fields

            if isinstance(node, ast.ClassDef):
                resolved_name = node.name if model_name == "GeneratedModel" else model_name
                for stmt in node.body:
                    if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                        type_str = ast.unparse(stmt.annotation)
                        has_def = stmt.value is not None
                        def_val = None
                        if has_def:
                            try:
                                def_val = ast.literal_eval(stmt.value)
                            except Exception:
                                def_val = None
                        fields.append(
                            {
                                "name": stmt.target.id,
                                "type_str": type_str,
                                "required": not has_def,
                                "has_default": has_def,
                                "default": def_val,
                            }
                        )
                return resolved_name, fields

            raise ValueError(f"Unsupported AST node `{type(node).__name__}` for model extraction.")

        if HAS_PYDANTIC and BaseModel is not None and isinstance(payload, type) and issubclass(payload, BaseModel):
            for fname, finfo in payload.model_fields.items():
                is_req = finfo.is_required()
                fields.append(
                    {
                        "name": fname,
                        "type_str": _type_name_from_annotation(finfo.annotation),
                        "required": is_req,
                        "has_default": not is_req,
                        "default": None if is_req else finfo.default,
                    }
                )
            return payload.__name__, fields

        if is_dataclass(payload):
            cls_obj = payload if isinstance(payload, type) else type(payload)
            from dataclasses import MISSING, fields as dc_fields

            for f in dc_fields(cls_obj):
                has_def = f.default is not MISSING or f.default_factory is not MISSING
                def_val = f.default if f.default is not MISSING else None
                fields.append(
                    {
                        "name": f.name,
                        "type_str": _type_name_from_annotation(f.type),
                        "required": not has_def,
                        "has_default": has_def,
                        "default": def_val,
                    }
                )
            return cls_obj.__name__, fields

    if source_kind == "json":
        obj = json.loads(payload) if isinstance(payload, str) else payload
        if not isinstance(obj, dict):
            raise ValueError("JSON input must be a JSON object (`{...}`) to extract fields.")
        for k, v in obj.items():
            fields.append(
                {
                    "name": k,
                    "type_str": _infer_type_str_from_value(v),
                    "required": True,
                    "has_default": True,
                    "default": v,
                }
            )
        return model_name, fields

    if source_kind == "schema":
        schema = json.loads(payload) if isinstance(payload, str) else payload
        if not isinstance(schema, dict):
            raise ValueError("Schema input must be a JSON Schema object.")
        resolved_name = schema.get("title") or model_name
        props = schema.get("properties", {})
        req_set = set(schema.get("required", []))
        for k, prop in props.items():
            prop_dict = prop if isinstance(prop, dict) else {}
            is_req = k in req_set
            has_def = "default" in prop_dict or not is_req
            def_val = prop_dict.get("default", None)
            fields.append(
                {
                    "name": k,
                    "type_str": _schema_prop_to_type_str(prop_dict, required=is_req),
                    "required": is_req,
                    "has_default": has_def,
                    "default": def_val,
                }
            )
        return resolved_name, fields

    raise ValueError(f"Unsupported `source_kind`: {source_kind}")


def build_pydantic_code_and_ast(model_name: str, fields: list[dict[str, Any]]) -> tuple[str, ast.ClassDef]:
    body: list[ast.stmt] = []
    for f in fields:
        ann = ast.parse(f["type_str"], mode="eval").body
        val = ast.Constant(value=f["default"]) if f["has_default"] else None
        body.append(
            ast.AnnAssign(
                target=ast.Name(id=f["name"], ctx=ast.Store()),
                annotation=ann,
                value=val,
                simple=1,
            )
        )
    if not body:
        body.append(ast.Pass())

    cls_node = ast.ClassDef(
        name=model_name,
        bases=[ast.Name(id="BaseModel", ctx=ast.Load())],
        keywords=[],
        body=body,
        decorator_list=[],
    )
    ast.fix_missing_locations(cls_node)
    header = "from typing import Any, Optional, Union\nfrom pydantic import BaseModel\n\n\n"
    return header + ast.unparse(cls_node), cls_node


def build_dataclass_code_and_ast(model_name: str, fields: list[dict[str, Any]]) -> tuple[str, ast.ClassDef]:
    ordered = [f for f in fields if not f["has_default"]] + [f for f in fields if f["has_default"]]
    body: list[ast.stmt] = []
    for f in ordered:
        ann = ast.parse(f["type_str"], mode="eval").body
        val = ast.Constant(value=f["default"]) if f["has_default"] else None
        body.append(
            ast.AnnAssign(
                target=ast.Name(id=f["name"], ctx=ast.Store()),
                annotation=ann,
                value=val,
                simple=1,
            )
        )
    if not body:
        body.append(ast.Pass())

    cls_node = ast.ClassDef(
        name=model_name,
        bases=[],
        keywords=[],
        body=body,
        decorator_list=[ast.Name(id="dataclass", ctx=ast.Load())],
    )
    ast.fix_missing_locations(cls_node)
    header = "from dataclasses import dataclass\nfrom typing import Any, Optional, Union\n\n\n"
    return header + ast.unparse(cls_node), cls_node


def build_function_ast_from_fields(fn_name: str, fields: list[dict[str, Any]]) -> tuple[str, ast.FunctionDef]:
    req_fields = [f for f in fields if not f["has_default"]]
    opt_fields = [f for f in fields if f["has_default"]]
    ordered = req_fields + opt_fields

    args = [
        ast.arg(arg=f["name"], annotation=ast.parse(f["type_str"], mode="eval").body)
        for f in ordered
    ]
    defaults = [ast.Constant(value=f["default"]) for f in opt_fields]

    dict_keys = [ast.Constant(value=f["name"]) for f in ordered]
    dict_vals = [ast.Name(id=f["name"], ctx=ast.Load()) for f in ordered]

    fn_node = ast.FunctionDef(
        name=fn_name,
        args=ast.arguments(
            posonlyargs=[],
            args=args,
            vararg=None,
            kwonlyargs=[],
            kw_defaults=[],
            kwarg=None,
            defaults=defaults,
        ),
        body=[ast.Return(value=ast.Dict(keys=dict_keys, values=dict_vals))],
        decorator_list=[],
        returns=ast.parse("dict[str, Any]", mode="eval").body,
    )
    ast.fix_missing_locations(fn_node)
    return ast.unparse(fn_node), fn_node


def convert_structure(
    source_kind: str,
    target_kind: str,
    payload: Any,
    model_name: str = "GeneratedModel",
    saved_registry: dict[str, str] | None = None,
) -> dict[str, Any]:
    if source_kind == "saved_ast":
        if not saved_registry or str(payload) not in saved_registry:
            raise KeyError(f"Saved AST `{payload}` not found in registry.")
        actual_source = "ast"
        actual_payload = saved_registry[str(payload)]
        if model_name == "GeneratedModel":
            model_name = f"{str(payload).title().replace('_', '')}Model"
    else:
        actual_source = source_kind
        actual_payload = payload

    resolved_name, fields = extract_field_specs(actual_source, actual_payload, model_name=model_name)

    if target_kind == "pydantic":
        code, cls_ast = build_pydantic_code_and_ast(resolved_name, fields)
        live_json_schema = None
        if HAS_PYDANTIC and create_model is not None:
            field_defs: dict[str, Any] = {}
            for f in fields:
                py_t = _py_type_from_str(f["type_str"])
                field_defs[f["name"]] = (py_t, f["default"] if f["has_default"] else ...)
            dyn_model = create_model(resolved_name, **field_defs)
            live_json_schema = dyn_model.model_json_schema()

        return {
            "source_kind": source_kind,
            "target_kind": "pydantic",
            "model_name": resolved_name,
            "fields": fields,
            "python_source": code,
            "serialized_ast": ast.dump(cls_ast),
            "pretty_ast": ast.dump(cls_ast, indent=2),
            "json_schema": live_json_schema,
        }

    if target_kind == "dataclass":
        code, cls_ast = build_dataclass_code_and_ast(resolved_name, fields)
        ordered = [f for f in fields if not f["has_default"]] + [f for f in fields if f["has_default"]]
        dc_spec = [
            (
                f["name"],
                _py_type_from_str(f["type_str"]),
                field(default=f["default"]),
            )
            if f["has_default"]
            else (f["name"], _py_type_from_str(f["type_str"]))
            for f in ordered
        ]
        make_dataclass(resolved_name, dc_spec)

        return {
            "source_kind": source_kind,
            "target_kind": "dataclass",
            "model_name": resolved_name,
            "fields": fields,
            "python_source": code,
            "serialized_ast": ast.dump(cls_ast),
            "pretty_ast": ast.dump(cls_ast, indent=2),
        }

    if target_kind in ("ast", "function_ast"):
        fn_name = resolved_name.lower()
        code, fn_ast = build_function_ast_from_fields(fn_name, fields)
        return {
            "source_kind": source_kind,
            "target_kind": "ast",
            "function_name": fn_name,
            "fields": fields,
            "python_source": code,
            "serialized_ast": ast.dump(fn_ast),
            "pretty_ast": ast.dump(fn_ast, indent=2),
        }

    if target_kind == "json_schema":
        properties: dict[str, Any] = {}
        required: list[str] = []
        reverse_map = {"str": "string", "int": "integer", "float": "number", "bool": "boolean"}
        for f in fields:
            raw_t = f["type_str"].removeprefix("Optional[").removesuffix("]")
            prop: dict[str, Any] = {"type": reverse_map.get(raw_t, "string")}
            if f["has_default"] and f["default"] is not None:
                prop["default"] = f["default"]
            properties[f["name"]] = prop
            if f["required"]:
                required.append(f["name"])

        return {
            "source_kind": source_kind,
            "target_kind": "json_schema",
            "model_name": resolved_name,
            "json_schema": {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "title": resolved_name,
                "type": "object",
                "properties": properties,
                "required": required,
            },
        }

    raise ValueError(f"Unsupported `target_kind`: {target_kind}")


# ---------------------------------------------------------------------------
# HATEOAS hal+json Policy Builders (MCP + HTTP REST Dual Links)
# ---------------------------------------------------------------------------
class MissingASTCallError(Exception):
    def __init__(self, missing_name: str, caller_context: str):
        super().__init__(f"Missing AST function '{missing_name}' referenced by '{caller_context}'")
        self.missing_name = missing_name
        self.caller_context = caller_context


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
            deps = sorted(n for n in _find_called_names(ast_node) if n in registry and n != node_name)
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


# ---------------------------------------------------------------------------
# Dynamic AST Program with Live Event Broadcasting & Colorized CLI Output
# ---------------------------------------------------------------------------
class DynamicASTMcpProgram:
    def __init__(
        self,
        mcp_server: FastMCP,
        initial_registry: dict[str, str] | None = None,
        auto_expose_keys: list[str] | None = None,
        stdout_is_mcp_stdio: bool = False,
    ) -> None:
        self.mcp = mcp_server
        self.registry: dict[str, str] = {}
        self.runtime_ns: dict[str, Any] = {"__builtins__": __builtins__}
        self._compiled: dict[str, dict[str, Any]] = {}
        self._exposed_tools: set[str] = set()
        self.stdout_is_mcp_stdio = stdout_is_mcp_stdio

        self._lock = threading.RLock()
        self._sse_subscribers: list[queue.Queue[str]] = []
        self.event_log: list[dict[str, Any]] = []

        for k, raw_ast in (initial_registry or {}).items():
            rt = verify_and_roundtrip_ast(k, raw_ast)
            self.registry[k] = rt["canonical_serialized_ast"]

        for key in auto_expose_keys or []:
            self._compile_needed(key)
            self._expose_on_mcp(key, self.runtime_ns[key])

    def _console_write(self, line: str) -> None:
        stream = sys.stderr if self.stdout_is_mcp_stdio else sys.stdout
        stream.write(line + "\n")
        stream.flush()

    def record_and_broadcast(
        self,
        source: str,
        tool: str,
        arguments: Any,
        result: dict[str, Any],
    ) -> None:
        route_label, route_color = _ROUTE_META.get(tool, ("CUSTOM_FN", "green"))
        rest_enriched_result = enrich_rest_hal(
            result,
            request_href=f"/api/command/{tool}",
            request_method="POST",
        )
        entry = {
            "id": len(self.event_log) + 1,
            "timestamp": time.strftime("%H:%M:%S"),
            "source": source,
            "route": route_label,
            "tool": tool,
            "arguments": arguments,
            "status": rest_enriched_result.get("status", "ok"),
            "result": rest_enriched_result,
        }
        with self._lock:
            self.event_log.append(entry)
            if len(self.event_log) > 100:
                self.event_log = self.event_log[-100:]
            dead: list[queue.Queue[str]] = []
            payload = json.dumps(
                {
                    "type": "state_update",
                    "event": entry,
                    "snapshot": self.get_dashboard_snapshot(),
                }
            )
            for q in self._sse_subscribers:
                try:
                    q.put_nowait(payload)
                except Exception:
                    dead.append(q)
            for q in dead:
                self._sse_subscribers.remove(q)

        src_key = source.lower()
        src_color = _SOURCE_COLORS.get(src_key, "white")
        status_str = str(entry["status"])
        status_color = "red" if status_str == "error" else "green"

        ts_part = _colorize(f"[{entry['timestamp']}]", "dim")
        src_part = _colorize(f"[SRC:{source.upper()}]", src_color)
        route_part = _colorize(f"[ROUTE:{route_label}]", route_color)
        call_part = _colorize(f"{tool}({json.dumps(arguments)})", route_color)
        status_part = _colorize(f"status={status_str}", status_color)
        summary_val = result.get("result", result.get("message", result.get("status")))
        val_part = _colorize(str(summary_val), status_color)

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
                fmt = export_all_formats(k, self.registry[k])
                ast_node = deserialize_ast_node(self.registry[k])
                deps = sorted(n for n in _find_called_names(ast_node) if n in self.registry and n != k)
                items.append(
                    {
                        "name": k,
                        "compiled": k in self._compiled,
                        "exposed_as_tool": k in self._exposed_tools,
                        "dependencies": deps,
                        "compile_metadata": self._compiled.get(k),
                        "formats": fmt["formats"],
                        "_links": self._resource_links(k),
                    }
                )
            return {
                "status": "ok",
                "count": len(items),
                "items": items,
                "events": list(self.event_log[-40:]),
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

        for dep_name in sorted(_find_called_names(node)):
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
        with self._lock:
            failed_params = {
                "name": name,
                "code_or_ast": code_or_ast,
                "description": description or "",
                "expose_as_tool": expose_as_tool,
                "overwrite": overwrite,
                "skip_roundtrip": skip_roundtrip,
            }
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
            self.registry[name] = serialized
            self._compiled.pop(name, None)

            try:
                fn, chain = self._compile_needed(name)
            except CircularDependencyError as exc:
                res = build_circular_dependency_hal_recovery(
                    exc=exc,
                    registry=self.registry,
                    failed_tool="ast_create",
                )
                if prev_entry is None:
                    self.registry.pop(name, None)
                else:
                    self.registry[name] = prev_entry
                self.record_and_broadcast(source, "ast_create", failed_params, res)
                return res
            except MissingModuleDependencyError as exc:
                if prev_entry is None:
                    self.registry.pop(name, None)
                else:
                    self.registry[name] = prev_entry
                res = build_missing_module_hal_recovery(
                    module_name=exc.module_name,
                    failed_tool="ast_create",
                    failed_params=failed_params,
                    caller_context=exc.caller_context,
                    imported_symbols=exc.imported_symbols,
                )
                self.record_and_broadcast(source, "ast_create", failed_params, res)
                return res
            except MissingASTCallError as exc:
                if prev_entry is None:
                    self.registry.pop(name, None)
                else:
                    self.registry[name] = prev_entry
                res = build_missing_call_hal_recovery(
                    missing_name=exc.missing_name,
                    failed_tool="ast_create",
                    failed_params=failed_params,
                    reason=str(exc),
                )
                self.record_and_broadcast(source, "ast_create", failed_params, res)
                return res

            notified = False
            if expose_as_tool:
                self._expose_on_mcp(name, fn, description)
                notified = await self._notify_tools_changed(ctx)

            res_data: dict[str, Any] = {
                "status": "created",
                "name": name,
                "roundtrip_checked": not skip_roundtrip,
                "roundtrip_verified_stages": roundtrip_stages,
                "compilation_order": chain,
                "exposed_as_tool": name in self._exposed_tools,
                "tool_list_changed_notified": notified,
                "compile_metadata": self._compiled[name],
            }
            if lint_warnings:
                res_data["lint_warnings"] = lint_warnings

            res = build_hal_response(
                self_href=f"ast://resource/{name}",
                data=res_data,
                links=self._resource_links(name),
                embedded={"resource": export_all_formats(name, self.registry[name])},
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
                embedded={"resource": export_all_formats(name, self.registry[name])},
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
            prev_meta = self._compiled.get(name)

            self.registry[name] = serialized
            self._compiled.pop(name, None)

            try:
                fn, chain = self._compile_needed(name)
            except CircularDependencyError as exc:
                res = build_circular_dependency_hal_recovery(
                    exc=exc,
                    registry=self.registry,
                    failed_tool="ast_update",
                )
                self.registry[name] = prev_serialized
                if prev_meta is not None:
                    self._compiled[name] = prev_meta
                self.record_and_broadcast(source, "ast_update", failed_params, res)
                return res
            except MissingModuleDependencyError as exc:
                self.registry[name] = prev_serialized
                if prev_meta is not None:
                    self._compiled[name] = prev_meta
                res = build_missing_module_hal_recovery(
                    module_name=exc.module_name,
                    failed_tool="ast_update",
                    failed_params=failed_params,
                    caller_context=exc.caller_context,
                    imported_symbols=exc.imported_symbols,
                )
                self.record_and_broadcast(source, "ast_update", failed_params, res)
                return res
            except MissingASTCallError as exc:
                self.registry[name] = prev_serialized
                if prev_meta is not None:
                    self._compiled[name] = prev_meta
                res = build_missing_call_hal_recovery(
                    missing_name=exc.missing_name,
                    failed_tool="ast_update",
                    failed_params=failed_params,
                    reason=str(exc),
                )
                self.record_and_broadcast(source, "ast_update", failed_params, res)
                return res

            try:
                for other_key in list(self._compiled.keys()):
                    if other_key != name:
                        self._compiled.pop(other_key, None)
                        self._compile_needed(other_key)
                        if other_key in self._exposed_tools:
                            self._expose_on_mcp(other_key, self.runtime_ns[other_key])
            except CircularDependencyError as exc:
                res = build_circular_dependency_hal_recovery(
                    exc=exc,
                    registry=self.registry,
                    failed_tool="ast_update",
                )
                self.registry[name] = prev_serialized
                if prev_meta is not None:
                    self._compiled[name] = prev_meta
                self.record_and_broadcast(source, "ast_update", failed_params, res)
                return res

            notified = False
            if expose_as_tool:
                self._expose_on_mcp(name, fn, description)
                notified = await self._notify_tools_changed(ctx)

            res_data: dict[str, Any] = {
                "status": "updated",
                "name": name,
                "roundtrip_checked": not skip_roundtrip,
                "roundtrip_verified_stages": roundtrip_stages,
                "compilation_order": chain,
                "exposed_as_tool": name in self._exposed_tools,
                "tool_list_changed_notified": notified,
                "compile_metadata": self._compiled[name],
            }
            if lint_warnings:
                res_data["lint_warnings"] = lint_warnings

            res = build_hal_response(
                self_href=f"ast://resource/{name}",
                data=res_data,
                links=self._resource_links(name),
                embedded={"resource": export_all_formats(name, self.registry[name])},
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
            self._compiled.pop(name, None)
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
            norm_args, norm_kwargs = normalize_eval_call_args(args, kwargs)
            failed_params = {"target": target, "args": norm_args, "kwargs": norm_kwargs}

            if target in self.registry:
                try:
                    fn, chain = self._compile_needed(target)
                except CircularDependencyError as exc:
                    res = build_circular_dependency_hal_recovery(
                        exc=exc,
                        registry=self.registry,
                        failed_tool="eval_ast_call",
                    )
                    self.record_and_broadcast(source, "eval_ast_call", failed_params, res)
                    return res
                except MissingModuleDependencyError as exc:
                    res = build_missing_module_hal_recovery(
                        module_name=exc.module_name,
                        failed_tool="eval_ast_call",
                        failed_params=failed_params,
                        caller_context=exc.caller_context,
                        imported_symbols=exc.imported_symbols,
                    )
                    self.record_and_broadcast(source, "eval_ast_call", failed_params, res)
                    return res
                except MissingASTCallError as exc:
                    res = build_missing_call_hal_recovery(
                        missing_name=exc.missing_name,
                        failed_tool="eval_ast_call",
                        failed_params=failed_params,
                        reason=str(exc),
                    )
                    self.record_and_broadcast(source, "eval_ast_call", failed_params, res)
                    return res

                try:
                    result = fn(*norm_args, **norm_kwargs)
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
                            call_args=norm_args,
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

                res = build_hal_response(
                    self_href=f"ast://resource/{target}/eval",
                    data={
                        "status": "evaluated_function",
                        "target": target,
                        "compilation_order": chain,
                        "args": norm_args,
                        "kwargs": norm_kwargs,
                        "result": result,
                    },
                    links=self._resource_links(target),
                    embedded={"resource": export_all_formats(target, self.registry[target])},
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

            for called_name in sorted(_find_called_names(expr_node)):
                if called_name in self.registry:
                    try:
                        self._compile_needed(called_name)
                    except CircularDependencyError as exc:
                        res = build_circular_dependency_hal_recovery(
                            exc=exc,
                            registry=self.registry,
                            failed_tool="eval_ast_call",
                        )
                        self.record_and_broadcast(source, "eval_ast_call", failed_params, res)
                        return res
                    except MissingModuleDependencyError as exc:
                        res = build_missing_module_hal_recovery(
                            module_name=exc.module_name,
                            failed_tool="eval_ast_call",
                            failed_params=failed_params,
                            caller_context=exc.caller_context,
                            imported_symbols=exc.imported_symbols,
                        )
                        self.record_and_broadcast(source, "eval_ast_call", failed_params, res)
                        return res
                    except MissingASTCallError as exc:
                        res = build_missing_call_hal_recovery(
                            missing_name=exc.missing_name,
                            failed_tool="eval_ast_call",
                            failed_params=failed_params,
                            reason=str(exc),
                        )
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

            code_obj = compile(expr_node, filename="<ast:eval>", mode="eval")
            result = eval(code_obj, self.runtime_ns)
            res = build_hal_response(
                self_href="mcp://tool/eval_ast_call",
                data={
                    "status": "evaluated_expression",
                    "expression_source": ast.unparse(expr_node),
                    "expression_serialized_ast": ast.dump(expr_node),
                    "result": result,
                },
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
        with self._lock:
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

            pkg = package_name or resolve_package_name(clean_mod)
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

            proc = subprocess.run(
                [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", pkg],
                capture_output=True,
                text=True,
                timeout=120.0,
            )
            importlib.invalidate_caches()

            installed_ok = proc.returncode == 0 and (
                is_module_available(clean_mod)
                or importlib.util.find_spec(pkg.replace("-", "_")) is not None
            )

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
                    "pip_stderr": (proc.stderr or proc.stdout or "").strip()[-600:],
                    "recommendation": (
                        f"Package `{pkg}` could not be installed via pip. Create local module "
                        f"`{stub['filename']}` exporting {stub['exports'] or ['__getattr__']} "
                        f"by following `_links.create_local_module_file`."
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

            target_path = os.path.join(os.getcwd(), safe_name)
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

            ast.parse(code, mode="exec")
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
            tool_name = cmd.get("tool") or cmd.get("action")
            raw_args = cmd.get("arguments") or cmd.get("args") or {}
            resolved_args = self._resolve_chain_refs(raw_args, completed)

            if tool_name in ("ast_create", "create"):
                out = await self.create(**resolved_args, ctx=ctx, source=source)
            elif tool_name in ("ast_read", "read"):
                out = self.read(**resolved_args, source=source)
            elif tool_name in ("ast_update", "update"):
                out = await self.update(**resolved_args, ctx=ctx, source=source)
            elif tool_name in ("ast_delete", "delete"):
                out = await self.delete(**resolved_args, ctx=ctx, source=source)
            elif tool_name in ("eval_ast_call", "eval"):
                if isinstance(resolved_args, dict) and "target" in resolved_args:
                    out = self.eval_target(**resolved_args, source=source)
                else:
                    out = self.eval_target(target=str(resolved_args), source=source)
            elif tool_name in ("ast_convert", "convert"):
                out = self.convert(**resolved_args, source=source)
            elif tool_name == "ast_install_module":
                out = self.install_module(**resolved_args, source=source)
            elif tool_name == "ast_write_module_file":
                out = self.write_local_module_file(**resolved_args, source=source)
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
                elif err_type in ("lint_errors_autofixed", "signature_mismatch_autofixed"):
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


# ---------------------------------------------------------------------------
# Light-Themed Live HTML/HTTP Dashboard + HATEOAS REST API (`/api`) + SSE Stream
# ---------------------------------------------------------------------------
DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Dynamic AST HATEOAS Live Console</title>
  <style>
    :root {
      --bg: #f1f5f9;
      --panel: #ffffff;
      --panel-alt: #f8fafc;
      --border: #cbd5e1;
      --border-soft: #e2e8f0;
      --text: #0f172a;
      --muted: #475569;
      --accent: #0284c7;
      --accent-hover: #0369a1;
      --accent-soft: #e0f2fe;
      --green: #16a34a;
      --green-soft: #dcfce7;
      --amber: #d97706;
      --amber-soft: #fef3c7;
      --red: #dc2626;
      --red-soft: #fee2e2;
      --mono: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
      background: var(--bg);
      color: var(--text);
      line-height: 1.45;
    }
    header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding: 14px 24px;
      background: var(--panel);
      border-bottom: 1px solid var(--border);
      box-shadow: 0 1px 3px rgba(15, 23, 42, 0.05);
      flex-wrap: wrap;
      gap: 10px;
    }
    header h1 { margin: 0; font-size: 1.15rem; letter-spacing: 0.01em; color: #0f172a; }
    .badge {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 4px 10px;
      border-radius: 999px;
      font-size: 0.78rem;
      font-weight: 600;
      background: var(--green-soft);
      color: #15803d;
      border: 1px solid #86efac;
    }
    .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--green); }
    main {
      display: grid;
      grid-template-columns: 1.15fr 1fr;
      gap: 18px;
      padding: 18px 24px;
      max-width: 1600px;
      margin: 0 auto;
    }
    @media (max-width: 1050px) { main { grid-template-columns: 1fr; } }
    .card {
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 16px;
      margin-bottom: 18px;
      box-shadow: 0 1px 3px rgba(15, 23, 42, 0.04);
    }
    .card h2 {
      margin: 0 0 12px 0;
      font-size: 0.94rem;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      color: var(--accent);
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
      border-bottom: 1px solid var(--border-soft);
      padding-bottom: 8px;
    }
    label { display: block; font-size: 0.8rem; font-weight: 600; color: var(--muted); margin-bottom: 4px; }
    input, select, textarea {
      width: 100%;
      background: #ffffff;
      color: var(--text);
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 8px 10px;
      font-family: var(--mono);
      font-size: 0.84rem;
      margin-bottom: 10px;
      transition: border-color 0.15s, box-shadow 0.15s;
    }
    input:focus, select:focus, textarea:focus {
      outline: none;
      border-color: var(--accent);
      box-shadow: 0 0 0 3px rgba(2, 132, 199, 0.14);
    }
    textarea { min-height: 110px; resize: vertical; }
    .row { display: flex; gap: 10px; }
    .row > * { flex: 1; }
    button {
      background: var(--accent);
      color: #ffffff;
      border: 1px solid var(--accent);
      border-radius: 6px;
      padding: 8px 14px;
      font-weight: 600;
      font-size: 0.84rem;
      cursor: pointer;
      transition: background 0.15s, border-color 0.15s, opacity 0.15s;
    }
    button:hover:not(:disabled) { background: var(--accent-hover); border-color: var(--accent-hover); }
    button:disabled { opacity: 0.45; cursor: not-allowed; }
    button.secondary {
      background: #ffffff;
      color: var(--text);
      border: 1px solid var(--border);
    }
    button.secondary:hover:not(:disabled) { border-color: var(--accent); color: var(--accent); background: #f8fafc; }
    button.warn { background: var(--amber); border-color: var(--amber); color: #ffffff; }
    button.warn:hover:not(:disabled) { background: #b45309; border-color: #b45309; }
    button.success { background: var(--green); border-color: var(--green); color: #ffffff; }
    button.success:hover:not(:disabled) { background: #15803d; border-color: #15803d; }
    button.danger { background: var(--red); border-color: var(--red); color: #ffffff; }
    button.danger:hover:not(:disabled) { background: #b91c1c; border-color: #b91c1c; }
    .fn-item {
      border: 1px solid var(--border);
      background: var(--panel-alt);
      border-radius: 8px;
      padding: 12px;
      margin-bottom: 10px;
    }
    .fn-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 8px;
      gap: 8px;
      flex-wrap: wrap;
    }
    .fn-title { font-family: var(--mono); font-weight: 700; color: #0f172a; font-size: 0.95rem; }
    .pill {
      font-size: 0.72rem;
      font-weight: 600;
      padding: 2px 8px;
      border-radius: 999px;
      background: var(--accent-soft);
      color: #0369a1;
      border: 1px solid #bae6fd;
      margin-left: 6px;
    }
    .tabs { display: flex; gap: 6px; margin-bottom: 6px; flex-wrap: wrap; }
    .tab-btn {
      padding: 3px 8px;
      font-size: 0.74rem;
      background: #ffffff;
      color: var(--muted);
      border: 1px solid var(--border);
    }
    .tab-btn.active { background: var(--accent); color: #ffffff; border-color: var(--accent); }
    pre {
      margin: 0;
      background: #f8fafc;
      color: #0f172a;
      border: 1px solid var(--border-soft);
      border-radius: 6px;
      padding: 10px;
      font-family: var(--mono);
      font-size: 0.78rem;
      overflow-x: auto;
      max-height: 280px;
      white-space: pre-wrap;
      word-break: break-word;
    }
    .result-banner {
      background: var(--green-soft);
      border: 1px solid #86efac;
      border-radius: 6px;
      padding: 8px 12px;
      margin-bottom: 10px;
      font-family: var(--mono);
      font-size: 0.84rem;
      font-weight: 600;
      color: #166534;
    }
    .result-banner.error {
      background: var(--red-soft);
      border-color: #fca5a5;
      color: #991b1b;
    }
    .event-entry {
      border-left: 4px solid var(--accent);
      background: var(--panel-alt);
      border-top: 1px solid var(--border-soft);
      border-right: 1px solid var(--border-soft);
      border-bottom: 1px solid var(--border-soft);
      padding: 8px 10px;
      margin-bottom: 8px;
      border-radius: 0 6px 6px 0;
      font-size: 0.8rem;
      cursor: pointer;
    }
    .event-entry:hover { background: #f1f5f9; border-color: var(--accent); }
    .event-entry.error { border-left-color: var(--red); }
    .event-entry.stdin { border-left-color: var(--amber); }
    .event-entry.mcp { border-left-color: #9333ea; }
    .event-meta {
      display: flex;
      justify-content: space-between;
      color: var(--muted);
      font-family: var(--mono);
      font-size: 0.74rem;
      margin-bottom: 4px;
    }

    /* ---------------- HATEOAS & Circularity Modals (Light Theme) ---------------- */
    .modal-backdrop {
      position: fixed;
      inset: 0;
      background: rgba(15, 23, 42, 0.52);
      backdrop-filter: blur(5px);
      display: none;
      align-items: center;
      justify-content: center;
      z-index: 9999;
      padding: 20px;
    }
    .modal-backdrop.open { display: flex; }
    .modal-card {
      width: 100%;
      max-width: 800px;
      max-height: 92vh;
      overflow-y: auto;
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 12px;
      box-shadow: 0 25px 55px rgba(15, 23, 42, 0.25);
      padding: 20px 22px;
    }
    .modal-card.wide-modal {
      max-width: 1180px;
      border: 2px solid var(--red);
    }
    .modal-header {
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 12px;
      border-bottom: 1px solid var(--border-soft);
      padding-bottom: 12px;
      margin-bottom: 14px;
    }
    .modal-title {
      margin: 0;
      font-size: 1.08rem;
      color: #0f172a;
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
    }
    .modal-sub {
      margin: 4px 0 0 0;
      font-size: 0.82rem;
      color: var(--muted);
    }
    .superseded-banner {
      display: none;
      background: var(--amber-soft);
      border: 1px solid #fcd34d;
      color: #92400e;
      border-radius: 8px;
      padding: 8px 12px;
      font-size: 0.8rem;
      margin-bottom: 12px;
    }
    .track-selector {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      margin-bottom: 14px;
    }
    .step-list {
      display: flex;
      flex-direction: column;
      gap: 10px;
      margin-bottom: 16px;
    }
    .step-card {
      background: var(--panel-alt);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 12px;
      transition: border-color 0.2s, background 0.2s;
    }
    .step-card.active {
      border-color: var(--accent);
      box-shadow: 0 0 0 3px rgba(2, 132, 199, 0.15);
      background: #ffffff;
    }
    .step-card.done {
      border-color: #86efac;
      background: #f0fdf4;
    }
    .step-card.failed {
      border-color: #fca5a5;
      background: #fef2f2;
    }
    .step-card.running {
      border-color: var(--amber);
    }
    .step-top {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 8px;
      margin-bottom: 6px;
      flex-wrap: wrap;
    }
    .step-name {
      font-family: var(--mono);
      font-weight: 700;
      font-size: 0.88rem;
      color: #0f172a;
    }
    .step-status-pill {
      font-family: var(--mono);
      font-size: 0.72rem;
      padding: 2px 8px;
      border-radius: 999px;
      font-weight: 700;
      text-transform: uppercase;
    }
    .st-pending { background: #e2e8f0; color: var(--muted); }
    .st-running { background: var(--amber-soft); color: #b45309; }
    .st-done { background: var(--green-soft); color: #15803d; }
    .st-failed { background: var(--red-soft); color: #b91c1c; }
    .modal-footer {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 10px;
      border-top: 1px solid var(--border-soft);
      padding-top: 14px;
      flex-wrap: wrap;
    }

    /* Side-by-Side Circularity Modal */
    .cycle-chain-strip {
      display: flex;
      align-items: center;
      flex-wrap: wrap;
      gap: 8px;
      padding: 10px 14px;
      border-radius: 8px;
      background: var(--red-soft);
      border: 1px solid #fca5a5;
      color: #991b1b;
      font-family: var(--mono);
      font-size: 0.84rem;
      font-weight: 700;
      margin-bottom: 12px;
    }
    .cycle-node-chip {
      background: #ffffff;
      border: 1px solid #fca5a5;
      padding: 2px 10px;
      border-radius: 999px;
    }
    .side-by-side-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 16px;
      margin-bottom: 14px;
    }
    @media (max-width: 860px) {
      .side-by-side-grid { grid-template-columns: 1fr; }
    }
    .split-pane {
      background: var(--panel-alt);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 12px;
      display: flex;
      flex-direction: column;
      gap: 8px;
    }
    .split-pane-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 8px;
      border-bottom: 1px solid var(--border-soft);
      padding-bottom: 6px;
    }
    .split-pane textarea {
      min-height: 195px;
      margin-bottom: 0;
    }
    .lock-badge {
      background: var(--red);
      color: #ffffff;
      font-size: 0.72rem;
      font-weight: 700;
      padding: 4px 10px;
      border-radius: 999px;
      text-transform: uppercase;
      letter-spacing: 0.04em;
    }

    /* Autocomplete & Suggestions Menu */
    .ac-menu {
      position: fixed;
      z-index: 10050;
      display: none;
      min-width: 260px;
      max-width: 520px;
      max-height: 260px;
      overflow-y: auto;
      background: var(--panel);
      border: 1px solid var(--accent);
      border-radius: 8px;
      box-shadow: 0 12px 28px rgba(15, 23, 42, 0.18);
      padding: 4px;
      font-family: var(--mono);
      font-size: 0.78rem;
    }
    .ac-menu.open { display: block; }
    .ac-item {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 10px;
      padding: 6px 10px;
      border-radius: 6px;
      cursor: pointer;
      color: var(--text);
    }
    .ac-item:hover, .ac-item.selected {
      background: var(--accent-soft);
      color: #0369a1;
    }
    .ac-label { font-weight: 600; white-space: pre; overflow: hidden; text-overflow: ellipsis; }
    .ac-meta {
      font-size: 0.7rem;
      color: var(--muted);
      background: var(--panel-alt);
      border: 1px solid var(--border-soft);
      padding: 1px 6px;
      border-radius: 999px;
      flex-shrink: 0;
    }
  </style>
</head>
<body>
  <header>
    <h1>Dynamic AST &amp; HATEOAS Live Console <span style="color:var(--muted);font-weight:400;">(REST /api &harr; MCP &harr; STDIN Synced)</span></h1>
    <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;">
      <button id="reopenCycleBtn" class="danger" style="display:none;padding:4px 10px;font-size:0.78rem;" onclick="openCircularityModal()">Resolve Circular AST</button>
      <button id="reopenWizardBtn" class="warn" style="display:none;padding:4px 10px;font-size:0.78rem;" onclick="openHateoasModal()">Open HATEOAS Action Modal</button>
      <a href="/api" target="_blank" style="color:var(--accent);font-family:var(--mono);font-size:0.82rem;font-weight:600;text-decoration:none;">GET /api (hal+json)</a>
      <div class="badge"><span class="dot"></span><span id="connStatus">Connected (SSE Live)</span></div>
    </div>
  </header>

  <main>
    <div>
      <div class="card">
        <h2>Command Runner <span style="font-size:0.75rem;color:var(--muted);text-transform:none;">Posts to REST /api &amp; reflects to STDOUT</span></h2>
        <div class="row">
          <div>
            <label for="toolSelect">Operation</label>
            <select id="toolSelect" onchange="updateFormFields()">
              <option value="eval_ast_call">eval_ast_call (POST /api/ast/{name}/eval)</option>
              <option value="ast_create">ast_create (POST /api/ast — single fn or full Python script)</option>
              <option value="ast_update">ast_update (PUT /api/ast/{name})</option>
              <option value="ast_read">ast_read (GET /api/ast)</option>
              <option value="ast_delete">ast_delete (DELETE /api/ast/{name})</option>
              <option value="ast_chain">ast_chain (POST /api/chain)</option>
              <option value="ast_convert">ast_convert (POST /api/convert)</option>
            </select>
          </div>
          <div id="nameWrap">
            <label id="nameLabel" for="nameInput">Target / Function Name</label>
            <input id="nameInput" value="entrypoint" autocomplete="off" placeholder="e.g. entrypoint or ast3" />
          </div>
        </div>

        <div id="convertWrap" style="display:none;" class="row">
          <div>
            <label for="sourceKindSelect">Source Kind</label>
            <select id="sourceKindSelect">
              <option value="saved_ast">saved_ast (Registry Key)</option>
              <option value="ast">ast (Code / Dump / JSON AST)</option>
              <option value="json">json (Sample JSON Object)</option>
              <option value="schema">schema (JSON Schema)</option>
              <option value="pydantic">pydantic (BaseModel Class)</option>
              <option value="dataclass">dataclass (@dataclass Class)</option>
            </select>
          </div>
          <div>
            <label for="targetKindSelect">Target Kind</label>
            <select id="targetKindSelect">
              <option value="pydantic">pydantic (BaseModel)</option>
              <option value="dataclass">dataclass (@dataclass)</option>
              <option value="ast">ast (FunctionDef AST)</option>
              <option value="json_schema">json_schema (JSON Schema)</option>
            </select>
          </div>
        </div>

        <div id="argsWrap">
          <label for="argsInput">Arguments (Literal `3`, List `[3, 4]`, List+Object `[3, {"k": 1}]`, or Object `{"x": 3}`)</label>
          <input id="argsInput" value="[3]" autocomplete="off" placeholder='e.g. 3 or [3, 4] or [3, {"k": 1}] or {"x": 3}' />
        </div>

        <div id="codeWrap" style="display:none;">
          <label id="codeLabel" for="codeInput">Full-Page Python Script, Single Function, Serialized `ast.dump()`, or JSON AST</label>
          <textarea id="codeInput" placeholder="import math&#10;&#10;class Helper:&#10;    def calc(self, x: int) -> float:&#10;        return math.sqrt(x)&#10;&#10;def fn(x: int = 9) -> float:&#10;    return Helper().calc(x)"></textarea>
          <label id="skipRoundtripWrap" for="skipRoundtripInput" style="display:none;align-items:center;gap:6px;cursor:pointer;margin-bottom:10px;">
            <input type="checkbox" id="skipRoundtripInput" style="width:auto;margin:0;" />
            Bypass 4-stage AST roundtrip verification (<code>skip_roundtrip</code>)
          </label>
        </div>

        <div style="display:flex;gap:8px;flex-wrap:wrap;">
          <button onclick="runCommand()">Execute Command</button>
          <button class="secondary" onclick="quickEval('entrypoint', [3])">Quick Eval entrypoint(3)</button>
          <button class="secondary" onclick="followRestLink('GET', '/api')">Discover GET /api</button>
          <button class="secondary" onclick="refreshSnapshot()">Refresh State</button>
        </div>
      </div>

      <div class="card">
        <h2>Latest Execution &amp; Conversion Output <span id="recoverySlot" style="display:flex;gap:6px;flex-wrap:wrap;"></span></h2>
        <div id="resultSummary" class="result-banner">Ready. Run an execution/conversion or call REST /api.</div>
        <div id="convertedCodeWrap" style="display:none;margin-bottom:10px;">
          <label for="convertedCodePreview">Generated / Converted Artifact</label>
          <pre id="convertedCodePreview"></pre>
        </div>
        <label for="lastResponse">Full HATEOAS `application/hal+json` Payload</label>
        <pre id="lastResponse">{}</pre>
      </div>

      <div class="card">
        <h2>Live Synced Executions &amp; Conversions <span style="font-size:0.75rem;color:var(--muted);text-transform:none;">Click any entry to inspect</span></h2>
        <div id="eventList"></div>
      </div>
    </div>

    <div>
      <div class="card">
        <h2>Live AST Registry (<span id="fnCount">0</span>)</h2>
        <div id="registryList"></div>
      </div>
    </div>
  </main>

  <!-- Non-Dismissible Side-by-Side Circular AST Dependency Resolution Modal -->
  <div id="circularityModalBackdrop" class="modal-backdrop" role="dialog" aria-modal="true" aria-labelledby="circularityModalTitle">
    <div class="modal-card wide-modal">
      <div class="modal-header">
        <div>
          <h3 id="circularityModalTitle" class="modal-title">Circular AST Dependency Detected — Side-by-Side Resolver</h3>
          <p id="circularityModalSub" class="modal-sub">Break the mutual call cycle between these AST functions. This modal remains locked until circularity is resolved.</p>
        </div>
        <span id="cycleLockBadge" class="lock-badge">Locked Until Resolved</span>
      </div>

      <div id="cycleChainStrip" class="cycle-chain-strip"></div>
      <div id="cycleStatusBanner" class="result-banner error">Remove the recursive/mutual call in at least one function below, then click Save &amp; Verify.</div>

      <div id="cycleSelectorRow" style="display:none;" class="row">
        <div>
          <label for="cycleLeftSelect">Left Editor Function</label>
          <select id="cycleLeftSelect" onchange="syncCycleEditorsFromSelect()"></select>
        </div>
        <div>
          <label for="cycleRightSelect">Right Editor Function</label>
          <select id="cycleRightSelect" onchange="syncCycleEditorsFromSelect()"></select>
        </div>
      </div>

      <div class="side-by-side-grid">
        <div class="split-pane">
          <div class="split-pane-header">
            <span id="leftNodeTitle" class="fn-title">ast1</span>
            <span id="leftNodeDeps" class="pill">calls: []</span>
          </div>
          <label for="cycleLeftCode">Left Function Python Source</label>
          <textarea id="cycleLeftCode" placeholder="def ast1(x: int) -> int:&#10;    return x * 2 + 1"></textarea>
        </div>

        <div class="split-pane">
          <div class="split-pane-header">
            <span id="rightNodeTitle" class="fn-title">ast2</span>
            <span id="rightNodeDeps" class="pill">calls: []</span>
          </div>
          <label for="cycleRightCode">Right Function Python Source</label>
          <textarea id="cycleRightCode" placeholder="def ast2(y: int) -> int:&#10;    return ast1(y) ** 2"></textarea>
        </div>
      </div>

      <div class="modal-footer">
        <div style="font-size:0.78rem;color:var(--muted);">
          Tip: Keep one direction (e.g. <code>ast2 &rarr; ast1</code>) and remove the back-edge call from <code>ast1</code>.
        </div>
        <div style="display:flex;gap:8px;flex-wrap:wrap;">
          <button id="cycleSaveVerifyBtn" class="success" onclick="saveAndVerifyCircularity()">Save Both &amp; Verify Cycle Resolved</button>
        </div>
      </div>
    </div>
  </div>

  <!-- HATEOAS Suggested Actions Step-by-Step Modal -->
  <div id="hateoasModalBackdrop" class="modal-backdrop" role="dialog" aria-modal="true" aria-labelledby="hateoasModalTitle">
    <div class="modal-card">
      <div class="modal-header">
        <div>
          <h3 id="hateoasModalTitle" class="modal-title">HATEOAS Suggested Action Plan</h3>
          <p id="hateoasModalSub" class="modal-sub">Execute suggested hypermedia transitions step-by-step.</p>
        </div>
        <span id="modalProgressBadge" class="pill">Step 0 / 0</span>
      </div>

      <div id="modalSupersededBanner" class="superseded-banner">
        New HATEOAS hypermedia state received — the previous command chain was outdated and has been replaced with the latest suggested actions.
      </div>

      <div id="modalTrackSelector" class="track-selector"></div>
      <div id="modalOverallStatus" class="result-banner">Awaiting step execution.</div>
      <div id="modalStepList" class="step-list"></div>

      <div class="modal-footer">
        <div style="display:flex;gap:8px;flex-wrap:wrap;">
          <button id="modalNextStepBtn" onclick="executeNextModalStep()">Execute Next Step</button>
          <button id="modalRunAllBtn" class="secondary" onclick="executeAllRemainingModalSteps()">Run Remaining Steps</button>
        </div>
        <button id="modalExitBtn" class="secondary" onclick="closeHateoasModal()">Exit</button>
      </div>
    </div>
  </div>

  <div id="acPopup" class="ac-menu" role="listbox" aria-label="Autocomplete suggestions"></div>

  <script>
    const activeFormatTab = {};
    let lastSeenEventId = 0;
    let latestSnapshot = { items: [], events: [] };

    const cycleState = {
      active: false,
      cyclePath: [],
      nodes: [],
      leftName: '',
      rightName: '',
      isSaving: false,
    };

    const wizardState = {
      signature: null,
      policy: '',
      message: '',
      tracks: [],
      selectedTrackIndex: 0,
      steps: [],
      isRunningStep: false,
      supersededCount: 0,
      completedAll: false,
    };

    wizardState.missingDefaultParams = [];

    function updateStepFieldFromEditor(idx, field, rawVal) {
      const step = wizardState.steps[idx];
      if (!step) return;
      if (field === 'href') {
        step.href = rawVal.trim();
      } else if (field === 'body') {
        step.rawBodyText = rawVal;
        try {
          step.body = rawVal.trim() === '' ? undefined : JSON.parse(rawVal);
          step.parseError = null;
        } catch (err) {
          step.parseError = `Invalid JSON: ${err.message}`;
        }
      }
    }

    function applyDefaultInputToStepCode(paramName, rawDefaultVal) {
      const step0 = wizardState.steps[0];
      if (!step0 || !step0.body || typeof step0.body.code_or_ast !== 'string') return;

      const trimmed = rawDefaultVal.trim();
      const pyVal = trimmed === '' ? 'None' : trimmed;
      let code = step0.body.code_or_ast;

      // Update existing `param: Type = old` or `param = old` or add `= pyVal` to `param: Type` / `param`
      const escaped = paramName.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
      const sigRegex = new RegExp(`(\\b${escaped}\\b(?:\\s*:\\s*[^=,\\)]+)?)(\\s*=\\s*[^,\\)]+)?`);
      if (sigRegex.test(code)) {
        code = code.replace(sigRegex, `$1 = ${pyVal}`);
      }

      step0.body.code_or_ast = code;
      step0.rawBodyText = JSON.stringify(step0.body, null, 2);
      const ta = document.getElementById('modalStepBody_0');
      if (ta) ta.value = step0.rawBodyText;
    }

    const ACTIONABLE_RELS = [
      ['set_default_and_retry', 'Set Default & Retry'],
      ['recovery_chain', 'Full Recovery Chain'],
      ['apply_patched_update', 'Apply Updated Signature / Patch'],
      ['install_module', 'Install Missing Module'],
      ['create_local_module_file', 'Create Local Module Script'],
      ['create_shell', 'Create Blank AST Shell'],
      ['bypass_and_create', 'Bypass Roundtrip & Create'],
      ['bypass_and_update', 'Bypass Roundtrip & Update'],
      ['register_converted_ast', 'Register Converted AST'],
      ['recreate', 'Recreate Deleted AST'],
      ['retry', 'Retry Original Request'],
    ];

    function hasUnresolvedTemplate(val) {
      if (!val) return false;
      const decoded = decodeURIComponent(String(val));
      return /\{[^}]+\}/.test(decoded);
    }

    function isValidPyIdentifier(val) {
      return typeof val === 'string' && /^[A-Za-z_][A-Za-z0-9_]*$/.test(val);
    }

    function detectCycleInRegistry(items) {
      const graph = {};
      const itemMap = {};
      for (const item of (items || [])) {
        itemMap[item.name] = item;
        const deps = (item.compile_metadata && item.compile_metadata.dependencies) || item.dependencies || [];
        graph[item.name] = Array.isArray(deps) ? deps : [];
      }

      const visited = new Set();
      const stack = [];

      function dfs(node) {
        const idx = stack.indexOf(node);
        if (idx !== -1) {
          return stack.slice(idx).concat([node]);
        }
        if (visited.has(node)) return null;
        visited.add(node);
        stack.push(node);
        for (const dep of (graph[node] || [])) {
          if (graph[dep]) {
            const found = dfs(dep);
            if (found) return found;
          }
        }
        stack.pop();
        return null;
      }

      for (const name of Object.keys(graph)) {
        const cycle = dfs(name);
        if (cycle) {
          const unique = [];
          const seen = new Set();
          for (const n of cycle) {
            if (!seen.has(n) && itemMap[n]) {
              seen.add(n);
              unique.push({
                name: n,
                python_source: (itemMap[n].formats && itemMap[n].formats.python_source) || '',
                dependencies: graph[n] || [],
              });
            }
          }
          return { cyclePath: cycle, nodes: unique };
        }
      }
      return null;
    }

    function parseCircularityFromError(data) {
      if (!data || data.status !== 'error') return null;
      const msg = String(data.message || '');
      const isCycleErr =
        data.error_type === 'circular_ast_dependency' ||
        data.error_type === 'CircularDependencyError' ||
        /circular ast dependency/i.test(msg);
      if (!isCycleErr) return null;

      if (Array.isArray(data.cycle_path) && Array.isArray(data.cycle_nodes) && data.cycle_nodes.length >= 1) {
        return { cyclePath: data.cycle_path, nodes: data.cycle_nodes };
      }

      const fromReg = detectCycleInRegistry(latestSnapshot.items || []);
      if (fromReg) return fromReg;

      const m = msg.match(/'([A-Za-z_][A-Za-z0-9_]*)'/);
      const key = m ? m[1] : null;
      const items = latestSnapshot.items || [];
      const primary = items.find(i => i.name === key) || items[0];
      const secondary = items.find(i => i.name !== (primary && primary.name)) || primary;
      if (!primary) return null;

      const nodes = [primary, secondary].filter(Boolean).map(it => ({
        name: it.name,
        python_source: (it.formats && it.formats.python_source) || '',
        dependencies: it.dependencies || (it.compile_metadata && it.compile_metadata.dependencies) || [],
      }));
      return {
        cyclePath: nodes.length > 1 ? [nodes[0].name, nodes[1].name, nodes[0].name] : [nodes[0].name, nodes[0].name],
        nodes,
      };
    }

    function openCircularityModal(cycleInfo) {
      if (cycleInfo) {
        cycleState.active = true;
        cycleState.cyclePath = cycleInfo.cyclePath;
        cycleState.nodes = cycleInfo.nodes;
        cycleState.leftName = cycleInfo.nodes[0].name;
        cycleState.rightName = (cycleInfo.nodes[1] || cycleInfo.nodes[0]).name;
      }
      if (!cycleState.active || !cycleState.nodes.length) return;

      document.getElementById('reopenCycleBtn').style.display = 'inline-block';

      const strip = document.getElementById('cycleChainStrip');
      strip.innerHTML = '<span>Cycle Path:</span> ' + cycleState.cyclePath
        .map(n => `<span class="cycle-node-chip">${escapeHtml(n)}</span>`)
        .join(' <span>&rarr;</span> ');

      const selRow = document.getElementById('cycleSelectorRow');
      const leftSel = document.getElementById('cycleLeftSelect');
      const rightSel = document.getElementById('cycleRightSelect');
      if (cycleState.nodes.length > 2) {
        selRow.style.display = 'flex';
        leftSel.innerHTML = cycleState.nodes.map(n => `<option value="${escapeHtml(n.name)}" ${n.name === cycleState.leftName ? 'selected' : ''}>${escapeHtml(n.name)}</option>`).join('');
        rightSel.innerHTML = cycleState.nodes.map(n => `<option value="${escapeHtml(n.name)}" ${n.name === cycleState.rightName ? 'selected' : ''}>${escapeHtml(n.name)}</option>`).join('');
      } else {
        selRow.style.display = 'none';
      }

      populateSideBySidePanes();
      document.getElementById('circularityModalBackdrop').classList.add('open');
    }

    function syncCycleEditorsFromSelect() {
      cycleState.leftName = document.getElementById('cycleLeftSelect').value;
      cycleState.rightName = document.getElementById('cycleRightSelect').value;
      populateSideBySidePanes();
    }

    function populateSideBySidePanes() {
      const leftNode = cycleState.nodes.find(n => n.name === cycleState.leftName) || cycleState.nodes[0];
      const rightNode = cycleState.nodes.find(n => n.name === cycleState.rightName) || cycleState.nodes[1] || leftNode;

      document.getElementById('leftNodeTitle').textContent = leftNode.name;
      document.getElementById('leftNodeDeps').textContent = 'calls: ' + JSON.stringify(leftNode.dependencies || []);
      document.getElementById('cycleLeftCode').value = leftNode.python_source || '';

      document.getElementById('rightNodeTitle').textContent = rightNode.name;
      document.getElementById('rightNodeDeps').textContent = 'calls: ' + JSON.stringify(rightNode.dependencies || []);
      document.getElementById('cycleRightCode').value = rightNode.python_source || '';
    }

    async function saveAndVerifyCircularity() {
      if (cycleState.isSaving) return;
      const leftName = cycleState.leftName;
      const rightName = cycleState.rightName;
      const leftCode = document.getElementById('cycleLeftCode').value;
      const rightCode = document.getElementById('cycleRightCode').value;
      const statusEl = document.getElementById('cycleStatusBanner');
      const saveBtn = document.getElementById('cycleSaveVerifyBtn');

      cycleState.isSaving = true;
      saveBtn.disabled = true;
      statusEl.className = 'result-banner';
      statusEl.textContent = 'Saving updated AST nodes and verifying dependency graph...';

      try {
        const resLeft = await fetch(`/api/ast/${encodeURIComponent(leftName)}`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json', 'Accept': 'application/hal+json' },
          body: JSON.stringify({ code_or_ast: leftCode })
        }).then(r => r.json());

        if (resLeft.status === 'error' && resLeft.error_type !== 'circular_ast_dependency') {
          statusEl.className = 'result-banner error';
          statusEl.textContent = `Left node (${leftName}) error: ${resLeft.message}`;
          return;
        }

        if (rightName && rightName !== leftName) {
          const resRight = await fetch(`/api/ast/${encodeURIComponent(rightName)}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json', 'Accept': 'application/hal+json' },
            body: JSON.stringify({ code_or_ast: rightCode })
          }).then(r => r.json());

          if (resRight.status === 'error' && resRight.error_type !== 'circular_ast_dependency') {
            statusEl.className = 'result-banner error';
            statusEl.textContent = `Right node (${rightName}) error: ${resRight.message}`;
            return;
          }

          const resLeftRetry = await fetch(`/api/ast/${encodeURIComponent(leftName)}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json', 'Accept': 'application/hal+json' },
            body: JSON.stringify({ code_or_ast: leftCode })
          }).then(r => r.json());

          const cycleCheck = parseCircularityFromError(resLeftRetry);
          if (cycleCheck) {
            statusEl.className = 'result-banner error';
            statusEl.textContent = `Circularity still present (${cycleCheck.cyclePath.join(' -> ')}). Remove the remaining mutual call to unlock.`;
            openCircularityModal(cycleCheck);
            return;
          }
        }

        const snap = await fetch('/api/snapshot').then(r => r.json());
        renderSnapshot(snap);

        const remainingCycle = detectCycleInRegistry(snap.items || []);
        if (remainingCycle) {
          statusEl.className = 'result-banner error';
          statusEl.textContent = `Circularity still present (${remainingCycle.cyclePath.join(' -> ')}). Remove the remaining mutual call to unlock.`;
          openCircularityModal(remainingCycle);
          return;
        }

        const readRes = await fetch('/api/ast', { headers: { 'Accept': 'application/hal+json' } }).then(r => r.json());
        cycleState.active = false;
        document.getElementById('reopenCycleBtn').style.display = 'none';
        document.getElementById('circularityModalBackdrop').classList.remove('open');
        renderResponse(readRes, false);
      } catch (err) {
        statusEl.className = 'result-banner error';
        statusEl.textContent = `Verification request failed: ${err.message}`;
      } finally {
        cycleState.isSaving = false;
        saveBtn.disabled = false;
      }
    }

    function updateFormFields() {
      const tool = document.getElementById('toolSelect').value;
      const nameWrap = document.getElementById('nameWrap');
      const argsWrap = document.getElementById('argsWrap');
      const codeWrap = document.getElementById('codeWrap');
      const convertWrap = document.getElementById('convertWrap');
      const skipRoundtripWrap = document.getElementById('skipRoundtripWrap');
      const nameLabel = document.getElementById('nameLabel');
      const codeLabel = document.getElementById('codeLabel');

      nameWrap.style.display = (tool === 'ast_chain') ? 'none' : 'block';
      argsWrap.style.display = (tool === 'eval_ast_call') ? 'block' : 'none';
      convertWrap.style.display = (tool === 'ast_convert') ? 'flex' : 'none';
      codeWrap.style.display = (tool === 'ast_create' || tool === 'ast_update' || tool === 'ast_chain' || tool === 'ast_convert') ? 'block' : 'none';
      skipRoundtripWrap.style.display = (tool === 'ast_create' || tool === 'ast_update') ? 'inline-flex' : 'none';

      if (tool === 'ast_convert') {
        nameLabel.textContent = 'Model Name (or Saved AST Key if payload empty)';
        codeLabel.textContent = 'Payload (Saved AST Key, JSON, Schema, Pydantic/Dataclass Code, or AST)';
      } else {
        nameLabel.textContent = (tool === 'eval_ast_call') ? 'Target Function or Expression' : 'Function Name (optional for ast_read)';
        codeLabel.textContent = 'Full-Page Python Script, Single Function, Serialized `ast.dump()`, or JSON AST';
      }

      if (tool === 'ast_chain' && !document.getElementById('codeInput').value.trim()) {
        document.getElementById('codeInput').value = JSON.stringify([
          { tool: "eval_ast_call", arguments: { target: "ast1", args: [4] } },
          { tool: "eval_ast_call", arguments: { target: "ast2", args: ["$prev"] } }
        ], null, 2);
      }
    }

    function toolStepToHttp(stepObj) {
      const tool = stepObj.tool || stepObj.action || '';
      const args = stepObj.arguments || stepObj.args || {};
      if (tool === 'ast_create') {
        return { method: 'POST', href: '/api/ast', body: args, tool, arguments: args };
      }
      if (tool === 'ast_update') {
        const nm = args.name || '';
        if (!isValidPyIdentifier(nm)) return null;
        return { method: 'PUT', href: `/api/ast/${encodeURIComponent(nm)}`, body: args, tool, arguments: args };
      }
      if (tool === 'ast_read') {
        const nm = args.name;
        if (nm && hasUnresolvedTemplate(nm)) return null;
        return { method: 'GET', href: nm ? `/api/ast/${encodeURIComponent(nm)}` : '/api/ast', body: undefined, tool, arguments: args };
      }
      if (tool === 'ast_delete') {
        const nm = args.name || '';
        if (!isValidPyIdentifier(nm)) return null;
        return { method: 'DELETE', href: `/api/ast/${encodeURIComponent(nm)}`, body: undefined, tool, arguments: args };
      }
      if (tool === 'eval_ast_call') {
        const target = args.target || '';
        if (isValidPyIdentifier(target)) {
          return {
            method: 'POST',
            href: `/api/ast/${encodeURIComponent(target)}/eval`,
            body: { args: args.args !== undefined ? args.args : [], kwargs: args.kwargs || {} },
            tool,
            arguments: args,
          };
        }
        if (!target || hasUnresolvedTemplate(target)) return null;
        return { method: 'POST', href: '/api/eval', body: args, tool, arguments: args };
      }
      if (tool === 'ast_convert') {
        return { method: 'POST', href: '/api/convert', body: args, tool, arguments: args };
      }
      if (tool === 'ast_install_module') {
        const mod = args.module_name || '';
        if (!isValidPyIdentifier(mod)) return null;
        return { method: 'POST', href: `/api/modules/${encodeURIComponent(mod)}/install`, body: args, tool, arguments: args };
      }
      if (tool === 'ast_write_module_file') {
        const fname = args.filename || '';
        if (!fname || hasUnresolvedTemplate(fname) || !/^[A-Za-z_][A-Za-z0-9_]*\.py$/.test(fname)) {
          return null;
        }
        return { method: 'POST', href: '/api/modules/files', body: args, tool, arguments: args };
      }
      if (tool === 'ast_chain') {
        return { method: 'POST', href: '/api/chain', body: args, tool, arguments: args };
      }
      return { method: 'POST', href: '/api/command', body: { tool, arguments: args }, tool, arguments: args };
    }

    function extractHateoasActionTracks(data) {
      if (!data || typeof data !== 'object' || !data._links) return [];
      const links = data._links;
      const tracks = [];

      for (const [rel, defaultLabel] of ACTIONABLE_RELS) {
        const link = links[rel];
        if (!link || typeof link !== 'object') continue;
        if (link.templated) continue;
        if (link.required_params && !link.arguments && !link.body) continue;
        if (link.href && hasUnresolvedTemplate(link.href)) continue;

        let steps = [];
        if (link.tool === 'ast_chain' && link.arguments && Array.isArray(link.arguments.commands)) {
          const mappedSteps = link.arguments.commands.map(cmd => toolStepToHttp(cmd));
          if (mappedSteps.some(m => m === null)) continue;
          steps = mappedSteps.map((mapped, idx) => ({
            index: idx,
            title: `${mapped.tool} (${mapped.method} ${mapped.href})`,
            method: mapped.method,
            href: mapped.href,
            body: mapped.body,
            tool: mapped.tool,
            arguments: mapped.arguments,
            status: 'pending',
            statusMessage: 'Pending execution',
            response: null,
          }));
        } else if (link.href && link.method) {
          if (link.body && typeof link.body === 'object') {
            if (link.body.filename && hasUnresolvedTemplate(link.body.filename)) continue;
            if (link.body.module_name && hasUnresolvedTemplate(link.body.module_name)) continue;
          }
          steps = [{
            index: 0,
            title: `${link.tool || rel} (${link.method} ${link.href})`,
            method: link.method,
            href: link.href,
            body: link.body,
            tool: link.tool || rel,
            arguments: link.arguments || link.body || {},
            status: 'pending',
            statusMessage: 'Pending execution',
            response: null,
          }];
        } else if (link.tool) {
          const mapped = toolStepToHttp(link);
          if (!mapped) continue;
          steps = [{
            index: 0,
            title: `${mapped.tool} (${mapped.method} ${mapped.href})`,
            method: mapped.method,
            href: mapped.href,
            body: mapped.body,
            tool: mapped.tool,
            arguments: mapped.arguments,
            status: 'pending',
            statusMessage: 'Pending execution',
            response: null,
          }];
        }

        if (steps.length > 0) {
          tracks.push({
            rel,
            label: defaultLabel,
            steps,
          });
        }
      }

      return tracks;
    }

    function computeTracksSignature(tracks) {
      return JSON.stringify(
        tracks.map(t => ({
          rel: t.rel,
          steps: t.steps.map(s => ({ method: s.method, href: s.href, body: s.body })),
        }))
      );
    }

    function syncWizardWithHateoas(data, shouldAutoPopup = true) {
      if (cycleState.active) return;

      const tracks = extractHateoasActionTracks(data);
      const reopenBtn = document.getElementById('reopenWizardBtn');

      if (!tracks.length) {
        reopenBtn.style.display = 'none';
        return;
      }

      reopenBtn.style.display = 'inline-block';
      const newSig = computeTracksSignature(tracks);
      const modalEl = document.getElementById('hateoasModalBackdrop');
      const wasOpen = modalEl.classList.contains('open');

      if (wizardState.signature !== newSig) {
        const hadPriorActivePlan =
          wizardState.signature !== null &&
          !wizardState.completedAll &&
          wizardState.steps.length > 0;

        wizardState.signature = newSig;
        wizardState.policy = data.policy || data.error_type || data.status || 'hateoas_transition';
        wizardState.message = data.message || 'Suggested HATEOAS actions are ready for step-by-step execution.';
        wizardState.missingDefaultParams = Array.isArray(data.missing_default_params) ? data.missing_default_params : [];
        wizardState.tracks = tracks;
        wizardState.selectedTrackIndex = 0;
        wizardState.steps = tracks[0].steps.map(s => ({ ...s }));
        wizardState.completedAll = false;
        wizardState.supersededCount = hadPriorActivePlan ? wizardState.supersededCount + 1 : 0;

        renderHateoasModal();
        if (shouldAutoPopup || wasOpen) {
          openHateoasModal();
        }
      } else if (wasOpen) {
        renderHateoasModal();
      }
    }

    function selectWizardTrack(idx) {
      if (wizardState.isRunningStep) return;
      if (idx < 0 || idx >= wizardState.tracks.length) return;
      wizardState.selectedTrackIndex = idx;
      wizardState.steps = wizardState.tracks[idx].steps.map(s => ({ ...s }));
      wizardState.completedAll = false;
      renderHateoasModal();
    }

    function openHateoasModal() {
      if (cycleState.active) return;
      renderHateoasModal();
      document.getElementById('hateoasModalBackdrop').classList.add('open');
    }

    function closeHateoasModal() {
      if (wizardState.isRunningStep) return;
      document.getElementById('hateoasModalBackdrop').classList.remove('open');
    }

    function renderHateoasModal() {
      const titleEl = document.getElementById('hateoasModalTitle');
      const subEl = document.getElementById('hateoasModalSub');
      const supBanner = document.getElementById('modalSupersededBanner');
      const trackSelEl = document.getElementById('modalTrackSelector');
      const overallEl = document.getElementById('modalOverallStatus');
      const stepListEl = document.getElementById('modalStepList');
      const badgeEl = document.getElementById('modalProgressBadge');
      const nextBtn = document.getElementById('modalNextStepBtn');
      const allBtn = document.getElementById('modalRunAllBtn');
      const exitBtn = document.getElementById('modalExitBtn');

      titleEl.textContent = `HATEOAS Suggested Actions (${wizardState.policy})`;
      subEl.textContent = wizardState.message;
      supBanner.style.display = wizardState.supersededCount > 0 ? 'block' : 'none';

      trackSelEl.innerHTML = '';
      if (wizardState.tracks.length > 1) {
        wizardState.tracks.forEach((tr, idx) => {
          const btn = document.createElement('button');
          btn.className = idx === wizardState.selectedTrackIndex ? '' : 'secondary';
          btn.style.padding = '5px 10px';
          btn.style.fontSize = '0.78rem';
          btn.disabled = wizardState.isRunningStep;
          btn.textContent = `${tr.label} (${tr.steps.length} step${tr.steps.length > 1 ? 's' : ''})`;
          btn.onclick = () => selectWizardTrack(idx);
          trackSelEl.appendChild(btn);
        });
      }

      const totalSteps = wizardState.steps.length;
      const doneSteps = wizardState.steps.filter(s => s.status === 'done').length;
      const failedStep = wizardState.steps.find(s => s.status === 'failed');
      const nextStepIdx = wizardState.steps.findIndex(s => s.status === 'pending' || s.status === 'failed');

      badgeEl.textContent = `Step ${doneSteps} / ${totalSteps}`;

      if (wizardState.isRunningStep) {
        overallEl.className = 'result-banner';
        overallEl.textContent = `Executing step ${nextStepIdx + 1} of ${totalSteps}...`;
      } else if (failedStep) {
        overallEl.className = 'result-banner error';
        overallEl.textContent = `Stopped at Step ${failedStep.index + 1}: ${failedStep.statusMessage}`;
      } else if (totalSteps > 0 && doneSteps === totalSteps) {
        overallEl.className = 'result-banner';
        overallEl.textContent = `All ${totalSteps} HATEOAS step(s) completed successfully. Click Exit to close.`;
      } else {
        overallEl.className = 'result-banner';
        overallEl.textContent = `Edit any suggested input below if needed, then execute Step ${nextStepIdx + 1} of ${totalSteps}.`;
      }

      stepListEl.innerHTML = '';

      // Dedicated editable Default Value inputs when triggered by omitted positional/keyword slot
      if (wizardState.missingDefaultParams && wizardState.missingDefaultParams.length > 0) {
        const defBox = document.createElement('div');
        defBox.className = 'step-card active';
        defBox.innerHTML = `
          <div class="step-top">
            <span class="step-name">Set Default Value(s) for Missing Argument(s)</span>
            <span class="pill">editable</span>
          </div>
        `;
        wizardState.missingDefaultParams.forEach((p, pIdx) => {
          const row = document.createElement('div');
          row.style.marginBottom = '8px';
          const lbl = document.createElement('label');
          lbl.textContent = `Default for "${p.name}" (${p.kind}, ${p.annotation})`;
          const inp = document.createElement('input');
          inp.type = 'text';
          inp.value = p.currentValue !== undefined ? String(p.currentValue) : String(p.suggested_default ?? '');
          inp.placeholder = 'Enter Python literal default (e.g. 0, 1, "abc", None)';
          inp.disabled = false;
          inp.readOnly = false;
          inp.oninput = (e) => {
            p.currentValue = e.target.value;
            applyDefaultInputToStepCode(p.name, e.target.value);
          };
          row.appendChild(lbl);
          row.appendChild(inp);
          defBox.appendChild(row);
          if (pIdx === 0) setTimeout(() => inp.focus(), 20);
        });
        stepListEl.appendChild(defBox);
      }

      wizardState.steps.forEach((step, idx) => {
        const isCurrent = idx === nextStepIdx && !wizardState.completedAll;
        const card = document.createElement('div');
        card.className = `step-card ${step.status} ${isCurrent ? 'active' : ''}`;

        const pillClass =
          step.status === 'done' ? 'st-done' :
          step.status === 'running' ? 'st-running' :
          step.status === 'failed' ? 'st-failed' : 'st-pending';

        if (step.rawBodyText === undefined) {
          step.rawBodyText = step.body !== undefined ? JSON.stringify(step.body, null, 2) : '';
        }

        card.innerHTML = `
          <div class="step-top">
            <span class="step-name">Step ${idx + 1}: ${escapeHtml(step.tool || step.title)}</span>
            <span class="step-status-pill ${pillClass}">${escapeHtml(step.status)}</span>
          </div>
          <div style="font-size:0.78rem;color:var(--muted);margin-bottom:6px;">
            ${escapeHtml(step.statusMessage)}
          </div>
          <label for="modalStepHref_${idx}">Endpoint (${escapeHtml(step.method)})</label>
          <input id="modalStepHref_${idx}" type="text" value="${escapeHtml(step.href)}" />
          <label for="modalStepBody_${idx}">Editable Request JSON Payload</label>
          <textarea id="modalStepBody_${idx}" style="min-height:105px;">${escapeHtml(step.rawBodyText)}</textarea>
        `;

        stepListEl.appendChild(card);

        const hrefInput = document.getElementById(`modalStepHref_${idx}`);
        const bodyTextarea = document.getElementById(`modalStepBody_${idx}`);
        hrefInput.disabled = false;
        hrefInput.readOnly = false;
        bodyTextarea.disabled = false;
        bodyTextarea.readOnly = false;

        hrefInput.oninput = (e) => updateStepFieldFromEditor(idx, 'href', e.target.value);
        bodyTextarea.oninput = (e) => updateStepFieldFromEditor(idx, 'body', e.target.value);

        if (step.response) {
          const outPre = document.createElement('pre');
          outPre.style.marginTop = '8px';
          outPre.textContent = `// Step Output:\n${JSON.stringify({
            status: step.response.status,
            result: step.response.result !== undefined ? step.response.result : step.response.message || step.response.status,
          }, null, 2)}`;
          card.appendChild(outPre);
        }
      });

      const allDone = totalSteps > 0 && doneSteps === totalSteps;
      wizardState.completedAll = allDone;

      nextBtn.disabled = wizardState.isRunningStep || allDone || nextStepIdx === -1;
      allBtn.disabled = wizardState.isRunningStep || allDone || nextStepIdx === -1;
      exitBtn.disabled = wizardState.isRunningStep;

      if (allDone) {
        nextBtn.style.display = 'none';
        allBtn.style.display = 'none';
        exitBtn.className = 'success';
        exitBtn.textContent = 'Exit (Completed)';
      } else {
        nextBtn.style.display = 'inline-block';
        allBtn.style.display = 'inline-block';
        nextBtn.textContent = nextStepIdx >= 0 ? `Execute Step ${nextStepIdx + 1}` : 'Execute Next Step';
        exitBtn.className = 'secondary';
        exitBtn.textContent = 'Exit';
      }
    }

    async function executeNextModalStep() {
      if (wizardState.isRunningStep) return;
      const idx = wizardState.steps.findIndex(s => s.status === 'pending' || s.status === 'failed');
      if (idx === -1) return;

      const capturedSig = wizardState.signature;
      const step = wizardState.steps[idx];

      wizardState.isRunningStep = true;
      step.status = 'running';
      step.statusMessage = `Calling ${step.method} ${step.href}...`;
      renderHateoasModal();

      try {
        const opts = { method: step.method, headers: { 'Accept': 'application/hal+json' } };
        if (step.body !== undefined && step.method !== 'GET' && step.method !== 'DELETE') {
          opts.headers['Content-Type'] = 'application/json';
          opts.body = JSON.stringify(step.body);
        }

        const resp = await fetch(step.href, opts);
        const data = await resp.json();

        await refreshSnapshot();
        renderResponse(data, false);

        const newTracks = extractHateoasActionTracks(data);
        const newSig = newTracks.length ? computeTracksSignature(newTracks) : null;

        wizardState.isRunningStep = false;

        if (data.status === 'error') {
          step.status = 'failed';
          step.statusMessage = data.message || `Failed (${data.error_type || 'error'})`;
          step.response = data;
          if (newSig && newSig !== capturedSig) {
            syncWizardWithHateoas(data, true);
            return;
          }
        } else {
          step.status = 'done';
          const outVal = data.result !== undefined ? `result = ${JSON.stringify(data.result)}` : (data.status || 'ok');
          step.statusMessage = `Completed (${outVal})`;
          step.response = data;

          const remainingPending = wizardState.steps.some(s => s.status === 'pending');
          if (!remainingPending && newSig && newSig !== capturedSig) {
            wizardState.completedAll = true;
            syncWizardWithHateoas(data, true);
            return;
          }
        }
      } catch (err) {
        wizardState.isRunningStep = false;
        step.status = 'failed';
        step.statusMessage = `Network/execution error: ${err}`;
      }

      renderHateoasModal();
    }

    async function executeAllRemainingModalSteps() {
      while (!wizardState.isRunningStep) {
        const idx = wizardState.steps.findIndex(s => s.status === 'pending' || s.status === 'failed');
        if (idx === -1) break;
        const sigBefore = wizardState.signature;
        await executeNextModalStep();
        if (wizardState.signature !== sigBefore) break;
        if (wizardState.steps[idx] && wizardState.steps[idx].status === 'failed') break;
      }
    }

    async function followRestLink(method, href, body) {
      if (hasUnresolvedTemplate(href)) {
        alert(`Refusing to call unresolved URI template: ${href}`);
        return null;
      }
      const opts = { method, headers: { 'Accept': 'application/hal+json' } };
      if (body !== undefined && method !== 'GET' && method !== 'DELETE') {
        opts.headers['Content-Type'] = 'application/json';
        opts.body = JSON.stringify(body);
      }
      const resp = await fetch(href, opts);
      const data = await resp.json();

      await refreshSnapshot();
      renderResponse(data, true);
      return data;
    }

    function parseFlexibleEvalInput(raw) {
      if (!raw) return [];
      try {
        return JSON.parse(raw);
      } catch (e) {
        return raw;
      }
    }

    async function runCommand() {
      const tool = document.getElementById('toolSelect').value;
      const name = document.getElementById('nameInput').value.trim();
      const rawCode = document.getElementById('codeInput').value;
      const rawArgs = document.getElementById('argsInput').value.trim();
      const skipRoundtrip = document.getElementById('skipRoundtripInput').checked;

      if (tool === 'eval_ast_call') {
        const parsedPayload = parseFlexibleEvalInput(rawArgs);
        if (isValidPyIdentifier(name)) {
          await followRestLink('POST', `/api/ast/${encodeURIComponent(name)}/eval`, parsedPayload);
        } else {
          await followRestLink('POST', '/api/eval', { target: name, args: parsedPayload });
        }
      } else if (tool === 'ast_create') {
        await followRestLink('POST', '/api/ast', { name, code_or_ast: rawCode, overwrite: true, skip_roundtrip: skipRoundtrip });
      } else if (tool === 'ast_update') {
        await followRestLink('PUT', `/api/ast/${encodeURIComponent(name)}`, { code_or_ast: rawCode, skip_roundtrip: skipRoundtrip });
      } else if (tool === 'ast_read') {
        await followRestLink('GET', name ? `/api/ast/${encodeURIComponent(name)}` : '/api/ast');
      } else if (tool === 'ast_delete') {
        await followRestLink('DELETE', `/api/ast/${encodeURIComponent(name)}`);
      } else if (tool === 'ast_chain') {
        let commands = [];
        try { commands = JSON.parse(rawCode); } catch (e) { alert('Invalid JSON commands array'); return; }
        await followRestLink('POST', '/api/chain', { commands });
      } else if (tool === 'ast_convert') {
        const source_kind = document.getElementById('sourceKindSelect').value;
        const target_kind = document.getElementById('targetKindSelect').value;
        await followRestLink('POST', '/api/convert', {
          source_kind,
          target_kind,
          payload: rawCode.trim() || name,
          model_name: name || 'GeneratedModel'
        });
      }
    }

    async function quickEval(target, arr) {
      await followRestLink('POST', `/api/ast/${encodeURIComponent(target)}/eval`, arr);
    }

    function renderResponse(data, triggerHateoasWizard = true) {
      if (!data) return;
      document.getElementById('lastResponse').textContent = JSON.stringify(data, null, 2);

      const cycleErr = parseCircularityFromError(data);
      if (cycleErr) {
        document.getElementById('hateoasModalBackdrop').classList.remove('open');
        openCircularityModal(cycleErr);
      }

      const summaryEl = document.getElementById('resultSummary');
      const convWrap = document.getElementById('convertedCodeWrap');
      const convPre = document.getElementById('convertedCodePreview');

      if (data.status === 'error') {
        summaryEl.className = 'result-banner error';
        summaryEl.textContent = `[ERROR: ${data.error_type || 'error'}] ${data.message || ''}`;
      } else if (data.status === 'evaluated_function') {
        summaryEl.className = 'result-banner';
        summaryEl.textContent = `EVAL ${data.target}(args=${JSON.stringify(data.args)}, kwargs=${JSON.stringify(data.kwargs || {})}) => ${JSON.stringify(data.result)}`;
      } else if (data.status === 'evaluated_expression') {
        summaryEl.className = 'result-banner';
        summaryEl.textContent = `EVAL ${data.expression_source} => ${JSON.stringify(data.result)}`;
      } else if (data.status === 'converted') {
        summaryEl.className = 'result-banner';
        summaryEl.textContent = `CONVERTED ${data.source_kind} -> ${data.target_kind} (${data.model_name || data.function_name || 'ok'})`;
      } else {
        summaryEl.className = 'result-banner';
        summaryEl.textContent = `STATUS: ${data.status || 'ok'}${data.name ? ' (' + data.name + ')' : ''}`;
      }

      if (data.python_source) {
        convWrap.style.display = 'block';
        convPre.textContent = data.python_source;
      } else if (data.json_schema) {
        convWrap.style.display = 'block';
        convPre.textContent = JSON.stringify(data.json_schema, null, 2);
      } else if (data.recommended_module_script && data.recommended_module_script.python_source) {
        convWrap.style.display = 'block';
        convPre.textContent = data.recommended_module_script.python_source;
      } else {
        convWrap.style.display = 'none';
      }

      const slot = document.getElementById('recoverySlot');
      slot.innerHTML = '';

      if (cycleErr) {
        const fixCycleBtn = document.createElement('button');
        fixCycleBtn.className = 'danger';
        fixCycleBtn.textContent = `Resolve Cycle (${cycleErr.cyclePath.join(' -> ')})`;
        fixCycleBtn.onclick = () => openCircularityModal(cycleErr);
        slot.appendChild(fixCycleBtn);
      }

      const tracks = extractHateoasActionTracks(data);
      if (tracks.length > 0 && !cycleErr) {
        const modalBtn = document.createElement('button');
        modalBtn.className = 'warn';
        modalBtn.textContent = `Step-by-Step HATEOAS Modal (${tracks[0].steps.length} step${tracks[0].steps.length > 1 ? 's' : ''})`;
        modalBtn.onclick = () => {
          syncWizardWithHateoas(data, true);
          openHateoasModal();
        };
        slot.appendChild(modalBtn);
      }

      if (triggerHateoasWizard && !cycleErr) {
        syncWizardWithHateoas(data, true);
      }
    }

    function summarizeEventOutput(ev) {
      const r = ev.result || {};
      if (r.status === 'evaluated_function' || r.status === 'evaluated_expression') {
        return `result = ${JSON.stringify(r.result)}`;
      }
      if (r.status === 'converted') {
        return `converted ${r.source_kind} -> ${r.target_kind} (${r.model_name || r.function_name || ''})`;
      }
      if (r.status === 'error') {
        return `error (${r.error_type}): ${r.message || ''}`;
      }
      return `status = ${r.status || ev.status}`;
    }

    function renderSnapshot(snap) {
      latestSnapshot = snap;
      const items = snap.items || [];
      const events = snap.events || [];
      document.getElementById('fnCount').textContent = items.length;

      const regCycle = detectCycleInRegistry(items);
      if (regCycle && !cycleState.isSaving && !cycleState.active) {
        openCircularityModal(regCycle);
      }

      const regEl = document.getElementById('registryList');
      regEl.innerHTML = '';
      for (const item of items) {
        const tab = activeFormatTab[item.name] || 'python_source';
        const div = document.createElement('div');
        div.className = 'fn-item';

        const content = tab === 'json_ast'
          ? JSON.stringify(item.formats.json_ast, null, 2)
          : item.formats[tab];

        div.innerHTML = `
          <div class="fn-header">
            <div>
              <span class="fn-title">${item.name}</span>
              <span class="pill">${item.compiled ? 'compiled' : 'uncompiled'}</span>
              ${item.exposed_as_tool ? '<span class="pill">MCP tool</span>' : ''}
            </div>
            <div style="display:flex;gap:6px;flex-wrap:wrap;">
              <button class="secondary" style="padding:4px 8px;font-size:0.75rem;" onclick="followRestLink('POST', '/api/convert', {source_kind: 'saved_ast', target_kind: 'pydantic', payload: '${item.name}'})">To Pydantic</button>
              <button class="secondary" style="padding:4px 8px;font-size:0.75rem;" onclick="loadIntoEditor('${item.name}')">Edit</button>
              <button class="secondary" style="padding:4px 8px;font-size:0.75rem;" onclick="quickEval('${item.name}', [3])">Eval(3)</button>
              <button class="danger" style="padding:4px 8px;font-size:0.75rem;" onclick="followRestLink('DELETE', '/api/ast/${item.name}')">Del</button>
            </div>
          </div>
          <div class="tabs">
            ${['python_source', 'serialized_ast', 'pretty_ast', 'json_ast'].map(fmt => `
              <button class="tab-btn ${tab === fmt ? 'active' : ''}" onclick="setTab('${item.name}', '${fmt}')">${fmt}</button>
            `).join('')}
          </div>
          <pre>${escapeHtml(content)}</pre>
        `;
        regEl.appendChild(div);
      }

      const evEl = document.getElementById('eventList');
      evEl.innerHTML = '';
      for (const ev of [...events].reverse()) {
        const div = document.createElement('div');
        const srcLower = (ev.source || '').toLowerCase();
        const cls = ev.status === 'error' ? 'error' : (srcLower === 'stdin' ? 'stdin' : (srcLower.startsWith('mcp') ? 'mcp' : ''));
        div.className = `event-entry ${cls}`;
        div.onclick = () => renderResponse(ev.result, true);
        div.innerHTML = `
          <div class="event-meta">
            <span>#${ev.id} [SRC:${ev.source.toUpperCase()}] &rarr; [ROUTE:${ev.route || 'RUNTIME'}] <strong>${ev.tool}</strong></span>
            <span>${ev.timestamp} &bull; ${ev.status}</span>
          </div>
          <div style="font-family:var(--mono);font-size:0.76rem;color:var(--muted);">
            args: ${escapeHtml(JSON.stringify(ev.arguments))}
          </div>
          <div style="font-family:var(--mono);font-size:0.78rem;font-weight:600;color:${ev.status === 'error' ? '#b91c1c' : '#15803d'};margin-top:3px;">
            ${escapeHtml(summarizeEventOutput(ev))}
          </div>
        `;
        evEl.appendChild(div);
      }

      if (events.length) {
        const newest = events[events.length - 1];
        if (newest.id && newest.id > lastSeenEventId) {
          lastSeenEventId = newest.id;
          if (newest.result) {
            renderResponse(newest.result, !wizardState.isRunningStep);
          }
        }
      }
    }

    function setTab(name, fmt) {
      activeFormatTab[name] = fmt;
      refreshSnapshot();
    }

    function loadIntoEditor(name) {
      const found = (latestSnapshot.items || []).find(i => i.name === name);
      if (!found) return;
      document.getElementById('toolSelect').value = 'ast_update';
      updateFormFields();
      document.getElementById('nameInput').value = name;
      document.getElementById('codeInput').value = found.formats.python_source;
    }

    function escapeHtml(str) {
      return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    }

    async function refreshSnapshot() {
      try {
        const resp = await fetch('/api/snapshot');
        const snap = await resp.json();
        renderSnapshot(snap);
      } catch (e) {}
    }

    function connectSSE() {
      const es = new EventSource('/api/events');
      es.onmessage = (evt) => {
        const msg = JSON.parse(evt.data);
        if (msg.snapshot) renderSnapshot(msg.snapshot);
        if (msg.event && msg.event.result) {
          if (msg.event.id && msg.event.id > lastSeenEventId) {
            lastSeenEventId = msg.event.id;
          }
          renderResponse(msg.event.result, !wizardState.isRunningStep);
        }
      };
      es.onerror = () => {
        document.getElementById('connStatus').textContent = 'Live Sync (Polling)';
      };
      es.onopen = () => {
        document.getElementById('connStatus').textContent = 'Connected (SSE Live)';
      };
    }

    (function initAutocompleteEngine() {
      const popup = document.getElementById('acPopup');
      let activeEl = null;
      let currentItems = [];
      let selectedIdx = 0;

      function getTargetFnMeta(targetName) {
        const items = (latestSnapshot && latestSnapshot.items) || [];
        const match = items.find(i => i.name === targetName);
        if (!match) return null;
        const src = (match.formats && match.formats.python_source) || '';
        const fnMatch = src.match(/^\s*def\s+[A-Za-z_][A-Za-z0-9_]*\s*\(([^)]*)\)/m);
        const params = [];
        if (fnMatch && fnMatch[1].trim()) {
          for (const rawPart of fnMatch[1].split(',')) {
            const cleaned = rawPart.trim();
            if (!cleaned || cleaned.startsWith('*')) continue;
            const [left, defPart] = cleaned.split('=');
            const paramName = left.split(':')[0].trim();
            const typeHint = (left.split(':')[1] || '').trim();
            if (paramName) {
              params.push({
                name: paramName,
                type: typeHint,
                hasDefault: defPart !== undefined,
                defaultRaw: defPart ? defPart.trim() : undefined,
              });
            }
          }
        }
        return { name: match.name, params, source: src };
      }

      function sampleValueForParam(param, idx) {
        if (param.hasDefault && param.defaultRaw) {
          try {
            return JSON.parse(param.defaultRaw.replace(/'/g, '"'));
          } catch (_) {
            if (/^-?\d+(\.\d+)?$/.test(param.defaultRaw)) return Number(param.defaultRaw);
          }
        }
        const t = (param.type || '').toLowerCase();
        if (t.includes('float')) return 2.5 + idx;
        if (t.includes('int')) return 3 + idx;
        if (t.includes('bool')) return true;
        if (t.includes('str')) return `sample_${idx + 1}`;
        if (t.includes('list')) return [1, 2];
        if (t.includes('dict')) return { k: 1 };
        return 3 + idx;
      }

      function buildArgsSuggestions(el) {
        const targetVal = (document.getElementById('nameInput')?.value || '').trim();
        const meta = getTargetFnMeta(targetVal);
        const suggestions = [];

        if (meta && meta.params.length > 0) {
          const posSamples = meta.params.map((p, i) => sampleValueForParam(p, i));
          const objSample = {};
          meta.params.forEach((p, i) => { objSample[p.name] = posSamples[i]; });

          suggestions.push({
            insert: JSON.stringify(posSamples),
            label: JSON.stringify(posSamples),
            meta: `${meta.name} list args`,
          });
          suggestions.push({
            insert: JSON.stringify(objSample),
            label: JSON.stringify(objSample),
            meta: `${meta.name} object kwargs`,
          });
          if (posSamples.length === 1) {
            suggestions.push({
              insert: JSON.stringify(posSamples[0]),
              label: JSON.stringify(posSamples[0]),
              meta: 'literal arg',
            });
          }
          suggestions.push({
            insert: JSON.stringify([posSamples[0], { extra_kw: 1 }]),
            label: JSON.stringify([posSamples[0], { extra_kw: 1 }]),
            meta: 'list + object',
          });
        }

        const defaults = [
          { insert: '3', label: '3', meta: 'literal int' },
          { insert: '[3, 4]', label: '[3, 4]', meta: 'list args' },
          { insert: '[3, {"k": 1}]', label: '[3, {"k": 1}]', meta: 'list + object' },
          { insert: '{"x": 3}', label: '{"x": 3}', meta: 'object kwargs' },
          { insert: '[]', label: '[]', meta: 'empty args' },
        ];
        for (const d of defaults) {
          if (!suggestions.some(s => s.label === d.label)) {
            suggestions.push(d);
          }
        }

        const q = (el.value || '').trim().toLowerCase();
        return q ? suggestions.filter(s => s.label.toLowerCase().includes(q) || s.meta.toLowerCase().includes(q)) : suggestions;
      }

      function buildNameSuggestions(el) {
        const tool = document.getElementById('toolSelect')?.value || '';
        const regNames = ((latestSnapshot && latestSnapshot.items) || []).map(i => i.name);
        const pool = [];

        for (const nm of regNames) {
          pool.push({ insert: nm, label: nm, meta: 'AST fn' });
          if (tool === 'eval_ast_call') {
            pool.push({ insert: `${nm}(3)`, label: `${nm}(3)`, meta: 'expr call' });
          }
        }
        if (tool === 'eval_ast_call') {
          pool.push({ insert: 'ast1(4) + ast2(2)', label: 'ast1(4) + ast2(2)', meta: 'expr' });
        }

        const q = (el.value || '').trim().toLowerCase();
        return q ? pool.filter(p => p.label.toLowerCase().includes(q)) : pool;
      }

      function getCaretTokenRange(el) {
        const start = el.selectionStart ?? el.value.length;
        const before = el.value.slice(0, start);
        const match = before.match(/[A-Za-z_][A-Za-z0-9_]*$/);
        return {
          token: match ? match[0] : '',
          tokenStart: match ? start - match[0].length : start,
          caretPos: start,
        };
      }

      function buildTextareaSuggestions(el, token) {
        const regNames = ((latestSnapshot && latestSnapshot.items) || []).map(i => i.name);
        const snippets = [
          { insert: 'def fn(x: int = 3) -> int:\n    return x * 2', label: 'def fn(x: int = 3) -> int: ...', meta: 'snippet' },
          { insert: 'return ', label: 'return ...', meta: 'keyword' },
          { insert: 'import math', label: 'import math', meta: 'import' },
          {
            insert: JSON.stringify([{ tool: 'eval_ast_call', arguments: { target: regNames[0] || 'entrypoint', args: [3] } }], null, 2),
            label: 'chain step template',
            meta: 'ast_chain',
          },
        ];

        for (const nm of regNames) {
          snippets.push({ insert: nm, label: nm, meta: 'AST symbol' });
          snippets.push({ insert: `${nm}(3)`, label: `${nm}(3)`, meta: 'AST call' });
        }

        const q = token.toLowerCase();
        return q
          ? snippets.filter(s => s.label.toLowerCase().includes(q) || s.insert.toLowerCase().includes(q))
          : snippets;
      }

      function positionPopupNearElement(el, tokenStart) {
        const rect = el.getBoundingClientRect();
        let left = rect.left + 8;
        let top = rect.bottom + 4;

        if (el.tagName === 'TEXTAREA') {
          const linesBefore = el.value.slice(0, tokenStart ?? el.selectionStart ?? 0).split('\n');
          const lineIdx = Math.max(0, linesBefore.length - 1);
          const lineHeight = parseFloat(getComputedStyle(el).lineHeight) || 20;
          const approxY = rect.top + 10 + lineIdx * lineHeight - el.scrollTop;
          top = Math.min(window.innerHeight - 270, Math.max(rect.top + 22, approxY + lineHeight));
          left = Math.min(window.innerWidth - 320, rect.left + 12);
        }

        if (top + 240 > window.innerHeight) {
          top = Math.max(8, rect.top - 246);
        }

        popup.style.left = `${Math.max(8, left)}px`;
        popup.style.top = `${Math.max(8, top)}px`;
      }

      function hidePopup() {
        popup.classList.remove('open');
        activeEl = null;
        currentItems = [];
        selectedIdx = 0;
      }

      function applySuggestion(item) {
        if (!activeEl || !item) return;
        const el = activeEl;
        if (el.tagName === 'TEXTAREA') {
          const { tokenStart, caretPos } = getCaretTokenRange(el);
          const nextVal = el.value.slice(0, tokenStart) + item.insert + el.value.slice(caretPos);
          el.value = nextVal;
          const newPos = tokenStart + item.insert.length;
          el.setSelectionRange(newPos, newPos);
        } else {
          el.value = item.insert;
        }
        el.dispatchEvent(new Event('input', { bubbles: true }));
        hidePopup();
        el.focus();
      }

      function renderPopup(el) {
        let items = [];
        let tokenStart = null;
        if (el.id === 'argsInput') {
          items = buildArgsSuggestions(el);
        } else if (el.id === 'nameInput') {
          items = buildNameSuggestions(el);
        } else if (el.tagName === 'TEXTAREA') {
          const range = getCaretTokenRange(el);
          tokenStart = range.tokenStart;
          items = buildTextareaSuggestions(el, range.token);
        }

        if (!items.length) {
          hidePopup();
          return;
        }

        activeEl = el;
        currentItems = items.slice(0, 12);
        selectedIdx = Math.min(selectedIdx, currentItems.length - 1);

        popup.innerHTML = '';
        currentItems.forEach((it, idx) => {
          const row = document.createElement('div');
          row.className = `ac-item ${idx === selectedIdx ? 'selected' : ''}`;
          row.setAttribute('role', 'option');
          row.innerHTML = `<span class="ac-label">${escapeHtml(it.label)}</span><span class="ac-meta">${escapeHtml(it.meta)}</span>`;
          row.onmousedown = (e) => {
            e.preventDefault();
            applySuggestion(it);
          };
          popup.appendChild(row);
        });

        positionPopupNearElement(el, tokenStart);
        popup.classList.add('open');
      }

      const watchedIds = ['nameInput', 'argsInput', 'codeInput', 'cycleLeftCode', 'cycleRightCode'];
      for (const id of watchedIds) {
        const el = document.getElementById(id);
        if (!el) continue;
        el.addEventListener('focus', () => { selectedIdx = 0; renderPopup(el); });
        el.addEventListener('input', () => { selectedIdx = 0; renderPopup(el); });
        el.addEventListener('click', () => renderPopup(el));
        el.addEventListener('keydown', (e) => {
          if (!popup.classList.contains('open')) {
            if (e.ctrlKey && e.code === 'Space') {
              e.preventDefault();
              renderPopup(el);
            }
            return;
          }
          if (e.key === 'ArrowDown') {
            e.preventDefault();
            selectedIdx = (selectedIdx + 1) % currentItems.length;
            renderPopup(el);
          } else if (e.key === 'ArrowUp') {
            e.preventDefault();
            selectedIdx = (selectedIdx - 1 + currentItems.length) % currentItems.length;
            renderPopup(el);
          } else if (e.key === 'Tab' || (e.key === 'Enter' && el.tagName !== 'TEXTAREA')) {
            e.preventDefault();
            applySuggestion(currentItems[selectedIdx]);
          } else if (e.key === 'Escape') {
            hidePopup();
          }
        });
        el.addEventListener('blur', () => setTimeout(hidePopup, 120));
      }
    })();

    updateFormFields();
    refreshSnapshot();
    connectSSE();
    setInterval(refreshSnapshot, 1200);
  </script>
</body>
</html>
"""


class QuietThreadingHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


def start_web_dashboard(
    prog: DynamicASTMcpProgram,
    loop: asyncio.AbstractEventLoop,
    host: str = "0.0.0.0",
    port: int = 8080,
) -> ThreadingHTTPServer:
    def _http_status_for_hal(res: dict[str, Any], default_ok: int = 200) -> int:
        if res.get("status") != "error":
            return default_ok
        err_type = res.get("error_type", "")
        if err_type == "missing_ast_call":
            return 404
        if err_type in ("already_exists", "file_already_exists"):
            return 409
        return 422

    async def _dispatch_tool(tool: str, args: Any, source: str = "rest") -> dict[str, Any]:
        if tool == "ast_create":
            return await prog.create(**args, source=source)
        if tool == "ast_read":
            return prog.read(**args, source=source)
        if tool == "ast_update":
            return await prog.update(**args, source=source)
        if tool == "ast_delete":
            return await prog.delete(**args, source=source)
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
            return await prog.execute_chain(**args, source=source)
        if tool == "ast_convert":
            return prog.convert(**args, source=source)
        if tool == "ast_install_module":
            return prog.install_module(**args, source=source)
        if tool == "ast_write_module_file":
            return prog.write_local_module_file(**args, source=source)
        if tool in prog.registry:
            return prog.eval_target(target=tool, args=args, source=source)
        return build_missing_call_hal_recovery(
            missing_name=str(tool),
            failed_tool="eval_ast_call",
            failed_params={"target": tool, "args": []},
            reason=f"Unknown command/function `{tool}`.",
        )

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
            length = int(self.headers.get("Content-Length", "0"))
            if not length:
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

        def _run_coro(self, coro: Any) -> dict[str, Any]:
            fut = asyncio.run_coroutine_threadsafe(coro, loop)
            return fut.result(timeout=150.0)

        def do_OPTIONS(self) -> None:
            self._send_bytes(b"", "text/plain", status=204)

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"

            if path == "/":
                self._send_bytes(DASHBOARD_HTML.encode("utf-8"), "text/html; charset=utf-8")
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
                        "snapshot": {"href": "/api/snapshot", "method": "GET"},
                        "events_sse": {"href": "/api/events", "method": "GET"},
                    },
                    "_embedded": registry_listing.get("_embedded", {}),
                }
                self._send_hal(root_doc, request_href="/api", request_method="GET")
                return

            if path == "/api/ast":
                res = prog.read(None, source="rest")
                self._send_hal(res, request_href="/api/ast", request_method="GET", status=_http_status_for_hal(res))
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
                try:
                    raw_payload = self._read_any_json_body()
                except Exception as exc:
                    self._send_hal(
                        {"status": "error", "error_type": "invalid_json", "message": str(exc)},
                        request_href=path,
                        request_method="POST",
                        status=400,
                    )
                    return

                res = prog.eval_target(
                    target=name,
                    args=raw_payload,
                    source="rest",
                )
                self._send_hal(
                    res,
                    request_href=f"/api/ast/{name}/eval",
                    request_method="POST",
                    status=_http_status_for_hal(res),
                )
                return

            try:
                body = self._read_json_object_body()
            except Exception as exc:
                self._send_hal(
                    {"status": "error", "error_type": "invalid_json", "message": str(exc)},
                    request_href=path,
                    request_method="POST",
                    status=400,
                )
                return

            if path == "/api/command":
                tool = body.get("tool", "")
                args = body.get("arguments") if "arguments" in body else body.get("args", {})
                source = body.get("source", "http")
                res = self._run_coro(_dispatch_tool(tool, args, source=source))
                self._send_hal(res, request_href="/api/command", request_method="POST")
                return

            if path == "/api/cli":
                raw_cmd = str(body.get("command") or body.get("line") or "").strip()
                if not raw_cmd:
                    self._send_hal(
                        {
                            "status": "error",
                            "error_type": "missing_cli_command",
                            "message": "Provide `{\"command\": \"<cli command>\"}` in the JSON body.",
                        },
                        request_href="/api/cli",
                        request_method="POST",
                        status=400,
                    )
                    return
                parsed_cli = _parse_stdin_line(raw_cmd)
                res = self._run_coro(
                    _dispatch_tool(
                        parsed_cli["tool"],
                        parsed_cli.get("arguments") or {},
                        source=body.get("source", "rest_cli"),
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
                res = self._run_coro(prog.execute_chain(commands=body.get("commands") or [], source="rest"))
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
                try:
                    body = self._read_json_object_body()
                except Exception as exc:
                    self._send_hal(
                        {"status": "error", "error_type": "invalid_json", "message": str(exc)},
                        request_href=path,
                        request_method="PUT",
                        status=400,
                    )
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


# ---------------------------------------------------------------------------
# STDIN -> HTTP Sync Bridge
# ---------------------------------------------------------------------------
def _parse_stdin_line(line: str) -> dict[str, Any]:
    stripped = line.strip()
    if stripped.startswith("{"):
        parsed = json.loads(stripped)
        parsed.setdefault("source", "stdin")
        return parsed

    skip_rt = False
    if "--no-roundtrip" in stripped:
        skip_rt = True
        stripped = stripped.replace("--no-roundtrip", "").strip()

    parts = stripped.split(None, 2)
    cmd = parts[0].lower()

    if cmd == "read":
        return {"tool": "ast_read", "arguments": {"name": parts[1] if len(parts) > 1 else ""}, "source": "stdin"}
    if cmd == "delete" and len(parts) >= 2:
        return {"tool": "ast_delete", "arguments": {"name": parts[1]}, "source": "stdin"}
    if cmd == "create" and len(parts) >= 2:
        return {
            "tool": "ast_create",
            "arguments": {
                "name": parts[1],
                "code_or_ast": parts[2] if len(parts) > 2 else "",
                "overwrite": True,
                "skip_roundtrip": skip_rt,
            },
            "source": "stdin",
        }
    if cmd == "update" and len(parts) >= 3:
        return {
            "tool": "ast_update",
            "arguments": {
                "name": parts[1],
                "code_or_ast": parts[2],
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
        return {"tool": "eval_ast_call", "arguments": {"target": target, "args": eval_payload}, "source": "stdin"}

    return {"tool": "eval_ast_call", "arguments": {"target": stripped, "args": []}, "source": "stdin"}


def start_stdin_to_http_bridge(http_base_url: str) -> None:
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
                payload = _parse_stdin_line(line)
                req = Request(
                    f"{http_base_url}/api/command",
                    data=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urlopen(req, timeout=150.0) as resp:
                    resp.read()
            except Exception as exc:
                sys.stdout.write(_colorize(f"[STDIN -> HTTP ERROR] {exc}\n", "red"))
                sys.stdout.flush()

    threading.Thread(target=_worker, daemon=True).start()


# ---------------------------------------------------------------------------
# FastMCP Server Setup
# ---------------------------------------------------------------------------
def create_server(
    host: str = "0.0.0.0",
    port: int = 8000,
    streamable_http_path: str = "/mcp",
    stdout_is_mcp_stdio: bool = False,
) -> tuple[FastMCP, DynamicASTMcpProgram]:
    server = FastMCP(
        "dynamic-ast-server",
        host=host,
        port=port,
        streamable_http_path=streamable_http_path,
    )

    list1: dict[str, str] = {
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
        initial_registry=list1,
        auto_expose_keys=["entrypoint"],
        stdout_is_mcp_stdio=stdout_is_mcp_stdio,
    )

    @server.resource("ast://resource/{name}")
    def read_ast_resource(name: str) -> str:
        return json.dumps(prog.read(name, source="mcp_resource"), indent=2)

    @server.tool(
        name="ast_create",
        description="CREATE a new AST function, blank shell, or full-page Python script (auto-wrapped into a callable function with dynamic imports/classes preserved) with HATEOAS hal+json recovery.",
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
        description="UPDATE an existing AST entry (single function or full-page Python script) with auto-fix lint pipeline, roundtrip check, or HATEOAS navigation.",
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

    return server, prog


def _resolve_disabled_servers(raw_items: list[str] | None, no_mcp: bool, no_ui: bool, no_stdin: bool) -> set[str]:
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
    threading.Thread(target=bg_loop.run_forever, daemon=True).start()

    ui_url = f"http://{ui_host}:{args.ui_port}"
    if ui_enabled:
        start_web_dashboard(prog=prog, loop=bg_loop, host=ui_host, port=args.ui_port)

    if not is_stdio:
        if stdin_enabled:
            start_stdin_to_http_bridge(ui_url)
        if ui_enabled:
            print(f"Live HTML/HTTP Dashboard : {_colorize(ui_url + '/', 'cyan')}")
            print(f"HATEOAS REST API Root    : {_colorize(ui_url + '/api', 'cyan')}")
        else:
            print(f"Live HTML/HTTP Dashboard : {_colorize('DISABLED (--disable-server ui)', 'dim')}")
        if mcp_enabled:
            print(f"MCP Streamable HTTP URL  : {_colorize(f'http://{args.mcp_host}:{args.mcp_port}{args.mcp_path}', 'magenta')}")
        else:
            print(f"MCP Server               : {_colorize('DISABLED (--disable-server mcp)', 'dim')}")
        print(
            _colorize("Legend:", "bold")
            + f" Sources=[{_colorize('STDIN', 'yellow')} | {_colorize('REST/HTTP', 'cyan')} | {_colorize('MCP', 'magenta')}]"
            + f" Routes=[{_colorize('AST_REGISTRY', 'blue')} | {_colorize('EVAL_RUNTIME', 'green')} | {_colorize('CONVERTER', 'teal')} | {_colorize('CHAIN_ORCHESTRATOR', 'purple')} | {_colorize('MODULE_ENV', 'white')}]"
        )
        if stdin_enabled:
            print("Type commands in stdin (e.g. `eval entrypoint 3`, `eval ast1 [3, 4]`, `read`):")
        if mcp_enabled:
            mcp.run(transport="streamable-http")
        else:
            try:
                threading.Event().wait()
            except KeyboardInterrupt:
                pass
    else:
        if ui_enabled:
            sys.stderr.write(f"Live HTML/HTTP Dashboard : {ui_url}/\n")
            sys.stderr.write(f"HATEOAS REST API Root    : {ui_url}/api\n")
            sys.stderr.flush()
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()