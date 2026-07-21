import type { VisualizationSpec } from "@/types/agent";

const SAFE_ID = /^[A-Za-z0-9_-]{1,128}$/;

export function parseVisualizationSpecs(value: unknown): VisualizationSpec[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((item): VisualizationSpec[] => {
    if (!item || typeof item !== "object") return [];
    const raw = item as Record<string, unknown>;
    if (
      raw.schema_version !== 1
      || raw.type !== "candlestick_volume"
      || typeof raw.visualization_id !== "string"
      || !SAFE_ID.test(raw.visualization_id)
      || typeof raw.data_ref !== "string"
      || raw.data_ref !== raw.visualization_id
    ) return [];

    const spec: VisualizationSpec = {
      schema_version: 1,
      type: "candlestick_volume",
      visualization_id: raw.visualization_id,
      data_ref: raw.data_ref,
    };
    const stringFields = [
      "title", "symbol", "market", "timeframe", "source", "adjustment", "timezone",
      "requested_start", "requested_end", "effective_fetch_start", "effective_fetch_end",
      "retention_policy", "actual_start", "actual_end",
      "fetched_at", "fallback_text",
    ] as const;
    for (const field of stringFields) {
      if (typeof raw[field] === "string") spec[field] = raw[field].slice(0, 500);
    }
    if (typeof raw.bar_count === "number" && Number.isInteger(raw.bar_count) && raw.bar_count >= 0) {
      spec.bar_count = raw.bar_count;
    }
    if (typeof raw.truncated === "boolean") spec.truncated = raw.truncated;
    return [spec];
  }).slice(0, 5);
}
