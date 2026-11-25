"""Call argument normalization, omission detection, signature patching, and mismatch recovery."""

from __future__ import annotations

import ast
import json
import random
import re
import string
from typing import Any

from .ast_core import (
    collect_all_identifiers,
    deserialize_ast_node,
    export_all_formats,
    serialize_function,
)
from .hal import build_hal_response

OMITTED_SENTINEL = object()

_EMPTY_KW_RE = re.compile(r'"([A-Za-z_][A-Za-z0-9_]*)"\s*:\s*(?=[,}])')
_LEADING_COMMA_RE = re.compile(r"^\s*,")
_CONSECUTIVE_COMMA_RE = re.compile(r",\s*(?=,)")


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


def _suggested_default_for_annotation(ann_str: str) -> Any:
    if ann_str == "int":
        return 0
    if ann_str == "float":
        return 0.0
    if ann_str == "bool":
        return False
    return ""


def parse_eval_args_with_omissions(raw_input: Any) -> tuple[list[Any], dict[str, Any], list[int], list[str]]:
    """
    Parse standard JSON or relaxed shorthand such as:
      - '[, {"k": 1}]'   -> positional index 0 omitted
      - '[3, {"k": }]'   -> keyword 'k' omitted
      - '[3, {"k": 1}]'  -> args=[3], kwargs={'k': 1}
    Returns (call_args, call_kwargs, omitted_pos_indices, omitted_kw_names).
    """
    omitted_pos: list[int] = []
    omitted_kws: list[str] = []

    if isinstance(raw_input, str):
        text = raw_input.strip()

        def _replace_empty_kw(match: re.Match[str]) -> str:
            kw_key = match.group(1)
            if kw_key not in omitted_kws:
                omitted_kws.append(kw_key)
            return f'"{kw_key}": "__OMITTED_ARG__"'

        normalized = _EMPTY_KW_RE.sub(_replace_empty_kw, text)

        if normalized.startswith("[") and normalized.endswith("]"):
            inner = normalized[1:-1]
            normalized = _LEADING_COMMA_RE.sub('"__OMITTED_ARG__",', inner)
            normalized = _CONSECUTIVE_COMMA_RE.sub(', "__OMITTED_ARG__"', normalized)
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
    """Update a function signature to match a call without inventing defaults for supplied params."""
    node = deserialize_ast_node(serialized_ast)
    node.name = name
    default_overrides = default_overrides or {}
    used_names = collect_all_identifiers(node)
    added_params: list[str] = []

    pos_params = list(node.args.posonlyargs) + list(node.args.args)
    kwonly_params = list(node.args.kwonlyargs)
    existing_param_names = {p.arg for p in pos_params} | {p.arg for p in kwonly_params}
    if node.args.vararg:
        existing_param_names.add(node.args.vararg.arg)
    if node.args.kwarg:
        existing_param_names.add(node.args.kwarg.arg)

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
                    while len(node.args.defaults) < (num_pos - idx):
                        fill_idx = num_pos - len(node.args.defaults) - 1
                        fill_arg = node.args.args[fill_idx]
                        fill_val = default_overrides.get(fill_arg.arg, 0)
                        node.args.defaults.insert(0, _literal_ast_for_value(fill_val))
                added_params.append(f"{arg_node.arg}={ast.unparse(lit)}")

        for kw_idx, kw_node in enumerate(node.args.kwonlyargs):
            if kw_node.arg in default_overrides:
                lit = _literal_ast_for_value(default_overrides[kw_node.arg])
                while len(node.args.kw_defaults) < len(node.args.kwonlyargs):
                    node.args.kw_defaults.append(None)
                node.args.kw_defaults[kw_idx] = lit
                added_params.append(f"{kw_node.arg}={ast.unparse(lit)}")

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

    if node.args.kwarg is None and call_kwargs:
        has_existing_pos_defaults = len(node.args.defaults) > 0
        for kw_name, kw_val in call_kwargs.items():
            if not isinstance(kw_name, str) or not kw_name.isidentifier():
                continue
            if kw_name in existing_param_names:
                continue
            ann_expr = _infer_type_annotation_expr(kw_val)
            if has_existing_pos_defaults or node.args.vararg is not None or node.args.kwonlyargs:
                node.args.kwonlyargs.append(ast.arg(arg=kw_name, annotation=ann_expr))
                node.args.kw_defaults.append(None)
            else:
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
    Check whether any omitted positional/keyword slot targets a parameter without a default.
    If so, return a `hal+json` response triggering the Set-Default modal.
    """
    node = deserialize_ast_node(serialized_ast)
    pos_args = list(node.args.posonlyargs) + list(node.args.args)
    num_pos = len(pos_args)
    first_default_idx = num_pos - len(node.args.defaults)

    params_needing_default: list[dict[str, Any]] = []

    for pos_idx in omitted_pos_indices:
        if 0 <= pos_idx < num_pos and pos_idx < first_default_idx:
            p_node = pos_args[pos_idx]
            ann_str = ast.unparse(p_node.annotation) if p_node.annotation else "Any"
            params_needing_default.append(
                {
                    "name": p_node.arg,
                    "kind": "positional",
                    "index": pos_idx,
                    "annotation": ann_str,
                    "suggested_default": _suggested_default_for_annotation(ann_str),
                }
            )

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
                params_needing_default.append(
                    {
                        "name": p_node.arg,
                        "kind": "keyword",
                        "annotation": ann_str,
                        "suggested_default": _suggested_default_for_annotation(ann_str),
                    }
                )
        else:
            params_needing_default.append(
                {
                    "name": kw_name,
                    "kind": "new_keyword",
                    "annotation": "int",
                    "suggested_default": 0,
                }
            )

    if not params_needing_default:
        return None

    seed_overrides = {p["name"]: p["suggested_default"] for p in params_needing_default}
    temp_kwargs = dict(call_kwargs)
    for p in params_needing_default:
        if p["kind"] == "new_keyword" and p["name"] not in temp_kwargs:
            temp_kwargs[p["name"]] = p["suggested_default"]

    updated_source, _, added_params = generate_updated_signature_ast(
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
                "Set default value(s) below and execute."
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


def build_signature_mismatch_hal_recovery(
    target_name: str,
    serialized_ast: str,
    failed_params: dict[str, Any],
    call_args: list[Any],
    call_kwargs: dict[str, Any],
    type_error_msg: str,
) -> dict[str, Any]:
    """
    Build HATEOAS recovery when calling `target_name` fails with a signature mismatch `TypeError`.
    - If required positional/keyword-only arguments were omitted, triggers `set_default_value` recovery.
    - Otherwise (extra positional or unexpected keyword arguments), generates an updated signature
      with `_new_[A-Z0-9]` / named parameters and returns `signature_mismatch_autofixed`.
    """
    node = deserialize_ast_node(serialized_ast)
    pos_args = list(node.args.posonlyargs) + list(node.args.args)
    num_pos = len(pos_args)
    first_default_idx = num_pos - len(node.args.defaults)

    if "missing" in type_error_msg:
        omitted_pos_indices = [
            idx
            for idx in range(len(call_args), first_default_idx)
            if pos_args[idx].arg not in call_kwargs
        ]
        omitted_kw_names = [
            kw_node.arg
            for kw_idx, kw_node in enumerate(node.args.kwonlyargs)
            if (kw_idx >= len(node.args.kw_defaults) or node.args.kw_defaults[kw_idx] is None)
            and kw_node.arg not in call_kwargs
        ]
        missing_default_res = check_and_build_missing_default_recovery(
            target_name=target_name,
            serialized_ast=serialized_ast,
            failed_params=failed_params,
            call_args=call_args,
            call_kwargs=call_kwargs,
            omitted_pos_indices=omitted_pos_indices,
            omitted_kw_names=omitted_kw_names,
        )
        if missing_default_res is not None:
            return missing_default_res

    updated_source, updated_serialized_ast, added_params = generate_updated_signature_ast(
        name=target_name,
        serialized_ast=serialized_ast,
        call_args=call_args,
        call_kwargs=call_kwargs,
    )

    if not added_params:
        return build_hal_response(
            self_href=f"ast://resource/{target_name}/eval",
            data={
                "status": "error",
                "error_type": "runtime_type_error",
                "policy": "navigation_only_no_patch",
                "target": target_name,
                "message": type_error_msg,
            },
            links={
                "inspect_current": {"tool": "ast_read", "arguments": {"name": target_name}},
                "update_call": {
                    "tool": "ast_update",
                    "required_params": ["name", "code_or_ast"],
                    "target": target_name,
                },
            },
        )

    apply_patch_step = {
        "tool": "ast_update",
        "arguments": {
            "name": target_name,
            "code_or_ast": updated_serialized_ast,
            "expose_as_tool": True,
            "skip_roundtrip": False,
        },
    }
    retry_step = {
        "tool": "eval_ast_call",
        "arguments": failed_params,
    }

    return build_hal_response(
        self_href=f"ast://resource/{target_name}/signature-mismatch",
        data={
            "status": "error",
            "error_type": "signature_mismatch_autofixed",
            "policy": "autofixed_patch_ready",
            "target": target_name,
            "added_parameters": added_params,
            "message": f"Signature mismatch on `{target_name}` ({type_error_msg}). Patched signature ready.",
            "python_source": updated_source,
            "patched_resource": export_all_formats(target_name, updated_serialized_ast),
        },
        links={
            "apply_patched_update": apply_patch_step,
            "retry": retry_step,
            "recovery_chain": {
                "tool": "ast_chain",
                "arguments": {"commands": [apply_patch_step, retry_step]},
            },
        },
    )
