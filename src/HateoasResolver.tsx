/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useEffect, useRef, useState } from "react";
import { AlertTriangle, ArrowRight, CheckCircle2, Play, Sparkles, X } from "lucide-react";
import { AutocompleteInput, SuggestionDropdown } from "./AutocompleteField.tsx";
import {
  buildDefaultParamSuggestions,
  buildEndpointSuggestions,
  buildJsonBodySuggestions,
  buildPythonTextareaSuggestions,
  getCaretTokenRange,
} from "./autocomplete.ts";
import { safeFetchJson } from "./http.ts";
import { HighlightedCode } from "./syntax.tsx";
import type {
  CycleInfo,
  HateoasStep,
  HateoasTrack,
  MissingDefaultParam,
  RegistryItem,
} from "./types.ts";

const ACTIONABLE_RELS: Array<[string, string]> = [
  ["set_default_and_retry", "Set Default & Retry"],
  ["recovery_chain", "Full Recovery Chain"],
  ["apply_patched_update", "Apply Updated Signature / Patch"],
  ["install_module", "Install Missing Module"],
  ["create_local_module_file", "Create Local Module Script"],
  ["create_shell", "Create Blank AST Shell"],
  ["bypass_and_create", "Bypass Roundtrip & Create"],
  ["bypass_and_update", "Bypass Roundtrip & Update"],
  ["register_converted_ast", "Register Converted AST"],
  ["recreate", "Recreate Deleted AST"],
  ["retry", "Retry Original Request"],
];

const NAV_ONLY_RELS = new Set([
  "self",
  "resource_self",
  "api_root",
  "ast_collection",
  "cli",
  "list",
  "list_registry",
  "inspect_current",
  "inspect_existing",
]);

function hasUnresolvedTemplate(val: unknown): boolean {
  if (!val) return false;
  try {
    return /\{[^}]+\}/.test(decodeURIComponent(String(val)));
  } catch {
    return /\{[^}]+\}/.test(String(val));
  }
}

function isValidPyIdentifier(val: unknown): val is string {
  return typeof val === "string" && /^[A-Za-z_][A-Za-z0-9_]*$/.test(val);
}

interface MappedHttpStep {
  method: string;
  href: string;
  body?: unknown;
  tool: string;
  arguments: Record<string, unknown>;
}

function toolStepToHttp(stepObj: Record<string, unknown>): MappedHttpStep | null {
  const tool = String(stepObj.tool || stepObj.action || "");
  const args =
    (stepObj.arguments && typeof stepObj.arguments === "object"
      ? stepObj.arguments
      : stepObj.args && typeof stepObj.args === "object"
        ? stepObj.args
        : {}) as Record<string, unknown>;

  if (tool === "ast_create") {
    return { method: "POST", href: "/api/ast", body: args, tool, arguments: args };
  }
  if (tool === "ast_update") {
    const nm = String(args.name || "");
    if (!isValidPyIdentifier(nm)) return null;
    return {
      method: "PUT",
      href: `/api/ast/${encodeURIComponent(nm)}`,
      body: args,
      tool,
      arguments: args,
    };
  }
  if (tool === "ast_read") {
    const nm = args.name ? String(args.name) : "";
    if (nm && hasUnresolvedTemplate(nm)) return null;
    return {
      method: "GET",
      href: nm ? `/api/ast/${encodeURIComponent(nm)}` : "/api/ast",
      body: undefined,
      tool,
      arguments: args,
    };
  }
  if (tool === "ast_delete") {
    const nm = String(args.name || "");
    if (!isValidPyIdentifier(nm)) return null;
    return {
      method: "DELETE",
      href: `/api/ast/${encodeURIComponent(nm)}`,
      body: undefined,
      tool,
      arguments: args,
    };
  }
  if (tool === "eval_ast_call") {
    const target = String(args.target || "");
    if (isValidPyIdentifier(target)) {
      return {
        method: "POST",
        href: `/api/ast/${encodeURIComponent(target)}/eval`,
        body: { args: args.args !== undefined ? args.args : [], kwargs: args.kwargs || {} },
        tool,
        arguments: args,
      };
    }
    if (!target || hasUnresolvedTemplate(target)) return null;
    return { method: "POST", href: "/api/eval", body: args, tool, arguments: args };
  }
  if (tool === "ast_convert") {
    return { method: "POST", href: "/api/convert", body: args, tool, arguments: args };
  }
  if (tool === "ast_install_module") {
    const mod = String(args.module_name || "");
    if (!isValidPyIdentifier(mod)) return null;
    return {
      method: "POST",
      href: `/api/modules/${encodeURIComponent(mod)}/install`,
      body: args,
      tool,
      arguments: args,
    };
  }
  if (tool === "ast_write_module_file") {
    const fname = String(args.filename || "");
    if (!fname || hasUnresolvedTemplate(fname) || !/^[A-Za-z_][A-Za-z0-9_]*\.py$/.test(fname)) {
      return null;
    }
    return { method: "POST", href: "/api/modules/files", body: args, tool, arguments: args };
  }
  if (tool === "ast_chain") {
    return { method: "POST", href: "/api/chain", body: args, tool, arguments: args };
  }
  if (!tool) return null;
  return { method: "POST", href: "/api/command", body: { tool, arguments: args }, tool, arguments: args };
}

