import "dotenv/config";
import { spawn, type ChildProcess } from "node:child_process";
import fs from "node:fs";
import http from "node:http";
import path from "node:path";
import { fileURLToPath } from "node:url";
import {
  GoogleGenAI,
  ThinkingLevel,
  Type,
  type Content,
  type FunctionDeclaration,
  type Part,
  type Schema,
} from "@google/genai";
import express, { type Request, type Response } from "express";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const PORT = Number(process.env.PORT || "3000");
const AST_BACKEND_PORT = Number(process.env.AST_UI_PORT || (PORT === 8081 ? "8082" : "8081"));
const AST_BACKEND_HOST = "127.0.0.1";
const GEMINI_MODEL = "gemini-4-hydrogen";

function createGeminiClient(): GoogleGenAI | null {
  const apiKey = (process.env.GEMINI_API_KEY || "").trim();
  if (!apiKey) {
    return null;
  }
  return new GoogleGenAI({
    apiKey,
    httpOptions: {
      headers: {
        "User-Agent": "aistudio-build",
      },
    },
  });
}

function extractGeminiErrorDetails(err: unknown): { status: number; message: string } {
  const rawStatus = Number((err as { status?: number; statusCode?: number })?.status ?? (err as { statusCode?: number })?.statusCode ?? 0);
  let rawMessage = err instanceof Error ? err.message : String(err ?? "Unknown error");
  let resolvedStatus = rawStatus;

  const trimmed = rawMessage.trim();
  if (trimmed.startsWith("{") && trimmed.endsWith("}")) {
    try {
      const parsed = JSON.parse(trimmed) as {
        error?: { code?: number; message?: string; status?: string };
        message?: string;
      };
      if (parsed?.error?.message) {
        rawMessage = String(parsed.error.message);
      } else if (parsed?.message) {
        rawMessage = String(parsed.message);
      }
      if (!resolvedStatus && parsed?.error?.code) {
        resolvedStatus = Number(parsed.error.code);
      }
    } catch {
      // Keep original message if not strict JSON
    }
  }

  return {
    status: resolvedStatus >= 400 && resolvedStatus < 600 ? resolvedStatus : 502,
    message: rawMessage,
  };
}

function getLeadingIndent(prefix: string): string {
  if (!prefix) return "";
  const lines = prefix.split("\n");
  const lastLine = lines[lines.length - 1] ?? "";
  const match = lastLine.match(/^([ \t]*)/);
  return match ? match[1] : "";
}

function normalizeGeneratedLine(rawLine: string): string {
  let line = String(rawLine || "").replace(/\r?\n$/, "");
  if (line.startsWith("```")) {
    const cleaned = line
      .split(/\r?\n/)
      .filter((ln) => !ln.trim().startsWith("```"));
    line = cleaned[0] ?? "";
  }
  return line;
}

async function generateLineWithRetry(
  ai: GoogleGenAI,
  payload: {
    prompt: string;
    prefix: string;
    suffix: string;
    targetName: string;
    language: string;
    registeredSymbols: string[];
  }
): Promise<{ line: string; explanation: string }> {
  const indentHint = getLeadingIndent(payload.prefix);
  const symbolsText = payload.registeredSymbols.length
    ? payload.registeredSymbols.join(", ")
    : "none";

  const contextPayload = JSON.stringify({
    language: payload.language || "python",
    target_function: payload.targetName || "entrypoint",
    registered_ast_symbols: symbolsText,
    instruction: payload.prompt || "Suggest the next single line of code based on context.",
    code_before_cursor: payload.prefix.slice(-3000),
    code_after_cursor: payload.suffix.slice(0, 1500),
    current_line_indent_spaces: indentHint.length,
  });

  let lastErr: unknown;
  for (let attempt = 0; attempt < 3; attempt++) {
    try {
      const response = await ai.models.generateContent({
        model: GEMINI_MODEL,
        contents: contextPayload,
        config: {
          systemInstruction:
            "You are a concise single-line code generator embedded in an interactive Python AST editor. " +
            "Generate ONLY ONE syntactic line (no markdown fences, no multi-line blocks) matching the target " +
            "language and cursor context. Reuse registered AST symbols or local variables where appropriate. " +
            "Return JSON matching the schema with `line` and a brief one-sentence `explanation`.",
          thinkingConfig: {
            thinkingLevel: ThinkingLevel.LOW,
          },
          responseMimeType: "application/json",
          responseSchema: {
            type: Type.OBJECT,
            properties: {
              line: {
                type: Type.STRING,
                description: "A single line of code to insert at the cursor, without markdown fences.",
              },
              explanation: {
                type: Type.STRING,
                description: "Short one-sentence explanation of the generated line.",
              },
            },
            required: ["line", "explanation"],
          },
        },
      });

      const rawText = (response.text ?? "").trim();
      if (!rawText) {
        throw new Error("Gemini API returned an empty response.");
      }
      const parsed = JSON.parse(rawText) as { line?: string; explanation?: string };
      return {
        line: normalizeGeneratedLine(parsed.line ?? ""),
        explanation: String(parsed.explanation ?? "Generated single line via Gemini.").trim(),
      };
    } catch (err: unknown) {
      lastErr = err;
      const status = Number((err as { status?: number })?.status ?? 0);
      if ((status === 429 || status >= 500 || status === 0) && attempt < 2) {
        await new Promise((resolve) => setTimeout(resolve, 400 * 2 ** attempt));
        continue;
      }
      throw err;
    }
  }
  throw lastErr;
}

