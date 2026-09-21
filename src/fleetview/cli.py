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
import re
import stat
import sys
from datetime import datetime
from pathlib import Path

import typer

from fleetview.config import DIR_MODE, Settings
from fleetview.fsguard import SLOW_FILESYSTEMS, SlowFilesystemError, filesystem_type
from fleetview.hook.install import write_agent_settings
from fleetview.hook.shim import post_json
from fleetview.terminal.paths import (
    UnsafeAgentIdError,
    agent_log_dir,
    list_segments,
    total_bytes,
)
from fleetview.spawn.env import build_agent_env
from fleetview.spawn.resolve import resolve_cli_binary, user_path
from fleetview.spawn.tmux import AgentSpawnError, create_agent_session
from fleetview.store import EventStore, TerminalChunkStore, apply_schema, connect

app = typer.Typer(add_completion=False, help="FleetView — orchestrate and observe coding agents.")
events_app = typer.Typer(help="Read the event log.")
app.add_typer(events_app, name="events")
terminal_app = typer.Typer(help="Read an agent's captured terminal bytes.")
app.add_typer(terminal_app, name="terminal")

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

    # Register the pane tap with the daemon. Fail-open, exactly like the hook
    # shim: a daemon that is down must never be able to fail a spawn, because
    # losing telemetry is a far smaller problem than losing the agent. The
    # daemon's reconcile() would find this session within a poll interval
    # anyway -- this only removes the wait.
    reply = post_json(
        {"agentId": session_name, "tmuxSession": session_name},
        str(settings.socket_path),
        endpoint="/v1/terminals",
    )
    if reply and "fifo" in reply:
        typer.echo(f"  terminal {reply['fifo']}")
    else:
        detail = (reply or {}).get("detail", f"no daemon at {settings.socket_path}")
        typer.secho(f"  terminal not capturing — {detail}", fg=typer.colors.YELLOW)
        typer.echo(f"           remedy: fleetview terminal attach {session_name} "
                   f"--session {session_name}")
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
    typer.echo(f"  fifos    {settings.fifo_dir}")
    typer.echo(f"  agents   max {settings.max_concurrent_active_agents} concurrent")

    # Printed rather than assumed: these bytes are unredacted until Phase 6, so
    # the mode is the mitigation and the operator should be able to see it.
    for label, directory in (("home", settings.home),
                             ("terminal", settings.terminal_log_dir),
                             ("fifos", settings.fifo_dir)):
        mode = stat.S_IMODE(directory.stat().st_mode)
        colour = typer.colors.GREEN if mode == DIR_MODE else typer.colors.RED
        typer.secho(f"  mode     {label:9s} {oct(mode)}", fg=colour)



# --- terminal plane reader ---------------------------------------------------
#
# The tier-1 counterpart of `events tail`, and deliberately built the same way:
# it reads the flat files and SQLite directly, with no daemon involved. A
# reader that needed the daemon up would be useless for exactly the post-mortem
# case it exists for.

_ANSI_RE = re.compile(rb"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07|\x1b[@-_]")


def _terminal_settings() -> Settings:
    settings = Settings.from_env()
    if not settings.db_path.exists():
        typer.secho(
            f"no event store at {settings.db_path} — run `fleetview init` first.",
            fg=typer.colors.YELLOW, err=True,
        )
        raise typer.Exit(1)
    return settings


def _write_out(data: bytes, *, strip_ansi: bool) -> None:
    """Raw bytes by default, so a real terminal renders the capture as it was.

    `--strip-ansi` exists because the same bytes piped into grep are otherwise
    unreadable, and reaching for `sed` at that point is how people conclude the
    capture is broken.
    """
    if strip_ansi:
        data = _ANSI_RE.sub(b"", data)
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


@terminal_app.command("tail")
def terminal_tail(
    agent: str = typer.Argument(..., help="Agent id."),
    n_bytes: int = typer.Option(65536, "--bytes", help="How much history to show."),
    since: str | None = typer.Option(None, "--since", help="ISO-8601 start time."),
    strip_ansi: bool = typer.Option(False, "--strip-ansi", help="Strip escape sequences."),
    follow: bool = typer.Option(False, "--follow", "-f", help="Stream as bytes land."),
) -> None:
    """Replay an agent's captured terminal output."""
    settings = _terminal_settings()
    try:
        asyncio.run(_terminal_tail(settings, agent, n_bytes, since, strip_ansi, follow))
    except UnsafeAgentIdError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc
    except KeyboardInterrupt:  # pragma: no cover - interactive
        pass