function buildStepFromLink(
  rel: string,
  link: Record<string, unknown>
): HateoasStep[] {
  if (link.templated) return [];
  if (link.required_params && !link.arguments && !link.body) return [];
  if (link.href && hasUnresolvedTemplate(link.href)) return [];

  const linkArgs = link.arguments as Record<string, unknown> | undefined;
  if (link.tool === "ast_chain" && linkArgs && Array.isArray(linkArgs.commands)) {
    const mappedSteps = linkArgs.commands.map((cmd) =>
      toolStepToHttp((cmd && typeof cmd === "object" ? cmd : {}) as Record<string, unknown>)
    );
    if (mappedSteps.some((m) => m === null)) return [];
    return (mappedSteps as MappedHttpStep[]).map((mapped, idx) => ({
      index: idx,
      title: `${mapped.tool} (${mapped.method} ${mapped.href})`,
      method: mapped.method,
      href: mapped.href,
      body: mapped.body,
      rawBodyText: mapped.body !== undefined ? JSON.stringify(mapped.body, null, 2) : "",
      parseError: null,
      tool: mapped.tool,
      arguments: mapped.arguments,
      status: "pending",
      statusMessage: "Pending execution",
      response: null,
    }));
  }

  if (link.href && link.method) {
    const bodyObj = link.body as Record<string, unknown> | undefined;
    if (bodyObj && typeof bodyObj === "object") {
      if (bodyObj.filename && hasUnresolvedTemplate(bodyObj.filename)) return [];
      if (bodyObj.module_name && hasUnresolvedTemplate(bodyObj.module_name)) return [];
    }
    const rawBody = link.body !== undefined ? JSON.stringify(link.body, null, 2) : "";
    return [
      {
        index: 0,
        title: `${String(link.tool || rel)} (${String(link.method)} ${String(link.href)})`,
        method: String(link.method),
        href: String(link.href),
        body: link.body,
        rawBodyText: rawBody,
        parseError: null,
        tool: String(link.tool || rel),
        arguments: link.arguments || link.body || {},
        status: "pending",
        statusMessage: "Pending execution",
        response: null,
      },
    ];
  }

  if (link.tool) {
    const mapped = toolStepToHttp(link);
    if (!mapped) return [];
    const rawBody = mapped.body !== undefined ? JSON.stringify(mapped.body, null, 2) : "";
    return [
      {
        index: 0,
        title: `${mapped.tool} (${mapped.method} ${mapped.href})`,
        method: mapped.method,
        href: mapped.href,
        body: mapped.body,
        rawBodyText: rawBody,
        parseError: null,
        tool: mapped.tool,
        arguments: mapped.arguments,
        status: "pending",
        statusMessage: "Pending execution",
        response: null,
      },
    ];
  }

  return [];
}

export function extractHateoasActionTracks(
  data: Record<string, unknown> | null | undefined
): HateoasTrack[] {
  if (!data || typeof data !== "object") return [];
  const links = data._links as Record<string, unknown> | undefined;
  if (!links || typeof links !== "object") return [];

  const tracks: HateoasTrack[] = [];
  const seenRels = new Set<string>();

  for (const [rel, defaultLabel] of ACTIONABLE_RELS) {
    const rawLink = links[rel];
    if (!rawLink || typeof rawLink !== "object") continue;
    const steps = buildStepFromLink(rel, rawLink as Record<string, unknown>);
    if (steps.length > 0) {
      tracks.push({ rel, label: defaultLabel, steps });
      seenRels.add(rel);
    }
  }

  // On execution failures, also surface any extra non-navigational suggested calls in `_links`
  if (data.status === "error") {
    for (const [rel, rawLink] of Object.entries(links)) {
      if (seenRels.has(rel) || NAV_ONLY_RELS.has(rel)) continue;
      if (!rawLink || typeof rawLink !== "object") continue;
      const steps = buildStepFromLink(rel, rawLink as Record<string, unknown>);
      if (steps.length > 0) {
        const pretty = rel
          .split("_")
          .map((w) => (w ? w[0].toUpperCase() + w.slice(1) : ""))
          .join(" ");
        tracks.push({ rel, label: pretty || rel, steps });
      }
    }
  }

  return tracks;
}

export function computeTracksSignature(tracks: HateoasTrack[]): string {
  return JSON.stringify(
    tracks.map((t) => ({
      rel: t.rel,
      steps: t.steps.map((s) => ({ method: s.method, href: s.href, body: s.body })),
    }))
  );
}

