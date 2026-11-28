import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { formatNumber, plainCategory } from "@/lib/labels";
import type { GatewaySnapshot } from "@/types";

interface Props {
  snapshot: GatewaySnapshot | null;
}

export function UsageAndNetwork({ snapshot }: Props) {
  const usage = snapshot?.abnormal_consumption ?? [];
  const nh = snapshot?.network_health;
  const silent = nh?.silent_nodes ?? [];
  const skewNodes = nh?.clock_skew_nodes ?? [];
  const balancePaused = nh?.balance_paused;

  return (
    <div className="grid gap-4 lg:grid-cols-2">
      <Card>
        <CardHeader>
          <CardTitle>Usage Check</CardTitle>
          <Badge>{usage.length} flagged</Badge>
        </CardHeader>
        <CardContent className="space-y-2 text-xs">
          {usage.length === 0 ? (
            <div className="border border-black bg-[var(--panel-alt)] p-3">
              Water usage looks normal across reporting nodes.
            </div>
          ) : (
            usage.map((item, idx) => (
              <div
                key={`${item.target}-${item.category}-${idx}`}
                className="flex flex-wrap items-center justify-between gap-2 border border-black bg-[var(--panel-alt)] p-2.5"
              >
                <div>
                  <span className="font-bold">{item.target}</span>{" "}
                  <span>{plainCategory(item.category)}</span>
                </div>
                <Badge>{item.severity ?? "NOTICE"}</Badge>
              </div>
            ))
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Sensor Network</CardTitle>
          <Badge tone={silent.length > 0 || balancePaused ? "dark" : "plain"}>
            {silent.length} silent
          </Badge>
        </CardHeader>
        <CardContent className="space-y-2 text-xs">
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
            <div className="border border-black bg-[var(--panel-alt)] p-2">
              <div className="text-[10px] uppercase">Live Alerts</div>
              <div className="font-bold">{nh?.online_alerts ?? 0}</div>
            </div>
            <div className="border border-black bg-[var(--panel-alt)] p-2">
              <div className="text-[10px] uppercase">Delayed Alerts</div>
              <div className="font-bold">{nh?.offline_alerts ?? 0}</div>
            </div>
            <div className="border border-black bg-[var(--panel-alt)] p-2">
              <div className="text-[10px] uppercase">Saved and Sent</div>
              <div className="font-bold">{nh?.buffered_recovered ?? 0}</div>
            </div>
            <div className="border border-black bg-[var(--panel-alt)] p-2">
              <div className="text-[10px] uppercase">Average Delay</div>
              <div className="font-bold">{formatNumber(nh?.mean_delay_s, "s", 1)}</div>
            </div>
            <div className="border border-black bg-[var(--panel-alt)] p-2">
              <div className="text-[10px] uppercase">Longest Delay</div>
              <div className="font-bold">{formatNumber(nh?.max_delay_s, "s", 1)}</div>
            </div>
            <div className="border border-black bg-[var(--panel-alt)] p-2">
              <div className="text-[10px] uppercase">Bad / Skewed</div>
              <div className="font-bold">
                {nh?.bad_packets ?? 0} / {nh?.skewed_packets ?? 0}
              </div>
            </div>
          </div>

          <div className="border border-black bg-[var(--panel-alt)] p-2">
            {balancePaused == null
              ? "Pipe balance status: Not available."
              : balancePaused
                ? "Pipe balance is paused because a sensor is silent or not enough nodes are reporting."
                : "Pipe balance check is active."}
          </div>

          {silent.length > 0 && (
            <div className="border border-black bg-[var(--panel-alt)] p-2">
              Not heard recently: {silent.join(", ")}
            </div>
          )}

          {skewNodes.length > 0 && (
            <div className="border border-black bg-[var(--panel-alt)] p-2">
              Sensor clock ahead of gateway: {skewNodes.join(", ")}
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
