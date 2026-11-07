import { Background, MarkerType, ReactFlow, type Edge, type Node } from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import type { Result, Scenario } from "../../../contracts/models";
import { formatDuration } from "../state/scenario";

/** "180 min (3 h)" or "10 min": always exact minutes, hours only when exact. */
export function describeMinutes(minutes: number): string {
  const hours = formatDuration(minutes);
  return hours.endsWith("m") ? `${minutes} min` : `${minutes} min (${hours} h)`;
}

export interface Line {
  deliverableId: string;
  service: string;
  providerId: string;
  ownerId: string;
  minutes: number;
  self: boolean;
}

export interface GraphModel {
  nodes: Node[];
  edges: Edge[];
  /** Every assignment, in result order, for the textual mirror. */
  lines: Line[];
  /** Non-empty when the result refers to ids the scenario does not contain. Nothing is drawn then. */
  problems: string[];
}

/**
 * Pure mapping Scenario + Result -> graph. Nodes use the business id as id. An exchange edge
 * runs provider -> owner (the business that needs the deliverable) and its id comes from the
 * deliverable id, so equal labels can never collide. Self-work is NOT an edge: it is shown on
 * the node. Nothing is invented: no assignments, no edges.
 */
export function buildGraph(scenario: Scenario, result: Result): GraphModel {
  const problems: string[] = [];
  const businessIds = new Set(scenario.businesses.map((b) => b.id));
  const lines: Line[] = [];
  const seen = new Set<string>();

  for (const a of result.assignments) {
    const deliverable = scenario.deliverables.find((d) => d.id === a.deliverableId);
    if (!deliverable) {
      problems.push(`Assignment refers to unknown deliverable "${a.deliverableId}".`);
      continue;
    }
    if (seen.has(a.deliverableId)) problems.push(`Deliverable "${a.deliverableId}" is assigned more than once.`);
    seen.add(a.deliverableId);
    if (!businessIds.has(a.providerId)) {
      problems.push(`Assignment refers to unknown provider "${a.providerId}".`);
      continue;
    }
    lines.push({
      deliverableId: a.deliverableId,
      service: deliverable.service,
      providerId: a.providerId,
      ownerId: deliverable.ownerId,
      minutes: a.minutes,
      self: a.providerId === deliverable.ownerId,
    });
  }
  if (problems.length > 0) return { nodes: [], edges: [], lines: [], problems };

  const count = scenario.businesses.length;
  const nodes: Node[] = scenario.businesses.map((b, i) => {
    const angle = (2 * Math.PI * i) / count - Math.PI / 2;
    const own = lines.filter((l) => l.self && l.ownerId === b.id);
    const selfText = own.map((l) => `Self: ${l.deliverableId} · ${l.service} · ${describeMinutes(l.minutes)}`);
    return {
      id: b.id,
      position: { x: 220 + 170 * Math.cos(angle), y: 150 + 110 * Math.sin(angle) },
      data: { label: [`${b.name} (${b.id})`, ...selfText].join("\n") },
      style: { whiteSpace: "pre-line", fontSize: 12, textAlign: "center", width: 190 },
      draggable: false,
      connectable: false,
    };
  });

  const edges: Edge[] = lines
    .filter((l) => !l.self)
    .map((l) => ({
      id: `edge-${l.deliverableId}`,
      source: l.providerId,
      target: l.ownerId,
      label: `${l.deliverableId} · ${l.service} · ${describeMinutes(l.minutes)}`,
      markerEnd: { type: MarkerType.ArrowClosed },
      labelBgPadding: [4, 2] as [number, number],
    }));

  return { nodes, edges, lines, problems };
}

interface Props {
  scenario: Scenario;
  result: Result;
}

export default function NetworkGraph({ scenario, result }: Props) {
  const graph = buildGraph(scenario, result);
  const nameOf = (id: string) => scenario.businesses.find((b) => b.id === id)?.name ?? id;

  if (graph.problems.length > 0) {
    return (
      <div role="alert" className="rounded border border-red-300 bg-red-50 p-3 text-sm text-red-900">
        <p className="font-medium">The result does not match the confirmed scenario, so no graph is drawn.</p>
        <ul className="list-disc pl-5">
          {graph.problems.map((p) => (
            <li key={p}>{p}</li>
          ))}
        </ul>
      </div>
    );
  }

  const exchanges = graph.lines.filter((l) => !l.self);
  return (
    <div>
      <div
        role="img"
        aria-label={`Exchange graph with ${graph.nodes.length} businesses and ${exchanges.length} exchange arrows. The assignment list below has the same information.`}
        className="h-80 rounded border border-slate-300 bg-white"
        data-testid="network-graph"
      >
        <ReactFlow
          nodes={graph.nodes}
          edges={graph.edges}
          fitView
          nodesDraggable={false}
          nodesConnectable={false}
          elementsSelectable={false}
          panOnDrag={false}
          zoomOnScroll={false}
        >
          <Background />
        </ReactFlow>
      </div>
      <p className="mt-1 text-xs text-slate-600">Arrows point from the business doing the work to the business that needs it.</p>
      {exchanges.length === 0 && (
        <p className="mt-1 text-sm font-medium text-slate-800" data-testid="no-exchange-note">
          No exchange arrows: every business does its own work.
        </p>
      )}
      <h4 className="mt-3 text-sm font-semibold">Assignments</h4>
      <ul className="mt-1 space-y-1 text-sm" data-testid="assignment-list">
        {graph.lines.map((l) => (
          <li key={l.deliverableId} data-kind={l.self ? "self" : "exchange"}>
            {l.self ? (
              <>
                <strong>{nameOf(l.providerId)}</strong> does its own <code>{l.deliverableId}</code> ({l.service},{" "}
                {describeMinutes(l.minutes)}).
              </>
            ) : (
              <>
                <strong>{nameOf(l.providerId)}</strong> does <code>{l.deliverableId}</code> ({l.service},{" "}
                {describeMinutes(l.minutes)}) for <strong>{nameOf(l.ownerId)}</strong>.
              </>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}
