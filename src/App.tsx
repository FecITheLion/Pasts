/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useCallback, useEffect, useRef, useState } from "react";
import {
  Activity,
  Bot,
  Code2,
  Edit3,
  FlaskConical,
  GitCompare,
  History,
  Play,
  RefreshCw,
  Send,
  Sparkles,
  Terminal,
  Trash2,
  Wrench,
} from "lucide-react";
import { AutocompleteInput, SuggestionDropdown } from "./AutocompleteField.tsx";
import {
  buildArgsSuggestions,
  buildChatPromptSuggestions,
  buildLinePromptSuggestions,
  buildNameSuggestions,
  buildPythonTextareaSuggestions,
  getCaretTokenRange,
} from "./autocomplete.ts";
import {
  CircularityResolverModal,
  HateoasResolverModal,
  computeTracksSignature,
  extractHateoasActionTracks,
  parseCircularityFromError,
} from "./HateoasResolver.tsx";
import { safeFetchJson } from "./http.ts";
import { HighlightedCode } from "./syntax.tsx";
import type {
  ChatMessage,
  CycleInfo,
  EventEntry,
  GraphNode,
  HateoasTrack,
  McpToolInfo,
  MissingDefaultParam,
  RegistryItem,
  ToolTrace,
} from "./types.ts";

type FormatTab = "python_source" | "serialized_ast" | "pretty_ast" | "json_ast";
type ToolOp =
  | "eval_ast_call"
  | "ast_create"
  | "ast_update"
  | "ast_read"
  | "ast_delete"
  | "ast_convert";

interface ResolverState {
  signature: string;
  policy: string;
  message: string;
  tracks: HateoasTrack[];
  missingDefaultParams: MissingDefaultParam[];
  supersededCount: number;
}

