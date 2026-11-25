"""Cross-platform ANSI color formatting and structured console logging."""

from __future__ import annotations

import io
import keyword
import os
import tokenize


def _enable_windows_ansi() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.name != "nt":
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        for handle_id in (-11, -12):  # STD_OUTPUT_HANDLE, STD_ERROR_HANDLE
            handle = kernel32.GetStdHandle(handle_id)
            mode = ctypes.c_uint32()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                kernel32.SetConsoleMode(handle, mode.value | 0x0004)
        return True
    except Exception:
        return False


ANSI_ENABLED = _enable_windows_ansi()

ANSI_CODES: dict[str, str] = {
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

SOURCE_COLORS: dict[str, str] = {
    "stdin": "yellow",
    "rest": "cyan",
    "rest_cli": "cyan",
    "http": "cyan",
    "mcp": "magenta",
    "mcp_resource": "magenta",
    "gemini_mcp": "purple",
}

ROUTE_META: dict[str, tuple[str, str]] = {
    "ast_create": ("AST_REGISTRY", "blue"),
    "ast_read": ("AST_REGISTRY", "blue"),
    "ast_update": ("AST_REGISTRY", "blue"),
    "ast_delete": ("AST_REGISTRY", "blue"),
    "eval_ast_call": ("EVAL_RUNTIME", "green"),
    "ast_convert": ("CONVERTER", "teal"),
    "ast_chain": ("CHAIN_ORCHESTRATOR", "purple"),
    "ast_install_module": ("MODULE_ENV", "white"),
    "ast_write_module_file": ("MODULE_ENV", "white"),
    "ast_generate_line": ("GEMINI_LINE", "cyan"),
    "ast_run_tests": ("TEST_RUNNER", "green"),
    "ast_analyze": ("ANALYZER", "teal"),
    "ast_rollback": ("VERSION_HISTORY", "yellow"),
}

_BUILTIN_TYPE_NAMES = frozenset(
    {
        "int",
        "float",
        "str",
        "bool",
        "list",
        "dict",
        "set",
        "tuple",
        "bytes",
        "object",
        "type",
        "Any",
        "Optional",
        "Union",
        "len",
        "range",
        "print",
        "sum",
        "min",
        "max",
        "abs",
        "round",
        "sorted",
        "enumerate",
        "zip",
        "map",
        "filter",
        "isinstance",
    }
)


def colorize(text: str, color_key: str) -> str:
    """Wrap text with ANSI escape codes if terminal color output is enabled."""
    if not ANSI_ENABLED or color_key not in ANSI_CODES:
        return text
    return f"{ANSI_CODES[color_key]}{text}{ANSI_CODES['reset']}"


def highlight_python_ansi(source: str) -> str:
    """Apply lightweight ANSI syntax highlighting to Python source using stdlib tokenize."""
    if not ANSI_ENABLED or not source:
        return source

    lines = source.splitlines(keepends=True)
    if not lines:
        return source

    spans: list[tuple[tuple[int, int], tuple[int, int], str]] = []
    prev_tok_str = ""
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, IndentationError):
        return source

    for tok_type, tok_str, start, end, _ in tokens:
        if start == end:
            continue
        color_key = ""
        if tok_type == tokenize.COMMENT:
            color_key = "dim"
        elif tok_type == tokenize.STRING:
            color_key = "green"
        elif tok_type == tokenize.NUMBER:
            color_key = "yellow"
        elif tok_type == tokenize.OP:
            if tok_str == "@":
                color_key = "magenta"
            else:
                color_key = "cyan"
        elif tok_type == tokenize.NAME:
            if keyword.iskeyword(tok_str) or tok_str in ("True", "False", "None"):
                color_key = "magenta"
            elif prev_tok_str in ("def", "class", "@"):
                color_key = "blue"
            elif tok_str in _BUILTIN_TYPE_NAMES:
                color_key = "teal"
        if color_key:
            spans.append((start, end, color_key))
        if tok_type not in (tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT, tokenize.COMMENT):
            prev_tok_str = tok_str

    if not spans:
        return source

    line_offsets: list[int] = []
    offset = 0
    for ln in lines:
        line_offsets.append(offset)
        offset += len(ln)

    def _to_offset(pos: tuple[int, int]) -> int:
        row, col = pos
        if row - 1 >= len(line_offsets):
            return len(source)
        return line_offsets[row - 1] + col

    out: list[str] = []
    cursor = 0
    for start, end, color_key in spans:
        s_idx = _to_offset(start)
        e_idx = _to_offset(end)
        if s_idx < cursor or e_idx <= s_idx:
            continue
        if s_idx > cursor:
            out.append(source[cursor:s_idx])
        out.append(colorize(source[s_idx:e_idx], color_key))
        cursor = e_idx
    if cursor < len(source):
        out.append(source[cursor:])
    return "".join(out)