async def _terminal_tail(settings, agent, n_bytes, since, strip_ansi, follow) -> None:
    conn = await connect(settings.db_path)
    chunks = TerminalChunkStore(conn, settings=settings)
    try:
        segments = list_segments(agent_log_dir(settings, agent))
        if not segments:
            typer.secho(f"no terminal capture for {agent!r}", fg=typer.colors.YELLOW, err=True)
            raise typer.Exit(1)

        if since is not None:
            start = datetime.fromisoformat(since)
            rows = await chunks.fetch(agent, since=start, limit=1)
            if not rows:
                typer.secho(f"no capture at or after {since}", fg=typer.colors.YELLOW, err=True)
                raise typer.Exit(1)
            position = (Path(rows[0].path), rows[0].byte_offset)
        else:
            position = _seek_back(segments, n_bytes)

        path, offset = position
        for segment in segments[segments.index(path):]:
            with open(segment, "rb") as fh:
                fh.seek(offset if segment == path else 0)
                _write_out(fh.read(), strip_ansi=strip_ansi)

        if not follow:
            return

        current = segments[-1]
        cursor = current.stat().st_size
        while True:
            await asyncio.sleep(0.25)
            # Re-list every pass: the writer may have rotated underneath us,
            # and following only the file we opened would go silent at the
            # rotation while the agent kept talking.
            live = list_segments(agent_log_dir(settings, agent))
            if not live:
                continue
            if live[-1] != current:
                with open(current, "rb") as fh:
                    fh.seek(cursor)
                    _write_out(fh.read(), strip_ansi=strip_ansi)
                current, cursor = live[-1], 0
            size = current.stat().st_size
            if size > cursor:
                with open(current, "rb") as fh:
                    fh.seek(cursor)
                    _write_out(fh.read(), strip_ansi=strip_ansi)
                cursor = size
    finally:
        await conn.close()


def _seek_back(segments: list[Path], n_bytes: int) -> tuple[Path, int]:
    """Walk backwards through segments until n_bytes are covered."""
    remaining = n_bytes
    for segment in reversed(segments):
        size = segment.stat().st_size
        if size >= remaining:
            return segment, size - remaining
        remaining -= size
    return segments[0], 0


@terminal_app.command("ls")
def terminal_ls(
    agent: str | None = typer.Argument(None, help="Limit to one agent."),
) -> None:
    """Show what has been captured, per agent."""
    settings = _terminal_settings()
    asyncio.run(_terminal_ls(settings, agent))


async def _terminal_ls(settings, agent) -> None:
    conn = await connect(settings.db_path)
    chunks = TerminalChunkStore(conn, settings=settings)
    try:
        agents = [agent] if agent else await chunks.agents()
        if not agent:
            # An agent with files but no rows yet should still be listed.
            for directory in sorted(p for p in settings.terminal_log_dir.glob("*") if p.is_dir()):
                if directory.name not in agents:
                    agents.append(directory.name)
        if not agents:
            typer.secho("nothing captured yet", fg=typer.colors.YELLOW)
            return

        for name in sorted(agents):
            directory = agent_log_dir(settings, name)
            segments = list_segments(directory)
            rows = await chunks.fetch(name, limit=1)
            newest = await chunks.fetch(name, limit=1, newest=True)
            typer.secho(name, bold=True)
            typer.echo(f"  segments {len(segments)}")
            typer.echo(f"  bytes    {total_bytes(directory):,}")
            typer.echo(f"  chunks   {await chunks.count(name):,}")
            if rows and newest:
                typer.echo(f"  first    {rows[0].created_at}")
                typer.echo(f"  last     {newest[-1].created_at}")
            typer.echo(f"  path     {directory}")
    finally:
        await conn.close()


