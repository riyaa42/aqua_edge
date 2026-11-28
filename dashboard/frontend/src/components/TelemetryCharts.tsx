import { useState } from "react";
import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  decodeViolMask,
  formatNumber,
  formatUtcTime,
  plainCategory,
  plainQualityClass,
  plainSupplyState,
} from "@/lib/labels";
import type { GatewaySnapshot, TelemetryPoint } from "@/types";

interface Props {
  snapshot: GatewaySnapshot | null;
  points: TelemetryPoint[];
  rangeHours: number;
  onRangeChange: (hours: number) => void;
  lineColor: string;
}

type QualityMetric = "ph" | "tds" | "turb";
type LeakMetric = "balance_pct" | "tank_level_m" | "tank_inflow_lps";

export function TelemetryCharts({ snapshot, points, rangeHours, onRangeChange, lineColor }: Props) {
  const [wqMetric, setWqMetric] = useState<QualityMetric>("ph");
  const [lkMetric, setLkMetric] = useState<LeakMetric>("balance_pct");

  const wq = snapshot?.water_quality;
  const lk = snapshot?.leakage;
  const sup = snapshot?.supply;
  const pipeLeak = lk?.pipe_leak;
  const flatLeaks = lk?.flat_leaks ?? [];

  const chartData = points.map((p) => ({
    timeLabel: formatUtcTime(p.created_at_utc),
    ph: p.ph,
    tds: p.tds,
    turb: p.turb,
    balance_pct: p.balance_pct,
    tank_level_m: p.tank_level_m,
    tank_inflow_lps: p.tank_inflow_lps,
  }));

  const hasSelectedWq = chartData.some((d) => d[wqMetric] != null);
  const hasSelectedLk = chartData.some((d) => d[lkMetric] != null);
  const balanceNow = lk?.last_balance_pct;
  const violList = decodeViolMask(wq?.viol_mask);

  const wqLabel = wqMetric === "ph" ? "pH" : wqMetric === "tds" ? "TDS (ppm)" : "Turbidity (NTU)";
  const lkLabel =
    lkMetric === "balance_pct"
      ? "Water Balance Difference (%)"
      : lkMetric === "tank_level_m"
        ? "Tank level (m)"
        : "Tank inflow (L/s)";

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2 border border-black bg-[var(--panel-bg)] px-4 py-2">
        <span className="text-xs font-bold uppercase tracking-wide">Chart Time Window</span>
        <div className="flex gap-1.5">
          {[1, 6, 24].map((h) => (
            <Button
              key={h}
              size="sm"
              variant={rangeHours === h ? "active" : "default"}
              onClick={() => onRangeChange(h)}
            >
              Last {h}h
            </Button>
          ))}
        </div>
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle>Water Check</CardTitle>
            <div className="flex flex-wrap items-center gap-1.5">
              {wq?.temporal_anomaly && <Badge tone="dark">SUDDEN CHANGE</Badge>}
              <Badge tone={wq?.compliant === false ? "dark" : "plain"}>
                {wq?.compliant === false ? "UNSAFE" : wq?.compliant === true ? "SAFE" : "NO DATA"}
              </Badge>
            </div>
          </CardHeader>
          <CardContent className="space-y-3">
            <div className="grid grid-cols-2 gap-2 text-xs sm:grid-cols-3">
              <div className="border border-black bg-[var(--panel-alt)] p-2">
                <div className="text-[10px] uppercase">Class</div>
                <div className="font-bold">{plainQualityClass(wq?.class)}</div>
              </div>
              <div className="border border-black bg-[var(--panel-alt)] p-2">
                <div className="text-[10px] uppercase">pH</div>
                <div className="font-bold">{formatNumber(wq?.ph, "", 2)}</div>
              </div>
              <div className="border border-black bg-[var(--panel-alt)] p-2">
                <div className="text-[10px] uppercase">TDS</div>
                <div className="font-bold">{formatNumber(wq?.tds, "ppm", 0)}</div>
              </div>
              <div className="border border-black bg-[var(--panel-alt)] p-2">
                <div className="text-[10px] uppercase">Turbidity</div>
                <div className="font-bold">{formatNumber(wq?.turb, "NTU", 2)}</div>
              </div>
              <div className="border border-black bg-[var(--panel-alt)] p-2">
                <div className="text-[10px] uppercase">IS 10500 Violations</div>
                <div className="font-bold">
                  {wq?.viol_mask == null
                    ? "Not available"
                    : violList.length > 0
                      ? violList.join(", ")
                      : "None"}
                </div>
              </div>
              <div className="border border-black bg-[var(--panel-alt)] p-2">
                <div className="text-[10px] uppercase">Temporal Check</div>
                <div className="font-bold">
                  {wq?.temporal_anomaly == null
                    ? "Not available"
                    : wq.temporal_anomaly
                      ? `Flagged (${wq.score != null ? wq.score.toFixed(2) : "no score"})`
                      : wq.score != null
                        ? `Normal (${wq.score.toFixed(2)})`
                        : "Normal"}
                </div>
              </div>
            </div>

            <div className="flex flex-wrap gap-1.5">
              {([
                ["ph", "pH"],
                ["tds", "TDS"],
                ["turb", "Turbidity"],
              ] as const).map(([key, label]) => (
                <Button
                  key={key}
                  size="sm"
                  variant={wqMetric === key ? "active" : "default"}
                  onClick={() => setWqMetric(key)}
                >
                  {label}
                </Button>
              ))}
            </div>

            <div className="h-48 border border-black bg-[var(--panel-alt)] p-2">
              {hasSelectedWq ? (
                <ResponsiveContainer width="100%" height="100%">
                  <LineChart data={chartData}>
                    <CartesianGrid stroke="#111111" strokeOpacity={0.15} strokeDasharray="3 3" />
                    <XAxis dataKey="timeLabel" tick={{ fontSize: 10, fill: "#111111" }} />
                    <YAxis tick={{ fontSize: 10, fill: "#111111" }} />
                    <Tooltip
                      contentStyle={{
                        backgroundColor: "var(--panel-bg)",
                        border: "1px solid #111111",
                        borderRadius: "0px",
                        color: "#111111",
                        fontSize: "12px",
                      }}
                    />
                    <Line
                      type="monotone"
                      dataKey={wqMetric}
                      name={wqLabel}
                      stroke={lineColor}
                      strokeWidth={2}
                      dot={false}
                      connectNulls={false}
                    />
                  </LineChart>
                </ResponsiveContainer>
              ) : (
                <div className="flex h-full items-center justify-center text-xs">
                  Waiting for {wqLabel} readings.
                </div>
              )}
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Water Distribution and Tank</CardTitle>
            <Badge tone={lk?.pipe_leak_active ? "dark" : "plain"}>
              {lk?.pipe_leak_active
                ? "PIPE_LEAK ACTIVE"
                : lk?.pipe_leak_active === false
                  ? "No active leak alert"
                  : "NO DATA"}
            </Badge>
          </CardHeader>
          <CardContent className="space-y-3">
            <div className="grid grid-cols-2 gap-2 text-xs sm:grid-cols-5">
              <div className="border border-black bg-[var(--panel-alt)] p-2">
                <div className="text-[10px] uppercase">Supply</div>
                <div className="font-bold">{plainSupplyState(sup?.state)}</div>
              </div>
              <div className="border border-black bg-[var(--panel-alt)] p-2">
                <div className="text-[10px] uppercase">Tank Level</div>
                <div className="font-bold">{formatNumber(sup?.level_m, "m", 2)}</div>
              </div>
              <div className="border border-black bg-[var(--panel-alt)] p-2">
                <div className="text-[10px] uppercase">Tank Inflow</div>
                <div className="font-bold">{formatNumber(sup?.inflow_lps, "L/s", 2)}</div>
              </div>
              <div className="border border-black bg-[var(--panel-alt)] p-2">
                <div className="text-[10px] uppercase">Unaccounted Water</div>
                <div className="font-bold">
                  {balanceNow == null ? "Waiting for more nodes" : `${balanceNow.toFixed(1)}%`}
                </div>
              </div>
              <div className="border border-black bg-[var(--panel-alt)] p-2">
                <div className="text-[10px] uppercase">PIPE_LEAK Alert</div>
                <div className="font-bold">
                  {lk?.pipe_leak_active == null
                    ? "Not available"
                    : lk.pipe_leak_active
                      ? "Active"
                      : "No active leak alert"}
                </div>
              </div>
            </div>

            {pipeLeak && (
              <div className="grid grid-cols-2 gap-2 border border-black bg-[var(--panel-alt)] p-2 text-xs sm:grid-cols-4">
                <div>
                  <div className="text-[10px] uppercase">Target</div>
                  <div className="font-bold">{pipeLeak.target ?? "Not available"}</div>
                </div>
                <div>
                  <div className="text-[10px] uppercase">Confidence</div>
                  <div className="font-bold">{pipeLeak.localization_confidence ?? "Not available"}</div>
                </div>
                <div>
                  <div className="text-[10px] uppercase">Unaccounted Water (%)</div>
                  <div className="font-bold">
                    {pipeLeak.error_pct != null ? `${pipeLeak.error_pct.toFixed(1)}%` : "Not available"}
                  </div>
                </div>
                <div>
                  <div className="text-[10px] uppercase">Recommended isolation valve</div>
                  <div className="font-bold">{pipeLeak.isolation_valve ?? "Not available"}</div>
                </div>
              </div>
            )}

            <div className="flex flex-wrap gap-1.5">
              {([
                ["balance_pct", "Water Balance Difference"],
                ["tank_level_m", "Tank Level"],
                ["tank_inflow_lps", "Tank Inflow"],
              ] as const).map(([key, label]) => (
                <Button
                  key={key}
                  size="sm"
                  variant={lkMetric === key ? "active" : "default"}
                  onClick={() => setLkMetric(key)}
                >
                  {label}
                </Button>
              ))}
            </div>

            <div className="h-48 border border-black bg-[var(--panel-alt)] p-2">
              {hasSelectedLk ? (
                <ResponsiveContainer width="100%" height="100%">
                  <LineChart data={chartData}>
                    <CartesianGrid stroke="#111111" strokeOpacity={0.15} strokeDasharray="3 3" />
                    <XAxis dataKey="timeLabel" tick={{ fontSize: 10, fill: "#111111" }} />
                    <YAxis
                      tick={{ fontSize: 10, fill: "#111111" }}
                      unit={lkMetric === "balance_pct" ? "%" : lkMetric === "tank_level_m" ? "m" : ""}
                    />
                    <Tooltip
                      contentStyle={{
                        backgroundColor: "var(--panel-bg)",
                        border: "1px solid #111111",
                        borderRadius: "0px",
                        color: "#111111",
                        fontSize: "12px",
                      }}
                    />
                    <Line
                      type="monotone"
                      dataKey={lkMetric}
                      name={lkLabel}
                      stroke={lineColor}
                      strokeWidth={2}
                      dot={false}
                      connectNulls={false}
                    />
                  </LineChart>
                </ResponsiveContainer>
              ) : (
                <div className="flex h-full items-center justify-center text-xs">
                  {lkMetric === "balance_pct"
                    ? "Waiting for more nodes before pipe balance can be shown."
                    : `Waiting for ${lkLabel} readings.`}
                </div>
              )}
            </div>

            <div className="space-y-1.5 border border-black bg-[var(--panel-alt)] p-2.5 text-xs">
              <div className="flex items-center justify-between">
                <span className="font-bold uppercase">Individual Home Pipe Leaks</span>
                <Badge>{flatLeaks.length} active</Badge>
              </div>
              {flatLeaks.length === 0 ? (
                <div>No individual home pipe leaks right now.</div>
              ) : (
                <div className="space-y-1">
                  {flatLeaks.map((fl, idx) => {
                    const ev = fl.evidence ?? {};
                    const rule = typeof ev.rule === "string" ? plainCategory(ev.rule) : plainCategory(fl.category);
                    const flow = typeof ev.flow === "number" ? `${ev.flow.toFixed(2)} L/s` : null;
                    return (
                      <div
                        key={`${fl.target}-${idx}`}
                        className="flex flex-wrap items-center justify-between gap-2 border border-black/30 bg-[var(--panel-bg)] px-2 py-1"
                      >
                        <div>
                          <span className="font-bold">{fl.target}</span>{" "}
                          <span>{rule}</span>
                          {flow && <span> ({flow})</span>}
                        </div>
                        <Badge tone={fl.severity === "CRITICAL" || fl.severity === "HIGH" ? "dark" : "plain"}>
                          {fl.severity ?? "NOTICE"}
                        </Badge>
                      </div>
                    );
                  })}
                </div>
              )}
            </div>
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
