/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 */

import type { McpToolInfo, RegistryItem, SuggestionItem } from "./types.ts";

interface ParamMeta {
  name: string;
  type: string;
  hasDefault: boolean;
  defaultRaw?: string;
}

function getTargetFnMeta(targetName: string, items: RegistryItem[]) {
  const match = items.find((i) => i.name === targetName);
  if (!match) return null;
  const src = match.formats?.python_source || "";
  const fnMatch = src.match(/^\s*def\s+[A-Za-z_][A-Za-z0-9_]*\s*\(([^)]*)\)/m);
  const params: ParamMeta[] = [];
  if (fnMatch && fnMatch[1].trim()) {
    for (const rawPart of fnMatch[1].split(",")) {
      const cleaned = rawPart.trim();
      if (!cleaned || cleaned.startsWith("*")) continue;
      const [left, defPart] = cleaned.split("=");
      const paramName = left.split(":")[0].trim();
      const typeHint = (left.split(":")[1] || "").trim();
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

function sampleValueForParam(param: ParamMeta, idx: number): unknown {
  if (param.hasDefault && param.defaultRaw) {
    try {
      return JSON.parse(param.defaultRaw.replace(/'/g, '"'));
    } catch {
      if (/^-?\d+(\.\d+)?$/.test(param.defaultRaw)) {
        return Number(param.defaultRaw);
      }
    }
  }
  const t = (param.type || "").toLowerCase();
  if (t.includes("float")) return 2.5 + idx;
  if (t.includes("int")) return 3 + idx;
  if (t.includes("bool")) return true;
  if (t.includes("str")) return `sample_${idx + 1}`;
  if (t.includes("list")) return [1, 2];
  if (t.includes("dict")) return { k: 1 };
  return 3 + idx;
}

export function extractLocalIdentifiers(code: string): string[] {
  const found = new Set<string>();
  const defMatch = code.matchAll(/def\s+[A-Za-z_][A-Za-z0-9_]*\s*\(([^)]*)\)/g);
  for (const m of defMatch) {
    for (const raw of (m[1] || "").split(",")) {
      const nm = raw.trim().replace(/^\*+/, "").split(":")[0].split("=")[0].trim();
      if (nm && nm !== "self" && nm !== "cls") found.add(nm);
    }
  }
  const assignMatch = code.matchAll(/^\s*([A-Za-z_][A-Za-z0-9_]*)\s*(?::[^=]+)?=/gm);
  for (const m of assignMatch) {
    if (m[1]) found.add(m[1]);
  }
  const forMatch = code.matchAll(/^\s*for\s+([A-Za-z_][A-Za-z0-9_]*)\s+in\b/gm);
  for (const m of forMatch) {
    if (m[1]) found.add(m[1]);
  }
  return [...found];
}

export function buildNameSuggestions(
  value: string,
  tool: string,
  items: RegistryItem[]
): SuggestionItem[] {
  const pool: SuggestionItem[] = [];
  for (const item of items) {
    const meta = getTargetFnMeta(item.name, items);
    const sig = meta ? `(${meta.params.map((p) => p.name).join(", ")})` : "";
    pool.push({ insert: item.name, label: `${item.name}${sig}`, meta: "AST fn" });
    if (tool === "eval_ast_call") {
      pool.push({ insert: `${item.name}(3)`, label: `${item.name}(3)`, meta: "expr call" });
    }
  }
  if (tool === "eval_ast_call") {
    pool.push({
      insert: "ast1(4) + ast2(2)",
      label: "ast1(4) + ast2(2)",
      meta: "expr",
    });
  }
  const q = value.trim().toLowerCase();
  return q
    ? pool.filter(
        (s) => s.label.toLowerCase().includes(q) || s.insert.toLowerCase().includes(q)
      )
    : pool;
}

export function buildArgsSuggestions(
  value: string,
  targetName: string,
  items: RegistryItem[]
): SuggestionItem[] {
  const meta = getTargetFnMeta(targetName.trim(), items);
  const suggestions: SuggestionItem[] = [];

  if (meta && meta.params.length > 0) {
    const posSamples = meta.params.map((p, i) => sampleValueForParam(p, i));
    const objSample: Record<string, unknown> = {};
    meta.params.forEach((p, i) => {
      objSample[p.name] = posSamples[i];
    });

    suggestions.push({
      insert: JSON.stringify(posSamples),
      label: JSON.stringify(posSamples),
      meta: `${meta.name} list`,
    });
    suggestions.push({
      insert: JSON.stringify(objSample),
      label: JSON.stringify(objSample),
      meta: `${meta.name} kwargs`,
    });
    if (posSamples.length === 1) {
      suggestions.push({
        insert: JSON.stringify(posSamples[0]),
        label: JSON.stringify(posSamples[0]),
        meta: "literal arg",
      });
    }
    suggestions.push({
      insert: JSON.stringify([posSamples[0], { extra_kw: 1 }]),
      label: JSON.stringify([posSamples[0], { extra_kw: 1 }]),
      meta: "list + kwargs",
    });
  }

  const defaults: SuggestionItem[] = [
    { insert: "3", label: "3", meta: "literal int" },
    { insert: "[3, 4]", label: "[3, 4]", meta: "list args" },
    { insert: '[3, {"k": 1}]', label: '[3, {"k": 1}]', meta: "list + kwargs" },
    { insert: '{"n": 3}', label: '{"n": 3}', meta: "object kwargs" },
    { insert: "[]", label: "[]", meta: "empty args" },
  ];
  for (const d of defaults) {
    if (!suggestions.some((s) => s.insert === d.insert)) {
      suggestions.push(d);
    }
  }

  const q = value.trim().toLowerCase();
  return q
    ? suggestions.filter(
        (s) => s.label.toLowerCase().includes(q) || s.meta.toLowerCase().includes(q)
      )
    : suggestions;
}

export function buildLinePromptSuggestions(
  value: string,
  targetName: string,
  items: RegistryItem[]
): SuggestionItem[] {
  const otherFn =
    items.find((i) => i.name !== targetName.trim())?.name || items[0]?.name || "ast1";
  const targetMeta = getTargetFnMeta(targetName.trim(), items);
  const firstParam = targetMeta?.params[0]?.name || "n";

  const pool: SuggestionItem[] = [
    {
      insert: `return ${otherFn}(${firstParam}) + 1`,
      label: `return ${otherFn}(${firstParam}) + 1`,
      meta: "call AST fn",
    },
    {
      insert: `validate ${firstParam} >= 0 else raise ValueError`,
      label: `validate ${firstParam} >= 0 else raise ValueError`,
      meta: "guard",
    },
    {
      insert: `print debug trace for ${firstParam}`,
      label: `print debug trace for ${firstParam}`,
      meta: "stdout log",
    },
    {
      insert: `handle None fallback for ${firstParam}`,
      label: `handle None fallback for ${firstParam}`,
      meta: "null-safe",
    },
    {
      insert: `loop range(${firstParam}) and accumulate total`,
      label: `loop range(${firstParam}) and accumulate total`,
      meta: "loop",
    },
    {
      insert: `return typed dict summary of ${firstParam}`,
      label: `return typed dict summary of ${firstParam}`,
      meta: "return dict",
    },
  ];

  const q = value.trim().toLowerCase();
  return q
    ? pool.filter(
        (s) => s.label.toLowerCase().includes(q) || s.meta.toLowerCase().includes(q)
      )
    : pool;
}

export function buildChatPromptSuggestions(
  value: string,
  items: RegistryItem[],
  mcpTools: McpToolInfo[]
): SuggestionItem[] {
  const fnNames = items.map((i) => i.name);
  const primaryFn = fnNames.includes("entrypoint") ? "entrypoint" : fnNames[0] || "entrypoint";

  const pool: SuggestionItem[] = [
    {
      insert: `List registered AST functions and analyze ${primaryFn} metrics`,
      label: `List registered AST functions and analyze ${primaryFn} metrics`,
      meta: "workflow",
    },
    {
      insert: `Run smoke tests on ${primaryFn} and evaluate ${primaryFn}(4)`,
      label: `Run smoke tests on ${primaryFn} and evaluate ${primaryFn}(4)`,
      meta: "test & eval",
    },
    {
      insert: "Create a fibonacci(n: int) AST function and test it with n=6",
      label: "Create a fibonacci(n: int) AST function and test it with n=6",
      meta: "create + test",
    },
    {
      insert: `Preview refactoring ${primaryFn} to validate non-negative inputs`,
      label: `Preview refactoring ${primaryFn} to validate non-negative inputs`,
      meta: "refactor",
    },
  ];

  for (const t of mcpTools) {
    pool.push({
      insert: `Call MCP tool ${t.name}`,
      label: `${t.name} — ${t.description.slice(0, 48)}`,
      meta: t.dynamic_ast_function ? "AST tool" : "MCP tool",
    });
  }

  const raw = value.trim().toLowerCase();
  const q = raw.startsWith("/") ? raw.slice(1) : raw;
  return q
    ? pool.filter(
        (s) =>
          s.label.toLowerCase().includes(q) ||
          s.insert.toLowerCase().includes(q) ||
          s.meta.toLowerCase().includes(q)
      )
    : pool;
}

export function getCaretTokenRange(value: string, caretPos: number) {
  const before = value.slice(0, caretPos);
  const match = before.match(/[A-Za-z_][A-Za-z0-9_]*$/);
  return {
    token: match ? match[0] : "",
    tokenStart: match ? caretPos - match[0].length : caretPos,
    caretPos,
  };
}

export function buildPythonTextareaSuggestions(
  token: string,
  code: string,
  items: RegistryItem[]
): SuggestionItem[] {
  const pool: SuggestionItem[] = [];

  for (const id of extractLocalIdentifiers(code)) {
    pool.push({ insert: id, label: id, meta: "local" });
  }

  for (const item of items) {
    const fnMeta = getTargetFnMeta(item.name, items);
    const paramCall = fnMeta?.params.length
      ? `${item.name}(${fnMeta.params.map((p, i) => JSON.stringify(sampleValueForParam(p, i))).join(", ")})`
      : `${item.name}()`;
    pool.push({ insert: item.name, label: item.name, meta: "AST symbol" });
    pool.push({ insert: paramCall, label: paramCall, meta: "AST call" });
  }

  const snippets: SuggestionItem[] = [
    {
      insert: "def fn(x: int = 3) -> int:\n    return x * 2",
      label: "def fn(x: int = 3) -> int: ...",
      meta: "snippet",
    },
    {
      insert: 'if x < 0:\n    raise ValueError("must be non-negative")',
      label: "if x < 0: raise ValueError(...)",
      meta: "guard",
    },
    {
      insert: "for i in range(n):\n    total += i",
      label: "for i in range(n): ...",
      meta: "loop",
    },
    {
      insert: "try:\n    pass\nexcept Exception as exc:\n    raise",
      label: "try / except Exception",
      meta: "snippet",
    },
    { insert: "return ", label: "return ...", meta: "keyword" },
    { insert: "isinstance(", label: "isinstance(obj, type)", meta: "builtin" },
    { insert: "print(", label: "print(...)", meta: "builtin" },
    { insert: "import math", label: "import math", meta: "import" },
    { insert: "ValueError", label: "ValueError", meta: "exception" },
    { insert: "None", label: "None", meta: "literal" },
    { insert: "True", label: "True", meta: "literal" },
    { insert: "False", label: "False", meta: "literal" },
  ];
  pool.push(...snippets);

  const q = token.toLowerCase();
  const seen = new Set<string>();
  const deduped = pool.filter((s) => {
    const k = `${s.insert}|${s.meta}`;
    if (seen.has(k)) return false;
    seen.add(k);
    return true;
  });

  return q
    ? deduped.filter(
        (s) => s.label.toLowerCase().includes(q) || s.insert.toLowerCase().includes(q)
      )
    : deduped;
}

export function buildJsonBodySuggestions(
  token: string,
  items: RegistryItem[]
): SuggestionItem[] {
  const regNames = items.map((i) => i.name);
  const pool: SuggestionItem[] = [
    { insert: '"code_or_ast"', label: '"code_or_ast"', meta: "JSON key" },
    { insert: '"name"', label: '"name"', meta: "JSON key" },
    { insert: '"args"', label: '"args"', meta: "JSON key" },
    { insert: '"kwargs"', label: '"kwargs"', meta: "JSON key" },
    { insert: '"skip_roundtrip": true', label: '"skip_roundtrip": true', meta: "bypass flag" },
    { insert: '"overwrite": true', label: '"overwrite": true', meta: "overwrite" },
    { insert: "true", label: "true", meta: "boolean" },
    { insert: "false", label: "false", meta: "boolean" },
    { insert: "null", label: "null", meta: "null" },
    ...regNames.map((nm) => ({ insert: `"${nm}"`, label: `"${nm}"`, meta: "AST target" })),
  ];
  const q = token.toLowerCase();
  return q
    ? pool.filter(
        (s) => s.label.toLowerCase().includes(q) || s.insert.toLowerCase().includes(q)
      )
    : pool;
}

export function buildEndpointSuggestions(
  value: string,
  items: RegistryItem[]
): SuggestionItem[] {
  const pool: SuggestionItem[] = [
    { insert: "/api/ast", label: "/api/ast", meta: "POST / GET" },
    { insert: "/api/chain", label: "/api/chain", meta: "POST chain" },
    { insert: "/api/eval", label: "/api/eval", meta: "POST expr" },
    { insert: "/api/convert", label: "/api/convert", meta: "POST convert" },
    { insert: "/api/modules/files", label: "/api/modules/files", meta: "POST module" },
  ];
  for (const item of items) {
    pool.push({
      insert: `/api/ast/${item.name}`,
      label: `/api/ast/${item.name}`,
      meta: "PUT / GET / DELETE",
    });
    pool.push({
      insert: `/api/ast/${item.name}/eval`,
      label: `/api/ast/${item.name}/eval`,
      meta: "POST eval",
    });
    pool.push({
      insert: `/api/ast/${item.name}/test`,
      label: `/api/ast/${item.name}/test`,
      meta: "POST test",
    });
  }
  const q = value.trim().toLowerCase();
  return q
    ? pool.filter((s) => s.label.toLowerCase().includes(q) || s.meta.toLowerCase().includes(q))
    : pool;
}

export function buildDefaultParamSuggestions(value: string, annotation: string): SuggestionItem[] {
  const t = (annotation || "").toLowerCase();
  const pool: SuggestionItem[] = [];
  if (t.includes("int")) {
    pool.push({ insert: "0", label: "0", meta: "int" }, { insert: "1", label: "1", meta: "int" }, { insert: "3", label: "3", meta: "int" });
  } else if (t.includes("float")) {
    pool.push({ insert: "0.0", label: "0.0", meta: "float" }, { insert: "1.0", label: "1.0", meta: "float" });
  } else if (t.includes("bool")) {
    pool.push({ insert: "False", label: "False", meta: "bool" }, { insert: "True", label: "True", meta: "bool" });
  } else if (t.includes("str")) {
    pool.push({ insert: '""', label: '""', meta: "str" }, { insert: '"default"', label: '"default"', meta: "str" });
  } else {
    pool.push(
      { insert: "None", label: "None", meta: "optional" },
      { insert: "0", label: "0", meta: "int" },
      { insert: "1", label: "1", meta: "int" },
      { insert: '""', label: '""', meta: "str" },
      { insert: "True", label: "True", meta: "bool" },
      { insert: "False", label: "False", meta: "bool" }
    );
  }
  if (!pool.some((p) => p.insert === "None")) {
    pool.push({ insert: "None", label: "None", meta: "nullable" });
  }
  const q = value.trim().toLowerCase();
  return q ? pool.filter((s) => s.label.toLowerCase().includes(q)) : pool;
}
