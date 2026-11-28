import { useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  formatEvidence,
  formatUtcTime,
  getReopenValveNodes,
  plainCategory,
  recommendedActionFor,
} from "@/lib/labels";
import type { IncidentItem, MaintenanceItem, NodeSnapshotState } from "@/types";

interface Props {
  history: IncidentItem[];
  nodes?: Record<string, NodeSnapshotState>;
  queue?: MaintenanceItem[];
}

function PastProblemCard({
  item,
  reopenReady,
}: {
  item: IncidentItem;
  reopenReady: boolean;
}) {
  const [expanded, setExpanded] = useState(false);
  const evidenceRows = formatEvidence(item.evidence);
  const symptoms = item.symptoms ?? [];
  const recommendation = recommendedActionFor(item.category, item.recommended_action);

  return (
    <div className="border border-black bg-[var(--panel-alt)] p-3 space-y-2">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap items-center gap-2">
          <span className="font-bold text-sm">{item.target}</span>
          <span className="text-sm">{plainCategory(item.category)}</span>
          {reopenReady && <Badge tone="yellow">Reopen valve: {item.target}</Badge>}
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Badge tone="green">{item.resolution_reason ?? "Cleared"}</Badge>
        </div>
      </div>

      {recommendation && (
        <p className="text-xs leading-relaxed">
          <span className="font-bold">Recommendation: </span>
          {recommendation}
        </p>
      )}

      <div className="flex flex-wrap items-center justify-between gap-2 text-[11px]">
        <div className="flex flex-wrap gap-3">
          <span>Started: {formatUtcTime(item.t_onset)}</span>
          <span>Resolved: {formatUtcTime(item.t_resolved)}</span>
          {item.repeat_count != null && item.repeat_count > 1 && (
            <span>Repeats: {item.repeat_count}</span>
          )}
        </div>
        <button
          type="button"
          onClick={() => setExpanded((v) => !v)}
          className="inline-flex items-center gap-1 border border-black bg-[var(--panel-bg)] px-2 py-0.5 text-[11px] font-semibold cursor-pointer"
        >
          {expanded ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
          Why was this flagged?
        </button>
      </div>

      {expanded && (
        <div className="border border-black bg-[var(--panel-deep)] p-2.5 text-xs space-y-1.5">
          <div>
            <span className="font-bold">Correlated symptoms: </span>
            <span>{symptoms.length > 0 ? symptoms.join(", ") : "None"}</span>
          </div>
          {evidenceRows.length > 0 ? (
            <div className="grid grid-cols-2 gap-1.5 sm:grid-cols-3">
              {evidenceRows.map((row) => (
                <div key={row.label} className="border border-black/30 bg-[var(--panel-alt)] px-2 py-1 text-[11px]">
                  <span className="uppercase">{row.label}: </span>
                  <span className="font-bold">{row.value}</span>
                </div>
              ))}
            </div>
          ) : (
            <div className="text-[11px]">No extra evidence readings attached.</div>
          )}
        </div>
      )}
    </div>
  );
}

export function IncidentHistory({ history, nodes, queue }: Props) {
  const reopenNodes = getReopenValveNodes(nodes, queue);
  const reopenSet = new Set(reopenNodes);

  return (
    <Card>
      <CardHeader>
        <CardTitle>Past Problems</CardTitle>
        <Badge>{history.length} resolved</Badge>
      </CardHeader>
      <CardContent className="space-y-3 text-xs">
        {reopenNodes.length > 0 && (
          <div className="border border-black bg-[var(--panel-alt)] p-3 text-xs space-y-1">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <span className="font-bold uppercase">Reopen valve</span>
              <Badge tone="yellow">{reopenNodes.length} ready</Badge>
            </div>
            <p>
              Readings are normal at <span className="font-bold">{reopenNodes.join(", ")}</span>. Reopen valve at the
              node or via gateway order.
            </p>
          </div>
        )}

        {history.length === 0 ? (
          <div className="border border-black bg-[var(--panel-alt)] p-3">
            No resolved problems recorded yet.
          </div>
        ) : (
          <div className="max-h-[620px] space-y-2.5 overflow-y-auto pr-1">
            {history.map((item, idx) => (
              <PastProblemCard
                key={`${item.target}-${item.category}-${item.t_onset}-${idx}`}
                item={item}
                reopenReady={reopenSet.has(item.target)}
              />
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
