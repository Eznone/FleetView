import { useEffect, useRef, useState } from "react";
import type { AgentView, FleetEdge } from "./types";

export interface Placed {
  id: string;
  x: number;
  y: number;
}

const NODE_WIDTH = 230;
const NODE_HEIGHT = 104;

/** Runs ELK in a worker and returns node positions, keyed by agent id. */
export function useLayout(agents: AgentView[], edges: FleetEdge[]) {
  const [positions, setPositions] = useState<Record<string, Placed>>({});
  const workerRef = useRef<Worker | null>(null);
  const pending = useRef(0);

  useEffect(() => {
    const worker = new Worker(new URL("./elk.worker.ts", import.meta.url), {
      type: "module",
    });
    workerRef.current = worker;
    worker.onmessage = (message: MessageEvent) => {
      const { id, laid, error } = message.data;
      // Drop a stale result: a layout for a fleet that has since changed would
      // snap nodes back to where they used to be.
      if (error || id !== pending.current) return;
      const next: Record<string, Placed> = {};
      for (const child of laid.children ?? []) {
        next[child.id] = { id: child.id, x: child.x ?? 0, y: child.y ?? 0 };
      }
      setPositions(next);
    };
    return () => worker.terminate();
  }, []);

  // Re-layout on *shape* change only. Re-running on every status change would
  // reshuffle the canvas under the operator's cursor while they are reading it.
  const shape = JSON.stringify([
    agents.map((a) => a.agentId).sort(),
    edges.map((e) => `${e.source}>${e.target}`).sort(),
  ]);

  useEffect(() => {
    const worker = workerRef.current;
    if (!worker || agents.length === 0) return;
    const id = pending.current + 1;
    pending.current = id;
    worker.postMessage({
      id,
      graph: {
        id: "fleet",
        layoutOptions: {
          "elk.algorithm": "layered",
          "elk.direction": "RIGHT",
          "elk.layered.spacing.nodeNodeBetweenLayers": "110",
          "elk.spacing.nodeNode": "40",
          "elk.layered.considerModelOrder.strategy": "NODES_AND_EDGES",
        },
        children: agents.map((agent) => ({
          id: agent.agentId,
          width: NODE_WIDTH,
          height: NODE_HEIGHT,
        })),
        edges: edges.map((edge) => ({
          id: edge.id,
          sources: [edge.source],
          targets: [edge.target],
        })),
      },
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [shape]);

  return positions;
}