@terminal_app.command("attach")
def terminal_attach(
    agent: str = typer.Argument(..., help="Agent id."),
    session: str = typer.Option(..., "--session", help="tmux session name."),
) -> None:
    """Ask the running daemon to tap an agent's pane."""
    settings = Settings.from_env()
    reply = post_json(
        {"agentId": agent, "tmuxSession": session},
        str(settings.socket_path),
        endpoint="/v1/terminals",
    )
    if reply is None:
        typer.secho(f"no daemon at {settings.socket_path}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    if "detail" in reply:
        typer.secho(f"attach refused: {reply['detail']}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    typer.secho(f"tapped {agent}", fg=typer.colors.GREEN)
    typer.echo(f"  fifo {reply.get('fifo')}")


@terminal_app.command("detach")
def terminal_detach(agent: str = typer.Argument(..., help="Agent id.")) -> None:
    """Ask the running daemon to stop tapping an agent's pane."""
    settings = Settings.from_env()
    reply = post_json({}, str(settings.socket_path),
                      endpoint=f"/v1/terminals/{agent}", method="DELETE")
    if reply is None:
        typer.secho(f"no daemon at {settings.socket_path}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    typer.echo("detached" if reply.get("detached") else f"{agent} was not tapped")


@terminal_app.command("selftest")
def terminal_selftest() -> None:
    """Push a sentinel through FIFO -> bus -> writer and read it back.

    Proves the plumbing with no agent, no tmux and no daemon. This is the first
    thing to run when a tap is empty and it is not yet clear which half is
    broken — an empty log with `#{pane_pipe}` reading 1 means the reader is
    wedged, not the tap.
    """
    ok = asyncio.run(_selftest())
    if ok:
        typer.secho("PASS — fifo, bus, writer and chunk index all work", fg=typer.colors.GREEN)
    else:
        typer.secho("FAIL — see the errors above", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)


async def _selftest() -> bool:
    import secrets as _secrets
    import shutil
    import tempfile

    from fleetview.bus import EventBus
    from fleetview.terminal.fifo import FifoReader
    from fleetview.terminal.writer import LogWriter, output_topic

    root = Path(tempfile.mkdtemp(prefix="fleetview-selftest-"))
    try:
        settings = Settings(home=root)
        settings.ensure_directories()
        conn = await connect(settings.db_path)
        await apply_schema(conn)
        chunks = TerminalChunkStore(conn, settings=settings)
        await chunks.start()
        bus = EventBus()
        writer = LogWriter("selftest", settings=settings, chunks=chunks, bus=bus)
        await writer.start()

        sentinel = _secrets.token_hex(8).encode()
        reader = FifoReader(settings.fifo_dir / "selftest.fifo",
                            on_bytes=lambda d: bus.publish(output_topic("selftest"), d))
        reader.start()
        typer.echo(f"  fifo     {reader.path}")

        proc = await asyncio.create_subprocess_exec(
            "sh", "-c", f"printf '%s' {sentinel.decode()} >> {reader.path}"
        )
        await proc.wait()
        for _ in range(400):
            await asyncio.sleep(0.001)
            if writer.bytes_written >= len(sentinel):
                break
        reader.stop()
        await writer.stop()
        await chunks.stop()

        segments = list_segments(agent_log_dir(settings, "selftest"))
        captured = b"".join(p.read_bytes() for p in segments)
        typer.echo(f"  captured {len(captured)} bytes in {len(segments)} segment(s)")

        rows = await chunks.fetch("selftest")
        typer.echo(f"  indexed  {len(rows)} chunk row(s)")
        await conn.close()

        if sentinel not in captured:
            typer.secho("  sentinel never reached the log", fg=typer.colors.RED, err=True)
            return False
        if not rows:
            typer.secho("  bytes landed but nothing was indexed", fg=typer.colors.RED, err=True)
            return False
        return True
    finally:
        shutil.rmtree(root, ignore_errors=True)


@app.command()
def daemon() -> None:
    """Run the FleetView daemon. Serves the hook plane over a Unix socket."""
    from fleetview.daemon.server import SocketPathTooLongError, serve

    settings = Settings.from_env()
    try:
        asyncio.run(serve(settings))
    except (SlowFilesystemError, SocketPathTooLongError) as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc
    except KeyboardInterrupt:  # pragma: no cover - interactive
        typer.echo("daemon stopped")


if __name__ == "__main__":
    app()