export function parseCircularityFromError(
  data: Record<string, unknown> | null | undefined,
  registryItems: RegistryItem[]
): CycleInfo | null {
  if (!data || data.status !== "error") return null;
  const msg = String(data.message || "");
  const isCycleErr =
    data.error_type === "circular_ast_dependency" ||
    data.error_type === "CircularDependencyError" ||
    /circular ast dependency/i.test(msg);
  if (!isCycleErr) return null;

  if (
    Array.isArray(data.cycle_path) &&
    Array.isArray(data.cycle_nodes) &&
    data.cycle_nodes.length >= 1
  ) {
    return {
      cyclePath: data.cycle_path.map(String),
      nodes: data.cycle_nodes as CycleInfo["nodes"],
    };
  }

  const m = msg.match(/'([A-Za-z_][A-Za-z0-9_]*)'/);
  const key = m ? m[1] : null;
  const primary = registryItems.find((i) => i.name === key) || registryItems[0];
  const secondary =
    registryItems.find((i) => i.name !== primary?.name) || primary;
  if (!primary) return null;

  const nodes = [primary, secondary]
    .filter((v): v is RegistryItem => Boolean(v))
    .map((it) => ({
      name: it.name,
      python_source: it.formats?.python_source || "",
      dependencies: it.dependencies || [],
    }));
  return {
    cyclePath:
      nodes.length > 1
        ? [nodes[0].name, nodes[1].name, nodes[0].name]
        : [nodes[0].name, nodes[0].name],
    nodes,
  };
}

interface HighlightedModalTextareaProps {
  value: string;
  onChange: (next: string) => void;
  formatHint: "json" | "python";
  registryItems: RegistryItem[];
  minRows?: number;
}

function HighlightedModalTextarea({
  value,
  onChange,
  formatHint,
  registryItems,
  minRows = 5,
}: HighlightedModalTextareaProps) {
  const taRef = useRef<HTMLTextAreaElement | null>(null);
  const preRef = useRef<HTMLPreElement | null>(null);
  const [acOpen, setAcOpen] = useState(false);
  const [acIdx, setAcIdx] = useState(0);
  const [acUserNav, setAcUserNav] = useState(false);

  const caret = taRef.current?.selectionStart ?? value.length;
  const { token } = getCaretTokenRange(value, caret);
  const suggestions = acOpen
    ? (formatHint === "json"
        ? buildJsonBodySuggestions(token, registryItems)
        : buildPythonTextareaSuggestions(token, value, registryItems)
      ).slice(0, 12)
    : [];

  const acceptSuggestion = (insertText: string) => {
    const ta = taRef.current;
    const pos = ta?.selectionStart ?? value.length;
    const { tokenStart, caretPos } = getCaretTokenRange(value, pos);
    const next = value.slice(0, tokenStart) + insertText + value.slice(caretPos);
    onChange(next);
    setAcOpen(false);
    setAcUserNav(false);
    const nextCaret = tokenStart + insertText.length;
    requestAnimationFrame(() => {
      if (taRef.current) {
        taRef.current.focus();
        taRef.current.setSelectionRange(nextCaret, nextCaret);
      }
    });
  };

  return (
    <div className="relative">
      <div className="relative rounded-md border border-slate-300 bg-white focus-within:border-sky-600 focus-within:ring-2 focus-within:ring-sky-500/20">
        <pre
          ref={preRef}
          aria-hidden="true"
          className="pointer-events-none absolute inset-0 m-0 p-2 font-mono text-xs leading-[1.45] whitespace-pre-wrap break-words overflow-hidden"
        >
          <HighlightedCode
            code={value.endsWith("\n") ? `${value} ` : value}
            formatHint={formatHint}
          />
        </pre>
        <textarea
          ref={taRef}
          rows={minRows}
          spellCheck={false}
          value={value}
          onChange={(e) => {
            const nextVal = e.target.value;
            onChange(nextVal);
            const nextPos = e.target.selectionStart ?? nextVal.length;
            const tok = getCaretTokenRange(nextVal, nextPos).token;
            if (tok.length >= 1) {
              setAcIdx(0);
              setAcUserNav(false);
              setAcOpen(true);
            } else {
              setAcOpen(false);
            }
          }}
          onScroll={() => {
            if (taRef.current && preRef.current) {
              preRef.current.scrollTop = taRef.current.scrollTop;
              preRef.current.scrollLeft = taRef.current.scrollLeft;
            }
          }}
          onBlur={() => {
            setTimeout(() => {
              setAcOpen(false);
              setAcUserNav(false);
            }, 120);
          }}
          onKeyDown={(e) => {
            if (e.ctrlKey && e.code === "Space") {
              e.preventDefault();
              setAcIdx(0);
              setAcUserNav(true);
              setAcOpen(true);
              return;
            }
            if (acOpen && suggestions.length > 0) {
              if (e.key === "ArrowDown") {
                e.preventDefault();
                setAcIdx((p) => (p + 1) % suggestions.length);
                setAcUserNav(true);
                return;
              }
              if (e.key === "ArrowUp") {
                e.preventDefault();
                setAcIdx((p) => (p - 1 + suggestions.length) % suggestions.length);
                setAcUserNav(true);
                return;
              }
              if (e.key === "Tab") {
                e.preventDefault();
                acceptSuggestion((suggestions[acIdx] ?? suggestions[0]).insert);
                return;
              }
              if (e.key === "Enter" && acUserNav) {
                e.preventDefault();
                acceptSuggestion((suggestions[acIdx] ?? suggestions[0]).insert);
                return;
              }
              if (e.key === "Escape") {
                e.preventDefault();
                setAcOpen(false);
                setAcUserNav(false);
                return;
              }
            }
            if (e.key === "Tab") {
              e.preventDefault();
              const ta = e.currentTarget;
              const start = ta.selectionStart;
              const end = ta.selectionEnd;
              const next = value.slice(0, start) + "    " + value.slice(end);
              onChange(next);
              requestAnimationFrame(() => ta.setSelectionRange(start + 4, start + 4));
            }
          }}
          className="relative z-10 block w-full resize-y bg-transparent p-2 font-mono text-xs leading-[1.45] text-transparent caret-slate-900 selection:bg-sky-500/20 focus:outline-none"
        />
      </div>
      <SuggestionDropdown
        open={acOpen && suggestions.length > 0}
        items={suggestions}
        selectedIdx={acIdx}
        onSelectIndex={(idx) => {
          setAcIdx(idx);
          setAcUserNav(true);
        }}
        onAccept={(it) => acceptSuggestion(it.insert)}
      />
    </div>
  );
}

