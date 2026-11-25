/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 */

export interface FunctionMetrics {
  cyclomatic_complexity: number;
  max_nesting_depth: number;
  line_count: number;
  param_count: number;
  type_coverage_pct: number;
  has_docstring: boolean;
  calls: string[];
}

export interface RegistryItem {
  name: string;
  compiled: boolean;
  exposed_as_tool: boolean;
  dependencies?: string[];
  history_count?: number;
  metrics?: FunctionMetrics;
  formats: {
    python_source: string;
    serialized_ast: string;
    pretty_ast: string;
    json_ast: Record<string, unknown>;
  };
}

export interface GraphNode {
  name: string;
  calls: string[];
  called_by: string[];
  metrics: FunctionMetrics;
}

export interface McpToolInfo {
  name: string;
  description: string;
  dynamic_ast_function?: boolean;
}

export interface ToolTrace {
  tool: string;
  arguments: Record<string, unknown>;
  status: string;
  result: Record<string, unknown>;
}

export interface ChatMessage {
  role: "user" | "model";
  text: string;
  toolTraces?: ToolTrace[];
  isError?: boolean;
}

export interface EventEntry {
  id: number;
  timestamp: string;
  source: string;
  route: string;
  tool: string;
  arguments: unknown;
  status: string;
  result: Record<string, unknown>;
}

export interface MissingDefaultParam {
  name: string;
  kind: string;
  annotation: string;
  suggested_default?: unknown;
  currentValue?: string;
}

export interface HateoasStep {
  index: number;
  title: string;
  method: string;
  href: string;
  body?: unknown;
  rawBodyText: string;
  parseError?: string | null;
  tool: string;
  arguments: unknown;
  status: "pending" | "running" | "done" | "failed";
  statusMessage: string;
  response: Record<string, unknown> | null;
}

export interface HateoasTrack {
  rel: string;
  label: string;
  steps: HateoasStep[];
}

export interface CycleNode {
  name: string;
  python_source: string;
  dependencies: string[];
}

export interface CycleInfo {
  cyclePath: string[];
  nodes: CycleNode[];
}

export interface SuggestionItem {
  insert: string;
  label: string;
  meta: string;
}
