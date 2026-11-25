"""Dependency inspection, circularity exceptions, package validation, and fallback stub generation."""

from __future__ import annotations

import ast
import importlib.util
import re
import sys
from functools import lru_cache
from importlib.metadata import packages_distributions
from typing import Any

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

_PEP508_PACKAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class MissingASTCallError(Exception):
    def __init__(self, missing_name: str, caller_context: str):
        super().__init__(f"Missing AST function '{missing_name}' referenced by '{caller_context}'")
        self.missing_name = missing_name
        self.caller_context = caller_context


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


@lru_cache(maxsize=1)
def _cached_packages_distributions() -> dict[str, list[str]]:
    return dict(packages_distributions())


def invalidate_package_cache() -> None:
    _cached_packages_distributions.cache_clear()


def resolve_package_name(module_name: str) -> str:
    pkg_map = _cached_packages_distributions()
    if module_name in pkg_map and pkg_map[module_name]:
        return pkg_map[module_name][0]
    return COMMON_IMPORT_TO_PACKAGE.get(module_name, module_name)


def validate_package_name(package_name: str) -> str:
    """Validate that a package name is a safe PEP 508 distribution identifier (no pip flags/URLs)."""
    clean = package_name.strip()
    if not clean or clean.startswith("-") or not _PEP508_PACKAGE_RE.match(clean):
        raise ValueError(
            f"Invalid package name `{package_name}`. Must match `[A-Za-z0-9][A-Za-z0-9._-]*`."
        )
    return clean


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
