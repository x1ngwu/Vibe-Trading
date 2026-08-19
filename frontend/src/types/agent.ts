/** Chat message types */
export type AgentMessageType =
  | "user" | "thinking" | "tool_call" | "tool_result"
  | "answer" | "error" | "run_complete" | "compact" | "swarm_status";

export type SwarmAgentDisplayStatus =
  | "waiting"
  | "running"
  | "done"
  | "failed"
  | "blocked"
  | "retry"
  | "cancelled";

export interface SwarmAgentStatus {
  agentId: string;
  taskId?: string;
  role?: string;
  status: SwarmAgentDisplayStatus;
  tool?: string;
  elapsed_s?: number;
  iterations?: number;
  startedAt?: number;
  lastText?: string;
  error?: string;
  layer?: number;
}

export interface SwarmRunStatus {
  runId: string;
  preset: string;
  status: "pending" | "running" | "completed" | "failed" | "cancelled" | "unknown";
  currentLayer: number;
  totalLayers: number;
  startedAt: number;
  completedAt?: number;
  agents: SwarmAgentStatus[];
}

export interface CandlestickVisualizationSpec {
  schema_version: 1;
  type: "candlestick_volume";
  visualization_id: string;
  data_ref: string;
  title?: string;
  symbol?: string;
  market?: string;
  timeframe?: string;
  source?: string;
  provider?: string;
  provider_version?: string;
  canonical_version?: string;
  adjustment?: string;
  timezone?: string;
  watermark?: string;
  units?: Record<string, unknown>;
  completeness?: string;
  fallback?: boolean;
  fallback_reason?: string;
  warnings?: string[];
  requested_start?: string;
  requested_end?: string;
  effective_fetch_start?: string;
  effective_fetch_end?: string;
  retention_policy?: string;
  actual_start?: string;
  actual_end?: string;
  fetched_at?: string;
  bar_count?: number;
  dropped_bar_count?: number;
  truncated?: boolean;
  fallback_text?: string;
}

export interface SimilarityChannelWeights {
  business: number;
  factor: number;
  price_volume: number;
}

export interface SimilarityRankingVisualizationSpec {
  schema_version: 1;
  type: "similarity_ranking";
  visualization_id: string;
  data_ref: string;
  title?: string;
  similarity_run_id: string;
  similarity_sha256: string;
  target_symbols: string[];
  as_of: string;
  candidate_universe: string;
  candidate_count: number;
  weights: SimilarityChannelWeights;
  fallback_text?: string;
}

export interface StrategyConfirmationVisualizationSpec {
  schema_version: 1;
  type: "strategy_confirmation";
  visualization_id: string;
  data_ref: string;
  title: string;
  stream_id: string;
  version_id: string;
  version_number: number;
  parent_version_id: string | null;
  head_event_id: string;
  head_revision: number;
  confirmation_hash: string | null;
  fallback_text: string;
}

export interface BacktestResultVisualizationSpec {
  schema_version: 1;
  type: "backtest_result";
  visualization_id: string;
  data_ref: string;
  title: string;
  stream_id: string;
  strategy_version_id: string;
  strategy_version_number: number;
  job_id: string;
  fallback_text: string;
}

export type VisualizationSpec =
  | CandlestickVisualizationSpec
  | SimilarityRankingVisualizationSpec
  | StrategyConfirmationVisualizationSpec
  | BacktestResultVisualizationSpec;
export interface AgentMessage {
  id: string;
  type: AgentMessageType;
  content: string;
  tool?: string;
  args?: Record<string, string>;
  status?: "running" | "ok" | "error";
  elapsed_ms?: number;
  timestamp: number;
  runId?: string;
  swarmRunId?: string;
  swarmStatus?: SwarmRunStatus;
  metrics?: Record<string, number>;
  equityCurve?: Array<{ time: string; equity: number | string }>;
  visualizations?: VisualizationSpec[];
  /** Phase label for thinking entries */
  stage?: string;
  /** Shadow Account id if render_shadow_report fired in this turn (RunCompleteCard renders a "View Shadow Report" button). */
  shadowId?: string;
}

/** Tool call tracking entry */
export interface ToolCallEntry {
  id: string;
  tool: string;
  arguments: Record<string, string>;
  status: "running" | "ok" | "error";
  preview?: string;
  elapsed_ms?: number;
  /** Live elapsed seconds while the tool is running (heartbeat). */
  elapsed_s?: number;
  /**
   * Structured progress emitted from the tool. All fields optional —
   * presence of `current`/`total > 0` indicates a determinate progress signal.
   */
  progress?: {
    stage?: string;
    current?: number;
    total?: number;
    message?: string;
  };
  timestamp: number;
}