interface HateoasResolverModalProps {
  open: boolean;
  policy: string;
  message: string;
  tracks: HateoasTrack[];
  missingDefaultParams: MissingDefaultParam[];
  supersededCount: number;
  registryItems: RegistryItem[];
  onClose: () => void;
  onResult: (data: Record<string, unknown>, followUp?: boolean) => Promise<void>;
}

export function HateoasResolverModal({
  open,
  policy,
  message,
  tracks,
  missingDefaultParams,
  supersededCount,
  registryItems,
  onClose,
  onResult,
}: HateoasResolverModalProps) {
  const [selectedTrackIdx, setSelectedTrackIdx] = useState(0);
  const [steps, setSteps] = useState<HateoasStep[]>([]);
  const [defaultValues, setDefaultValues] = useState<Record<string, string>>({});
  const [isRunningStep, setIsRunningStep] = useState(false);

  const trackSig = computeTracksSignature(tracks);

  useEffect(() => {
    setSelectedTrackIdx(0);
    setSteps(tracks[0]?.steps.map((s) => ({ ...s })) || []);
    const initDefs: Record<string, string> = {};
    for (const p of missingDefaultParams) {
      initDefs[p.name] =
        p.currentValue !== undefined
          ? String(p.currentValue)
          : String(p.suggested_default ?? "");
    }
    setDefaultValues(initDefs);
  }, [trackSig, message, policy]);

  if (!open || tracks.length === 0) return null;

  const selectTrack = (idx: number) => {
    if (isRunningStep || idx < 0 || idx >= tracks.length) return;
    setSelectedTrackIdx(idx);
    setSteps(tracks[idx].steps.map((s) => ({ ...s })));
  };

  const updateStepHref = (idx: number, nextHref: string) => {
    setSteps((prev) =>
      prev.map((s, i) => (i === idx ? { ...s, href: nextHref.trim() } : s))
    );
  };

  const updateStepBodyText = (idx: number, rawText: string) => {
    setSteps((prev) =>
      prev.map((s, i) => {
        if (i !== idx) return s;
        if (rawText.trim() === "") {
          return { ...s, rawBodyText: rawText, body: undefined, parseError: null };
        }
        try {
          const parsed = JSON.parse(rawText);
          return { ...s, rawBodyText: rawText, body: parsed, parseError: null };
        } catch (err: unknown) {
          return {
            ...s,
            rawBodyText: rawText,
            parseError: `Invalid JSON: ${err instanceof Error ? err.message : String(err)}`,
          };
        }
      })
    );
  };

  const applyDefaultParam = (paramName: string, rawDefaultVal: string) => {
    setDefaultValues((prev) => ({ ...prev, [paramName]: rawDefaultVal }));
    setSteps((prev) => {
      if (!prev[0]) return prev;
      const s0 = prev[0];
      const bodyObj =
        s0.body && typeof s0.body === "object"
          ? ({ ...(s0.body as Record<string, unknown>) })
          : null;
      if (!bodyObj || typeof bodyObj.code_or_ast !== "string") return prev;

      const trimmed = rawDefaultVal.trim();
      const pyVal = trimmed === "" ? "None" : trimmed;
      const escaped = paramName.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
      const sigRegex = new RegExp(
        `(\\b${escaped}\\b(?:\\s*:\\s*[^=,\\)]+)?)(\\s*=\\s*[^,\\)]+)?`
      );
      if (sigRegex.test(bodyObj.code_or_ast)) {
        bodyObj.code_or_ast = bodyObj.code_or_ast.replace(sigRegex, `$1 = ${pyVal}`);
      }
      const nextRaw = JSON.stringify(bodyObj, null, 2);
      return prev.map((st, idx) =>
        idx === 0
          ? { ...st, body: bodyObj, rawBodyText: nextRaw, parseError: null }
          : st
      );
    });
  };

  const executeSingleStep = async (
    currentSteps: HateoasStep[]
  ): Promise<{ nextSteps: HateoasStep[]; superseded: boolean; failed: boolean }> => {
    const idx = currentSteps.findIndex(
      (s) => s.status === "pending" || s.status === "failed"
    );
    if (idx === -1) {
      return { nextSteps: currentSteps, superseded: false, failed: false };
    }

    const step = currentSteps[idx];
    if (step.parseError) {
      const updated = currentSteps.map((s, i) =>
        i === idx ? { ...s, status: "failed" as const, statusMessage: step.parseError! } : s
      );
      setSteps(updated);
      return { nextSteps: updated, superseded: false, failed: true };
    }

    setIsRunningStep(true);
    setSteps(
      currentSteps.map((s, i) =>
        i === idx
          ? {
              ...s,
              status: "running",
              statusMessage: `Calling ${s.method} ${s.href}...`,
            }
          : s
      )
    );

    const init: RequestInit = {
      method: step.method,
      headers: { Accept: "application/hal+json" },
    };
    if (step.body !== undefined && step.method !== "GET" && step.method !== "DELETE") {
      init.headers = { ...init.headers, "Content-Type": "application/json" };
      init.body = JSON.stringify(step.body);
    }

    const { data } = await safeFetchJson(step.href, init);
    setIsRunningStep(false);

    const newTracks = extractHateoasActionTracks(data);
    const newSig = newTracks.length > 0 ? computeTracksSignature(newTracks) : null;

    if (data.status === "error") {
      const errSteps = currentSteps.map((s, i) =>
        i === idx
          ? {
              ...s,
              status: "failed" as const,
              statusMessage:
                data.message || `Failed (${data.error_type || "error"})`,
              response: data,
            }
          : s
      );
      setSteps(errSteps);
      await onResult(data, Boolean(newSig && newSig !== trackSig));
      return {
        nextSteps: errSteps,
        superseded: Boolean(newSig && newSig !== trackSig),
        failed: true,
      };
    }

    const outVal =
      data.result !== undefined
        ? `result = ${JSON.stringify(data.result)}`
        : String(data.status || "ok");
    const doneSteps = currentSteps.map((s, i) =>
      i === idx
        ? {
            ...s,
            status: "done" as const,
            statusMessage: `Completed (${outVal})`,
            response: data,
          }
        : s
    );
    setSteps(doneSteps);

    const remainingPending = doneSteps.some((s) => s.status === "pending");
    const hasFollowupErrorTrack =
      !remainingPending &&
      data.status === "error" &&
      Boolean(newSig && newSig !== trackSig);
    await onResult(data, hasFollowupErrorTrack);
    return { nextSteps: doneSteps, superseded: hasFollowupErrorTrack, failed: false };
  };

  const handleRunRemaining = async () => {
    let working = steps;
    while (true) {
      const idx = working.findIndex(
        (s) => s.status === "pending" || s.status === "failed"
      );
      if (idx === -1) break;
      const outcome = await executeSingleStep(working);
      working = outcome.nextSteps;
      if (outcome.superseded || outcome.failed) break;
    }
  };

  const totalSteps = steps.length;
  const doneCount = steps.filter((s) => s.status === "done").length;
  const failedStep = steps.find((s) => s.status === "failed");
  const nextStepIdx = steps.findIndex(
    (s) => s.status === "pending" || s.status === "failed"
  );
  const allDone = totalSteps > 0 && doneCount === totalSteps;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/50 backdrop-blur-xs p-4"
      role="dialog"
      aria-modal="true"
      aria-labelledby="hateoasResolverTitle"
    >
      <div className="w-full max-w-3xl max-h-[90vh] overflow-y-auto rounded-xl border border-slate-200 bg-white shadow-2xl p-5 space-y-4">
        <div className="flex items-start justify-between gap-3 border-b border-slate-100 pb-3">
          <div>
            <div className="flex flex-wrap items-center gap-2">
              <Sparkles className="w-4 h-4 text-amber-600" />
              <h3 id="hateoasResolverTitle" className="text-base font-bold text-slate-900">
                HATEOAS Step-By-Step Error Resolver ({policy})
              </h3>
            </div>
            <p className="text-xs text-slate-600 mt-1">{message}</p>
          </div>
          <div className="flex items-center gap-2 shrink-0">
            <span className="rounded-md border border-sky-200 bg-sky-50 px-2.5 py-1 font-mono text-xs font-semibold text-sky-800">
              Step {doneCount} / {totalSteps}
            </span>
            <button
              type="button"
              disabled={isRunningStep}
              onClick={onClose}
              className="rounded-md p-1 text-slate-500 hover:bg-slate-100 hover:text-slate-900 cursor-pointer"
              aria-label="Close HATEOAS Error Resolver"
            >
              <X className="w-4 h-4" />
            </button>
          </div>
        </div>

        {supersededCount > 0 && (
          <div className="rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-xs text-amber-900">
            New HATEOAS recovery state received — the suggested repair steps have been updated.
          </div>
        )}

        {tracks.length > 1 && (
          <div className="flex flex-wrap gap-2">
            {tracks.map((tr, idx) => (
              <button
                key={tr.rel}
                type="button"
                disabled={isRunningStep}
                onClick={() => selectTrack(idx)}
                className={`rounded-md px-3 py-1.5 text-xs font-semibold border cursor-pointer ${
                  idx === selectedTrackIdx
                    ? "bg-sky-600 text-white border-sky-600"
                    : "bg-white text-slate-700 border-slate-300 hover:bg-slate-50"
                }`}
              >
                {tr.label} ({tr.steps.length} step{tr.steps.length > 1 ? "s" : ""})
              </button>
            ))}
          </div>
        )}

        <div
          className={`rounded-md border px-3 py-2 font-mono text-xs font-semibold ${
            failedStep
              ? "bg-red-50 border-red-200 text-red-800"
              : allDone
                ? "bg-emerald-50 border-emerald-200 text-emerald-800"
                : "bg-sky-50 border-sky-200 text-sky-900"
          }`}
        >
          {isRunningStep
            ? `Executing step ${nextStepIdx + 1} of ${totalSteps}...`
            : failedStep
              ? `Stopped at Step ${failedStep.index + 1}: ${failedStep.statusMessage}`
              : allDone
                ? `All ${totalSteps} HATEOAS repair step(s) completed.`
                : `Review or edit suggested call parameters below, then execute Step ${nextStepIdx + 1} of ${totalSteps}.`}
        </div>

        {missingDefaultParams.length > 0 && (
          <div className="rounded-lg border border-sky-200 bg-sky-50/50 p-3 space-y-2.5">
            <div className="text-xs font-bold text-slate-800">
              Set Default Value(s) for Missing Argument(s)
            </div>
            {missingDefaultParams.map((p) => (
              <div key={p.name}>
                <label className="block text-[11px] font-semibold text-slate-600 mb-1">
                  Default for &quot;{p.name}&quot; ({p.kind}, {p.annotation})
                </label>
                <AutocompleteInput
                  value={defaultValues[p.name] ?? ""}
                  onValueChange={(val) => applyDefaultParam(p.name, val)}
                  getSuggestions={(val) => buildDefaultParamSuggestions(val, p.annotation)}
                  className="w-full rounded-md border border-slate-300 bg-white px-2.5 py-1.5 text-xs font-mono focus:outline-none focus:ring-2 focus:ring-sky-500/30 focus:border-sky-600"
                  placeholder='Enter Python literal default (e.g. 0, 1, "abc", None)'
                />
              </div>
            ))}
          </div>
        )}

        <div className="space-y-3">
          {steps.map((step, idx) => {
            const isCurrent = idx === nextStepIdx && !allDone;
            return (
              <div
                key={`${step.tool}-${idx}`}
                className={`rounded-lg border p-3 space-y-2 transition-colors ${
                  step.status === "done"
                    ? "border-emerald-300 bg-emerald-50/40"
                    : step.status === "failed"
                      ? "border-red-300 bg-red-50/40"
                      : isCurrent
                        ? "border-sky-500 bg-white ring-2 ring-sky-500/15"
                        : "border-slate-200 bg-slate-50"
                }`}
              >
                <div className="flex items-center justify-between gap-2">
                  <span className="font-mono text-xs font-bold text-slate-900">
                    Step {idx + 1}: {step.tool}
                  </span>
                  <span
                    className={`rounded px-2 py-0.5 font-mono text-[10px] font-bold uppercase ${
                      step.status === "done"
                        ? "bg-emerald-100 text-emerald-800"
                        : step.status === "running"
                          ? "bg-amber-100 text-amber-800"
                          : step.status === "failed"
                            ? "bg-red-100 text-red-800"
                            : "bg-slate-200 text-slate-700"
                    }`}
                  >
                    {step.status}
                  </span>
                </div>
                <div className="text-xs text-slate-600">{step.statusMessage}</div>

                <div>
                  <label className="block text-[11px] font-semibold text-slate-600 mb-1">
                    Endpoint ({step.method})
                  </label>
                  <AutocompleteInput
                    value={step.href}
                    onValueChange={(val) => updateStepHref(idx, val)}
                    getSuggestions={(val) => buildEndpointSuggestions(val, registryItems)}
                    className="w-full rounded-md border border-slate-300 bg-white px-2.5 py-1.5 text-xs font-mono focus:outline-none focus:ring-2 focus:ring-sky-500/30 focus:border-sky-600"
                  />
                </div>

                {step.method !== "GET" && step.method !== "DELETE" && (
                  <div>
                    <label className="block text-[11px] font-semibold text-slate-600 mb-1">
                      Editable Request JSON Payload
                    </label>
                    <HighlightedModalTextarea
                      value={step.rawBodyText}
                      onChange={(raw) => updateStepBodyText(idx, raw)}
                      formatHint="json"
                      registryItems={registryItems}
                      minRows={5}
                    />
                  </div>
                )}

                {step.response && (
                  <pre className="rounded border border-slate-200 bg-slate-50 p-2 font-mono text-[11px] max-h-36 overflow-auto whitespace-pre-wrap break-words">
                    <HighlightedCode
                      code={JSON.stringify(
                        {
                          status: step.response.status,
                          result:
                            step.response.result !== undefined
                              ? step.response.result
                              : step.response.message || step.response.status,
                        },
                        null,
                        2
                      )}
                      formatHint="json"
                    />
                  </pre>
                )}
              </div>
            );
          })}
        </div>

        <div className="flex flex-wrap items-center justify-between gap-2 border-t border-slate-100 pt-3">
          <div className="flex flex-wrap gap-2">
            {!allDone && (
              <>
                <button
                  type="button"
                  disabled={isRunningStep || nextStepIdx === -1}
                  onClick={() => executeSingleStep(steps)}
                  className="inline-flex items-center gap-1.5 rounded-md bg-sky-600 hover:bg-sky-700 disabled:opacity-50 text-white px-3.5 py-1.5 text-xs font-semibold cursor-pointer"
                >
                  <Play className="w-3.5 h-3.5" />
                  {nextStepIdx >= 0 ? `Execute Step ${nextStepIdx + 1}` : "Execute Next Step"}
                </button>
                <button
                  type="button"
                  disabled={isRunningStep || nextStepIdx === -1}
                  onClick={handleRunRemaining}
                  className="inline-flex items-center gap-1.5 rounded-md border border-slate-300 bg-white hover:bg-slate-50 disabled:opacity-50 text-slate-800 px-3.5 py-1.5 text-xs font-semibold cursor-pointer"
                >
                  Run Remaining Steps
                </button>
              </>
            )}
          </div>
          <button
            type="button"
            disabled={isRunningStep}
            onClick={onClose}
            className={`inline-flex items-center gap-1.5 rounded-md px-3.5 py-1.5 text-xs font-semibold cursor-pointer ${
              allDone
                ? "bg-emerald-600 hover:bg-emerald-700 text-white"
                : "border border-slate-300 bg-white hover:bg-slate-50 text-slate-700"
            }`}
          >
            {allDone && <CheckCircle2 className="w-3.5 h-3.5" />}
            {allDone ? "Done (Close)" : "Close"}
          </button>
        </div>
      </div>
    </div>
  );
}

