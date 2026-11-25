"""Full-page Python script wrapper, AST auto-fix transformer, and source linter."""

from __future__ import annotations

import ast
import copy
import io
import json
import re
import tokenize
from dataclasses import dataclass
from typing import Any

from .ast_core import (
    dict_to_ast,
    find_called_names,
    safe_deserialize_ast_dump,
    serialize_function,
)

_BLOCK_HEADER_RE = re.compile(
    r"^(\s*(?:def|async\s+def|class|if|elif|else|for|async\s+for|while|try|except|finally|with|async\s+with|match|case)\b.*?)"
    r"(\s*(?:#.*)?)$"
)
_COND_ASSIGN_RE = re.compile(r"^(\s*(?:if|elif|while)\s+[^=:<>\n]+?)\s*=\s*([^=:\n]+:.*)$")


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


def wrap_module_script_as_function(
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
    new_lines: list[str] = []
    for lineno, line in enumerate(lines, start=1):
        code_part, _, comment_part = line.partition("#")
        code_stripped = code_part.strip()
        if (
            code_stripped
            and not code_stripped.endswith((":", "\\", "(", "[", "{", ","))
            and _BLOCK_HEADER_RE.match(line)
        ):
            m = _BLOCK_HEADER_RE.match(line)
            assert m is not None
            line = f"{m.group(1).rstrip()}:{m.group(2)}"
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
        cm = _COND_ASSIGN_RE.match(line)
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

    # Intentional: direct function calls are excluded here so _compile_needed can raise
    # MissingASTCallError and emit a HATEOAS blank-shell recovery plan.
    called_func_names = find_called_names(fn_node)

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
        elif hasattr(ast, "MatchAs") and isinstance(sub, ast.MatchAs) and sub.name:
            bound_names.add(sub.name)
        elif hasattr(ast, "MatchStar") and isinstance(sub, ast.MatchStar) and sub.name:
            bound_names.add(sub.name)
        elif hasattr(ast, "MatchMapping") and isinstance(sub, ast.MatchMapping) and sub.rest:
            bound_names.add(sub.rest)

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
        node = dict_to_ast(payload)
        if isinstance(node, ast.Module):
            fn_node = wrap_module_script_as_function(name, node, issues)
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
                node = safe_deserialize_ast_dump(stripped)
                if isinstance(node, ast.FunctionDef):
                    fn_node = node
                    original_source = ast.unparse(fn_node)
                elif isinstance(node, ast.Module):
                    fn_node = wrap_module_script_as_function(name, node, issues)
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
                fn_node = wrap_module_script_as_function(name, mod, issues)
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
        node = dict_to_ast(payload)
        if isinstance(node, ast.Module):
            wrapped = wrap_module_script_as_function(fallback_name, node, [])
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
            wrapped = wrap_module_script_as_function(fallback_name, mod, [])
            return serialize_function(ast.fix_missing_locations(wrapped))
    except SyntaxError:
        pass
    node = safe_deserialize_ast_dump(stripped)
    if isinstance(node, ast.Module):
        wrapped = wrap_module_script_as_function(fallback_name, node, [])
        return serialize_function(ast.fix_missing_locations(wrapped))
    if not isinstance(node, ast.FunctionDef):
        raise ValueError("Input did not resolve to a valid FunctionDef")
    return serialize_function(node)
