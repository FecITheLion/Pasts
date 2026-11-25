"""Static AST metrics, dependency graph analysis, unified diffs, and pytest generation."""

from __future__ import annotations

import ast
import difflib
from typing import Any

from .ast_core import deserialize_ast_node, find_called_names


def _max_nesting_depth(node: ast.AST, current: int = 0) -> int:
    nesting_types = (
        ast.If,
        ast.For,
        ast.AsyncFor,
        ast.While,
        ast.With,
        ast.AsyncWith,
        ast.Try,
    )
    max_d = current
    for child in ast.iter_child_nodes(node):
        next_depth = current + 1 if isinstance(child, nesting_types) else current
        max_d = max(max_d, _max_nesting_depth(child, next_depth))
    return max_d


def compute_function_metrics(name: str, serialized_ast: str) -> dict[str, Any]:
    """Compute cyclomatic complexity, nesting depth, annotation coverage, and call targets."""
    fn_node = deserialize_ast_node(serialized_ast)
    source = ast.unparse(fn_node)

    complexity = 1
    for sub in ast.walk(fn_node):
        if isinstance(sub, (ast.If, ast.While, ast.For, ast.AsyncFor, ast.ExceptHandler, ast.IfExp)):
            complexity += 1
        elif isinstance(sub, ast.BoolOp):
            complexity += max(0, len(sub.values) - 1)
        elif isinstance(sub, ast.comprehension):
            complexity += len(sub.ifs)
        elif hasattr(ast, "MatchCase") and isinstance(sub, ast.MatchCase):
            complexity += 1

    all_params = [
        *fn_node.args.posonlyargs,
        *fn_node.args.args,
        *fn_node.args.kwonlyargs,
    ]
    if fn_node.args.vararg:
        all_params.append(fn_node.args.vararg)
    if fn_node.args.kwarg:
        all_params.append(fn_node.args.kwarg)

    total_slots = len(all_params) + 1  # parameters + return annotation
    annotated_slots = sum(1 for p in all_params if p.annotation is not None) + (
        1 if fn_node.returns is not None else 0
    )
    type_coverage = round((annotated_slots / total_slots) * 100) if total_slots else 100

    return {
        "name": name,
        "cyclomatic_complexity": complexity,
        "max_nesting_depth": _max_nesting_depth(fn_node),
        "line_count": len(source.splitlines()),
        "param_count": len(all_params),
        "type_coverage_pct": type_coverage,
        "has_docstring": ast.get_docstring(fn_node) is not None,
        "calls": sorted(n for n in find_called_names(fn_node) if n != name),
    }


def compute_unified_diff(name: str, old_source: str, new_source: str) -> str:
    """Produce a readable unified diff between current and candidate Python source."""
    old_lines = old_source.splitlines(keepends=True)
    new_lines = new_source.splitlines(keepends=True)
    return "".join(
        difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile=f"{name}.py (current)",
            tofile=f"{name}.py (candidate)",
            n=3,
        )
    )


def build_dependency_graph(registry: dict[str, str]) -> dict[str, Any]:
    """Build directed call graph, reverse dependents, and cycle check across all AST nodes."""
    calls_map: dict[str, list[str]] = {}
    called_by_map: dict[str, list[str]] = {k: [] for k in registry}
    metrics_map: dict[str, dict[str, Any]] = {}

    for name, serialized in registry.items():
        node = deserialize_ast_node(serialized)
        deps = sorted(n for n in find_called_names(node) if n in registry and n != name)
        calls_map[name] = deps
        for dep in deps:
            called_by_map[dep].append(name)
        metrics_map[name] = compute_function_metrics(name, serialized)

    edges = [{"from": src, "to": dst} for src, deps in calls_map.items() for dst in deps]

    # Cycle detection via DFS
    visiting: set[str] = set()
    visited: set[str] = set()
    cycle_path: list[str] = []

    def _dfs(node_name: str, stack: list[str]) -> bool:
        visiting.add(node_name)
        stack.append(node_name)
        for nxt in calls_map.get(node_name, []):
            if nxt in visiting:
                idx = stack.index(nxt)
                cycle_path.extend([*stack[idx:], nxt])
                return True
            if nxt not in visited and _dfs(nxt, stack):
                return True
        stack.pop()
        visiting.remove(node_name)
        visited.add(node_name)
        return False

    for k in registry:
        if k not in visited and _dfs(k, []):
            break

    nodes = [
        {
            "name": name,
            "calls": calls_map[name],
            "called_by": sorted(called_by_map[name]),
            "metrics": metrics_map[name],
        }
        for name in registry
    ]
    return {
        "nodes": nodes,
        "edges": edges,
        "has_cycle": bool(cycle_path),
        "cycle_path": cycle_path,
    }


def sample_value_for_annotation(annotation_node: ast.expr | None, default_node: ast.expr | None) -> Any:
    """Infer a safe sample Python literal for starter smoke tests and pytest generation."""
    if default_node is not None:
        try:
            return ast.literal_eval(default_node)
        except Exception:
            pass
    if annotation_node is None:
        return 3
    ann_str = ast.unparse(annotation_node).lower()
    if "bool" in ann_str:
        return True
    if "float" in ann_str:
        return 2.5
    if "str" in ann_str:
        return "sample"
    if "list" in ann_str:
        return [1, 2]
    if "dict" in ann_str:
        return {"k": 1}
    return 3


def generate_default_test_cases(name: str, serialized_ast: str) -> list[dict[str, Any]]:
    """Build deterministic starter smoke-test inputs from function signature/defaults."""
    fn_node = deserialize_ast_node(serialized_ast)
    pos_args = list(fn_node.args.posonlyargs) + list(fn_node.args.args)
    defaults_offset = len(pos_args) - len(fn_node.args.defaults)
    sample_args: list[Any] = []
    for idx, arg_node in enumerate(pos_args):
        def_node = (
            fn_node.args.defaults[idx - defaults_offset]
            if idx >= defaults_offset
            else None
        )
        sample_args.append(sample_value_for_annotation(arg_node.annotation, def_node))

    sample_kwargs: dict[str, Any] = {}
    for kw_idx, kw_node in enumerate(fn_node.args.kwonlyargs):
        kw_def = fn_node.args.kw_defaults[kw_idx] if kw_idx < len(fn_node.args.kw_defaults) else None
        sample_kwargs[kw_node.arg] = sample_value_for_annotation(kw_node.annotation, kw_def)

    return [
        {
            "name": f"smoke_{name}_default",
            "args": sample_args,
            "kwargs": sample_kwargs,
        }
    ]


def generate_pytest_snippet(name: str, serialized_ast: str, sample_output: Any = None) -> str:
    """Generate a clean standalone pytest test file for the target AST function."""
    fn_node = deserialize_ast_node(serialized_ast)
    source = ast.unparse(fn_node)
    cases = generate_default_test_cases(name, serialized_ast)
    case0 = cases[0]
    call_parts = [repr(a) for a in case0["args"]] + [f"{k}={repr(v)}" for k, v in case0["kwargs"].items()]
    call_expr = f"{name}({', '.join(call_parts)})"

    lines = [
        f'"""Auto-generated pytest module for `{name}`."""',
        "",
        source,
        "",
        "",
        f"def test_{name}_smoke() -> None:",
        f"    result = {call_expr}",
    ]
    if sample_output is not None:
        lines.append(f"    assert result == {repr(sample_output)}")
    else:
        lines.append("    assert result is not None")
    lines.append("")
    return "\n".join(lines)
