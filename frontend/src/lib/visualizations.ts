import type {
  CandlestickVisualizationSpec,
  SimilarityChannelWeights,
  SimilarityRankingVisualizationSpec,
  VisualizationSpec,
} from "@/types/agent";

const SAFE_ID = /^[A-Za-z0-9_-]{1,128}$/;
const SHA256 = /^[0-9a-f]{64}$/;
const SIMILARITY_RUN_ID = /^similarity_run:([0-9a-f]{64})$/;
const SYMBOL = /^[A-Z0-9][A-Z0-9._-]{0,31}$/;
const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;

function isIsoDate(value: unknown): value is string {
  if (typeof value !== "string" || !ISO_DATE.test(value)) return false;
  const [year, month, day] = value.split("-").map(Number);
  const parsed = new Date(Date.UTC(year, month - 1, day));
  return parsed.getUTCFullYear() === year
    && parsed.getUTCMonth() === month - 1
    && parsed.getUTCDate() === day;
}

function parseWeights(value: unknown): SimilarityChannelWeights | null {
  if (!value || typeof value !== "object") return null;
  const raw = value as Record<string, unknown>;
  const business = raw.business;
  const factor = raw.factor;
  const priceVolume = raw.price_volume;
  if (
    typeof business !== "number"
    || typeof factor !== "number"
    || typeof priceVolume !== "number"
    || ![business, factor, priceVolume].every((weight) => Number.isFinite(weight) && weight >= 0 && weight <= 1)
    || Math.abs(business + factor + priceVolume - 1) > 1e-9
  ) return null;
  return { business, factor, price_volume: priceVolume };
}

function parseCandlestickSpec(raw: Record<string, unknown>): CandlestickVisualizationSpec | null {
  if (
    raw.type !== "candlestick_volume"
    || typeof raw.visualization_id !== "string"
    || !SAFE_ID.test(raw.visualization_id)
    || typeof raw.data_ref !== "string"
    || raw.data_ref !== raw.visualization_id
  ) return null;

  const spec: CandlestickVisualizationSpec = {
    schema_version: 1,
    type: "candlestick_volume",
    visualization_id: raw.visualization_id,
    data_ref: raw.data_ref,
  };
  const stringFields = [
    "title", "symbol", "market", "timeframe", "source", "adjustment", "timezone",
    "requested_start", "requested_end", "effective_fetch_start", "effective_fetch_end",
    "retention_policy", "actual_start", "actual_end", "fetched_at", "fallback_text",
  ] as const;
  for (const field of stringFields) {
    if (typeof raw[field] === "string") spec[field] = raw[field].slice(0, 500);
  }
  if (typeof raw.bar_count === "number" && Number.isInteger(raw.bar_count) && raw.bar_count >= 0) {
    spec.bar_count = raw.bar_count;
  }
  if (typeof raw.dropped_bar_count === "number" && Number.isInteger(raw.dropped_bar_count) && raw.dropped_bar_count >= 0) {
    spec.dropped_bar_count = raw.dropped_bar_count;
  }
  if (typeof raw.truncated === "boolean") spec.truncated = raw.truncated;
  return spec;
}

function parseSimilaritySpec(raw: Record<string, unknown>): SimilarityRankingVisualizationSpec | null {
  const runMatch = typeof raw.similarity_run_id === "string"
    ? raw.similarity_run_id.match(SIMILARITY_RUN_ID)
    : null;
  const weights = parseWeights(raw.weights);
  const targetSymbols = Array.isArray(raw.target_symbols)
    ? raw.target_symbols.filter((symbol): symbol is string => typeof symbol === "string" && SYMBOL.test(symbol))
    : [];
  if (
    raw.type !== "similarity_ranking"
    || typeof raw.visualization_id !== "string"
    || !SAFE_ID.test(raw.visualization_id)
    || typeof raw.data_ref !== "string"
    || raw.data_ref !== raw.visualization_id
    || !runMatch
    || typeof raw.similarity_sha256 !== "string"
    || !SHA256.test(raw.similarity_sha256)
    || raw.similarity_sha256 !== runMatch[1]
    || !Array.isArray(raw.target_symbols)
    || targetSymbols.length !== raw.target_symbols.length
    || targetSymbols.length < 1
    || targetSymbols.length > 12
    || new Set(targetSymbols).size !== targetSymbols.length
    || !isIsoDate(raw.as_of)
    || typeof raw.candidate_universe !== "string"
    || !raw.candidate_universe.trim()
    || raw.candidate_universe.length > 128
    || typeof raw.candidate_count !== "number"
    || !Number.isInteger(raw.candidate_count)
    || raw.candidate_count < 1
    || raw.candidate_count > 50
    || !weights
  ) return null;

  const spec: SimilarityRankingVisualizationSpec = {
    schema_version: 1,
    type: "similarity_ranking",
    visualization_id: raw.visualization_id,
    data_ref: raw.data_ref,
    similarity_run_id: raw.similarity_run_id as string,
    similarity_sha256: raw.similarity_sha256,
    target_symbols: targetSymbols,
    as_of: raw.as_of,
    candidate_universe: raw.candidate_universe,
    candidate_count: raw.candidate_count,
    weights,
  };
  if (typeof raw.title === "string" && raw.title.trim()) spec.title = raw.title.slice(0, 200);
  if (typeof raw.fallback_text === "string" && raw.fallback_text.trim()) {
    spec.fallback_text = raw.fallback_text.slice(0, 500);
  }
  return spec;
}

export function parseVisualizationSpecs(value: unknown): VisualizationSpec[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((item): VisualizationSpec[] => {
    if (!item || typeof item !== "object") return [];
    const raw = item as Record<string, unknown>;
    if (raw.schema_version !== 1) return [];
    const spec = raw.type === "similarity_ranking"
      ? parseSimilaritySpec(raw)
      : parseCandlestickSpec(raw);
    return spec ? [spec] : [];
  }).slice(0, 5);
}
