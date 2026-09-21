"""The hook shim — the agent-side half of the hook plane (§3.4, channel A).

**This package imports nothing from FleetView and nothing from PyPI.** It runs
on every single lifecycle hook of every agent, several times per tool call, and
it runs *in the agent's own process tree* — so its cost is the agent's latency.
Importing pydantic here would add tens of milliseconds to every tool call an
agent makes, in exchange for nothing: the payload is already JSON and the
daemon validates it on arrival.

Keep it stdlib-only. If this file grows a dependency, the reason had better be
written down next to it.
"""
