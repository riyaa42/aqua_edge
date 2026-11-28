import { useState } from "react";
import { ChevronDown, ChevronRight, Folder, FolderOpen } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import type { IncidentItem, NodeSnapshotState, TopologyData, TopologyNode } from "@/types";

interface Props {
  topology: TopologyData | null;
  topologyError: string | null;
  nodes?: Record<string, NodeSnapshotState>;
  activeIncidents?: IncidentItem[];
}

function nodeStatusLabel(
  id: string,
  nodes: Record<string, NodeSnapshotState>,
  incidents: IncidentItem[]
): { label: string; alert: boolean } {
  const hasIncident = incidents.some((inc) => inc.target === id);
  const st = nodes[id]?.status;
  if (hasIncident) return { label: "ALERT", alert: true };
  if (st === "silent") return { label: "SILENT", alert: true };
  if (st === "ok") return { label: "OK", alert: false };
  return { label: "NO DATA", alert: false };
}

function TreeRow({
  node,
  depth,
  nodes,
  incidents,
}: {
  node: TopologyNode;
  depth: number;
  nodes: Record<string, NodeSnapshotState>;
  incidents: IncidentItem[];
}) {
  const [open, setOpen] = useState(true);
  const children = node.children ?? [];
  const hasChildren = children.length > 0;
  const status = node.type === "junction" ? null : nodeStatusLabel(node.id, nodes, incidents);

  return (
    <div>
      <div
        className="flex flex-wrap items-center justify-between gap-2 border-b border-black/25 py-1.5 pr-2 text-xs"
        style={{ paddingLeft: `${depth * 18 + 6}px` }}
      >
        <div className="flex items-center gap-1.5">
          {hasChildren ? (
            <button
              type="button"
              onClick={() => setOpen((v) => !v)}
              className="inline-flex items-center justify-center border border-black bg-[var(--panel-bg)] p-0.5 cursor-pointer"
              aria-label={open ? `Collapse ${node.id}` : `Expand ${node.id}`}
            >
              {open ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
            </button>
          ) : (
            <span className="inline-block w-4" />
          )}
          {hasChildren ? (
            open ? <FolderOpen className="h-3.5 w-3.5 shrink-0" /> : <Folder className="h-3.5 w-3.5 shrink-0" />
          ) : null}
          <span className="font-bold">{node.id}</span>
          <span className="text-[11px] uppercase">({node.type})</span>
          {node.label && <span className="text-[11px]">[{node.label}]</span>}
        </div>
        <div className="flex items-center gap-1.5">
          {typeof node.valve === "boolean" && (
            <Badge>{node.valve ? "Valve" : "No valve"}</Badge>
          )}
          {status && (
            <Badge tone={status.alert ? "dark" : "plain"}>{status.label}</Badge>
          )}
        </div>
      </div>
      {hasChildren && open && (
        <div>
          {children.map((child) => (
            <TreeRow
              key={child.id}
              node={child}
              depth={depth + 1}
              nodes={nodes}
              incidents={incidents}
            />
          ))}
        </div>
      )}
    </div>
  );
}

export function TopologyTree({ topology, topologyError, nodes, activeIncidents }: Props) {
  const nodeMap = nodes ?? {};
  const incidents = activeIncidents ?? [];
  const reportedIds = Object.keys(nodeMap).sort();

  return (
    <Card className="flex flex-col">
      <CardHeader>
        <CardTitle>Building Pipes</CardTitle>
        <Badge>{topology ? topology.building : `${reportedIds.length} nodes`}</Badge>
      </CardHeader>
      <CardContent className="flex-1 space-y-3">
        {topology ? (
          <div className="border border-black bg-[var(--panel-alt)]">
            <TreeRow
              node={topology.root}
              depth={0}
              nodes={nodeMap}
              incidents={incidents}
            />
          </div>
        ) : (
          <div className="space-y-3">
            <div className="border border-black bg-[var(--panel-alt)] p-3 text-xs">
              Topology unavailable: showing reporting nodes only
              {topologyError ? ` (${topologyError})` : ""}.
            </div>
            {reportedIds.length > 0 && (
              <div className="border border-black bg-[var(--panel-alt)]">
                {reportedIds.map((id) => {
                  const st = nodeStatusLabel(id, nodeMap, incidents);
                  return (
                    <div
                      key={id}
                      className="flex items-center justify-between border-b border-black/25 px-3 py-1.5 text-xs last:border-b-0"
                    >
                      <span className="font-bold">{id}</span>
                      <Badge tone={st.alert ? "dark" : "plain"}>{st.label}</Badge>
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
