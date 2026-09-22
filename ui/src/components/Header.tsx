import type { FleetSnapshot } from "../types";
import type { Connection } from "../useFleet";
import { styleFor } from "../status";

/**
 * §5.6 replaces AgentPulse's FinOps cost ribbon with a quota meter, because
 * cost per token is meaningless on a subscription -- proximity to the plan's
 * limit is the number that matters. Phase 2 ships the meter driven by the
 * `rate_limited` state; real quota detection is Phase 6.
 *
 * The agent count beside it is §8.5 requirement 5, and it is a requirement
 * rather than a nicety: the operator should never be unaware of the scale they
 * are running at against their own subscription.
 */
export function Header({
  snapshot,
  connection,
}: {
  snapshot: FleetSnapshot;
  connection: Connection;
}) {
  const live = snapshot.agents.filter((a) => !a.terminated);
  const limited = live.filter((a) => a.state === "rate_limited");
  const needsHuman = live.filter((a) => a.state === "waiting_on_human");
  const cap = snapshot.maxConcurrentAgents || 1;
  const load = Math.min(live.length / cap, 1);
  const synthetic = snapshot.agents.some((a) => a.synthetic);

  return (
    <header className="header">
      <div className="brand">
        <span className="mark" />
        FleetView
      </div>

      <div className="fleet-banner">
        <strong>{live.length}</strong>
        <span>
          {live.length === 1 ? "agent" : "agents"} running against your own subscription
        </span>
        <div className="meter" title={`${live.length} of ${cap} concurrent agents`}>
          <div
            className={`meter-fill ${limited.length ? "limited" : ""}`}
            style={{ width: `${Math.max(load * 100, 4)}%` }}
          />
        </div>
        <span className="cap">cap {cap}</span>
      </div>

      <div className="header-right">
        {synthetic && <span className="ribbon">demo data</span>}
        {limited.length > 0 && (
          <span className="badge sev-fault" title="Parked until the window resets — never retried through.">
            <i className="dot" />
            {limited.length} rate limited
          </span>
        )}
        {needsHuman.length > 0 && (
          <span className={`badge sev-${styleFor(needsHuman[0]).severity}`}>
            <i className="dot" />
            {needsHuman.length} need you
          </span>
        )}
        <span className={`conn conn-${connection}`}>{connection}</span>
      </div>
    </header>
  );
}
