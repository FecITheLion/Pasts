"""Server-side Gemini API integration for context-aware single-line code generation."""

from __future__ import annotations

import json
import os
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

DEFAULT_GEMINI_MODEL = "gemini-4-hydrogen"
_GEMINI_ENDPOINT_TEMPLATE = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


class GeminiLineError(RuntimeError):
    """Structured error raised when Gemini line generation fails."""

    def __init__(self, message: str, error_type: str = "gemini_api_error", status_code: int = 502) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.status_code = status_code


def _extract_leading_indent(prefix: str) -> str:
    if not prefix:
        return ""
    last_line = prefix.splitlines()[-1] if not prefix.endswith("\n") else ""
    if prefix.endswith("\n"):
        lines = prefix.splitlines()
        last_line = lines[-1] if lines else ""
        leading = len(last_line) - len(last_line.lstrip(" \t"))
        return last_line[:leading]
    leading = len(last_line) - len(last_line.lstrip(" \t"))
    return last_line[:leading]


def generate_line_with_gemini(
    prompt: str,
    prefix: str = "",
    suffix: str = "",
    target_name: str = "",
    language: str = "python",
    registered_symbols: list[str] | None = None,
    model: str = DEFAULT_GEMINI_MODEL,
    timeout: float = 25.0,
) -> dict[str, Any]:
    """Call Gemini `generateContent` server-side to generate a single context-aware line."""
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise GeminiLineError(
            "GEMINI_API_KEY is not configured in the server environment.",
            error_type="gemini_api_key_missing",
            status_code=503,
        )

    clean_prompt = str(prompt or "").strip()
    if not clean_prompt and not prefix.strip():
        raise GeminiLineError(
            "Provide an instruction prompt or surrounding code context for line generation.",
            error_type="validation_error",
            status_code=400,
        )

    indent_hint = _extract_leading_indent(prefix)
    symbols_text = ", ".join(registered_symbols or []) or "none"

    user_context = json.dumps(
        {
            "language": language,
            "target_function": target_name or "entrypoint",
            "registered_ast_symbols": symbols_text,
            "instruction": clean_prompt or "Suggest the next single line of code based on context.",
            "code_before_cursor": prefix[-3000:],
            "code_after_cursor": suffix[:1500],
            "current_line_indent_spaces": len(indent_hint),
        },
        ensure_ascii=False,
    )

    request_payload = {
        "systemInstruction": {
            "parts": [
                {
                    "text": (
                        "You are a concise single-line code generator embedded in an interactive Python AST editor. "
                        "Generate ONLY ONE syntactic line (no markdown fences, no multi-line blocks unless required by a single statement) "
                        "matching the target language and cursor context. Preserve variable names, types, and registered AST function calls "
                        "when relevant. Return valid JSON matching the schema with keys `line` (the exact single line without leading indentation "
                        "if inserted inline, or matching context) and `explanation` (short 1-sentence note)."
                    )
                }
            ]
        },
        "contents": [
            {
                "role": "user",
                "parts": [{"text": user_context}],
            }
        ],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": {
                "type": "OBJECT",
                "properties": {
                    "line": {
                        "type": "STRING",
                        "description": "A single line of code to insert at the cursor, with no markdown code fences.",
                    },
                    "explanation": {
                        "type": "STRING",
                        "description": "Brief one-sentence explanation of the generated line.",
                    },
                },
                "required": ["line", "explanation"],
            },
            "thinkingConfig": {"thinkingLevel": "LOW"},
        },
    }

    url = _GEMINI_ENDPOINT_TEMPLATE.format(model=model)
    body_bytes = json.dumps(request_payload).encode("utf-8")

    last_exc: Exception | None = None
    for attempt in range(3):
        req = Request(
            url,
            data=body_bytes,
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": api_key,
                "User-Agent": "aistudio-build",
            },
            method="POST",
        )
        try:
            with urlopen(req, timeout=timeout) as resp:
                raw = json.loads(resp.read().decode("utf-8"))
            break
        except HTTPError as exc:
            err_body = ""
            try:
                err_body = exc.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            if exc.code in (429, 500, 502, 503, 504) and attempt < 2:
                time.sleep(0.5 * (2**attempt))
                last_exc = exc
                continue
            err_type = "gemini_api_error"
            if exc.code in (400, 403):
                err_type = "gemini_auth_or_request_error"
            elif exc.code == 404:
                err_type = "gemini_model_not_found"
            elif exc.code == 429:
                err_type = "gemini_quota_exhausted"
            detail = err_body[:400] if err_body else str(exc.reason)
            raise GeminiLineError(
                f"Gemini API error ({exc.code}): {detail}",
                error_type=err_type,
                status_code=exc.code if 400 <= exc.code < 600 else 502,
            ) from exc
        except URLError as exc:
            if attempt < 2:
                time.sleep(0.5 * (2**attempt))
                last_exc = exc
                continue
            raise GeminiLineError(
                f"Network error contacting Gemini API: {exc.reason}",
                error_type="gemini_network_error",
                status_code=502,
            ) from exc
    else:
        raise GeminiLineError(f"Gemini API request failed: {last_exc}")

    candidates = raw.get("candidates") or []
    parts = ((candidates[0].get("content") or {}).get("parts") or []) if candidates else []
    text_out = "".join(str(p.get("text", "")) for p in parts if isinstance(p, dict)).strip()
    if not text_out:
        raise GeminiLineError("Gemini API returned an empty response.", error_type="gemini_empty_response", status_code=502)

    try:
        parsed = json.loads(text_out)
    except json.JSONDecodeError:
        line_val = text_out.splitlines()[0].strip() if text_out else ""
        return {
            "model": model,
            "line": line_val,
            "explanation": "Generated single line via Gemini.",
        }

    line_val = str(parsed.get("line", "")).rstrip("\r\n")
    # Ensure single-line normalization if model includes surrounding backticks or extra newline
    if line_val.startswith("```"):
        cleaned_lines = [ln for ln in line_val.splitlines() if not ln.strip().startswith("```")]
        line_val = cleaned_lines[0] if cleaned_lines else ""
    explanation = str(parsed.get("explanation", "Generated single line via Gemini.")).strip()

    return {
        "model": model,
        "line": line_val,
        "explanation": explanation,
    }


