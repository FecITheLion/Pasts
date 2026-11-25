"""Safe AST serialization, deserialization, roundtrip validation, and compilation."""

from __future__ import annotations

import ast
import inspect
import json
from typing import Any

_AST_NODE_CLASSES: dict[str, type[ast.AST]] = {
    name: getattr(ast, name)
    for name in dir(ast)
    if not name.startswith("_")
    and isinstance(getattr(ast, name), type)
    and issubclass(getattr(ast, name), ast.AST)
}


def _eval_ast_dump_expr(expr: ast.expr) -> Any:
    """Safely evaluate an `ast.dump()` expression tree without invoking Python `eval()`."""
    if isinstance(expr, ast.Constant):
        return expr.value

    if isinstance(expr, ast.UnaryOp) and isinstance(expr.op, (ast.UAdd, ast.USub)):
        operand = _eval_ast_dump_expr(expr.operand)
        if isinstance(operand, (int, float)) and not isinstance(operand, bool):
            return +operand if isinstance(expr.op, ast.UAdd) else -operand
        raise ValueError(f"Unsupported unary operand in AST dump: {type(operand).__name__}")

    if isinstance(expr, ast.List):
        return [_eval_ast_dump_expr(elt) for elt in expr.elts]

    if isinstance(expr, ast.Tuple):
        return tuple(_eval_ast_dump_expr(elt) for elt in expr.elts)

    if isinstance(expr, ast.Call):
        if expr.starargs if hasattr(expr, "starargs") else False:
            raise ValueError("Starred arguments are not permitted in AST dumps.")
        if isinstance(expr.func, ast.Name):
            cls_name = expr.func.id
        elif (
            isinstance(expr.func, ast.Attribute)
            and isinstance(expr.func.value, ast.Name)
            and expr.func.value.id == "ast"
        ):
            cls_name = expr.func.attr
        else:
            raise ValueError(f"Disallowed callable in AST dump: {ast.dump(expr.func)}")

        node_cls = _AST_NODE_CLASSES.get(cls_name)
        if node_cls is None:
            raise ValueError(f"Unknown or disallowed AST node class: `{cls_name}`")

        args: list[Any] = []
        for arg in expr.args:
            if isinstance(arg, ast.Starred):
                raise ValueError("Starred positional arguments are not permitted in AST dumps.")
            args.append(_eval_ast_dump_expr(arg))

        kwargs: dict[str, Any] = {}
        for kw in expr.keywords:
            if kw.arg is None:
                raise ValueError("Double-starred keyword arguments are not permitted in AST dumps.")
            kwargs[kw.arg] = _eval_ast_dump_expr(kw.value)

        return node_cls(*args, **kwargs)

    raise ValueError(f"Disallowed expression node `{type(expr).__name__}` in serialized AST.")


def safe_deserialize_ast_dump(serialized_ast: str) -> ast.AST:
    """Parse an `ast.dump()` string safely without `eval()`."""
    expr_ast = ast.parse(serialized_ast, mode="eval")
    node = _eval_ast_dump_expr(expr_ast.body)
    if not isinstance(node, ast.AST):
        raise TypeError(f"Expected ast.AST node, got {type(node).__name__}")
    return ast.fix_missing_locations(node)


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


def ast_to_dict(node: Any) -> Any:
    if isinstance(node, ast.AST):
        result: dict[str, Any] = {"_type": type(node).__name__}
        for field_name in node._fields:
            result[field_name] = ast_to_dict(getattr(node, field_name))
        return result
    if isinstance(node, list):
        return [ast_to_dict(item) for item in node]
    return node


def dict_to_ast(data: Any) -> Any:
    if isinstance(data, list):
        return [dict_to_ast(item) for item in data]
    if isinstance(data, dict) and "_type" in data:
        node_cls = _AST_NODE_CLASSES.get(str(data["_type"]))
        if node_cls is None:
            raise ValueError(f"Unknown AST node type: {data['_type']}")
        fields = {k: dict_to_ast(v) for k, v in data.items() if k != "_type"}
        return node_cls(**fields)
    return data


def deserialize_ast_node(serialized_ast: str) -> ast.FunctionDef:
    node = safe_deserialize_ast_dump(serialized_ast)
    if not isinstance(node, ast.FunctionDef):
        raise TypeError(f"Expected ast.FunctionDef, got {type(node).__name__}")
    return node


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
            "json_ast": ast_to_dict(node),
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
    from_json = dict_to_ast(json.loads(json_blob))
    if not isinstance(from_json, ast.FunctionDef):
        raise ValueError("Roundtrip failed at `json_ast`: did not reconstruct a FunctionDef.")
    ast.fix_missing_locations(from_json)
    if ast.dump(from_json) != baseline_dump:
        raise ValueError("Roundtrip mismatch after `json_ast` (`ast_to_dict` -> `dict_to_ast`).")
    stages_verified.append("json_ast")

    return {
        "name": name,
        "verified": True,
        "stages": stages_verified,
        "canonical_serialized_ast": baseline_dump,
        "formats": formats,
    }


def find_called_names(node: ast.AST) -> set[str]:
    called: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name):
            called.add(sub.func.id)
    return called


def collect_all_identifiers(node: ast.AST) -> set[str]:
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


def parse_expression_or_ast(expr_or_ast: str) -> ast.Expression:
    stripped = expr_or_ast.strip()
    if stripped.startswith("{"):
        node = dict_to_ast(json.loads(stripped))
    else:
        parsed = ast.parse(stripped, mode="eval")
        if (
            isinstance(parsed.body, ast.Call)
            and isinstance(parsed.body.func, ast.Name)
            and parsed.body.func.id in _AST_NODE_CLASSES
        ):
            node = safe_deserialize_ast_dump(stripped)
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
