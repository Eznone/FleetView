import { useEffect, useState } from "react";
import "@xyflow/react/dist/style.css";
import "./styles.css";
import { useFleet } from "./useFleet";
import { Header } from "./components/Header";
import { FleetTree } from "./components/FleetTree";
import { Canvas } from "./components/Canvas";
import { Inspector } from "./components/Inspector";
import { Waterfall } from "./components/Waterfall";

export default function App() {
  const { snapshot, events, connection } = useFleet();
  const [selected, setSelected] = useState<string | null>(null);

  // Follow the fleet when nothing is pinned, so a single-agent run needs no
  // click at all -- and so an agent that has gone away stops being inspected.
  useEffect(() => {
    if (snapshot.agents.length === 0) {
      setSelected(null);
    } else if (!snapshot.agents.some((a) => a.agentId === selected)) {
      setSelected(snapshot.agents[0].agentId);
    }
  }, [snapshot.agents, selected]);

  const agent = snapshot.agents.find((a) => a.agentId === selected) ?? null;

  return (
    <div className="app">
      <Header snapshot={snapshot} connection={connection} />
      <main className="panes">
        <FleetTree agents={snapshot.agents} selected={selected} onSelect={setSelected} />
        <Canvas
          agents={snapshot.agents}
          edges={snapshot.edges}
          selected={selected}
          onSelect={setSelected}
        />
        <Inspector agent={agent} events={events} />
      </main>
      <Waterfall events={events} onSelect={setSelected} />
    </div>
  );
}