def _schema_to_gemini_upper(schema: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(schema, dict):
        return {"type": "STRING"}
    if isinstance(schema.get("anyOf"), list) and schema["anyOf"]:
        non_null = next(
            (b for b in schema["anyOf"] if isinstance(b, dict) and b.get("type") != "null"),
            None,
        )
        if non_null:
            schema = {**schema, **non_null}
    t = str(schema.get("type", "string")).upper()
    out: dict[str, Any] = {"type": t}
    if "description" in schema:
        out["description"] = str(schema["description"])
    if isinstance(schema.get("properties"), dict):
        props: dict[str, Any] = {}
        for k, v in schema["properties"].items():
            props[k] = _schema_to_gemini_upper(v if isinstance(v, dict) else {"type": "string"})
        if not props and t == "OBJECT":
            # Gemini OBJECT schema cannot be empty when declaring properties
            props["value"] = {"type": "STRING", "description": "Optional value"}
        out["properties"] = props
    if isinstance(schema.get("required"), list) and schema["required"]:
        out["required"] = [str(x) for x in schema["required"]]
    if "items" in schema and isinstance(schema["items"], dict):
        out["items"] = _schema_to_gemini_upper(schema["items"])
    return out


def chat_with_gemini_mcp_tools(
    message: str,
    mcp_tools: list[dict[str, Any]],
    tool_executor: Any,
    history: list[dict[str, Any]] | None = None,
    model: str = DEFAULT_GEMINI_MODEL,
    max_rounds: int = 6,
    timeout: float = 35.0,
) -> dict[str, Any]:
    """Run a multi-turn Gemini conversation wired directly to this MCP server's tools."""
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise GeminiLineError(
            "GEMINI_API_KEY is not configured in the server environment.",
            error_type="gemini_api_key_missing",
            status_code=503,
        )

    clean_msg = str(message or "").strip()
    if not clean_msg:
        raise GeminiLineError(
            "Provide a message prompt for Gemini.",
            error_type="validation_error",
            status_code=400,
        )

    fn_decls = [
        {
            "name": str(t["name"]),
            "description": str(t.get("description") or f"MCP tool {t['name']}"),
            "parameters": _schema_to_gemini_upper(t.get("parameters")),
        }
        for t in mcp_tools
        if t.get("name")
    ]

    contents: list[dict[str, Any]] = []
    for item in history or []:
        if isinstance(item, dict) and item.get("role") in ("user", "model"):
            text_val = str(item.get("text") or "").strip()
            if text_val:
                contents.append({"role": item["role"], "parts": [{"text": text_val}]})
    contents.append({"role": "user", "parts": [{"text": clean_msg}]})

    url = _GEMINI_ENDPOINT_TEMPLATE.format(model=model)
    tool_traces: list[dict[str, Any]] = []
    final_reply = ""

    for _round in range(max_rounds):
        payload: dict[str, Any] = {
            "systemInstruction": {
                "parts": [
                    {
                        "text": (
                            "You are an expert Python AST assistant connected directly to the live "
                            "Dynamic AST MCP Server. Use the provided MCP tools to inspect, create, "
                            "update, test, analyze, rollback, or evaluate AST functions as requested. "
                            "Keep answers concise, accurate, and grounded in actual tool outputs."
                        )
                    }
                ]
            },
            "contents": contents,
            "generationConfig": {"thinkingConfig": {"thinkingLevel": "LOW"}},
        }
        if fn_decls:
            payload["tools"] = [{"functionDeclarations": fn_decls}]

        body_bytes = json.dumps(payload).encode("utf-8")
        raw: dict[str, Any] | None = None
        last_exc: Exception | None = None
        for attempt in range(3):
            req = Request(
                url,
                data=body_bytes,
                headers={
                    "Content-Type": "application/json",
                    "x-goog-api-key": api_key,
                    "User-Agent": "aistudio-build",
                },
                method="POST",
            )
            try:
                with urlopen(req, timeout=timeout) as resp:
                    raw = json.loads(resp.read().decode("utf-8"))
                break
            except HTTPError as exc:
                err_body = ""
                try:
                    err_body = exc.read().decode("utf-8", errors="replace")
                except Exception:
                    pass
                if exc.code in (429, 500, 502, 503, 504) and attempt < 2:
                    time.sleep(0.5 * (2**attempt))
                    last_exc = exc
                    continue
                raise GeminiLineError(
                    f"Gemini API error ({exc.code}): {err_body[:400] or exc.reason}",
                    error_type="gemini_api_error",
                    status_code=exc.code if 400 <= exc.code < 600 else 502,
                ) from exc
            except URLError as exc:
                if attempt < 2:
                    time.sleep(0.5 * (2**attempt))
                    last_exc = exc
                    continue
                raise GeminiLineError(
                    f"Network error contacting Gemini API: {exc.reason}",
                    error_type="gemini_network_error",
                    status_code=502,
                ) from exc
        if raw is None:
            raise GeminiLineError(f"Gemini API request failed: {last_exc}")

        candidates = raw.get("candidates") or []
        cand_content = (candidates[0].get("content") or {}) if candidates else {}
        parts = cand_content.get("parts") or []
        if cand_content:
            contents.append(cand_content)

        fn_calls = [p["functionCall"] for p in parts if isinstance(p, dict) and isinstance(p.get("functionCall"), dict)]
        if not fn_calls:
            final_reply = "".join(str(p.get("text", "")) for p in parts if isinstance(p, dict) and "text" in p).strip()
            break

        response_parts: list[dict[str, Any]] = []
        for fc in fn_calls:
            t_name = str(fc.get("name", ""))
            t_args = fc.get("args") if isinstance(fc.get("args"), dict) else {}
            t_result = tool_executor(t_name, t_args)
            tool_traces.append(
                {
                    "tool": t_name,
                    "arguments": t_args,
                    "status": t_result.get("status", "ok") if isinstance(t_result, dict) else "ok",
                    "result": t_result,
                }
            )
            fn_resp: dict[str, Any] = {
                "name": t_name,
                "response": {"output": t_result},
            }
            if fc.get("id"):
                fn_resp["id"] = fc["id"]
            response_parts.append({"functionResponse": fn_resp})

        contents.append({"role": "user", "parts": response_parts})

    return {
        "model": model,
        "reply": final_reply or "Completed MCP tool execution.",
        "tool_traces": tool_traces,
    }
