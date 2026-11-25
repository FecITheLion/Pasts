"""Bidirectional structure converter across AST, JSON, JSON Schema, Pydantic, and Dataclass."""

from __future__ import annotations

import ast
import inspect
import json
from dataclasses import MISSING, field, fields as dc_fields, is_dataclass, make_dataclass
from typing import Any, Optional, Union

from .ast_core import dict_to_ast, safe_deserialize_ast_dump

try:
    from pydantic import BaseModel, create_model

    HAS_PYDANTIC = True
except Exception:
    BaseModel = None  # type: ignore[assignment]
    create_model = None  # type: ignore[assignment]
    HAS_PYDANTIC = False


_BASE_TYPES: dict[str, Any] = {
    "int": int,
    "float": float,
    "str": str,
    "bool": bool,
    "dict": dict,
    "list": list,
    "Any": Any,
    "None": type(None),
    "NoneType": type(None),
}


def _resolve_type_ast(expr: ast.expr) -> Any:
    if isinstance(expr, ast.Name):
        return _BASE_TYPES.get(expr.id, Any)
    if isinstance(expr, ast.Constant) and expr.value is None:
        return type(None)
    if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.BitOr):
        left = _resolve_type_ast(expr.left)
        right = _resolve_type_ast(expr.right)
        return Union[left, right]
    if isinstance(expr, ast.Subscript) and isinstance(expr.value, ast.Name):
        container = expr.value.id
        if container == "Optional":
            return Optional[_resolve_type_ast(expr.slice)]
        if container == "list":
            return list[_resolve_type_ast(expr.slice)]  # type: ignore[misc]
        if container == "dict" and isinstance(expr.slice, ast.Tuple) and len(expr.slice.elts) == 2:
            k_t = _resolve_type_ast(expr.slice.elts[0])
            v_t = _resolve_type_ast(expr.slice.elts[1])
            return dict[k_t, v_t]  # type: ignore[misc]
        if container == "Union" and isinstance(expr.slice, ast.Tuple):
            args = tuple(_resolve_type_ast(elt) for elt in expr.slice.elts)
            return Union[args]  # type: ignore[valid-type]
    return Any


def _py_type_from_str(type_str: str) -> Any:
    if type_str in _BASE_TYPES:
        return _BASE_TYPES[type_str]
    try:
        expr = ast.parse(type_str, mode="eval").body
        return _resolve_type_ast(expr)
    except Exception:
        return Any


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
        return ast.fix_missing_locations(dict_to_ast(payload))
    stripped = payload.strip()
    if stripped.startswith("{"):
        return ast.fix_missing_locations(dict_to_ast(json.loads(stripped)))
    if stripped.startswith(("FunctionDef(", "ClassDef(", "Module(")):
        return safe_deserialize_ast_dump(stripped)
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
