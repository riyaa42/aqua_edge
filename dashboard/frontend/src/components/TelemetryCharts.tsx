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
import { formatNumber, formatUtcTime, plainQualityClass, plainSupplyState } from "@/lib/labels";
import type { GatewaySnapshot, TelemetryPoint } from "@/types";

interface Props {
  snapshot: GatewaySnapshot | null;
  points: TelemetryPoint[];
  rangeHours: number;
  onRangeChange: (hours: number) => void;
  lineColor: string;
}

export function TelemetryCharts({ snapshot, points, rangeHours, onRangeChange, lineColor }: Props) {
  const wq = snapshot?.water_quality;
  const lk = snapshot?.leakage;
  const sup = snapshot?.supply;

  const chartData = points.map((p) => ({
    timeLabel: formatUtcTime(p.created_at_utc),
    ph: p.ph,
    tds: p.tds,
    balance_pct: p.balance_pct,
  }));

  const hasQualityData = chartData.some((d) => d.ph != null || d.tds != null);
  const hasBalanceData = chartData.some((d) => d.balance_pct != null);
  const balanceNow = lk?.last_balance_pct;

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
            <Badge tone={wq?.compliant === false ? "dark" : "plain"}>
              {wq?.compliant === false ? "UNSAFE" : wq?.compliant === true ? "SAFE" : "NO DATA"}
            </Badge>
          </CardHeader>
          <CardContent className="space-y-3">
            <div className="grid grid-cols-2 gap-2 text-xs sm:grid-cols-4">
              <div className="border border-black bg-[var(--panel-alt)] p-2">
                <div className="text-[10px] uppercase">Status</div>
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
                <div className="text-[10px] uppercase">Cloudiness</div>
                <div className="font-bold">{formatNumber(wq?.turb, "NTU", 2)}</div>
              </div>
            </div>

            <div className="h-48 border border-black bg-[var(--panel-alt)] p-2">
              {hasQualityData ? (
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
                      dataKey="ph"
                      name="pH"
                      stroke={lineColor}
                      strokeWidth={2}
                      dot={false}
                      connectNulls={false}
                    />
                  </LineChart>
                </ResponsiveContainer>
              ) : (
                <div className="flex h-full items-center justify-center text-xs">
                  Waiting for water quality readings.
                </div>
              )}
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Leak Check and Tank</CardTitle>
            <Badge tone={lk?.pipe_leak_active ? "dark" : "plain"}>
              {lk?.pipe_leak_active ? "PIPE LEAK" : lk?.pipe_leak_active === false ? "CLEAR" : "NO DATA"}
            </Badge>
          </CardHeader>
          <CardContent className="space-y-3">
            <div className="grid grid-cols-2 gap-2 text-xs sm:grid-cols-4">
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
                <div className="text-[10px] uppercase">Pipe Loss</div>
                <div className="font-bold">
                  {balanceNow == null ? "Waiting for more nodes" : `${balanceNow.toFixed(1)}%`}
                </div>
              </div>
            </div>

            <div className="h-48 border border-black bg-[var(--panel-alt)] p-2">
              {hasBalanceData ? (
                <ResponsiveContainer width="100%" height="100%">
                  <LineChart data={chartData}>
                    <CartesianGrid stroke="#111111" strokeOpacity={0.15} strokeDasharray="3 3" />
                    <XAxis dataKey="timeLabel" tick={{ fontSize: 10, fill: "#111111" }} />
                    <YAxis tick={{ fontSize: 10, fill: "#111111" }} unit="%" />
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
                      dataKey="balance_pct"
                      name="Pipe loss %"
                      stroke={lineColor}
                      strokeWidth={2}
                      dot={false}
                      connectNulls={false}
                    />
                  </LineChart>
                </ResponsiveContainer>
              ) : (
                <div className="flex h-full items-center justify-center text-xs">
                  Waiting for more nodes before pipe balance can be shown.
                </div>
              )}
            </div>
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
