"""Environment-hygiene behaviour for spawned agents."""

import pytest

from fleetview.spawn.env import (
    CONFIG_ALLOWLIST,
    ESSENTIAL_KEYS,
    MAX_VALUE_BYTES,
    build_agent_env,
    is_blocked_key,
)

# Captured live from a real Claude Code session on 2026-09-21. munder-difflin's
# note records that a hardcoded five-name list was "seven short" of a live
# session's dump; this is thirteen, so their count was essentially exact. The
# list is here as evidence for WHY the strip is by prefix, not by name — it is
# not the specification, and it will drift.
LIVE_SESSION_MARKERS = [
    "CLAUDECODE",
    "CLAUDE_AGENT_SDK_VERSION",
    "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING",
    "CLAUDE_CODE_ENABLE_TASKS",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_EXECPATH",
    "CLAUDE_CODE_MESSAGING_SOCKET",
    "CLAUDE_CODE_MESSAGING_TOKEN",
    "CLAUDE_CODE_SESSION_ATTENDED",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_EFFORT",
    "CLAUDE_PID",
]


def test_essential_keys_are_forwarded():
    parent = {k: f"value-{k}" for k in ESSENTIAL_KEYS}
    env = build_agent_env(parent)
    assert env["HOME"] == "value-HOME"
    assert env["PATH"] == "value-PATH"
    assert env["SSH_AUTH_SOCK"] == "value-SSH_AUTH_SOCK"


def test_unlisted_parent_variables_do_not_pass():
    env = build_agent_env({"HOME": "/home/x", "SOME_RANDOM_VAR": "leak"})
    assert "SOME_RANDOM_VAR" not in env


@pytest.mark.parametrize("marker", LIVE_SESSION_MARKERS)
def test_live_session_markers_are_stripped(marker):
    """Every identity marker a real Claude Code session exports must be dropped."""
    assert is_blocked_key(marker), f"{marker} would reach the agent"
    env = build_agent_env({"HOME": "/home/x", marker: "1"})
    assert marker not in env


def test_child_session_marker_never_survives():
    """The specific regression: an inherited CLAUDE_CODE_CHILD_SESSION makes a
    spawned agent believe it is a child session, which silently disables
    transcript saving and breaks --resume for the whole run. It fails with no
    error, so only a test catches it."""
    env = build_agent_env({"HOME": "/home/x", "CLAUDE_CODE_CHILD_SESSION": "1"})
    assert "CLAUDE_CODE_CHILD_SESSION" not in env


def test_unknown_future_marker_is_stripped_by_prefix():
    """A marker that does not exist yet must still be caught — this is the whole
    argument for prefix-matching over a name list."""
    env = build_agent_env({"HOME": "/home/x", "CLAUDE_CODE_SOMETHING_INVENTED_IN_2027": "x"})
    assert "CLAUDE_CODE_SOMETHING_INVENTED_IN_2027" not in env


@pytest.mark.parametrize("key", sorted(CONFIG_ALLOWLIST))
def test_operator_config_survives_the_strip(key):
    """These share the vendor prefix but describe the OPERATOR's own setup, not
    a parent session. Stripping them breaks agents in exactly the quiet way the
    strip exists to prevent."""
    env = build_agent_env({"HOME": "/home/x", key: "operator-value"})
    assert env[key] == "operator-value"


def test_terminal_identity_is_set():
    env = build_agent_env({"HOME": "/home/x"})
    assert env["TERM"] == "xterm-256color"
    assert env["COLORTERM"] == "truecolor"
    assert env["FORCE_COLOR"] == "1"


def test_locale_defaults_to_utf8_but_operator_choice_wins():
    assert build_agent_env({"HOME": "/h"})["LC_CTYPE"] == "en_US.UTF-8"
    assert build_agent_env({"HOME": "/h", "LANG": "en_GB.UTF-8"})["LANG"] == "en_GB.UTF-8"
    assert build_agent_env({"HOME": "/h", "LC_ALL": "ja_JP.UTF-8"})["LC_CTYPE"] == "ja_JP.UTF-8"


def test_agent_env_wins_over_every_earlier_layer():
    env = build_agent_env({"HOME": "/home/x", "TERM": "dumb"}, agent_env={"TERM": "screen-256color"})
    assert env["TERM"] == "screen-256color"


def test_oversized_values_are_dropped():
    """Keeps the tmux `new-session -e` argv under the kernel limit on a busy host."""
    env = build_agent_env({"HOME": "/h", "PATH": "x" * (MAX_VALUE_BYTES + 1)})
    assert "PATH" not in env
