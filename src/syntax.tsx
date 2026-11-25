import React from "react";

export type HighlightFormat = "python" | "json" | "ast_dump" | "diff";

interface TokenSpan {
  text: string;
  className?: string;
}

const PY_KEYWORDS = new Set([
  "False",
  "None",
  "True",
  "and",
  "as",
  "assert",
  "async",
  "await",
  "break",
  "class",
  "continue",
  "def",
  "del",
  "elif",
  "else",
  "except",
  "finally",
  "for",
  "from",
  "global",
  "if",
  "import",
  "in",
  "is",
  "lambda",
  "match",
  "case",
  "nonlocal",
  "not",
  "or",
  "pass",
  "raise",
  "return",
  "try",
  "while",
  "with",
  "yield",
]);

const PY_BUILTINS = new Set([
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
  "BaseModel",
  "Field",
  "dataclass",
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
  "isinstance",
]);

export function detectHighlightFormat(text: string, hint?: string): HighlightFormat {
  if (hint === "diff" || hint === "unified_diff") return "diff";
  if (hint === "json_ast" || hint === "json_schema" || hint === "json") return "json";
  if (hint === "serialized_ast" || hint === "pretty_ast" || hint === "ast_dump") return "ast_dump";
  if (hint === "python_source" || hint === "python") return "python";
  const trimmed = text.trim();
  if (/^(--- |\+\+\+ |@@ )/m.test(trimmed)) return "diff";
  if (trimmed.startsWith("{") || trimmed.startsWith("[")) return "json";
  if (/^(Module|FunctionDef|AsyncFunctionDef|ClassDef|Expression)\s*\(/.test(trimmed)) {
    return "ast_dump";
  }
  return "python";
}

function tokenizeDiff(src: string): TokenSpan[] {
  const spans: TokenSpan[] = [];
  const lines = src.split("\n");
  lines.forEach((ln, idx) => {
    const suffix = idx < lines.length - 1 ? "\n" : "";
    if (ln.startsWith("+++") || ln.startsWith("---")) {
      spans.push({ text: ln + suffix, className: "text-slate-700 font-bold" });
    } else if (ln.startsWith("@@")) {
      spans.push({ text: ln + suffix, className: "text-sky-700 font-semibold bg-sky-50" });
    } else if (ln.startsWith("+")) {
      spans.push({ text: ln + suffix, className: "text-emerald-800 bg-emerald-50" });
    } else if (ln.startsWith("-")) {
      spans.push({ text: ln + suffix, className: "text-red-800 bg-red-50" });
    } else {
      spans.push({ text: ln + suffix, className: "text-slate-700" });
    }
  });
  return spans;
}

function tokenizePython(src: string): TokenSpan[] {
  const spans: TokenSpan[] = [];
  const tokenRe =
    /(#[^\n]*)|(@[A-Za-z_][A-Za-z0-9_.]*)|((?:[fFrRbBuU]{0,2})(?:"""[\s\S]*?"""|'''[\s\S]*?'''|"(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*'))|(0[xXoObB][0-9A-Fa-f_]+|\b\d+(?:_\d+)*(?:\.\d+(?:_\d+)*)?(?:[eE][+-]?\d+)?\b)|(\b[A-Za-z_][A-Za-z0-9_]*\b)|([+\-*/%=!<>&|^~@:]+)|(\s+)/g;
  let lastIdx = 0;
  let prevSigTok = "";
  let m: RegExpExecArray | null;

  while ((m = tokenRe.exec(src)) !== null) {
    if (m.index > lastIdx) {
      spans.push({ text: src.slice(lastIdx, m.index) });
    }
    const full = m[0];
    if (m[1]) {
      spans.push({ text: full, className: "text-slate-500 italic" });
    } else if (m[2]) {
      spans.push({ text: full, className: "text-pink-700 font-semibold" });
    } else if (m[3]) {
      spans.push({ text: full, className: "text-emerald-700" });
      prevSigTok = full;
    } else if (m[4]) {
      spans.push({ text: full, className: "text-orange-700" });
      prevSigTok = full;
    } else if (m[5]) {
      if (full === "True" || full === "False" || full === "None") {
        spans.push({ text: full, className: "text-purple-700 font-semibold" });
      } else if (PY_KEYWORDS.has(full)) {
        spans.push({ text: full, className: "text-violet-700 font-semibold" });
      } else if (prevSigTok === "def") {
        spans.push({ text: full, className: "text-sky-700 font-semibold" });
      } else if (prevSigTok === "class") {
        spans.push({ text: full, className: "text-amber-700 font-semibold" });
      } else if (PY_BUILTINS.has(full)) {
        spans.push({ text: full, className: "text-teal-700" });
      } else {
        spans.push({ text: full });
      }
      prevSigTok = full;
    } else if (m[6]) {
      spans.push({ text: full, className: "text-slate-600" });
      prevSigTok = full;
    } else {
      spans.push({ text: full });
    }
    lastIdx = tokenRe.lastIndex;
  }
  if (lastIdx < src.length) {
    spans.push({ text: src.slice(lastIdx) });
  }
  return spans;
}

function tokenizeJson(src: string): TokenSpan[] {
  const spans: TokenSpan[] = [];
  const tokenRe =
    /("(?:\\.|[^"\\])*")(\s*:)?|(\b(?:true|false|null)\b)|(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)|([{}[\],:])|(\s+)/g;
  let lastIdx = 0;
  let m: RegExpExecArray | null;

  while ((m = tokenRe.exec(src)) !== null) {
    if (m.index > lastIdx) {
      spans.push({ text: src.slice(lastIdx, m.index) });
    }
    if (m[1] && m[2]) {
      spans.push({ text: m[1], className: "text-sky-700 font-semibold" });
      spans.push({ text: m[2], className: "text-slate-600" });
    } else if (m[1]) {
      spans.push({ text: m[1], className: "text-emerald-700" });
    } else if (m[3]) {
      spans.push({ text: m[3], className: "text-purple-700 font-semibold" });
    } else if (m[4]) {
      spans.push({ text: m[4], className: "text-orange-700" });
    } else if (m[5]) {
      spans.push({ text: m[5], className: "text-slate-600" });
    } else {
      spans.push({ text: m[0] });
    }
    lastIdx = tokenRe.lastIndex;
  }
  if (lastIdx < src.length) {
    spans.push({ text: src.slice(lastIdx) });
  }
  return spans;
}

function tokenizeAstDump(src: string): TokenSpan[] {
  const spans: TokenSpan[] = [];
  const tokenRe =
    /("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')|(\b[A-Za-z_][A-Za-z0-9_]*)(?=\s*\()|(\b[A-Za-z_][A-Za-z0-9_]*)(?=\s*=)|(\b(?:True|False|None)\b)|(-?\d+(?:\.\d+)?)|([()[\],=])|(\s+)/g;
  let lastIdx = 0;
  let m: RegExpExecArray | null;

  while ((m = tokenRe.exec(src)) !== null) {
    if (m.index > lastIdx) {
      spans.push({ text: src.slice(lastIdx, m.index) });
    }
    if (m[1]) {
      spans.push({ text: m[1], className: "text-emerald-700" });
    } else if (m[2]) {
      spans.push({ text: m[2], className: "text-sky-700 font-semibold" });
    } else if (m[3]) {
      spans.push({ text: m[3], className: "text-violet-700" });
    } else if (m[4]) {
      spans.push({ text: m[4], className: "text-purple-700 font-semibold" });
    } else if (m[5]) {
      spans.push({ text: m[5], className: "text-orange-700" });
    } else if (m[6]) {
      spans.push({ text: m[6], className: "text-slate-600" });
    } else {
      spans.push({ text: m[0] });
    }
    lastIdx = tokenRe.lastIndex;
  }
  if (lastIdx < src.length) {
    spans.push({ text: src.slice(lastIdx) });
  }
  return spans;
}

export function HighlightedCode({
  code,
  formatHint,
}: {
  code: string;
  formatHint?: string;
}): React.ReactElement {
  const fmt = detectHighlightFormat(code, formatHint);
  const spans =
    fmt === "diff"
      ? tokenizeDiff(code)
      : fmt === "json"
        ? tokenizeJson(code)
        : fmt === "ast_dump"
          ? tokenizeAstDump(code)
          : tokenizePython(code);

  return (
    <>
      {spans.map((span, idx) =>
        span.className ? (
          <span key={idx} className={span.className}>
            {span.text}
          </span>
        ) : (
          <React.Fragment key={idx}>{span.text}</React.Fragment>
        )
      )}
    </>
  );
}
