import { useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { formatEvidence, getReopenValveNodes, plainCategory, recommendedActionFor, severityTone } from "@/lib/labels";
import type { GatewaySnapshot, IncidentItem, MaintenanceItem, NodeSnapshotState } from "@/types";

type PipeLeakInfo = NonNullable<NonNullable<GatewaySnapshot["leakage"]>["pipe_leak"]> | null;

interface Props {
  queue?: MaintenanceItem[];
  pipeLeak?: PipeLeakInfo;
  nodes?: Record<string, NodeSnapshotState>;
  loading: boolean;
}

interface AllProblemsProps extends Props {
  activeIncidents?: IncidentItem[];
}

function QueueCard({
  item,
  index,
  pipeLeak,
}: {
  item: MaintenanceItem;
  index: number;
  pipeLeak?: PipeLeakInfo;
}) {
  const [expanded, setExpanded] = useState(false);
  const ev = item.evidence ?? {};
  const isPipeLeak = item.category === "PIPE_LEAK";
  const suspectedFloor =
    (isPipeLeak ? pipeLeak?.suspected_floor : undefined) ??
    (typeof ev.suspected_floor === "string" ? ev.suspected_floor : null);
  const confidence =
    (isPipeLeak ? pipeLeak?.localization_confidence : undefined) ??
    (typeof ev.localization_confidence === "string" ? ev.localization_confidence : null);
  const errorPct =
    (isPipeLeak ? pipeLeak?.error_pct : undefined) ??
    (typeof ev.error_pct === "number" ? ev.error_pct : null);
  const isolationValve =
    (isPipeLeak ? pipeLeak?.isolation_valve : undefined) ??
    (typeof ev.isolation_valve === "string" ? ev.isolation_valve : null);

  const evidenceRows = formatEvidence(ev);
  const symptoms = item.symptoms ?? [];

  return (
    <div className="border border-black bg-[var(--panel-alt)] p-2.5 space-y-1.5">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap items-center gap-2">
          <Badge tone="dark">#{item.rank ?? index + 1}</Badge>
          <span className="font-bold text-sm">{item.target}</span>
          <span className="text-sm">{plainCategory(item.category)}</span>
        </div>
        <div className="flex items-center gap-2">
          {item.risk_score != null && <Badge>Score {item.risk_score}</Badge>}
          <Badge tone={severityTone(item.severity)}>
            {item.severity ?? "UNKNOWN"}
          </Badge>
        </div>
      </div>

      {isPipeLeak && (
        <div className="grid grid-cols-2 gap-1.5 border border-black bg-[var(--panel-bg)] p-2 text-[11px] sm:grid-cols-4">
          <div>
            <span className="uppercase">Section: </span>
            <span className="font-bold">{suspectedFloor ?? "Not available"}</span>
          </div>
          <div>
            <span className="uppercase">Confidence: </span>
            <span className="font-bold">{confidence ?? "Not available"}</span>
          </div>
          <div>
            <span className="uppercase">Unaccounted water: </span>
            <span className="font-bold">{errorPct != null ? `${errorPct.toFixed(1)}%` : "Not available"}</span>
          </div>
          <div>
            <span className="uppercase">Recommended isolation valve: </span>
            <span className="font-bold">{isolationValve ?? "Not available"}</span>
          </div>
        </div>
      )}

      {item.recommended_action && (
        <p className="text-xs leading-relaxed">{item.recommended_action}</p>
      )}

      <div className="flex flex-wrap items-center justify-between gap-2 text-[11px]">
        <div className="flex flex-wrap gap-3">
          {item.affected_flats != null && <span>Affected units: {item.affected_flats}</span>}
          {item.duration_min != null && <span>Active for: {item.duration_min} min</span>}
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

function ReopenValveBanner({ reopenNodes }: { reopenNodes: string[] }) {
  if (reopenNodes.length === 0) return null;
  return (
    <div className="border border-black bg-[var(--panel-alt)] p-2.5 text-xs space-y-1">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="font-bold uppercase">Reopen valve</span>
        <Badge tone="yellow">{reopenNodes.length} ready</Badge>
      </div>
      <p>
        Readings are normal at <span className="font-bold">{reopenNodes.join(", ")}</span>. Reopen valve at the
        node or via gateway order.
      </p>
    </div>
  );
}

export function MaintenanceQueue({ queue, pipeLeak, nodes, loading }: Props) {
  const allItems = queue ?? [];
  const items = allItems.filter((it) => it.severity !== "LOW");
  const lowCount = allItems.length - items.length;
  const reopenNodes = getReopenValveNodes(nodes, allItems);

  return (
    <Card className="flex max-h-[360px] flex-col lg:h-0 lg:max-h-none lg:min-h-full">
      <CardHeader>
        <CardTitle>Fix First</CardTitle>
        <Badge>{items.length} priority</Badge>
      </CardHeader>
      <CardContent className="flex-1 space-y-2 overflow-y-auto">
        <ReopenValveBanner reopenNodes={reopenNodes} />
        {loading ? (
          <p className="text-sm">Loading priority list...</p>
        ) : items.length === 0 ? (
          <div className="border border-black bg-[var(--panel-alt)] p-4 text-sm space-y-1">
            <p>No high-priority problems need attention right now.</p>
            {lowCount > 0 && (
              <p className="text-xs">
                {lowCount} lower-priority {lowCount === 1 ? "notice is" : "notices are"} listed in All Problems.
              </p>
            )}
          </div>
        ) : (
          items.map((item, idx) => (
            <QueueCard
              key={`${item.target}-${item.category}-${idx}`}
              item={item}
              index={idx}
              pipeLeak={pipeLeak}
            />
          ))
        )}
      </CardContent>
    </Card>
  );
}

export function AllProblems({ queue, activeIncidents, pipeLeak, nodes, loading }: AllProblemsProps) {
  const queued = queue ?? [];
  const seen = new Set(queued.map((it) => `${it.target}|${it.category}`));
  const extras: MaintenanceItem[] = (activeIncidents ?? [])
    .filter((inc) => !seen.has(`${inc.target}|${inc.category}`))
    .map((inc) => ({
      target: inc.target,
      category: inc.category,
      risk_score: inc.risk_score ?? undefined,
      severity: inc.severity ?? "LOW",
      affected_flats: inc.affected_flats,
      symptoms: inc.symptoms,
      evidence: inc.evidence,
      recommended_action: recommendedActionFor(inc.category, inc.recommended_action) ?? undefined,
    }));
  const items = [...queued, ...extras];
  const reopenNodes = getReopenValveNodes(nodes, items);

  return (
    <Card className="flex h-[320px] flex-col">
      <CardHeader>
        <CardTitle>All Problems</CardTitle>
        <Badge>{items.length} active</Badge>
      </CardHeader>
      <CardContent className="flex-1 space-y-2 overflow-y-auto">
        <ReopenValveBanner reopenNodes={reopenNodes} />
        {loading ? (
          <p className="text-sm">Loading active problems...</p>
        ) : items.length === 0 ? (
          <div className="border border-black bg-[var(--panel-alt)] p-4 text-sm">
            No active problems or notices right now.
          </div>
        ) : (
          items.map((item, idx) => (
            <QueueCard
              key={`${item.target}-${item.category}-${idx}`}
              item={item}
              index={idx}
              pipeLeak={pipeLeak}
            />
          ))
        )}
      </CardContent>
    </Card>
  );
}
