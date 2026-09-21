"""The `fleetview` command line.

Two eras live here at the moment. ``spike`` and ``env`` are Phase 0 scaffolding
— they proved that a vendor CLI spawned from a daemon-like process
authenticates on the operator's own subscription, saves a transcript and
resumes, and they stay as the hand-driver until the daemon owns spawning.

``init`` and ``events tail`` are Phase 1: the data directory and the log
reader. §4.1 makes the reader a deliverable rather than a nicety — SQLite is
the single copy, so without it there is no way to inspect a run without the UI.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
from pathlib import Path

import typer

from fleetview.config import Settings
from fleetview.fsguard import SLOW_FILESYSTEMS, SlowFilesystemError, filesystem_type
from fleetview.hook.install import write_agent_settings
from fleetview.spawn.env import build_agent_env
from fleetview.spawn.resolve import resolve_cli_binary, user_path
from fleetview.spawn.tmux import AgentSpawnError, create_agent_session
from fleetview.store import EventStore, apply_schema, connect

app = typer.Typer(add_completion=False, help="FleetView — orchestrate and observe coding agents.")
events_app = typer.Typer(help="Read the event log.")
app.add_typer(events_app, name="events")

#: Permission posture per PROJECT_PLAN.md §3.5.1: moderate, NOT full bypass.
#: Both reference implementations default to bypass because a headless worker
#: with no human attached stalls at the first prompt. FleetView does have a
#: human attached, and surfacing that stall is the entire point of the tool.
PROVIDER_ARGV: dict[str, list[str]] = {
    "claude": ["--permission-mode", "acceptEdits"],
    "codex": ["-s", "workspace-write"],
}


def _warn_if_slow_filesystem(path: Path) -> None:
    """WSL2: /mnt/c is a 9p mount where file locking is unreliable and fsync is
    glacial. Fine for an agent's *working* directory, fatal for the event store
    — so here it is a warning, while `fsguard` refuses outright for the store.
    One implementation of "which filesystem is this", used by both."""
    fstype = filesystem_type(path)
    if fstype in SLOW_FILESYSTEMS:
        typer.secho(
            f"  warning: {path} is on a {fstype} filesystem. Workable as a working directory, "
            f"but the event store must live on ext4 (~/.fleetview/).",
            fg=typer.colors.YELLOW,
        )


@app.command()
def spike(
    provider: str = typer.Argument("claude", help="Which CLI to spawn."),
    cwd: Path = typer.Option(Path.cwd(), "--cwd", help="Working directory for the agent."),
    resume: str | None = typer.Option(None, "--resume", help="Resume a prior session id."),
    name: str | None = typer.Option(None, "--name", help="tmux session name."),
    hooks: bool = typer.Option(False, "--hooks", help="Install FleetView's hooks for this agent."),
) -> None:
    """Spawn one agent CLI in a detached tmux session."""
    if provider not in PROVIDER_ARGV:
        raise typer.BadParameter(f"unknown provider {provider!r}; known: {', '.join(PROVIDER_ARGV)}")

    cwd = cwd.expanduser().resolve()
    if not cwd.is_dir():
        raise typer.BadParameter(f"not a directory: {cwd}")
    _warn_if_slow_filesystem(cwd)

    session_name = name or f"fleetview-{provider}-{secrets.token_hex(3)}"

    binary = resolve_cli_binary(provider)
    argv = [binary, *PROVIDER_ARGV[provider]]
    if resume:
        argv += ["--resume", resume]

    settings = Settings.from_env()
    agent_env = {
        "FLEETVIEW_AGENT_ID": session_name,
        "FLEETVIEW_HOME": str(settings.home),
        "FLEETVIEW_PROVIDER": provider,
        "PATH": user_path(),
    }

    settings_path = None
    if hooks:
        if provider != "claude":
            # Codex has no --settings equivalent; its hooks ride in through a
            # per-agent CODEX_HOME instead (§6.2). Phase 4.
            raise typer.BadParameter(f"--hooks is Claude-only for now (got {provider!r})")
        settings_path = write_agent_settings(session_name, settings=settings)
        argv += ["--settings", str(settings_path)]

    env = build_agent_env(dict(os.environ), agent_env=agent_env)

    try:
        create_agent_session(session_name=session_name, argv=argv, cwd=str(cwd), env=env)
    except AgentSpawnError as exc:
        typer.secho(f"spawn failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc

    typer.secho(f"spawned {session_name}", fg=typer.colors.GREEN)
    typer.echo(f"  binary   {binary}")
    typer.echo(f"  cwd      {cwd}")
    typer.echo(f"  attach   tmux attach -t {session_name}")
    typer.echo(f"  kill     tmux kill-session -t {session_name}")
    if settings_path:
        typer.echo(f"  hooks    {settings_path}")
    typer.echo()
    typer.echo("  Transcript should appear under:")
    typer.echo(f"    ~/.claude/projects/{str(cwd).replace('/', '-').replace('.', '-')}/")


@app.command()
def env(provider: str = typer.Argument("claude")) -> None:
    """Show the environment a spawned agent would receive. Diagnostic only."""
    built = build_agent_env(dict(os.environ), agent_env={"FLEETVIEW_AGENT_ID": "<agent-id>"})
    typer.secho("environment handed to the agent:", bold=True)
    for key in sorted(built):
        value = built[key]
        shown = value if len(value) <= 70 else value[:67] + "..."
        typer.echo(f"  {key}={shown}")

    leaked = sorted(k for k in built if "TOKEN" in k or "KEY" in k or "SECRET" in k)
    typer.echo()
    if leaked:
        typer.secho(f"  CREDENTIAL LEAK: {leaked}", fg=typer.colors.RED, bold=True)
        raise typer.Exit(1)
    typer.secho("  no credential-shaped variables present", fg=typer.colors.GREEN)

    stripped = sorted(k for k in os.environ if k not in built)
    typer.echo(f"  {len(stripped)} parent variables withheld, including:")
    for key in [k for k in stripped if k.startswith(("CLAUDE", "CODEX", "ANTHROPIC", "OPENAI"))][:8]:
        typer.echo(f"    - {key}")


# --- event log reader --------------------------------------------------------
#
# §4.1: SQLite stays the single copy, and this recovers the greppability a
# JSONL-only design would have given for free. That transparency matters
# disproportionately here, because the thing being debugged *is* the debugger.

@events_app.command("tail")
def events_tail(
    run: str | None = typer.Option(None, "--run", help="Filter by run id."),
    agent: str | None = typer.Option(None, "--agent", help="Filter by agent id."),
    event_type: str | None = typer.Option(None, "--type", help="Filter by event type."),
    grep: str | None = typer.Option(None, "--grep", help="Substring match on payload or type."),
    limit: int = typer.Option(100, "--limit", help="Most recent N events."),
    follow: bool = typer.Option(False, "--follow", "-f", help="Stream new events as they land."),
) -> None:
    """Emit matching events as JSONL on stdout."""
    settings = Settings.from_env()
    if not settings.db_path.exists():
        typer.secho(
            f"no event store at {settings.db_path} — start the daemon first.",
            fg=typer.colors.YELLOW, err=True,
        )
        raise typer.Exit(1)

    try:
        asyncio.run(_tail(settings, run, agent, event_type, grep, limit, follow))
    except KeyboardInterrupt:  # pragma: no cover - interactive
        pass


async def _tail(settings, run, agent, event_type, grep, limit, follow) -> None:
    conn = await connect(settings.db_path)
    store = EventStore(conn, settings=settings)
    try:
        filters = dict(run_id=run, agent_id=agent, event_type=event_type, grep=grep)
        events = await store.fetch(**filters, limit=limit, newest=True)
        for event in events:
            _emit(event)

        if not follow:
            return

        # Poll rather than subscribe: the bus lives in the daemon's process,
        # and a reader that needed the daemon running would be useless for
        # exactly the post-mortem case this command exists for.
        last_id = events[-1].id if events else ""
        while True:
            await asyncio.sleep(0.25)
            new = await store.fetch(**filters, after_id=last_id, limit=500)
            for event in new:
                _emit(event)
            if new:
                last_id = new[-1].id
    finally:
        await conn.close()


def _emit(event) -> None:
    print(json.dumps(event.to_wire(), separators=(",", ":")), flush=True)


@app.command()
def init() -> None:
    """Create the data directory and event store. Refuses a slow filesystem."""
    settings = Settings.from_env()
    settings.ensure_directories()

    async def _create() -> None:
        conn = await connect(settings.db_path)
        await apply_schema(conn)
        await conn.close()

    asyncio.run(_create())
    typer.secho(f"initialised {settings.home}", fg=typer.colors.GREEN)
    typer.echo(f"  store    {settings.db_path}")
    typer.echo(f"  terminal {settings.terminal_log_dir}  (flat files — never in the DB)")
    typer.echo(f"  agents   max {settings.max_concurrent_active_agents} concurrent")



@app.command()
def daemon() -> None:
    """Run the FleetView daemon. Serves the hook plane over a Unix socket."""
    from fleetview.daemon.server import serve

    settings = Settings.from_env()
    try:
        asyncio.run(serve(settings))
    except SlowFilesystemError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc
    except KeyboardInterrupt:  # pragma: no cover - interactive
        typer.echo("daemon stopped")


if __name__ == "__main__":
    app()
