"""Terms-of-service invariants from PROJECT_PLAN.md §8.5.

These encode vendor requirements rather than behaviour. They are the reason
FleetView's architecture sits on the permitted side of both Anthropic's and
OpenAI's credential rules, so a failure here is a compliance problem, not a bug.
"""

import os
from pathlib import Path

import pytest

import fleetview
from fleetview.spawn.env import CREDENTIAL_DENY, build_agent_env
from fleetview.spawn.resolve import resolve_cli_binary

# Shapes a credential takes, for the blanket assertion below. Deliberately
# broader than CREDENTIAL_DENY: this catches a variable nobody thought to list.
CREDENTIAL_SUBSTRINGS = ("TOKEN", "KEY", "SECRET", "PASSWORD", "CREDENTIAL")

# Variables that name a credential's *location* rather than carrying one. A path
# is not a secret, and forwarding it is how the operator points an agent at a
# non-default config directory.
LOCATION_NOT_SECRET = {"CLAUDE_CONFIG_DIR"}


# --- Requirement 3: zero credential handling ---------------------------------

@pytest.mark.parametrize("key", sorted(CREDENTIAL_DENY))
def test_no_known_credential_is_ever_forwarded(key):
    env = build_agent_env({"HOME": "/home/x", key: "sk-live-do-not-leak"})
    assert key not in env, f"{key} reached the agent environment"


def test_no_credential_shaped_variable_survives_by_any_route():
    """A blanket check against the parent environment of whatever process runs
    the suite. When run from inside a Claude Code session this is a live test:
    that environment really does carry CLAUDE_CODE_MESSAGING_TOKEN."""
    env = build_agent_env(dict(os.environ))
    leaked = [
        k for k in env
        if k not in LOCATION_NOT_SECRET
        and any(s in k.upper() for s in CREDENTIAL_SUBSTRINGS)
    ]
    assert not leaked, f"credential-shaped variables reached the agent: {leaked}"


def test_home_is_present_because_it_is_the_only_credential_route():
    """Agents authenticate by reading their own vendor store under HOME. If HOME
    were dropped the agent would fail to authenticate — and the tempting fix
    would be to inject a key, which is the thing we must never do."""
    env = build_agent_env({"HOME": "/home/x"})
    assert env["HOME"] == "/home/x"


def test_agent_env_cannot_be_used_to_smuggle_a_credential():
    """The per-agent layer overrides everything else, so it must not become a
    bypass for the denylist."""
    with pytest.raises(ValueError, match="credential"):
        build_agent_env({"HOME": "/h"}, agent_env={"ANTHROPIC_API_KEY": "sk-nope"})


# --- Requirement 4: unmodified vendor binary ---------------------------------

def test_resolution_returns_a_real_executable():
    claude = resolve_cli_binary("claude")
    path = Path(claude)
    assert path.is_file(), f"{claude} is not a file"
    assert os.access(path, os.X_OK), f"{claude} is not executable"


def test_resolution_never_interposes_our_own_code():
    """Vendor terms require the binary be run as published — no wrapper, no shim
    we authored, no interception of its auth flow. Assert the resolved path lies
    outside this package entirely."""
    resolved = Path(resolve_cli_binary("claude")).resolve()
    package_root = Path(fleetview.__file__).parent.resolve()
    repo_root = package_root.parent.parent
    assert package_root not in resolved.parents, "resolved a path inside the fleetview package"
    assert "fleetview" not in resolved.parts, f"resolved path looks like our own shim: {resolved}"
    assert repo_root not in resolved.parents, "resolved a path inside this repository"


def test_no_argv_flag_disables_an_authentication_method():
    """Vendor terms: may not remove, disable, or restrict any authentication
    method built into the CLI. Guards against a future edit adding such a flag
    to the spike's provider table."""
    from fleetview.cli import PROVIDER_ARGV

    forbidden = ("--no-login", "--api-key", "--auth", "--token", "--no-auth")
    for provider, flags in PROVIDER_ARGV.items():
        joined = " ".join(flags).lower()
        for bad in forbidden:
            assert bad not in joined, f"{provider} argv touches authentication: {flags}"
