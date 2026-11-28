import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { formatUtcTime, plainCategory } from "@/lib/labels";
import type { IncidentItem } from "@/types";

interface Props {
  history: IncidentItem[];
}

export function IncidentHistory({ history }: Props) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Past Problems</CardTitle>
        <Badge>{history.length} resolved</Badge>
      </CardHeader>
      <CardContent className="text-xs">
        {history.length === 0 ? (
          <div className="border border-black bg-[var(--panel-alt)] p-3">
            No resolved problems recorded yet.
          </div>
        ) : (
          <div className="overflow-x-auto border border-black bg-[var(--panel-alt)]">
            <table className="w-full border-collapse text-left">
              <thead>
                <tr className="border-b border-black bg-[var(--panel-bg)] text-[11px] uppercase">
                  <th className="p-2">Location</th>
                  <th className="p-2">Problem</th>
                  <th className="p-2">Started</th>
                  <th className="p-2">Resolved</th>
                  <th className="p-2">Reason</th>
                </tr>
              </thead>
              <tbody>
                {history.map((item, idx) => (
                  <tr key={`${item.target}-${item.category}-${item.t_onset}-${idx}`} className="border-b border-black/25 last:border-b-0">
                    <td className="p-2 font-bold">{item.target}</td>
                    <td className="p-2">{plainCategory(item.category)}</td>
                    <td className="p-2">{formatUtcTime(item.t_onset)}</td>
                    <td className="p-2">{formatUtcTime(item.t_resolved)}</td>
                    <td className="p-2">{item.resolution_reason ?? "Cleared"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
