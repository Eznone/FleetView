import { useMemo } from "react";
import { Background, Controls, ReactFlow, type Edge, type Node } from "@xyflow/react";
import type { AgentView, FleetEdge } from "../types";
import { styleFor } from "../status";
import { useLayout } from "../useLayout";
import { AgentNode } from "./AgentNode";

const nodeTypes = { agent: AgentNode };

/**
 * §5.6's centre pane. Positions come from ELK (in a worker); React Flow only
 * renders.
 *
 * A "blocked edge" is one whose *source* is waiting on its target, which is
 * what `blockingDependencyIds` names. Highlighting the edge rather than only
 * the node is the point: the question the canvas answers is not "is this agent
 * stuck" but "what is it stuck behind".
 */
export function Canvas({
  agents,
  edges,
  selected,
  onSelect,
}: {
  agents: AgentView[];
  edges: FleetEdge[];
  selected: string | null;
  onSelect: (id: string) => void;
}) {
  const positions = useLayout(agents, edges);

  const nodes: Node[] = useMemo(
    () =>
      agents.map((agent, index) => ({
        id: agent.agentId,
        type: "agent",
        position: positions[agent.agentId] ?? { x: 40, y: index * 140 },
        data: { agent, selected: selected === agent.agentId },
      })),
    [agents, positions, selected],
  );

  const flowEdges: Edge[] = useMemo(() => {
    const byId = new Map(agents.map((a) => [a.agentId, a]));
    return edges.map((edge) => {
      const source = byId.get(edge.source);
      const blocking = Boolean(source?.blockingDependencyIds.includes(edge.target));
      return {
        id: edge.id,
        source: edge.source,
        target: edge.target,
        animated: blocking,
        label: edge.orchestration,
        className: `edge ${blocking ? "blocked" : ""} ${
          edge.state === "closed" ? "closed" : ""
        } sev-${source ? styleFor(source).severity : "ok"}`,
      };
    });
  }, [agents, edges]);

  return (
    <section className="pane pane-canvas">
      <ReactFlow
        nodes={nodes}
        edges={flowEdges}
        nodeTypes={nodeTypes}
        onNodeClick={(_event, node) => onSelect(node.id)}
        fitView
        proOptions={{ hideAttribution: true }}
      >
        <Background gap={22} size={1} />
        <Controls showInteractive={false} />
      </ReactFlow>
    </section>
  );
}