function startPythonAstBackend(): ChildProcess {
  const scriptPath = path.join(__dirname, "ast_mcp_server.py");
  const child = spawn(
    "python3",
    [
      scriptPath,
      "--disable-server",
      "mcp",
      "--no-stdin",
      "--ui-host",
      AST_BACKEND_HOST,
      "--ui-port",
      String(AST_BACKEND_PORT),
    ],
    {
      cwd: __dirname,
      env: process.env,
      stdio: ["ignore", "pipe", "pipe"],
    }
  );

  child.stdout?.on("data", (chunk: Buffer) => {
    const line = chunk.toString("utf-8").trim();
    if (line) process.stdout.write(`[ast-backend] ${line}\n`);
  });
  child.stderr?.on("data", (chunk: Buffer) => {
    const line = chunk.toString("utf-8").trim();
    if (line) process.stderr.write(`[ast-backend] ${line}\n`);
  });

  return child;
}

function proxyToAstBackend(req: Request, res: Response): void {
  const targetUrl = req.originalUrl;
  const proxyReq = http.request(
    {
      hostname: AST_BACKEND_HOST,
      port: AST_BACKEND_PORT,
      path: targetUrl,
      method: req.method,
      headers: {
        ...req.headers,
        host: `${AST_BACKEND_HOST}:${AST_BACKEND_PORT}`,
      },
    },
    (proxyRes) => {
      res.writeHead(proxyRes.statusCode || 502, proxyRes.headers);
      proxyRes.pipe(res, { end: true });
    }
  );

  proxyReq.on("error", (err) => {
    if (!res.headersSent) {
      res.status(503).json({
        status: "error",
        error_type: "ast_backend_unavailable",
        message: `Python AST runtime is starting or unavailable (${err.message}).`,
      });
    }
  });

  req.pipe(proxyReq, { end: true });
}

async function fetchRegisteredSymbols(): Promise<string[]> {
  return new Promise((resolve) => {
    const req = http.get(
      `http://${AST_BACKEND_HOST}:${AST_BACKEND_PORT}/api/snapshot`,
      { timeout: 1500 },
      (resp) => {
        const chunks: Buffer[] = [];
        resp.on("data", (c: Buffer) => chunks.push(c));
        resp.on("end", () => {
          try {
            const parsed = JSON.parse(Buffer.concat(chunks).toString("utf-8")) as {
              items?: Array<{ name?: string }>;
            };
            resolve((parsed.items || []).map((it) => String(it.name || "")).filter(Boolean));
          } catch {
            resolve([]);
          }
        });
      }
    );
    req.on("error", () => resolve([]));
    req.on("timeout", () => {
      req.destroy();
      resolve([]);
    });
  });
}

interface RawMcpTool {
  name: string;
  description?: string;
  parameters?: Record<string, unknown>;
}

