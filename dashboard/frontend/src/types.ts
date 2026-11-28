export type Mode = "edge" | "cloud";

export interface TopologyNode {
  id: string;
  type: "tank" | "junction" | "flat" | string;
  valve?: boolean;
  label?: string;
  children?: TopologyNode[];
}

export interface TopologyData {
  schema_version: number;
  building: string;
  root: TopologyNode;
}

export interface MaintenanceItem {
  rank?: number;
  target: string;
  category: string;
  risk_score?: number;
  severity?: string;
  affected_flats?: number;
  duration_min?: number;
  symptoms?: string[];
  evidence?: Record<string, unknown>;
  recommended_action?: string;
}

export interface IncidentItem {
  target: string;
  category: string;
  symptoms?: string[];
  t_onset: number;
  t_last?: number;
  t_resolved?: number | null;
  resolution_reason?: string | null;
  repeat_count?: number;
  affected_flats?: number;
  risk_score?: number | null;
  severity?: string | null;
  status?: string;
  recommended_action?: string | null;
  evidence?: Record<string, unknown>;
}

export interface NodeSnapshotState {
  status?: string;
  last_seen?: number | null;
  last_sum?: Record<string, unknown> | null;
}

export interface GatewaySnapshot {
  t?: number;
  building_status?: string;
  maintenance_queue?: MaintenanceItem[];
  active_incidents?: IncidentItem[];
  incident_history?: IncidentItem[];
  water_quality?: {
    class?: string;
    compliant?: boolean;
    viol_mask?: number;
    temporal_anomaly?: boolean;
    ph?: number | null;
    tds?: number | null;
    turb?: number | null;
    score?: number | null;
  };
  leakage?: {
    pipe_leak_active?: boolean;
    last_balance_pct?: number | null;
    pipe_leak?: {
      target?: string;
      suspected_floor?: string | null;
      suspected_range?: string[];
      localization_confidence?: string;
      isolation_valve?: string | null;
      error_pct?: number | null;
    } | null;
    flat_leaks?: MaintenanceItem[];
  };
  supply?: {
    state?: string;
    level_m?: number | null;
    inflow_lps?: number | null;
    t?: number | null;
  };
  abnormal_consumption?: MaintenanceItem[];
  network_health?: {
    silent_nodes?: string[];
    balance_paused?: boolean;
    bad_packets?: number;
    skewed_packets?: number;
    clock_skew_nodes?: string[];
    online_alerts?: number;
    offline_alerts?: number;
    buffered_recovered?: number;
    mean_delay_s?: number;
    max_delay_s?: number;
  };
  nodes?: Record<string, NodeSnapshotState>;
}

export interface FrameRecord {
  id: number;
  gateway_id: string;
  seq: number;
  mode: Mode;
  created_at_utc: number;
  received_at_utc: number;
  buffered: boolean;
  node_count: number;
  snapshot: GatewaySnapshot;
}

export interface TelemetryPoint {
  frame_id: number;
  gateway_id: string;
  seq: number;
  mode: Mode;
  created_at_utc: number;
  snapshot_t: number | null;
  building_status: string | null;
  node_count: number;
  balance_pct: number | null;
  pipe_leak_active: boolean | null;
  quality_class: string | null;
  quality_compliant: boolean | null;
  quality_temporal_anomaly?: boolean | null;
  quality_viol_mask?: number | null;
  ph: number | null;
  tds: number | null;
  turb: number | null;
  quality_score: number | null;
  supply_state: string | null;
  tank_level_m: number | null;
  tank_inflow_lps: number | null;
  active_incident_count: number | null;
  mean_delay_s: number | null;
  max_delay_s: number | null;
}

export interface GatewaySummary {
  gateway_id: string;
  latest_seq: number;
  acked_seq: number;
  latest_mode: Mode;
  last_received_at_utc: number;
  node_count: number;
}