export default function App() {
  const [items, setItems] = useState<RegistryItem[]>([]);
  const [events, setEvents] = useState<EventEntry[]>([]);
  const [graphNodes, setGraphNodes] = useState<GraphNode[]>([]);
  const [mcpTools, setMcpTools] = useState<McpToolInfo[]>([]);
  const [connStatus, setConnStatus] = useState<string>("Connecting...");
  const [activeTabs, setActiveTabs] = useState<Record<string, FormatTab>>({});

  const [tool, setTool] = useState<ToolOp>("ast_update");
  const [nameInput, setNameInput] = useState<string>("entrypoint");
  const [argsInput, setArgsInput] = useState<string>("[3]");
  const [codeInput, setCodeInput] = useState<string>(
    "def entrypoint(n: int = 3) -> int:\n    '''Default entrypoint compiled and exposed at startup.'''\n    return ast2(n) + 10\n"
  );
  const [linePrompt, setLinePrompt] = useState<string>("");
  const [isGeneratingLine, setIsGeneratingLine] = useState<boolean>(false);
  const [isExecuting, setIsExecuting] = useState<boolean>(false);

  const [lastResponse, setLastResponse] = useState<Record<string, unknown> | null>(null);

  const [chatInput, setChatInput] = useState<string>("");
  const [chatMessages, setChatMessages] = useState<ChatMessage[]>([]);
  const [isChatting, setIsChatting] = useState<boolean>(false);
  const [showToolsList, setShowToolsList] = useState<boolean>(false);

  // Code textarea autocomplete state
  const [codeAcOpen, setCodeAcOpen] = useState<boolean>(false);
  const [codeAcIdx, setCodeAcIdx] = useState<number>(0);
  const [codeAcUserNav, setCodeAcUserNav] = useState<boolean>(false);

  // HATEOAS Step-By-Step Error Resolver & Circularity Resolver state
  const [resolverState, setResolverState] = useState<ResolverState | null>(null);
  const [resolverModalOpen, setResolverModalOpen] = useState<boolean>(false);
  const [activeCycleInfo, setActiveCycleInfo] = useState<CycleInfo | null>(null);

  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const highlightPreRef = useRef<HTMLPreElement | null>(null);
  const itemsRef = useRef<RegistryItem[]>([]);
  itemsRef.current = items;
  const lastSeenEventIdRef = useRef<number>(0);

  const handlePayloadLifecycle = useCallback(
    (data: Record<string, unknown> | null, autoOpenResolver: boolean) => {
      if (!data) return;
      setLastResponse(data);

      const cycleErr = parseCircularityFromError(data, itemsRef.current);
      if (cycleErr) {
        setResolverModalOpen(false);
        setActiveCycleInfo(cycleErr);
        return;
      }

      const tracks = extractHateoasActionTracks(data);
      const isFailure = data.status === "error";
      if (tracks.length > 0 && isFailure) {
        const nextSig = computeTracksSignature(tracks);
        setResolverState((prev) => {
          const sameSig = prev?.signature === nextSig;
          const hadPrior = Boolean(prev && !sameSig);
          return {
            signature: nextSig,
            policy: String(data.policy || data.error_type || data.status || "hateoas_recovery"),
            message: String(
              data.message ||
                "Suggested HATEOAS calls are available to repair this execution failure."
            ),
            tracks,
            missingDefaultParams: Array.isArray(data.missing_default_params)
              ? (data.missing_default_params as MissingDefaultParam[])
              : [],
            supersededCount: sameSig
              ? prev?.supersededCount || 0
              : hadPrior
                ? (prev?.supersededCount || 0) + 1
                : 0,
          };
        });
        if (autoOpenResolver) {
          setResolverModalOpen(true);
        }
      }
    },
    []
  );

  const refreshMeta = useCallback(async () => {
    try {
      const [graphRes, toolsRes] = await Promise.all([
        safeFetchJson<{ nodes?: GraphNode[] }>("/api/graph"),
        safeFetchJson<{ tools?: McpToolInfo[] }>("/api/mcp/tools"),
      ]);
      if (graphRes.ok && Array.isArray(graphRes.data.nodes)) {
        setGraphNodes(graphRes.data.nodes);
      }
      if (toolsRes.ok && Array.isArray(toolsRes.data.tools)) {
        setMcpTools(toolsRes.data.tools);
      }
    } catch {
      // Transient backend startup
    }
  }, []);

  const refreshSnapshot = useCallback(async () => {
    try {
      const res = await safeFetchJson<{
        items?: RegistryItem[];
        events?: EventEntry[];
      }>("/api/snapshot");
      if (!res.ok) return;
      const nextItems = res.data.items || [];
      const nextEvents = res.data.events || [];
      setItems(nextItems);
      setEvents(nextEvents);
      itemsRef.current = nextItems;
      if (nextEvents.length > 0) {
        const newest = nextEvents[nextEvents.length - 1];
        if (newest.id > lastSeenEventIdRef.current) {
          lastSeenEventIdRef.current = newest.id;
        }
      }
      refreshMeta();
    } catch {
      // Snapshot poll ignores transient startup connection states
    }
  }, [refreshMeta]);

  useEffect(() => {
    refreshSnapshot();
    let pollTimer: ReturnType<typeof setInterval> | null = null;
    const es = new EventSource("/api/events");

    es.onopen = () => {
      setConnStatus("Connected (SSE Live)");
      if (pollTimer) {
        clearInterval(pollTimer);
        pollTimer = null;
      }
    };

    es.onmessage = (evt) => {
      try {
        const msg = JSON.parse(evt.data) as {
          snapshot?: { items?: RegistryItem[]; events?: EventEntry[] };
          event?: EventEntry;
        };
        if (msg.snapshot) {
          const nextItems = msg.snapshot.items || [];
          setItems(nextItems);
          itemsRef.current = nextItems;
          setEvents(msg.snapshot.events || []);
          refreshMeta();
        }
        if (msg.event?.result && msg.event.id > lastSeenEventIdRef.current) {
          lastSeenEventIdRef.current = msg.event.id;
          handlePayloadLifecycle(msg.event.result, false);
        }
      } catch {
        // Ignore malformed SSE keepalive frames
      }
    };

    es.onerror = () => {
      setConnStatus("Live Sync (Polling)");
      if (!pollTimer) {
        pollTimer = setInterval(refreshSnapshot, 1800);
      }
    };

    return () => {
      es.close();
      if (pollTimer) clearInterval(pollTimer);
    };
  }, [refreshSnapshot, refreshMeta, handlePayloadLifecycle]);

  const callRest = async (method: string, href: string, body?: unknown) => {
    setIsExecuting(true);
    try {
      const init: RequestInit = {
        method,
        headers: { Accept: "application/hal+json" },
      };
      if (body !== undefined && method !== "GET" && method !== "DELETE") {
        init.headers = {
          ...init.headers,
          "Content-Type": "application/json",
        };
        init.body = JSON.stringify(body);
      }
      const { data } = await safeFetchJson<Record<string, unknown>>(href, init);
      await refreshSnapshot();
      handlePayloadLifecycle(data, true);
      return data;
    } finally {
      setIsExecuting(false);
    }
  };

  const handleExecute = async () => {
    const cleanName = nameInput.trim();
    if (tool === "eval_ast_call") {
      let parsedArgs: unknown = argsInput.trim();
      if (!parsedArgs) {
        parsedArgs = [];
      } else {
        try {
          parsedArgs = JSON.parse(argsInput.trim());
        } catch {
          parsedArgs = argsInput.trim();
        }
      }
      if (/^[A-Za-z_][A-Za-z0-9_]*$/.test(cleanName)) {
        await callRest("POST", `/api/ast/${encodeURIComponent(cleanName)}/eval`, parsedArgs);
      } else {
        await callRest("POST", "/api/eval", { target: cleanName, args: parsedArgs });
      }
    } else if (tool === "ast_create") {
      await callRest("POST", "/api/ast", {
        name: cleanName,
        code_or_ast: codeInput,
        overwrite: true,
      });
    } else if (tool === "ast_update") {
      await callRest("PUT", `/api/ast/${encodeURIComponent(cleanName)}`, {
        code_or_ast: codeInput,
      });
    } else if (tool === "ast_read") {
      await callRest("GET", cleanName ? `/api/ast/${encodeURIComponent(cleanName)}` : "/api/ast");
    } else if (tool === "ast_delete") {
      await callRest("DELETE", `/api/ast/${encodeURIComponent(cleanName)}`);
    } else if (tool === "ast_convert") {
      await callRest("POST", "/api/convert", {
        source_kind: "saved_ast",
        target_kind: "pydantic",
        payload: codeInput.trim() || cleanName,
        model_name: cleanName || "GeneratedModel",
      });
    }
  };

  const handleGenerateLine = async () => {
    const ta = textareaRef.current;
    const start = ta?.selectionStart ?? codeInput.length;
    const end = ta?.selectionEnd ?? start;
    const prefix = codeInput.slice(0, start);
    const suffix = codeInput.slice(end);
    const prompt = linePrompt.trim() || "Suggest the next single line of code at the cursor.";

    setIsGeneratingLine(true);
    try {
      const { ok, data } = await safeFetchJson<{
        status?: string;
        line?: string;
        explanation?: string;
        message?: string;
        error_type?: string;
      }>("/api/gemini/generate-line", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          prompt,
          prefix,
          suffix,
          target_name: nameInput.trim(),
          language: "python",
        }),
      });
      setLastResponse(data);

      if (ok && typeof data.line === "string" && data.line.length > 0) {
        const curLinePrefix = prefix.slice(prefix.lastIndexOf("\n") + 1);
        const needsIndent =
          curLinePrefix.trim() === "" && !/^[ \t]/.test(data.line) && curLinePrefix.length === 0;
        const prevNonEmpty =
          [...prefix.split("\n")].reverse().find((ln) => ln.trim().length > 0) || "";
        const prevIndent = prevNonEmpty.match(/^([ \t]*)/)?.[1] ?? "";
        const inferredIndent = prevNonEmpty.trim().endsWith(":") ? `${prevIndent}    ` : prevIndent;
        const inserted = `${needsIndent ? inferredIndent : ""}${data.line}`;

        const nextCode = prefix + inserted + suffix;
        setCodeInput(nextCode);

        requestAnimationFrame(() => {
          if (textareaRef.current) {
            const nextPos = start + inserted.length;
            textareaRef.current.focus();
            textareaRef.current.setSelectionRange(nextPos, nextPos);
          }
        });
      }
    } finally {
      setIsGeneratingLine(false);
    }
  };

  const handleSendChat = async (overrideMsg?: string) => {
    const msgText = (overrideMsg ?? chatInput).trim();
    if (!msgText || isChatting) return;

    const priorHistory = chatMessages
      .filter((m) => !m.isError)
      .slice(-10)
      .map((m) => ({ role: m.role, text: m.text }));

    setChatMessages((prev) => [...prev, { role: "user", text: msgText }]);
    if (!overrideMsg) setChatInput("");
    setIsChatting(true);

    try {
      const { ok, status, data } = await safeFetchJson<{
        status?: string;
        reply?: string;
        message?: string;
        error_type?: string;
        tool_traces?: ToolTrace[];
      }>("/api/gemini/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: msgText, history: priorHistory }),
      });

      if (!ok || data.status === "error") {
        const errText = data.message || `Error (${data.error_type || status})`;
        setChatMessages((prev) => [...prev, { role: "model", text: errText, isError: true }]);
        handlePayloadLifecycle(data as Record<string, unknown>, true);
      } else {
        const traces = data.tool_traces || [];
        setChatMessages((prev) => [
          ...prev,
          {
            role: "model",
            text: data.reply || "Completed.",
            toolTraces: traces,
          },
        ]);
        await refreshSnapshot();
        const failedTraceWithRepair = [...traces]
          .reverse()
          .find(
            (t) =>
              t.result?.status === "error" &&
              (extractHateoasActionTracks(t.result).length > 0 ||
                Boolean(parseCircularityFromError(t.result, itemsRef.current)))
          );
        if (failedTraceWithRepair) {
          handlePayloadLifecycle(failedTraceWithRepair.result, false);
        }
      }
    } finally {
      setIsChatting(false);
    }
  };

  // Code editor autocomplete suggestions around cursor
  const caretPos = textareaRef.current?.selectionStart ?? codeInput.length;
  const { token: codeToken } = getCaretTokenRange(codeInput, caretPos);
  const codeSuggestions = codeAcOpen
    ? buildPythonTextareaSuggestions(codeToken, codeInput, items).slice(0, 12)
    : [];

  const applyCodeSuggestion = (insertText: string) => {
    const ta = textareaRef.current;
    const pos = ta?.selectionStart ?? codeInput.length;
    const { tokenStart, caretPos: curEnd } = getCaretTokenRange(codeInput, pos);
    const nextCode = codeInput.slice(0, tokenStart) + insertText + codeInput.slice(curEnd);
    setCodeInput(nextCode);
    setCodeAcOpen(false);
    setCodeAcUserNav(false);
    const nextCaret = tokenStart + insertText.length;
    requestAnimationFrame(() => {
      if (textareaRef.current) {
        textareaRef.current.focus();
        textareaRef.current.setSelectionRange(nextCaret, nextCaret);
      }
    });
  };

  const handleEditorKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.altKey && (e.key === "g" || e.key === "G")) {
      e.preventDefault();
      handleGenerateLine();
      return;
    }

    if (e.ctrlKey && e.code === "Space") {
      e.preventDefault();
      setCodeAcIdx(0);
      setCodeAcUserNav(true);
      setCodeAcOpen(true);
      return;
    }

    if (codeAcOpen && codeSuggestions.length > 0) {
      if (e.key === "ArrowDown") {
        e.preventDefault();
        setCodeAcIdx((prev) => (prev + 1) % codeSuggestions.length);
        setCodeAcUserNav(true);
        return;
      }
      if (e.key === "ArrowUp") {
        e.preventDefault();
        setCodeAcIdx((prev) => (prev - 1 + codeSuggestions.length) % codeSuggestions.length);
        setCodeAcUserNav(true);
        return;
      }
      if (e.key === "Tab" && !e.shiftKey) {
        e.preventDefault();
        applyCodeSuggestion((codeSuggestions[codeAcIdx] ?? codeSuggestions[0]).insert);
        return;
      }
      if (e.key === "Enter" && codeAcUserNav) {
        e.preventDefault();
        applyCodeSuggestion((codeSuggestions[codeAcIdx] ?? codeSuggestions[0]).insert);
        return;
      }
      if (e.key === "Escape") {
        e.preventDefault();
        setCodeAcOpen(false);
        setCodeAcUserNav(false);
        return;
      }
    }

    const ta = e.currentTarget;
    const start = ta.selectionStart;
    const end = ta.selectionEnd;

    if (e.key === "Tab") {
      e.preventDefault();
      if (e.shiftKey) {
        const lineStart = codeInput.lastIndexOf("\n", start - 1) + 1;
        const selectedSegment = codeInput.slice(lineStart, end);
        const lines = selectedSegment.split("\n");
        let removedFirst = 0;
        let totalRemoved = 0;
        const dedented = lines.map((ln, idx) => {
          const m = ln.match(/^ {1,4}/);
          const cut = m ? m[0].length : ln.startsWith("\t") ? 1 : 0;
          if (idx === 0) removedFirst = cut;
          totalRemoved += cut;
          return ln.slice(cut);
        });
        const nextVal = codeInput.slice(0, lineStart) + dedented.join("\n") + codeInput.slice(end);
        setCodeInput(nextVal);
        requestAnimationFrame(() => {
          ta.setSelectionRange(
            Math.max(lineStart, start - removedFirst),
            Math.max(lineStart, end - totalRemoved)
          );
        });
      } else if (start === end) {
        const nextVal = codeInput.slice(0, start) + "    " + codeInput.slice(end);
        setCodeInput(nextVal);
        requestAnimationFrame(() => ta.setSelectionRange(start + 4, start + 4));
      } else {
        const lineStart = codeInput.lastIndexOf("\n", start - 1) + 1;
        const selectedSegment = codeInput.slice(lineStart, end);
        const lines = selectedSegment.split("\n");
        const indented = lines.map((ln) => `    ${ln}`).join("\n");
        const nextVal = codeInput.slice(0, lineStart) + indented + codeInput.slice(end);
        setCodeInput(nextVal);
        requestAnimationFrame(() =>
          ta.setSelectionRange(start + 4, end + lines.length * 4)
        );
      }
      return;
    }

    if (e.key === "Enter" && !e.shiftKey && !e.ctrlKey && !e.metaKey) {
      setCodeAcOpen(false);
      setCodeAcUserNav(false);
      const lineStart = codeInput.lastIndexOf("\n", start - 1) + 1;
      const curLine = codeInput.slice(lineStart, start);
      const baseIndent = curLine.match(/^([ \t]*)/)?.[1] ?? "";
      const extraIndent = curLine.trimEnd().endsWith(":") ? "    " : "";
      const insertIndent = `\n${baseIndent}${extraIndent}`;
      e.preventDefault();
      const nextVal = codeInput.slice(0, start) + insertIndent + codeInput.slice(end);
      setCodeInput(nextVal);
      const nextCaret = start + insertIndent.length;
      requestAnimationFrame(() => ta.setSelectionRange(nextCaret, nextCaret));
    }
  };

  const syncEditorScroll = () => {
    if (textareaRef.current && highlightPreRef.current) {
      highlightPreRef.current.scrollTop = textareaRef.current.scrollTop;
      highlightPreRef.current.scrollLeft = textareaRef.current.scrollLeft;
    }
  };

  const loadItemIntoEditor = (item: RegistryItem) => {
    setTool("ast_update");
    setNameInput(item.name);
    setCodeInput(item.formats.python_source);
  };

  const handleRollback = async (targetName: string) => {
    const data = await callRest("POST", `/api/ast/${encodeURIComponent(targetName)}/rollback`, {});
    if (data && typeof data.python_source === "string" && nameInput.trim() === targetName) {
      setCodeInput(data.python_source);
    }
  };

  const activeResponseTracks =
    lastResponse?.status === "error" ? extractHateoasActionTracks(lastResponse) : [];
  const activeResponseCycle = parseCircularityFromError(lastResponse, items);

  const summaryBanner = (() => {
    if (!lastResponse) {
      return {
        isError: false,
        text: "Ready. Edit an AST function, chat with Gemini over MCP tools, or run tests.",
      };
    }
    const status = String(lastResponse.status ?? "ok");
    const timing =
      typeof lastResponse.duration_ms === "number" ? ` (${lastResponse.duration_ms} ms)` : "";
    if (status === "error") {
      return {
        isError: true,
        text: `[ERROR: ${String(lastResponse.error_type ?? "error")}] ${String(lastResponse.message ?? "")}${timing}`,
      };
    }
    if (status === "line_generated") {
      return {
        isError: false,
        text: `GEMINI LINE (${String(lastResponse.model ?? "gemini-4-hydrogen")}): ${String(lastResponse.line ?? "")}${lastResponse.explanation ? ` — ${String(lastResponse.explanation)}` : ""}`,
      };
    }
    if (status === "evaluated_function") {
      return {
        isError: false,
        text: `EVAL ${String(lastResponse.target)}(${JSON.stringify(lastResponse.args ?? [])}) => ${JSON.stringify(lastResponse.result)}${timing}`,
      };
    }
    if (status === "tests_passed" || status === "tests_failed") {
      return {
        isError: status === "tests_failed",
        text: `TESTS ${String(lastResponse.name)}: ${String(lastResponse.passed)}/${String(lastResponse.total)} passed`,
      };
    }
    if (status === "diff_preview") {
      return {
        isError: false,
        text: `DIFF PREVIEW (${String(lastResponse.name)}): ${lastResponse.has_changes ? "Changes detected" : "Identical to saved"}`,
      };
    }
    if (status === "rolled_back") {
      return {
        isError: false,
        text: `ROLLED BACK ${String(lastResponse.name)} to v${String(lastResponse.restored_from_version)} (now v${String(lastResponse.version)})`,
      };
    }
    return {
      isError: false,
      text: `STATUS: ${status}${lastResponse.name ? ` (${String(lastResponse.name)})` : ""}${timing}`,
    };
  })();

  const stdoutText = typeof lastResponse?.stdout === "string" ? lastResponse.stdout : "";

  const artifactInfo = (() => {
    if (!lastResponse) return null;
    if (typeof lastResponse.unified_diff === "string") {
      return { label: "Unified Diff Preview", code: lastResponse.unified_diff, hint: "diff" };
    }
    if (typeof lastResponse.pytest_source === "string") {
      return { label: "Generated pytest Suite", code: lastResponse.pytest_source, hint: "python" };
    }
    if (typeof lastResponse.python_source === "string") {
      return { label: "Python Artifact", code: lastResponse.python_source, hint: "python" };
    }
    if (typeof lastResponse.line === "string") {
      return { label: "Generated Line", code: lastResponse.line, hint: "python" };
    }
    if (lastResponse.json_schema) {
      return {
        label: "JSON Schema",
        code: JSON.stringify(lastResponse.json_schema, null, 2),
        hint: "json",
      };
    }
    return null;
  })();

  const graphLookup = Object.fromEntries(graphNodes.map((n) => [n.name, n]));

  return (
    <div className="min-h-screen bg-slate-100 text-slate-900 font-sans">
      <header className="bg-white border-b border-slate-200 px-6 py-3 flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-2.5">
          <Code2 className="w-5 h-5 text-sky-600" />
          <h1 className="text-base font-bold tracking-tight text-slate-900">
            Dynamic AST &amp; Gemini MCP Studio
          </h1>
        </div>
        <div className="flex flex-wrap items-center gap-3">
          {resolverState && resolverState.tracks.length > 0 && (
            <button
              type="button"
              onClick={() => setResolverModalOpen(true)}
              className="inline-flex items-center gap-1.5 rounded-md bg-amber-600 hover:bg-amber-700 text-white px-2.5 py-1 text-xs font-semibold cursor-pointer"
            >
              <Sparkles className="w-3.5 h-3.5" />
              HATEOAS Error Resolver ({resolverState.tracks[0].steps.length})
            </button>
          )}
          <span className="text-xs font-mono text-slate-600">
            MCP Tools: <strong>{mcpTools.length}</strong>
          </span>
          <a
            href="/api"
            target="_blank"
            rel="noreferrer"
            className="text-xs font-mono font-semibold text-sky-700 hover:underline"
          >
            GET /api
          </a>
          <span className="inline-flex items-center gap-1.5 text-xs font-semibold text-emerald-700">
            <span className="w-2 h-2 rounded-full bg-emerald-500" />
            {connStatus}
          </span>
        </div>
      </header>

      <main className="max-w-[1560px] mx-auto p-5 grid grid-cols-1 lg:grid-cols-12 gap-5">
        {/* Left Column: Editor + Gemini MCP Tool Agent + Execution Output */}
        <section className="lg:col-span-7 space-y-5">
          {/* AST Workbench */}
          <div className="bg-white border border-slate-200 rounded-xl p-4 space-y-3">
            <div className="flex items-center justify-between border-b border-slate-100 pb-2.5">
              <h2 className="text-xs font-bold uppercase tracking-wider text-sky-700">
                AST Workbench &amp; Gemini Line Generation
              </h2>
              <span className="text-xs text-slate-500">
                Tab/Ctrl+Space autocomplete · Alt+G line gen
              </span>
            </div>

            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
              <div>
                <label className="block text-xs font-semibold text-slate-600 mb-1" htmlFor="opSelect">
                  Operation
                </label>
                <select
                  id="opSelect"
                  value={tool}
                  onChange={(e) => setTool(e.target.value as ToolOp)}
                  className="w-full rounded-md border border-slate-300 bg-white px-2.5 py-1.5 text-sm font-mono focus:outline-none focus:ring-2 focus:ring-sky-500/30 focus:border-sky-600"
                >
                  <option value="ast_update">ast_update (PUT /api/ast/&#123;name&#125;)</option>
                  <option value="ast_create">ast_create (POST /api/ast)</option>
                  <option value="eval_ast_call">eval_ast_call (POST /api/ast/&#123;name&#125;/eval)</option>
                  <option value="ast_read">ast_read (GET /api/ast)</option>
                  <option value="ast_delete">ast_delete (DELETE /api/ast/&#123;name&#125;)</option>
                  <option value="ast_convert">ast_convert (POST /api/convert)</option>
                </select>
              </div>
              <div>
                <label
                  className="block text-xs font-semibold text-slate-600 mb-1"
                  htmlFor="targetNameInput"
                >
                  Function / Target Name
                </label>
                <AutocompleteInput
                  id="targetNameInput"
                  value={nameInput}
                  onValueChange={setNameInput}
                  getSuggestions={(val) => buildNameSuggestions(val, tool, items)}
                  className="w-full rounded-md border border-slate-300 bg-white px-2.5 py-1.5 text-sm font-mono focus:outline-none focus:ring-2 focus:ring-sky-500/30 focus:border-sky-600"
                  placeholder="entrypoint"
                />
              </div>
            </div>

            {tool === "eval_ast_call" && (
              <div>
                <label
                  className="block text-xs font-semibold text-slate-600 mb-1"
                  htmlFor="evalArgsInput"
                >
                  Arguments (Literal, JSON list, or kwargs object)
                </label>
                <AutocompleteInput
                  id="evalArgsInput"
                  value={argsInput}
                  onValueChange={setArgsInput}
                  getSuggestions={(val) => buildArgsSuggestions(val, nameInput, items)}
                  submitOnEnter={handleExecute}
                  className="w-full rounded-md border border-slate-300 bg-white px-2.5 py-1.5 text-sm font-mono focus:outline-none focus:ring-2 focus:ring-sky-500/30 focus:border-sky-600"
                  placeholder="[3]"
                />
              </div>
            )}

            {(tool === "ast_create" || tool === "ast_update" || tool === "ast_convert") && (
              <div className="space-y-2">
                <div className="flex flex-wrap gap-2 items-center">
                  <AutocompleteInput
                    id="linePromptInput"
                    value={linePrompt}
                    onValueChange={setLinePrompt}
                    getSuggestions={(val) => buildLinePromptSuggestions(val, nameInput, items)}
                    submitOnEnter={handleGenerateLine}
                    containerClassName="relative flex-1 min-w-[220px]"
                    className="w-full rounded-md border border-slate-300 bg-slate-50 px-3 py-1.5 text-sm font-mono focus:outline-none focus:ring-2 focus:ring-sky-500/30 focus:border-sky-600"
                    placeholder="Gemini Line Gen (Alt+G): describe next line to insert..."
                  />
                  <button
                    type="button"
                    disabled={isGeneratingLine}
                    onClick={handleGenerateLine}
                    className="inline-flex items-center gap-1.5 rounded-md bg-violet-600 hover:bg-violet-700 disabled:opacity-50 text-white px-3 py-1.5 text-xs font-semibold transition-colors cursor-pointer"
                  >
                    <Sparkles className="w-3.5 h-3.5" />
                    {isGeneratingLine ? "Generating..." : "Generate Line"}
                  </button>
                </div>

                <div className="relative">
                  <div className="relative rounded-md border border-slate-300 bg-white focus-within:border-sky-600 focus-within:ring-2 focus-within:ring-sky-500/20">
                    <pre
                      ref={highlightPreRef}
                      aria-hidden="true"
                      className="pointer-events-none absolute inset-0 m-0 p-2.5 font-mono text-[13px] leading-[1.45] whitespace-pre-wrap break-words overflow-hidden"
                    >
                      <HighlightedCode
                        code={codeInput.endsWith("\n") ? `${codeInput} ` : codeInput}
                        formatHint="python"
                      />
                    </pre>
                    <textarea
                      id="codeEditorInput"
                      ref={textareaRef}
                      value={codeInput}
                      onChange={(e) => {
                        const nextVal = e.target.value;
                        setCodeInput(nextVal);
                        const pos = e.target.selectionStart ?? nextVal.length;
                        const tok = getCaretTokenRange(nextVal, pos).token;
                        if (tok.length >= 1) {
                          setCodeAcIdx(0);
                          setCodeAcUserNav(false);
                          setCodeAcOpen(true);
                        } else {
                          setCodeAcOpen(false);
                        }
                      }}
                      onScroll={syncEditorScroll}
                      onKeyDown={handleEditorKeyDown}
                      onBlur={() => {
                        setTimeout(() => {
                          setCodeAcOpen(false);
                          setCodeAcUserNav(false);
                        }, 120);
                      }}
                      spellCheck={false}
                      rows={8}
                      className="relative z-10 block w-full resize-y bg-transparent p-2.5 font-mono text-[13px] leading-[1.45] text-transparent caret-slate-900 selection:bg-sky-500/20 focus:outline-none"
                    />
                  </div>
                  <SuggestionDropdown
                    open={codeAcOpen && codeSuggestions.length > 0}
                    items={codeSuggestions}
                    selectedIdx={codeAcIdx}
                    onSelectIndex={(idx) => {
                      setCodeAcIdx(idx);
                      setCodeAcUserNav(true);
                    }}
                    onAccept={(it) => applyCodeSuggestion(it.insert)}
                  />
                </div>
              </div>
            )}

            <div className="flex flex-wrap gap-2 pt-1">
              <button
                type="button"
                disabled={isExecuting}
                onClick={handleExecute}
                className="inline-flex items-center gap-1.5 rounded-md bg-sky-600 hover:bg-sky-700 disabled:opacity-50 text-white px-3.5 py-1.5 text-xs font-semibold cursor-pointer"
              >
                <Play className="w-3.5 h-3.5" />
                Execute
              </button>
              <button
                type="button"
                disabled={isExecuting || !nameInput.trim()}
                onClick={() =>
                  callRest("POST", `/api/ast/${encodeURIComponent(nameInput.trim())}/diff`, {
                    candidate_code: codeInput,
                  })
                }
                className="inline-flex items-center gap-1.5 rounded-md border border-slate-300 bg-white hover:bg-slate-50 text-slate-800 px-3 py-1.5 text-xs font-semibold cursor-pointer"
              >
                <GitCompare className="w-3.5 h-3.5" />
                Preview Diff
              </button>
              <button
                type="button"
                disabled={isExecuting || !nameInput.trim()}
                onClick={() =>
                  callRest("POST", `/api/ast/${encodeURIComponent(nameInput.trim())}/test`, {})
                }
                className="inline-flex items-center gap-1.5 rounded-md border border-slate-300 bg-white hover:bg-slate-50 text-slate-800 px-3 py-1.5 text-xs font-semibold cursor-pointer"
              >
                <FlaskConical className="w-3.5 h-3.5" />
                Run Tests
              </button>
              <button
                type="button"
                disabled={isExecuting || !nameInput.trim()}
                onClick={() => handleRollback(nameInput.trim())}
                className="inline-flex items-center gap-1.5 rounded-md border border-slate-300 bg-white hover:bg-slate-50 text-slate-800 px-3 py-1.5 text-xs font-semibold cursor-pointer"
              >
                <History className="w-3.5 h-3.5" />
                Rollback
              </button>
              <button
                type="button"
                disabled={isExecuting}
                onClick={() => callRest("POST", "/api/ast/entrypoint/eval", [3])}
                className="inline-flex items-center gap-1.5 rounded-md border border-slate-300 bg-white hover:bg-slate-50 text-slate-800 px-3 py-1.5 text-xs font-semibold cursor-pointer"
              >
                <Terminal className="w-3.5 h-3.5" />
                Eval(3)
              </button>
              <button
                type="button"
                onClick={refreshSnapshot}
                className="inline-flex items-center gap-1.5 rounded-md border border-slate-300 bg-white hover:bg-slate-50 text-slate-800 px-2.5 py-1.5 text-xs font-semibold cursor-pointer"
              >
                <RefreshCw className="w-3.5 h-3.5" />
              </button>
            </div>
          </div>

          {/* Gemini <-> MCP Server Tools Chat */}
          <div className="bg-white border border-slate-200 rounded-xl p-4 space-y-3">
            <div className="flex flex-wrap items-center justify-between gap-2 border-b border-slate-100 pb-2.5">
              <div className="flex items-center gap-2">
                <Bot className="w-4 h-4 text-violet-600" />
                <h2 className="text-xs font-bold uppercase tracking-wider text-violet-700">
                  Gemini MCP Tool Agent
                </h2>
              </div>
              <button
                type="button"
                onClick={() => setShowToolsList((v) => !v)}
                className="inline-flex items-center gap-1 text-xs font-mono text-slate-600 hover:text-slate-900 cursor-pointer"
              >
                <Wrench className="w-3.5 h-3.5" />
                {showToolsList ? "Hide" : "Show"} {mcpTools.length} MCP tools
              </button>
            </div>

            {showToolsList && (
              <div className="rounded-md border border-slate-200 bg-slate-50 p-2.5 max-h-40 overflow-y-auto text-xs font-mono space-y-1">
                {mcpTools.map((t) => (
                  <div key={t.name} className="flex items-baseline gap-2">
                    <span className="font-semibold text-violet-700 shrink-0">{t.name}</span>
                    <span className="text-slate-600 truncate">{t.description}</span>
                  </div>
                ))}
              </div>
            )}

            <div className="flex flex-wrap gap-1.5">
              {[
                "List registered functions and analyze entrypoint metrics",
                "Run smoke tests on entrypoint and evaluate entrypoint(4)",
                "Create a fibonacci(n: int) AST function and test it with n=6",
              ].map((preset) => (
                <button
                  key={preset}
                  type="button"
                  disabled={isChatting}
                  onClick={() => handleSendChat(preset)}
                  className="text-[11px] rounded border border-slate-200 bg-slate-50 hover:bg-slate-100 px-2 py-1 text-slate-700 cursor-pointer"
                >
                  {preset}
                </button>
              ))}
            </div>

            {chatMessages.length > 0 && (
              <div className="space-y-2.5 max-h-72 overflow-y-auto rounded-lg border border-slate-200 bg-slate-50 p-3">
                {chatMessages.map((msg, i) => (
                  <div
                    key={i}
                    className={`rounded-md p-2.5 text-xs ${
                      msg.role === "user"
                        ? "bg-sky-50 border border-sky-200 text-slate-900 ml-6"
                        : msg.isError
                          ? "bg-red-50 border border-red-200 text-red-800 mr-4"
                          : "bg-white border border-slate-200 text-slate-900 mr-4"
                    }`}
                  >
                    <div className="font-mono text-[10px] uppercase tracking-wider text-slate-500 mb-1">
                      {msg.role === "user" ? "Developer" : "Gemini (MCP Connected)"}
                    </div>
                    <div className="whitespace-pre-wrap leading-relaxed">{msg.text}</div>
                    {msg.toolTraces && msg.toolTraces.length > 0 && (
                      <div className="mt-2 pt-2 border-t border-slate-100 space-y-1">
                        <div className="text-[10px] font-mono uppercase text-violet-700 font-semibold">
                          MCP Tool Invocations ({msg.toolTraces.length})
                        </div>
                        {msg.toolTraces.map((tr, tIdx) => {
                          const canRepair =
                            tr.result?.status === "error" &&
                            (extractHateoasActionTracks(tr.result).length > 0 ||
                              Boolean(parseCircularityFromError(tr.result, items)));
                          return (
                            <div
                              key={tIdx}
                              className="rounded bg-slate-50 border border-slate-200 px-2 py-1 font-mono text-[11px] flex flex-wrap items-center justify-between gap-2"
                            >
                              <div>
                                <span className="font-semibold text-violet-700">{tr.tool}</span>
                                <span className="text-slate-600">
                                  ({JSON.stringify(tr.arguments)})
                                </span>
                                <span
                                  className={`ml-2 font-semibold ${
                                    tr.status === "error" ? "text-red-700" : "text-emerald-700"
                                  }`}
                                >
                                  → {tr.status}
                                </span>
                              </div>
                              {canRepair && (
                                <button
                                  type="button"
                                  onClick={() => handlePayloadLifecycle(tr.result, true)}
                                  className="rounded bg-amber-600 hover:bg-amber-700 text-white px-2 py-0.5 text-[10px] font-sans font-semibold cursor-pointer"
                                >
                                  Repair in HATEOAS Resolver
                                </button>
                              )}
                            </div>
                          );
                        })}
                      </div>
                    )}
                  </div>
                ))}
              </div>
            )}

            <div className="flex gap-2">
              <AutocompleteInput
                id="geminiChatInput"
                value={chatInput}
                onValueChange={setChatInput}
                getSuggestions={(val) => buildChatPromptSuggestions(val, items, mcpTools)}
                submitOnEnter={() => handleSendChat()}
                containerClassName="relative flex-1"
                className="w-full rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-violet-500/30 focus:border-violet-600"
                placeholder="Ask Gemini or type a tool name (e.g. ast_run_tests)..."
              />
              <button
                type="button"
                disabled={isChatting || !chatInput.trim()}
                onClick={() => handleSendChat()}
                className="inline-flex items-center gap-1.5 rounded-md bg-violet-600 hover:bg-violet-700 disabled:opacity-50 text-white px-3.5 py-1.5 text-xs font-semibold cursor-pointer"
              >
                <Send className="w-3.5 h-3.5" />
                {isChatting ? "Running..." : "Send"}
              </button>
            </div>
          </div>

          {/* Execution & Analysis Output */}
          <div className="bg-white border border-slate-200 rounded-xl p-4 space-y-3">
            <div className="flex flex-wrap items-center justify-between gap-2 border-b border-slate-100 pb-2">
              <h2 className="text-xs font-bold uppercase tracking-wider text-sky-700">
                Execution, Diff &amp; Test Output
              </h2>
              <div className="flex flex-wrap gap-2">
                {activeResponseCycle && (
                  <button
                    type="button"
                    onClick={() => setActiveCycleInfo(activeResponseCycle)}
                    className="rounded-md bg-red-600 hover:bg-red-700 text-white px-2.5 py-1 text-xs font-semibold cursor-pointer"
                  >
                    Resolve Cycle ({activeResponseCycle.cyclePath.join(" -> ")})
                  </button>
                )}
                {activeResponseTracks.length > 0 && !activeResponseCycle && (
                  <button
                    type="button"
                    onClick={() => handlePayloadLifecycle(lastResponse, true)}
                    className="inline-flex items-center gap-1 rounded-md bg-amber-600 hover:bg-amber-700 text-white px-2.5 py-1 text-xs font-semibold cursor-pointer"
                  >
                    <Sparkles className="w-3.5 h-3.5" />
                    Step-by-Step HATEOAS Error Resolver ({activeResponseTracks[0].steps.length})
                  </button>
                )}
              </div>
            </div>

            <div
              className={`rounded-md border px-3 py-2 font-mono text-xs font-semibold ${
                summaryBanner.isError
                  ? "bg-red-50 border-red-200 text-red-800"
                  : "bg-emerald-50 border-emerald-200 text-emerald-800"
              }`}
            >
              {summaryBanner.text}
            </div>

            {stdoutText && (
              <div>
                <div className="text-xs font-semibold text-slate-600 mb-1">Captured stdout</div>
                <pre className="rounded-md border border-slate-200 bg-slate-900 text-emerald-300 p-2.5 font-mono text-xs overflow-x-auto max-h-40 whitespace-pre-wrap break-words">
                  {stdoutText}
                </pre>
              </div>
            )}

            {artifactInfo && (
              <div>
                <div className="text-xs font-semibold text-slate-600 mb-1">
                  {artifactInfo.label}
                </div>
                <pre className="rounded-md border border-slate-200 bg-slate-50 p-2.5 font-mono text-xs overflow-x-auto max-h-60 whitespace-pre-wrap break-words">
                  <HighlightedCode code={artifactInfo.code} formatHint={artifactInfo.hint} />
                </pre>
              </div>
            )}

            {lastResponse && (
              <div>
                <div className="text-xs font-semibold text-slate-600 mb-1">
                  HATEOAS Payload (application/hal+json)
                </div>
                <pre className="rounded-md border border-slate-200 bg-slate-50 p-2.5 font-mono text-xs overflow-x-auto max-h-64 whitespace-pre-wrap break-words">
                  <HighlightedCode code={JSON.stringify(lastResponse, null, 2)} formatHint="json" />
                </pre>
              </div>
            )}
          </div>
        </section>

        {/* Right Column: Registry + Call Graph + Recent Activity */}
        <section className="lg:col-span-5 space-y-5">
          <div className="bg-white border border-slate-200 rounded-xl p-4 space-y-3">
            <h2 className="text-xs font-bold uppercase tracking-wider text-sky-700 border-b border-slate-100 pb-2">
              Live AST Registry ({items.length})
            </h2>
            {items.length === 0 ? (
              <p className="text-xs text-slate-500">Connecting to Python AST runtime...</p>
            ) : (
              <div className="space-y-3">
                {items.map((item) => {
                  const tab = activeTabs[item.name] || "python_source";
                  const content =
                    tab === "json_ast"
                      ? JSON.stringify(item.formats.json_ast, null, 2)
                      : item.formats[tab];
                  const gNode = graphLookup[item.name];
                  const m = item.metrics;
                  return (
                    <div
                      key={item.name}
                      className="rounded-lg border border-slate-200 bg-slate-50 p-3 space-y-2"
                    >
                      <div className="flex flex-wrap items-center justify-between gap-2">
                        <div className="flex flex-wrap items-center gap-2">
                          <span className="font-mono font-bold text-sm text-slate-900">
                            {item.name}
                          </span>
                          <span className="text-[11px] font-mono text-slate-600">
                            {item.compiled ? "compiled" : "uncompiled"}
                            {m ? ` · CC=${m.cyclomatic_complexity} · ${m.type_coverage_pct}% typed` : ""}
                            {item.history_count ? ` · v${item.history_count}` : ""}
                          </span>
                        </div>
                        <div className="flex flex-wrap items-center gap-1">
                          <button
                            type="button"
                            onClick={() => loadItemIntoEditor(item)}
                            className="inline-flex items-center gap-1 rounded border border-slate-300 bg-white hover:border-sky-500 px-2 py-1 text-[11px] font-semibold cursor-pointer"
                          >
                            <Edit3 className="w-3 h-3" />
                            Edit
                          </button>
                          <button
                            type="button"
                            onClick={() =>
                              callRest("POST", `/api/ast/${encodeURIComponent(item.name)}/test`, {})
                            }
                            className="rounded border border-slate-300 bg-white hover:border-sky-500 px-2 py-1 text-[11px] font-semibold cursor-pointer"
                          >
                            Test
                          </button>
                          <button
                            type="button"
                            onClick={() =>
                              callRest("GET", `/api/ast/${encodeURIComponent(item.name)}/analyze`)
                            }
                            className="rounded border border-slate-300 bg-white hover:border-sky-500 px-2 py-1 text-[11px] font-semibold cursor-pointer"
                          >
                            Pytest
                          </button>
                          <button
                            type="button"
                            onClick={() => handleRollback(item.name)}
                            className="rounded border border-slate-300 bg-white hover:border-sky-500 px-2 py-1 text-[11px] font-semibold cursor-pointer"
                            title="Rollback to previous revision"
                          >
                            Undo
                          </button>
                          <button
                            type="button"
                            onClick={() =>
                              callRest("DELETE", `/api/ast/${encodeURIComponent(item.name)}`)
                            }
                            className="inline-flex items-center rounded border border-red-200 bg-red-50 hover:bg-red-100 text-red-700 px-2 py-1 text-[11px] font-semibold cursor-pointer"
                            title="Delete AST"
                          >
                            <Trash2 className="w-3 h-3" />
                          </button>
                        </div>
                      </div>

                      {gNode && (gNode.calls.length > 0 || gNode.called_by.length > 0) && (
                        <div className="text-[11px] font-mono text-slate-600 flex flex-wrap gap-x-4 gap-y-0.5">
                          {gNode.calls.length > 0 && (
                            <span>calls: {gNode.calls.join(", ")}</span>
                          )}
                          {gNode.called_by.length > 0 && (
                            <span className="text-sky-700">
                              used by: {gNode.called_by.join(", ")}
                            </span>
                          )}
                        </div>
                      )}

                      <div className="flex flex-wrap gap-1">
                        {(
                          [
                            "python_source",
                            "serialized_ast",
                            "pretty_ast",
                            "json_ast",
                          ] as FormatTab[]
                        ).map((fmt) => (
                          <button
                            key={fmt}
                            type="button"
                            onClick={() => setActiveTabs((prev) => ({ ...prev, [item.name]: fmt }))}
                            className={`rounded px-2 py-0.5 text-[11px] font-mono border cursor-pointer ${
                              tab === fmt
                                ? "bg-sky-600 text-white border-sky-600"
                                : "bg-white text-slate-600 border-slate-300"
                            }`}
                          >
                            {fmt}
                          </button>
                        ))}
                      </div>

                      <pre className="rounded border border-slate-200 bg-white p-2.5 font-mono text-xs overflow-x-auto max-h-52 whitespace-pre-wrap break-words">
                        <HighlightedCode code={content} formatHint={tab} />
                      </pre>
                    </div>
                  );
                })}
              </div>
            )}
          </div>

          <div className="bg-white border border-slate-200 rounded-xl p-4 space-y-2">
            <div className="flex items-center gap-2 border-b border-slate-100 pb-2">
              <Activity className="w-3.5 h-3.5 text-sky-700" />
              <h2 className="text-xs font-bold uppercase tracking-wider text-sky-700">
                Recent Activity ({events.length})
              </h2>
            </div>
            <div className="space-y-2 max-h-80 overflow-y-auto">
              {[...events].reverse().map((ev) => (
                <button
                  key={ev.id}
                  type="button"
                  onClick={() => handlePayloadLifecycle(ev.result, true)}
                  className={`w-full text-left rounded-r-md border border-slate-200 border-l-4 bg-slate-50 hover:bg-slate-100 p-2 text-xs cursor-pointer ${
                    ev.status === "error"
                      ? "border-l-red-500"
                      : ev.source === "gemini_mcp"
                        ? "border-l-violet-500"
                        : "border-l-sky-500"
                  }`}
                >
                  <div className="flex justify-between font-mono text-[11px] text-slate-500">
                    <span>
                      #{ev.id} [{ev.source.toUpperCase()}] {ev.tool}
                    </span>
                    <span>
                      {ev.timestamp} · {ev.status}
                    </span>
                  </div>
                </button>
              ))}
            </div>
          </div>
        </section>
      </main>

      <HateoasResolverModal
        open={resolverModalOpen && Boolean(resolverState)}
        policy={resolverState?.policy || ""}
        message={resolverState?.message || ""}
        tracks={resolverState?.tracks || []}
        missingDefaultParams={resolverState?.missingDefaultParams || []}
        supersededCount={resolverState?.supersededCount || 0}
        registryItems={items}
        onClose={() => setResolverModalOpen(false)}
        onResult={async (data, followUp) => {
          await refreshSnapshot();
          handlePayloadLifecycle(data, Boolean(followUp));
        }}
      />

      <CircularityResolverModal
        cycleInfo={activeCycleInfo}
        registryItems={items}
        onResolved={async (data) => {
          setActiveCycleInfo(null);
          await refreshSnapshot();
          handlePayloadLifecycle(data, false);
        }}
        onClose={() => setActiveCycleInfo(null)}
      />
    </div>
  );
}