function toGeminiSchema(raw: unknown): Schema {
  let obj = (raw && typeof raw === "object" ? raw : {}) as Record<string, unknown>;
  if (Array.isArray(obj.anyOf) && obj.anyOf.length > 0) {
    const nonNull = obj.anyOf.find(
      (b) => b && typeof b === "object" && (b as Record<string, unknown>).type !== "null"
    );
    if (nonNull && typeof nonNull === "object") {
      obj = {
        ...obj,
        ...(nonNull as Record<string, unknown>),
      };
    }
  }
  const rawType = String(obj.type || "string").toLowerCase();
  const typeMap: Record<string, Type> = {
    string: Type.STRING,
    integer: Type.INTEGER,
    number: Type.NUMBER,
    boolean: Type.BOOLEAN,
    array: Type.ARRAY,
    object: Type.OBJECT,
  };
  const schemaType = typeMap[rawType] ?? Type.STRING;
  const out: Schema = { type: schemaType };
  if (typeof obj.description === "string" && obj.description) {
    out.description = obj.description;
  }
  if (schemaType === Type.OBJECT) {
    const rawProps = (obj.properties && typeof obj.properties === "object" ? obj.properties : {}) as Record<
      string,
      unknown
    >;
    const props: Record<string, Schema> = {};
    for (const [k, v] of Object.entries(rawProps)) {
      props[k] = toGeminiSchema(v);
    }
    if (Object.keys(props).length === 0) {
      props._unused = { type: Type.STRING, description: "Optional unused placeholder" };
    }
    out.properties = props;
    if (Array.isArray(obj.required) && obj.required.length > 0) {
      out.required = obj.required.map((r) => String(r));
    }
  } else if (schemaType === Type.ARRAY) {
    out.items = obj.items ? toGeminiSchema(obj.items) : { type: Type.STRING };
  }
  return out;
}

async function fetchMcpFunctionDeclarations(): Promise<FunctionDeclaration[]> {
  const fetchOnce = () =>
    new Promise<FunctionDeclaration[]>((resolve, reject) => {
      const req = http.get(
        `http://${AST_BACKEND_HOST}:${AST_BACKEND_PORT}/api/mcp/tools`,
        { timeout: 2000 },
        (resp) => {
          const chunks: Buffer[] = [];
          resp.on("data", (c: Buffer) => chunks.push(c));
          resp.on("end", () => {
            try {
              const parsed = JSON.parse(Buffer.concat(chunks).toString("utf-8")) as {
                tools?: RawMcpTool[];
              };
              const decls: FunctionDeclaration[] = (parsed.tools || [])
                .filter((t) => Boolean(t.name))
                .map((t) => {
                  const rawParams = t.parameters as Record<string, unknown> | undefined;
                  const rawProps =
                    rawParams && typeof rawParams.properties === "object"
                      ? (rawParams.properties as Record<string, unknown>)
                      : {};
                  const decl: FunctionDeclaration = {
                    name: t.name,
                    description: t.description || `MCP tool ${t.name}`,
                  };
                  if (Object.keys(rawProps).length > 0) {
                    decl.parameters = toGeminiSchema(rawParams);
                  }
                  return decl;
                });
              resolve(decls);
            } catch {
              resolve([]);
            }
          });
        }
      );
      req.on("error", (err) => reject(err));
      req.on("timeout", () => {
        req.destroy();
        reject(new Error("timeout"));
      });
    });

  for (let attempt = 0; attempt < 3; attempt++) {
    try {
      return await fetchOnce();
    } catch {
      if (attempt < 2) {
        await new Promise((r) => setTimeout(r, 250));
      }
    }
  }
  return [];
}

async function executeMcpTool(
  tool: string,
  args: Record<string, unknown>
): Promise<Record<string, unknown>> {
  return new Promise((resolve) => {
    const payload = JSON.stringify({
      tool,
      arguments: args,
      source: "gemini_mcp",
    });
    const req = http.request(
      {
        hostname: AST_BACKEND_HOST,
        port: AST_BACKEND_PORT,
        path: "/api/command",
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Accept: "application/hal+json",
          "Content-Length": Buffer.byteLength(payload),
        },
        timeout: 30000,
      },
      (resp) => {
        const chunks: Buffer[] = [];
        resp.on("data", (c: Buffer) => chunks.push(c));
        resp.on("end", () => {
          try {
            resolve(JSON.parse(Buffer.concat(chunks).toString("utf-8")) as Record<string, unknown>);
          } catch (err) {
            resolve({ status: "error", message: String(err) });
          }
        });
      }
    );
    req.on("error", (err) => {
      resolve({ status: "error", error_type: "mcp_bridge_error", message: err.message });
    });
    req.on("timeout", () => {
      req.destroy();
      resolve({ status: "error", error_type: "mcp_timeout", message: "MCP tool execution timed out." });
    });
    req.write(payload);
    req.end();
  });
}

