"""Throwaway CLI for driving the Phase 0 spike by hand.

Phase 0 proves one thing: that we can spawn a vendor CLI from a daemon-like
process and have it authenticate on the operator's own subscription, save a
transcript, and resume. This module is scaffolding for that experiment and is
expected to be replaced by the real daemon in Phase 1.
"""

from __future__ import annotations

import os
import secrets
import subprocess
from pathlib import Path

import typer

from fleetview.spawn.env import build_agent_env
from fleetview.spawn.resolve import resolve_cli_binary, user_path
from fleetview.spawn.tmux import AgentSpawnError, create_agent_session

app = typer.Typer(add_completion=False, help="FleetView — Phase 0 spike driver.")

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
    glacial. Harmless for a spike, fatal for the Phase 1 SQLite store — worth
    surfacing early either way."""
    try:
        fstype = subprocess.run(
            ["findmnt", "-no", "FSTYPE", "--target", str(path)],
            capture_output=True, text=True, timeout=3, check=False,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return
    if fstype in {"9p", "drvfs", "cifs"}:
        typer.secho(
            f"  warning: {path} is on a {fstype} filesystem. Fine for this spike, but the "
            f"Phase 1 event store must live on ext4 (~/.fleetview/).",
            fg=typer.colors.YELLOW,
        )


@app.command()
def spike(
    provider: str = typer.Argument("claude", help="Which CLI to spawn."),
    cwd: Path = typer.Option(Path.cwd(), "--cwd", help="Working directory for the agent."),
    resume: str | None = typer.Option(None, "--resume", help="Resume a prior session id."),
    name: str | None = typer.Option(None, "--name", help="tmux session name."),
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

    env = build_agent_env(dict(os.environ), agent_env={
        "FLEETVIEW_AGENT_ID": session_name,
        "PATH": user_path(),
    })

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


if __name__ == "__main__":
    app()
