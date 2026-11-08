import { useId } from "react";
import {
  Background,
  BaseEdge,
  EdgeLabelRenderer,
  MarkerType,
  ReactFlow,
  useInternalNode,
  type Edge,
  type EdgeProps,
  type Node,
} from "@xyflow/react";
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

/** Where the ellipse of businesses is centred, in node-centre coordinates. */
const CENTER = { x: 220, y: 150 };
const NODE_WIDTH = 140;
const NODE_HALF_HEIGHT = 22; // estimate; only used to place node centres on the ellipse

export interface ExchangeEdgeData extends Record<string, unknown> {
  /** Sideways bow in px: 0 is a straight line. Opposite and parallel edges bow apart. */
  offset: number;
  /** Middle of the whole picture; edge labels sit on the outer side of their arrow. */
  center: { x: number; y: number };
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
      position: {
        x: CENTER.x + 150 * Math.cos(angle) - NODE_WIDTH / 2,
        y: CENTER.y + 105 * Math.sin(angle) - NODE_HALF_HEIGHT,
      },
      data: { label: [`${b.name} (${b.id})`, ...selfText].join("\n") },
      style: { whiteSpace: "pre-line", fontSize: 12, textAlign: "center", width: NODE_WIDTH },
      draggable: false,
      connectable: false,
    };
  });

  const exchanges = lines.filter((l) => !l.self);
  const pairKey = (l: Line) => [l.providerId, l.ownerId].sort().join("|");
  const edges: Edge<ExchangeEdgeData>[] = exchanges.map((l) => {
    const sameDirection = exchanges.filter((o) => o.providerId === l.providerId && o.ownerId === l.ownerId);
    const samePair = exchanges.filter((o) => pairKey(o) === pairKey(l));
    // A lone arrow between two businesses is straight; a second one (either direction) bows away.
    const offset = samePair.length === 1 ? 0 : 34 + 28 * sameDirection.indexOf(l);
    return {
      id: `edge-${l.deliverableId}`,
      type: "exchange",
      source: l.providerId,
      target: l.ownerId,
      label: `${l.deliverableId} · ${l.service} · ${describeMinutes(l.minutes)}`,
      markerEnd: { type: MarkerType.ArrowClosed, color: "#334155" },
      data: { offset, center: CENTER },
    };
  });

  return { nodes, edges, lines, problems };
}

/** Point where the line from a box's centre towards `toward` leaves the box. */
function borderPoint(c: { x: number; y: number }, hw: number, hh: number, toward: { x: number; y: number }) {
  const dx = toward.x - c.x;
  const dy = toward.y - c.y;
  if (dx === 0 && dy === 0) return c;
  const s = 1 / Math.max(Math.abs(dx) / hw, Math.abs(dy) / hh);
  return { x: c.x + dx * s, y: c.y + dy * s };
}

/**
 * Arrow between the edges of two boxes (not their top/bottom dots), bowed by `data.offset`
 * and labelled just outside the picture so the label never sits under a node.
 */
function ExchangeEdge({ source, target, markerEnd, label, data }: EdgeProps<Edge<ExchangeEdgeData>>) {
  const from = useInternalNode(source);
  const to = useInternalNode(target);
  if (!from || !to || !from.measured.width || !from.measured.height || !to.measured.width || !to.measured.height) return null;
  const hwA = from.measured.width / 2;
  const hhA = from.measured.height / 2;
  const hwB = to.measured.width / 2;
  const hhB = to.measured.height / 2;
  const a = { x: from.internals.positionAbsolute.x + hwA, y: from.internals.positionAbsolute.y + hhA };
  const b = { x: to.internals.positionAbsolute.x + hwB, y: to.internals.positionAbsolute.y + hhB };
  const length = Math.hypot(b.x - a.x, b.y - a.y) || 1;
  const offset = data?.offset ?? 0;
  // The bow goes to the left of the direction of travel, so opposite arrows separate.
  const control = {
    x: (a.x + b.x) / 2 + (-(b.y - a.y) / length) * offset * 2,
    y: (a.y + b.y) / 2 + ((b.x - a.x) / length) * offset * 2,
  };
  const start = borderPoint(a, hwA, hhA, control);
  const end = borderPoint(b, hwB, hhB, control);
  const path = `M ${start.x},${start.y} Q ${control.x},${control.y} ${end.x},${end.y}`;
  const mid = { x: 0.25 * start.x + 0.5 * control.x + 0.25 * end.x, y: 0.25 * start.y + 0.5 * control.y + 0.25 * end.y };
  const centre = data?.center ?? CENTER;
  const outX = mid.x - centre.x;
  const outY = mid.y - centre.y;
  const outLength = Math.hypot(outX, outY) || 1;
  const label_ = { x: mid.x + (outX / outLength) * 30, y: mid.y + (outY / outLength) * 30 };
  return (
    <>
      <BaseEdge path={path} markerEnd={markerEnd} style={{ stroke: "#334155", strokeWidth: 1.5 }} />
      <EdgeLabelRenderer>
        <div
          className="nodrag nopan pointer-events-none absolute max-w-[7.5rem] rounded bg-white px-1 text-center text-[11px] leading-tight text-slate-800"
          style={{ transform: `translate(-50%, -50%) translate(${label_.x}px, ${label_.y}px)` }}
        >
          {label}
        </div>
      </EdgeLabelRenderer>
    </>
  );
}

const edgeTypes = { exchange: ExchangeEdge };

interface Props {
  scenario: Scenario;
  result: Result;
}

export default function NetworkGraph({ scenario, result }: Props) {
  const graph = buildGraph(scenario, result);
  // React Flow derives DOM ids (descriptions, patterns, live region) from this; two graphs may be on screen.
  const flowId = `flow${useId().replace(/[^a-zA-Z0-9_-]/g, "")}`;
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
        role="group"
        aria-label={`Exchange graph with ${graph.nodes.length} businesses and ${exchanges.length} exchange arrows. The assignment list below has the same information.`}
        className="h-80 rounded border border-slate-300 bg-white"
        data-testid="network-graph"
      >
        <ReactFlow
          id={flowId}
          nodes={graph.nodes}
          edges={graph.edges}
          edgeTypes={edgeTypes}
          fitView
          fitViewOptions={{ padding: 0.12 }}
          nodesDraggable={false}
          nodesConnectable={false}
          nodesFocusable={false}
          edgesFocusable={false}
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
