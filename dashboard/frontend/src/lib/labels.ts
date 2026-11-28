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
};

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
