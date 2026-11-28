import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { plainCategory } from "@/lib/labels";
import type { MaintenanceItem } from "@/types";

interface Props {
  queue?: MaintenanceItem[];
  loading: boolean;
}

export function MaintenanceQueue({ queue, loading }: Props) {
  const items = queue ?? [];

  return (
    <Card className="flex flex-col">
      <CardHeader>
        <CardTitle>Fix First</CardTitle>
        <Badge>{items.length} open</Badge>
      </CardHeader>
      <CardContent className="flex-1 space-y-3">
        {loading ? (
          <p className="text-sm">Loading priority list...</p>
        ) : items.length === 0 ? (
          <div className="border border-black bg-[var(--panel-alt)] p-4 text-sm">
            No problems need attention right now.
          </div>
        ) : (
          items.map((item, idx) => (
            <div
              key={`${item.target}-${item.category}-${idx}`}
              className="border border-black bg-[var(--panel-alt)] p-3 space-y-1.5"
            >
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="flex items-center gap-2">
                  <Badge tone="dark">#{item.rank ?? idx + 1}</Badge>
                  <span className="font-bold text-sm">{item.target}</span>
                  <span className="text-sm">{plainCategory(item.category)}</span>
                </div>
                <div className="flex items-center gap-2">
                  {item.risk_score != null && (
                    <Badge>Score {item.risk_score}</Badge>
                  )}
                  <Badge tone={item.severity === "CRITICAL" || item.severity === "HIGH" ? "dark" : "plain"}>
                    {item.severity ?? "UNKNOWN"}
                  </Badge>
                </div>
              </div>
              {item.recommended_action && (
                <p className="text-xs leading-relaxed">{item.recommended_action}</p>
              )}
              <div className="flex flex-wrap gap-3 text-[11px]">
                {item.affected_flats != null && (
                  <span>Affected units: {item.affected_flats}</span>
                )}
                {item.duration_min != null && (
                  <span>Active for: {item.duration_min} min</span>
                )}
              </div>
            </div>
          ))
        )}
      </CardContent>
    </Card>
  );
}
