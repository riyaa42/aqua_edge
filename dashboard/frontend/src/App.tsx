import { useCallback, useEffect, useState } from "react";
import { ArrowLeftRight } from "lucide-react";
import { IncidentHistory } from "@/components/IncidentHistory";
import { MaintenanceQueue } from "@/components/MaintenanceQueue";
import { TelemetryCharts } from "@/components/TelemetryCharts";
import { TopologyTree } from "@/components/TopologyTree";
import { SensorNetwork, UsageAndNetwork } from "@/components/UsageAndNetwork";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { formatUtcTime, plainBuildingStatus, severityTone } from "@/lib/labels";
import type {
  FrameRecord,
  GatewaySummary,
  IncidentItem,
  Mode,
  TelemetryPoint,
  TopologyData,
} from "@/types";

type ConnectionState = "connecting" | "live" | "disconnected";

function frameToPoint(fr: FrameRecord): TelemetryPoint {
  const s = fr.snapshot;
  const wq = s.water_quality;
  const lk = s.leakage;
  const sup = s.supply;
  const nh = s.network_health;
  return {
    frame_id: fr.id,
    gateway_id: fr.gateway_id,
    seq: fr.seq,
    mode: fr.mode,
    created_at_utc: fr.created_at_utc,
    snapshot_t: s.t ?? null,
    building_status: s.building_status ?? null,
    node_count: fr.node_count,
    balance_pct: lk?.last_balance_pct ?? null,
    pipe_leak_active: lk?.pipe_leak_active ?? null,
    quality_class: wq?.class ?? null,
    quality_compliant: wq?.compliant ?? null,
    quality_temporal_anomaly: wq?.temporal_anomaly ?? null,
    quality_viol_mask: wq?.viol_mask ?? null,
    ph: wq?.ph ?? null,
    tds: wq?.tds ?? null,
    turb: wq?.turb ?? null,
    quality_score: wq?.score ?? null,
    supply_state: sup?.state ?? null,
    tank_level_m: sup?.level_m ?? null,
    tank_inflow_lps: sup?.inflow_lps ?? null,
    active_incident_count: s.active_incidents?.length ?? null,
    mean_delay_s: nh?.mean_delay_s ?? null,
    max_delay_s: nh?.max_delay_s ?? null,
  };
}

