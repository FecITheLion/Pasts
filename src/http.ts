/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 */

export interface SafeJsonResult<T = Record<string, unknown>> {
  ok: boolean;
  status: number;
  data: T & {
    status?: string;
    error_type?: string;
    message?: string;
  };
}

function summarizeHtmlFallback(rawText: string, httpStatus: number): string {
  const titleMatch = rawText.match(/<title[^>]*>([^<]+)<\/title>/i);
  const pageTitle = titleMatch ? titleMatch[1].trim() : "";
  const suffix = pageTitle ? ` (${pageTitle})` : "";
  return (
    `API endpoint returned HTML instead of JSON (HTTP ${httpStatus}${suffix}). ` +
    `The backend or preview proxy may still be restarting — please retry.`
  );
}

export async function safeParseJsonResponse<T = Record<string, unknown>>(
  resp: Response
): Promise<SafeJsonResult<T>> {
  const rawText = await resp.text();
  const trimmed = rawText.trim();

  if (!trimmed) {
    return {
      ok: resp.ok,
      status: resp.status,
      data: (resp.ok
        ? { status: "ok" }
        : {
            status: "error",
            error_type: "empty_response",
            message: `Empty response body from server (HTTP ${resp.status}).`,
          }) as SafeJsonResult<T>["data"],
    };
  }

  try {
    const parsed = JSON.parse(trimmed) as SafeJsonResult<T>["data"];
    if (parsed && typeof parsed === "object") {
      return { ok: resp.ok, status: resp.status, data: parsed };
    }
    return {
      ok: resp.ok,
      status: resp.status,
      data: {
        status: resp.ok ? "ok" : "error",
        value: parsed,
      } as unknown as SafeJsonResult<T>["data"],
    };
  } catch {
    const looksHtml =
      trimmed.startsWith("<") ||
      trimmed.toLowerCase().includes("<!doctype") ||
      trimmed.toLowerCase().includes("<html");

    const message = looksHtml
      ? summarizeHtmlFallback(trimmed, resp.status)
      : `Invalid JSON response (HTTP ${resp.status}): ${trimmed.slice(0, 180)}`;

    return {
      ok: false,
      status: resp.status || 502,
      data: {
        status: "error",
        error_type: looksHtml ? "html_instead_of_json" : "invalid_json_response",
        message,
      } as SafeJsonResult<T>["data"],
    };
  }
}

export async function safeFetchJson<T = Record<string, unknown>>(
  input: string,
  init?: RequestInit
): Promise<SafeJsonResult<T>> {
  try {
    const resp = await fetch(input, init);
    return await safeParseJsonResponse<T>(resp);
  } catch (err: unknown) {
    return {
      ok: false,
      status: 0,
      data: {
        status: "error",
        error_type: "network_error",
        message: err instanceof Error ? err.message : String(err),
      } as SafeJsonResult<T>["data"],
    };
  }
}