interface CircularityResolverModalProps {
  cycleInfo: CycleInfo | null;
  registryItems: RegistryItem[];
  onResolved: (data: Record<string, unknown>) => Promise<void>;
  onClose: () => void;
}

export function CircularityResolverModal({
  cycleInfo,
  registryItems,
  onResolved,
  onClose,
}: CircularityResolverModalProps) {
  const [leftName, setLeftName] = useState("");
  const [rightName, setRightName] = useState("");
  const [leftCode, setLeftCode] = useState("");
  const [rightCode, setRightCode] = useState("");
  const [statusMsg, setStatusMsg] = useState(
    "Remove the mutual call in at least one function below, then click Save Both & Verify."
  );
  const [isSaving, setIsSaving] = useState(false);

  useEffect(() => {
    if (!cycleInfo || !cycleInfo.nodes.length) return;
    const lNode = cycleInfo.nodes[0];
    const rNode = cycleInfo.nodes[1] || lNode;
    setLeftName(lNode.name);
    setRightName(rNode.name);
    setLeftCode(lNode.python_source || "");
    setRightCode(rNode.python_source || "");
    setStatusMsg(
      "Remove the recursive/mutual call in at least one function below, then click Save Both & Verify."
    );
  }, [cycleInfo]);

  if (!cycleInfo || !cycleInfo.nodes.length) return null;

  const handleSave = async () => {
    if (isSaving || !leftName) return;
    setIsSaving(true);
    setStatusMsg("Saving updated AST nodes and verifying dependency graph...");
    try {
      const resLeft = await safeFetchJson(
        `/api/ast/${encodeURIComponent(leftName)}`,
        {
          method: "PUT",
          headers: { "Content-Type": "application/json", Accept: "application/hal+json" },
          body: JSON.stringify({ code_or_ast: leftCode }),
        }
      );
      if (
        resLeft.data.status === "error" &&
        resLeft.data.error_type !== "circular_ast_dependency"
      ) {
        setStatusMsg(`Left node (${leftName}) error: ${resLeft.data.message}`);
        return;
      }

      if (rightName && rightName !== leftName) {
        const resRight = await safeFetchJson(
          `/api/ast/${encodeURIComponent(rightName)}`,
          {
            method: "PUT",
            headers: { "Content-Type": "application/json", Accept: "application/hal+json" },
            body: JSON.stringify({ code_or_ast: rightCode }),
          }
        );
        if (
          resRight.data.status === "error" &&
          resRight.data.error_type !== "circular_ast_dependency"
        ) {
          setStatusMsg(`Right node (${rightName}) error: ${resRight.data.message}`);
          return;
        }
        const retryLeft = await safeFetchJson(
          `/api/ast/${encodeURIComponent(leftName)}`,
          {
            method: "PUT",
            headers: { "Content-Type": "application/json", Accept: "application/hal+json" },
            body: JSON.stringify({ code_or_ast: leftCode }),
          }
        );
        if (retryLeft.data.error_type === "circular_ast_dependency") {
          setStatusMsg(retryLeft.data.message || "Circularity still present.");
          return;
        }
        await onResolved(retryLeft.data);
        return;
      }

      if (resLeft.data.error_type === "circular_ast_dependency") {
        setStatusMsg(resLeft.data.message || "Circularity still present.");
        return;
      }
      await onResolved(resLeft.data);
    } finally {
      setIsSaving(false);
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/50 backdrop-blur-xs p-4"
      role="dialog"
      aria-modal="true"
    >
      <div className="w-full max-w-5xl max-h-[90vh] overflow-y-auto rounded-xl border-2 border-red-500 bg-white shadow-2xl p-5 space-y-4">
        <div className="flex items-start justify-between gap-3 border-b border-slate-100 pb-3">
          <div>
            <div className="flex items-center gap-2 text-red-700">
              <AlertTriangle className="w-4 h-4" />
              <h3 className="text-base font-bold">
                Circular AST Dependency Detected — Side-by-Side Resolver
              </h3>
            </div>
            <p className="text-xs text-slate-600 mt-1">
              Break the mutual call cycle between these AST functions.
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="rounded-md p-1 text-slate-500 hover:bg-slate-100 cursor-pointer"
          >
            <X className="w-4 h-4" />
          </button>
        </div>

        <div className="flex flex-wrap items-center gap-2 rounded-lg border border-red-200 bg-red-50 px-3 py-2 font-mono text-xs font-bold text-red-900">
          <span>Cycle Path:</span>
          {cycleInfo.cyclePath.map((nodeName, idx) => (
            <React.Fragment key={`${nodeName}-${idx}`}>
              <span className="rounded-md border border-red-300 bg-white px-2 py-0.5">
                {nodeName}
              </span>
              {idx < cycleInfo.cyclePath.length - 1 && <ArrowRight className="w-3.5 h-3.5" />}
            </React.Fragment>
          ))}
        </div>

        <div className="rounded-md border border-red-200 bg-red-50 px-3 py-2 font-mono text-xs font-semibold text-red-800">
          {statusMsg}
        </div>

        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <div className="rounded-lg border border-slate-200 bg-slate-50 p-3 space-y-2">
            <div className="font-mono text-xs font-bold text-slate-900">{leftName}</div>
            <HighlightedModalTextarea
              value={leftCode}
              onChange={setLeftCode}
              formatHint="python"
              registryItems={registryItems}
              minRows={8}
            />
          </div>
          <div className="rounded-lg border border-slate-200 bg-slate-50 p-3 space-y-2">
            <div className="font-mono text-xs font-bold text-slate-900">{rightName}</div>
            <HighlightedModalTextarea
              value={rightCode}
              onChange={setRightCode}
              formatHint="python"
              registryItems={registryItems}
              minRows={8}
            />
          </div>
        </div>

        <div className="flex justify-end gap-2 border-t border-slate-100 pt-3">
          <button
            type="button"
            disabled={isSaving}
            onClick={handleSave}
            className="rounded-md bg-emerald-600 hover:bg-emerald-700 disabled:opacity-50 text-white px-4 py-1.5 text-xs font-semibold cursor-pointer"
          >
            {isSaving ? "Verifying..." : "Save Both & Verify Cycle Resolved"}
          </button>
        </div>
      </div>
    </div>
  );
}