async function main(): Promise<void> {
  const pythonBackend = startPythonAstBackend();
  const cleanupBackend = () => {
    if (!pythonBackend.killed) {
      pythonBackend.kill("SIGTERM");
    }
  };
  process.on("SIGINT", cleanupBackend);
  process.on("SIGTERM", cleanupBackend);
  process.on("exit", cleanupBackend);

  const app = express();

  app.get("/api/health", (_req, res) => {
    res.json({ status: "ok", model: GEMINI_MODEL });
  });

  app.post("/api/gemini/generate-line", express.json({ limit: "1mb" }), async (req, res) => {
    const ai = createGeminiClient();
    if (!ai) {
      res.status(503).json({
        status: "error",
        error_type: "gemini_api_key_missing",
        message: "GEMINI_API_KEY is not configured. Check your Settings > Secrets panel.",
      });
      return;
    }

    const body = (req.body || {}) as Record<string, unknown>;
    const prompt = String(body.prompt || "").trim();
    const prefix = String(body.prefix || "");
    const suffix = String(body.suffix || "");
    const targetName = String(body.target_name || body.targetName || "").trim();
    const language = String(body.language || "python").trim();

    if (!prompt && !prefix.trim()) {
      res.status(400).json({
        status: "error",
        error_type: "validation_error",
        message: "Provide a prompt or surrounding code context to generate a line.",
      });
      return;
    }

    try {
      const registeredSymbols = await fetchRegisteredSymbols();
      const result = await generateLineWithRetry(ai, {
        prompt,
        prefix,
        suffix,
        targetName,
        language,
        registeredSymbols,
      });
      res.json({
        status: "line_generated",
        model: GEMINI_MODEL,
        line: result.line,
        explanation: result.explanation,
        target_name: targetName,
      });
    } catch (err: unknown) {
      const { status: safeStatus, message: errMsg } = extractGeminiErrorDetails(err);
      res.status(safeStatus).json({
        status: "error",
        error_type:
          safeStatus === 429
            ? "gemini_quota_exhausted"
            : safeStatus === 400 || safeStatus === 403
              ? "gemini_auth_or_request_error"
              : safeStatus === 404
                ? "gemini_model_not_found"
                : "gemini_api_error",
        message: errMsg,
      });
    }
  });

  app.post("/api/gemini/chat", express.json({ limit: "1mb" }), async (req, res) => {
    const ai = createGeminiClient();
    if (!ai) {
      res.status(503).json({
        status: "error",
        error_type: "gemini_api_key_missing",
        message: "GEMINI_API_KEY is not configured. Check your Settings > Secrets panel.",
      });
      return;
    }

    const body = (req.body || {}) as Record<string, unknown>;
    const message = String(body.message || "").trim();
    const rawHistory = Array.isArray(body.history) ? body.history : [];

    if (!message) {
      res.status(400).json({
        status: "error",
        error_type: "validation_error",
        message: "Provide a message prompt to chat with Gemini using the MCP server tools.",
      });
      return;
    }

    try {
      const fnDecls = await fetchMcpFunctionDeclarations();
      const contents: Content[] = [];
      for (const h of rawHistory) {
        if (
          h &&
          typeof h === "object" &&
          ((h as { role?: unknown }).role === "user" || (h as { role?: unknown }).role === "model")
        ) {
          const textVal = String((h as { text?: unknown }).text || "").trim();
          if (textVal) {
            contents.push({
              role: (h as { role: "user" | "model" }).role,
              parts: [{ text: textVal }],
            });
          }
        }
      }
      contents.push({ role: "user", parts: [{ text: message }] });

      const toolTraces: Array<{
        tool: string;
        arguments: Record<string, unknown>;
        status: string;
        result: Record<string, unknown>;
      }> = [];
      let finalReply = "";

      for (let round = 0; round < 6; round++) {
        let response;
        let lastErr: unknown;
        for (let attempt = 0; attempt < 3; attempt++) {
          try {
            response = await ai.models.generateContent({
              model: GEMINI_MODEL,
              contents,
              config: {
                systemInstruction:
                  "You are an expert Python AST assistant connected directly to the Dynamic AST MCP Server. " +
                  "You have access to MCP tools (including `ast_read`, `ast_create`, `ast_update`, `ast_delete`, " +
                  "`eval_ast_call`, `ast_run_tests`, `ast_analyze`, `ast_rollback`, `ast_convert`, and dynamically " +
                  "exposed Python functions). Prefer inspecting or executing real MCP tools to answer accurately.",
                thinkingConfig: { thinkingLevel: ThinkingLevel.LOW },
                ...(fnDecls.length ? { tools: [{ functionDeclarations: fnDecls }] } : {}),
              },
            });
            break;
          } catch (err: unknown) {
            lastErr = err;
            const status = Number((err as { status?: number })?.status ?? 0);
            if ((status === 429 || status >= 500 || status === 0) && attempt < 2) {
              await new Promise((resolve) => setTimeout(resolve, 400 * 2 ** attempt));
              continue;
            }
            throw err;
          }
        }
        if (!response) {
          throw lastErr || new Error("Gemini API failed to return a response.");
        }

        const prevContent = response.candidates?.[0]?.content;
        if (prevContent) {
          contents.push(prevContent);
        }

        const calls = response.functionCalls;
        if (!calls || calls.length === 0) {
          finalReply = (response.text ?? "").trim();
          break;
        }

        const responseParts: Part[] = [];
        for (const call of calls) {
          const toolName = String(call.name || "");
          const toolArgs =
            call.args && typeof call.args === "object"
              ? (call.args as Record<string, unknown>)
              : {};
          const toolResult = await executeMcpTool(toolName, toolArgs);
          toolTraces.push({
            tool: toolName,
            arguments: toolArgs,
            status: String(toolResult.status || "ok"),
            result: toolResult,
          });
          responseParts.push({
            functionResponse: {
              ...(call.id ? { id: call.id } : {}),
              name: toolName,
              response: { output: toolResult },
            },
          });
        }
        contents.push({ role: "user", parts: responseParts });
      }

      res.json({
        status: "ok",
        model: GEMINI_MODEL,
        reply: finalReply || "Completed MCP tool execution.",
        tool_traces: toolTraces,
        available_tools_count: fnDecls.length,
      });
    } catch (err: unknown) {
      const { status: safeStatus, message: errMsg } = extractGeminiErrorDetails(err);
      res.status(safeStatus).json({
        status: "error",
        error_type:
          safeStatus === 429
            ? "gemini_quota_exhausted"
            : safeStatus === 400 || safeStatus === 403
              ? "gemini_auth_or_request_error"
              : safeStatus === 404
                ? "gemini_model_not_found"
                : "gemini_api_error",
        message: errMsg,
      });
    }
  });

  // Proxy all Python AST REST & SSE routes cleanly before Vite SPA fallback
  app.use((req, res, next) => {
    if (req.path === "/api" || req.path.startsWith("/api/")) {
      proxyToAstBackend(req, res);
      return;
    }
    next();
  });

  // Ensure any API error (e.g. malformed JSON body) returns JSON rather than Express HTML
  app.use(
    "/api",
    (
      err: unknown,
      _req: Request,
      res: Response,
      _next: express.NextFunction
    ) => {
      if (res.headersSent) return;
      const { status: safeStatus, message: errMsg } = extractGeminiErrorDetails(err);
      const isSyntax = err instanceof SyntaxError;
      res.status(isSyntax ? 400 : safeStatus).json({
        status: "error",
        error_type: isSyntax ? "invalid_json" : "api_server_error",
        message: errMsg,
      });
    }
  );

  if (process.env.NODE_ENV !== "production") {
    const { createServer: createViteServer } = await import("vite");
    const vite = await createViteServer({
      server: { middlewareMode: true },
      appType: "spa",
    });
    app.use(vite.middlewares);
  } else {
    const distDir = path.join(__dirname, "dist");
    app.use(express.static(distDir));
    app.get("*", (_req, res) => {
      const indexFile = path.join(distDir, "index.html");
      if (fs.existsSync(indexFile)) {
        res.sendFile(indexFile);
      } else {
        res.status(404).send("Build artifacts not found. Run npm run build first.");
      }
    });
  }

  app.listen(PORT, "0.0.0.0", () => {
    console.log(`Dynamic AST & Line Studio listening on http://0.0.0.0:${PORT}`);
  });
}

main().catch((err) => {
  console.error("Failed to start server:", err);
  process.exit(1);
});
