import type { MaintenanceItem, NodeSnapshotState } from "@/types";

const CATEGORY_NAMES: Record<string, string> = {
  QUALITY_CHEMICAL: "Chemical in water",
  QUALITY_LIMIT_VIOLATION: "Unsafe water reading",
  QUALITY_RUNOFF: "Dirty water runoff",
  QUALITY_TEMPORAL_ANOMALY: "Sudden water change",
  PIPE_LEAK: "Main pipe leak",
  FLAT_LEAK: "Home pipe leak",
  LEAK_TANK_DRAIN: "Tank draining fast",
  SUPPLY_EVENT: "Water supply cut",
  SUPPLY_UNCERTAIN: "Supply check needed",
  ABNORMAL_CONSUMPTION: "Unusual high usage",
  ABNORMAL_SUPPLY: "Unusual tank flow",
  PROLONGED_ZERO_FLOW: "No water used for 24h",
  NODE_SILENT: "Sensor stopped talking",
  CLOCK_SKEW: "Sensor clock off",
  VALVE_FAULT: "Valve fault",
};

const CATEGORY_ACTIONS: Record<string, string> = {
  VALVE_FAULT: "The node could not move its water valve: check it by hand and close it if needed.",
  QUALITY_CHEMICAL: "Isolate tank distribution and flush/test water immediately (chemical hazard).",
  QUALITY_LIMIT_VIOLATION: "Inspect tank water quality against IS 10500 limits immediately.",
  QUALITY_RUNOFF: "Inspect rooftop tank cover/inlet for contamination or turbidity ingress.",
  QUALITY_TEMPORAL_ANOMALY: "Inspect tank water quality sensors for sudden drift or early contamination.",
  PIPE_LEAK: "Inspect vertical riser/downtake section and shut down affected branch.",
  FLAT_LEAK: "Inspect flat plumbing/fixtures for internal leak.",
  LEAK_TANK_DRAIN: "Locate active leak draining rooftop tank and verify pump capacity.",
  SUPPLY_EVENT: "Check building pump, power supply, and municipal inlet.",
  SUPPLY_UNCERTAIN: "Inspect tank inflow/level sensor and verify pump operation.",
  ABNORMAL_CONSUMPTION: "Notify resident of unusual water consumption.",
  PROLONGED_ZERO_FLOW: "Verify occupancy or check flat meter/shutoff valve.",
  NODE_SILENT: "Check node power, SD card, and SX1278 LoRa link.",
  CLOCK_SKEW: "Verify node RTC sync and ACK reception.",
};

export function recommendedActionFor(category: string, explicit?: string | null): string | null {
  return explicit ?? CATEGORY_ACTIONS[category] ?? null;
}

export function getReopenValveNodes(
  nodes?: Record<string, NodeSnapshotState> | null,
  queue?: MaintenanceItem[] | null,
): string[] {
  if (!nodes) return [];
  const activeTargets = new Set((queue ?? []).map((item) => item.target));
  return Object.entries(nodes)
    .filter(([nodeId, state]) => state?.valve === "closed" && !activeTargets.has(nodeId))
    .map(([nodeId]) => nodeId)
    .sort();
}

const QUALITY_CLASS_NAMES: Record<string, string> = {
  NORMAL: "Clean",
  RUNOFF: "Dirty runoff",
  CHEMICAL: "Chemical hazard",
};

const SUPPLY_STATE_NAMES: Record<string, string> = {
  NORMAL: "Normal",
  SUPPLY_EVENT: "Supply cut",
  LEAK_TANK_DRAIN: "Draining fast",
  SUPPLY_UNCERTAIN: "Needs check",
};

const STATUS_NAMES: Record<string, string> = {
  NORMAL: "All good",
  LOW: "Minor notice",
  MEDIUM: "Check soon",
  HIGH: "Needs attention",
  CRITICAL: "Fix right away",
};

export function plainCategory(code: string): string {
  return CATEGORY_NAMES[code] ?? code.toLowerCase().replace(/_/g, " ");
}

export function plainQualityClass(code?: string | null): string {
  if (!code) return "Not available";
  return QUALITY_CLASS_NAMES[code] ?? code;
}

export function plainSupplyState(code?: string | null): string {
  if (!code) return "Not available";
  return SUPPLY_STATE_NAMES[code] ?? code;
}

export function plainBuildingStatus(code?: string | null): string {
  if (!code) return "Waiting for data";
  return STATUS_NAMES[code] ?? code;
}

export function severityTone(code?: string | null): "plain" | "red" | "yellow" | "green" {
  if (!code) return "plain";
  const upper = code.toUpperCase();
  if (upper === "CRITICAL" || upper === "HIGH") return "red";
  if (upper === "MEDIUM" || upper === "LOW" || upper === "NOTICE") return "yellow";
  if (upper === "NORMAL" || upper === "OK") return "green";
  return "plain";
}

export function formatUtcTime(unixSec?: number | null): string {
  if (unixSec == null || Number.isNaN(unixSec)) return "Not available";
  return new Date(unixSec * 1000).toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

export function formatNumber(val?: number | null, unit = "", digits = 2): string {
  if (val == null || Number.isNaN(val)) return "Not available";
  return `${val.toFixed(digits)}${unit ? ` ${unit}` : ""}`;
}

export function decodeViolMask(mask?: number | null): string[] {
  if (mask == null || Number.isNaN(mask)) return [];
  const out: string[] = [];
  if (mask & 1) out.push("pH");
  if (mask & 2) out.push("TDS");
  if (mask & 4) out.push("Turbidity");
  return out;
}

const EVIDENCE_LABELS: Record<string, string> = {
  ph: "pH",
  tds: "TDS (ppm)",
  turb: "Turbidity (NTU)",
  score: "Temporal score",
  flow: "Flow (L/s)",
  p_min: "Min pressure (m)",
  p_mean: "Mean pressure (m)",
  error_pct: "Unaccounted water (%)",
  inflow_lps: "Tank inflow (L/s)",
  last_heard_min: "Silent for (min)",
  ahead_s: "Clock ahead (s)",
  suspected_floor: "Suspected section",
  localization_confidence: "Confidence",
  isolation_valve: "Recommended isolation valve",
  rule: "Trigger rule",
  possible_cause: "Possible cause",
};

export function formatEvidence(evidence?: Record<string, unknown>): Array<{ label: string; value: string }> {
  if (!evidence) return [];
  const rows: Array<{ label: string; value: string }> = [];
  for (const [k, v] of Object.entries(evidence)) {
    if (v === undefined || v === null || k === "floor_drops" || k === "suspected_range") continue;
    if (k === "viol" && typeof v === "number") {
      const decoded = decodeViolMask(v);
      rows.push({ label: "IS 10500 limits", value: decoded.length > 0 ? decoded.join(", ") : "None" });
      continue;
    }
    const label = EVIDENCE_LABELS[k] ?? k.replace(/_/g, " ");
    const value = typeof v === "number" ? (Number.isInteger(v) ? String(v) : v.toFixed(2)) : String(v);
    rows.push({ label, value });
  }
  return rows;
}