export function App() {
  const [mode, setMode] = useState<Mode>("edge");
  const [connState, setConnState] = useState<ConnectionState>("connecting");
  const [loading, setLoading] = useState(true);
  const [gateways, setGateways] = useState<GatewaySummary[]>([]);
  const [selectedGateway, setSelectedGateway] = useState<string | null>(null);
  const [latestFrame, setLatestFrame] = useState<FrameRecord | null>(null);
  const [topology, setTopology] = useState<TopologyData | null>(null);
  const [topologyError, setTopologyError] = useState<string | null>(null);
  const [points, setPoints] = useState<TelemetryPoint[]>([]);
  const [history, setHistory] = useState<IncidentItem[]>([]);
  const [rangeHours, setRangeHours] = useState<number>(6);
  const [nowSec, setNowSec] = useState<number>(() => Date.now() / 1000);

  useEffect(() => {
    document.documentElement.setAttribute("data-theme", mode);
  }, [mode]);

  useEffect(() => {
    const id = window.setInterval(() => setNowSec(Date.now() / 1000), 15000);
    return () => window.clearInterval(id);
  }, []);

  const loadInitial = useCallback(async (gwId: string | null, hours: number) => {
    try {
      const [gwRes, topoRes] = await Promise.all([
        fetch("/api/v1/gateways"),
        fetch("/api/v1/topology"),
      ]);

      let activeGw = gwId;
      if (gwRes.ok) {
        const gwJson = await gwRes.json();
        const list: GatewaySummary[] = gwJson.gateways ?? [];
        setGateways(list);
        if (!activeGw && list.length > 0) {
          activeGw = list[0].gateway_id;
          setSelectedGateway(activeGw);
        }
      }

      if (topoRes.ok) {
        const topoJson = await topoRes.json();
        setTopology(topoJson.topology ?? null);
        setTopologyError(null);
      } else {
        const errJson = await topoRes.json().catch(() => ({}));
        setTopology(null);
        setTopologyError(errJson.error ?? `HTTP ${topoRes.status}`);
      }

      const gwQuery = activeGw ? `gateway_id=${encodeURIComponent(activeGw)}&` : "";
      const startUtc = Date.now() / 1000 - hours * 3600;
      const [snapRes, telRes, incRes] = await Promise.all([
        fetch(`/api/v1/snapshots/latest?${gwQuery}`),
        fetch(`/api/v1/telemetry?${gwQuery}start_utc=${startUtc}&limit=500`),
        fetch(`/api/v1/incidents?${gwQuery}status=resolved&limit=100`),
      ]);

      if (snapRes.ok) {
        const snapJson = await snapRes.json();
        setLatestFrame(snapJson.frame ?? null);
      } else if (snapRes.status === 404) {
        setLatestFrame(null);
      }

      if (telRes.ok) {
        const telJson = await telRes.json();
        setPoints(telJson.points ?? []);
      }

      if (incRes.ok) {
        const incJson = await incRes.json();
        setHistory(incJson.incidents ?? []);
      }
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadInitial(selectedGateway, rangeHours);
  }, [loadInitial, selectedGateway, rangeHours]);

  useEffect(() => {
    const params = new URLSearchParams();
    if (selectedGateway) params.set("gateway_id", selectedGateway);
    const es = new EventSource(`/api/v1/stream?${params.toString()}`);

    es.onopen = () => setConnState("live");
    es.onerror = () => setConnState("disconnected");

    es.addEventListener("snapshot", (ev) => {
      setConnState("live");
      try {
        const fr: FrameRecord = JSON.parse((ev as MessageEvent).data);
        setLatestFrame(fr);
        setPoints((prev) => {
          const next = [...prev.filter((p) => p.frame_id !== fr.id), frameToPoint(fr)];
          return next.slice(-500);
        });
        const hist = fr.snapshot.incident_history;
        if (Array.isArray(hist) && hist.length > 0) {
          setHistory((prev) => {
            const byKey = new Map<string, IncidentItem>();
            for (const item of prev) {
              byKey.set(`${item.target}|${item.category}|${item.t_onset}`, item);
            }
            for (const item of hist) {
              byKey.set(`${item.target}|${item.category}|${item.t_onset}`, item);
            }
            return Array.from(byKey.values()).sort(
              (a, b) => (b.t_resolved ?? b.t_last ?? b.t_onset) - (a.t_resolved ?? a.t_last ?? a.t_onset),
            );
          });
        }
      } catch {
        // Ignore malformed SSE payload
      }
    });

    es.addEventListener("resync", () => {
      setConnState("connecting");
      es.close();
      loadInitial(selectedGateway, rangeHours);
    });

    return () => es.close();
  }, [selectedGateway, rangeHours, loadInitial]);

  const snap = latestFrame?.snapshot ?? null;
  const stale = latestFrame != null && nowSec - latestFrame.received_at_utc > 180;
  const chartLineColor = mode === "edge" ? "#3e2c59" : "#264738";

  return (
    <div className="min-h-screen bg-[var(--page-bg)] p-[clamp(12px,4vw,64px)] text-[var(--ink)]">
      <div className="mx-auto w-full max-w-[1600px] border-2 border-black bg-[var(--frame-bg)] p-4 md:p-6 lg:p-8">
        <div className="space-y-4">
          <header className="flex flex-wrap items-center justify-between gap-3 border-2 border-black bg-[var(--panel-header)] px-4 py-3">
            <div className="space-y-0.5">
              <h1 className="text-lg font-bold tracking-tight">
                Aqua Edge {topology ? `(${topology.building})` : ""}
              </h1>
              <p className="text-xs">
                {mode === "edge" ? "Edge Mode (Local Detection)" : "Cloud Mode (Central Detection)"}
                {latestFrame ? ` | Last update: ${formatUtcTime(latestFrame.created_at_utc)}` : ""}
              </p>
            </div>

            <div className="flex flex-wrap items-center gap-2">
              {gateways.length > 1 && (
                <select
                  value={selectedGateway ?? ""}
                  onChange={(e) => setSelectedGateway(e.target.value)}
                  className="border border-black bg-[var(--panel-bg)] px-2 py-1 text-xs font-semibold"
                  aria-label="Select gateway"
                >
                  {gateways.map((g) => (
                    <option key={g.gateway_id} value={g.gateway_id}>
                      {g.gateway_id}
                    </option>
                  ))}
                </select>
              )}
              <Badge tone={severityTone(snap?.building_status)}>
                {plainBuildingStatus(snap?.building_status)}
              </Badge>
              {latestFrame?.buffered && <Badge tone="yellow">Buffered recovery</Badge>}
              {stale && <Badge tone="red">Stale data</Badge>}
              <Badge tone={connState === "live" ? "green" : connState === "connecting" ? "yellow" : "red"}>
                {connState === "live"
                  ? "Live"
                  : connState === "connecting"
                    ? "Connecting"
                    : "Disconnected"}
              </Badge>
            </div>
          </header>

          {!loading && !latestFrame && (
            <Card>
              <CardContent className="text-sm">
                Waiting for the first update from the gateway.
              </CardContent>
            </Card>
          )}

          <div className="grid gap-4 lg:grid-cols-2">
            <MaintenanceQueue
              queue={snap?.maintenance_queue}
              pipeLeak={snap?.leakage?.pipe_leak ?? null}
              nodes={snap?.nodes}
              loading={loading}
            />
            <TopologyTree
              topology={topology}
              topologyError={topologyError}
              nodes={snap?.nodes}
              activeIncidents={snap?.active_incidents}
            />
          </div>

          <TelemetryCharts
            snapshot={snap}
            points={points}
            rangeHours={rangeHours}
            onRangeChange={setRangeHours}
            lineColor={chartLineColor}
          />

          <UsageAndNetwork snapshot={snap} loading={loading} />

          <SensorNetwork snapshot={snap} />

          <IncidentHistory
            history={history}
            nodes={snap?.nodes}
            queue={snap?.maintenance_queue}
          />
        </div>
      </div>

      <Button
        size="icon"
        variant="default"
        onClick={() => setMode((m) => (m === "edge" ? "cloud" : "edge"))}
        title={mode === "edge" ? "Switch to Cloud colors" : "Switch to Edge colors"}
        aria-label={mode === "edge" ? "Switch to Cloud colors" : "Switch to Edge colors"}
        className="fixed bottom-4 right-4 z-50"
      >
        <ArrowLeftRight className="h-4 w-4" />
      </Button>
    </div>
  );
}

export default App;
