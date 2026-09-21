"""tmux-based terminal session management for Network Lab MCP.

All terminal state lives in a dedicated tmux server (a separate named socket),
which is the single source of truth for session lifetime. This module does not
keep any session registry of its own.

Two structurally separate session namespaces are used:

    network-lab-device-<device-id>       production topology device sessions
    network-lab-validation-<validation>  local validation-only sessions

The namespaces are distinguished by their fixed prefix, never by searching for
the substring "validation" inside a name. This keeps a legitimate topology
device named e.g. "validation-router" (session
"network-lab-device-validation-router") clearly separate from a validation
session such as "network-lab-validation-terminal-io".

Production and local validation callers share the same internal session
primitives (`_ensure_managed_session`, `_send_literal_text`, `_send_special_keys`,
`_capture_pane`, `_list_sessions`, `_close_session`); only the derived session
name and the launched command differ.
"""

from __future__ import annotations

import re
import shutil
import subprocess

TMUX_SOCKET_NAME = "network-lab-mcp"

PRODUCTION_PREFIX = "network-lab-device-"
VALIDATION_PREFIX = "network-lab-validation-"
BOOTSTRAP_SESSION = "network-lab-bootstrap-initializer"

HISTORY_LIMIT = 20000
DEFAULT_READ_LINES = 100

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")

_SPECIAL_KEYS = {
    "Enter",
    "Escape",
    "Tab",
    "Space",
    "BSpace",
    "Up",
    "Down",
    "Left",
    "Right",
    "Home",
    "End",
    "PageUp",
    "PageDown",
}
_SPECIAL_KEY_RE = re.compile(r"^C-[A-Za-z]$|^M-[A-Za-z]$")


class TerminalError(Exception):
    """A clear, user-facing terminal/session management error."""


# --------------------------------------------------------------------------
# Session name derivation
# --------------------------------------------------------------------------


def _validate_identifier(identifier: str, what: str) -> None:
    if not isinstance(identifier, str) or not _NAME_RE.match(identifier):
        raise TerminalError(
            f"{what} '{identifier}' is not a valid identifier. Identifiers must start with "
            "a letter or digit and contain only letters, digits, '-', and '_'."
        )


def derive_production_session_name(device_name: str) -> str:
    """Derive the production tmux session name for an active-topology device."""
    _validate_identifier(device_name, "Device name")
    return f"{PRODUCTION_PREFIX}{device_name}"


def derive_validation_session_name(validation_id: str) -> str:
    """Derive the validation-only tmux session name for a local validation identifier."""
    _validate_identifier(validation_id, "Validation identifier")
    return f"{VALIDATION_PREFIX}{validation_id}"


def is_production_session(session_name: str) -> bool:
    return session_name.startswith(PRODUCTION_PREFIX)


def is_validation_session(session_name: str) -> bool:
    return session_name.startswith(VALIDATION_PREFIX)


def production_device_name(session_name: str) -> str:
    """Recover the device name encoded in a production session name."""
    if not is_production_session(session_name):
        raise TerminalError(f"'{session_name}' is not a production session.")
    return session_name[len(PRODUCTION_PREFIX) :]


# --------------------------------------------------------------------------
# Low-level tmux invocation
# --------------------------------------------------------------------------


def _tmux_base() -> list[str]:
    if shutil.which("tmux") is None:
        raise TerminalError("The 'tmux' binary is not available on this system.")
    return ["tmux", "-L", TMUX_SOCKET_NAME]


def _run(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        _tmux_base() + args,
        capture_output=True,
        text=True,
    )
    if check and result.returncode != 0:
        raise TerminalError(f"tmux command failed: {' '.join(args)}: {result.stderr.strip()}")
    return result


# --------------------------------------------------------------------------
# Internal session-management primitives
#
# These are shared by production terminal_open()/... and the local validation
# helpers below; only the session-name namespace and the launched command
# differ between the two callers.
# --------------------------------------------------------------------------


def _server_has_any_session() -> bool:
    result = _run(["list-sessions", "-F", "#{session_name}"], check=False)
    return result.returncode == 0


def _session_exists(session_name: str) -> bool:
    result = _run(["has-session", "-t", session_name], check=False)
    return result.returncode == 0


def _create_session(session_name: str, command: list[str]) -> None:
    _run(["new-session", "-d", "-s", session_name, "-x", "220", "-y", "50", *command])


def _kill_session_if_exists(session_name: str) -> None:
    if _session_exists(session_name):
        _run(["kill-session", "-t", session_name], check=False)


def _ensure_tmux_environment() -> bool:
    """Ensure the dedicated tmux server exists and the history-limit is configured.

    Returns True if a temporary bootstrap session had to be created to start
    the dedicated tmux server. The bootstrap session is internal only and is
    never exposed through any public tool.
    """
    bootstrap_created = False
    if not _server_has_any_session():
        _create_session(BOOTSTRAP_SESSION, ["cat"])
        bootstrap_created = True
    # Idempotent: safe to run on every call, including when the server and its
    # managed panes already existed before this process started.
    _run(["set-option", "-g", "history-limit", str(HISTORY_LIMIT)])
    # Keep a pane around after its command exits (e.g. ssh/telnet disconnects)
    # so terminal_read() can still observe the final output instead of the
    # session silently disappearing.
    _run(["set-option", "-g", "remain-on-exit", "on"])
    return bootstrap_created


def _ensure_managed_session(session_name: str, command: list[str]) -> bool:
    """Ensure a managed session exists, creating it with `command` if needed.

    Returns True if an existing session was reused, False if a new one was
    created. Existing sessions are never destroyed to reapply configuration.
    """
    bootstrap_created = _ensure_tmux_environment()
    reused = _session_exists(session_name)
    if not reused:
        _create_session(session_name, command)
    if bootstrap_created and session_name != BOOTSTRAP_SESSION:
        _kill_session_if_exists(BOOTSTRAP_SESSION)
    return reused


def _send_literal_text(session_name: str, text: str) -> None:
    if not _session_exists(session_name):
        raise TerminalError(f"Session '{session_name}' does not exist.")
    _run(["send-keys", "-t", session_name, "-l", "--", text])


def _send_special_keys(session_name: str, keys: list[str]) -> None:
    if not _session_exists(session_name):
        raise TerminalError(f"Session '{session_name}' does not exist.")
    for key in keys:
        if key not in _SPECIAL_KEYS and not _SPECIAL_KEY_RE.match(key):
            raise TerminalError(
                f"Unsupported special key '{key}'. Supported: {sorted(_SPECIAL_KEYS)} "
                "or 'C-<letter>' / 'M-<letter>' combinations."
            )
    if keys:
        _run(["send-keys", "-t", session_name, "--", *keys])


def _send_enter(session_name: str) -> None:
    if not _session_exists(session_name):
        raise TerminalError(f"Session '{session_name}' does not exist.")
    _run(["send-keys", "-t", session_name, "--", "Enter"])


def _capture_pane(session_name: str, lines: int) -> str:
    if not _session_exists(session_name):
        raise TerminalError(f"Session '{session_name}' does not exist.")
    # Capture the whole available buffer (history + current screen) and slice
    # in Python. tmux's negative -S offsets are relative to history, which is
    # empty until the visible pane has scrolled at least once, so asking tmux
    # itself for "the last N lines" via -S/-E is unreliable while a pane is
    # still filling its first screen. capture-pane is cheap even over the
    # full history, so this stays simple and correct in both cases.
    result = _run(["capture-pane", "-t", session_name, "-p", "-S", "-"])
    text = result.stdout.rstrip("\n")
    if not text:
        return ""
    return "\n".join(text.split("\n")[-lines:])


def _pane_state(session_name: str) -> str:
    result = _run(["list-panes", "-t", session_name, "-F", "#{pane_dead}"], check=False)
    if result.returncode != 0:
        return "unknown"
    lines = result.stdout.strip().splitlines()
    return "exited" if lines and lines[0] == "1" else "running"


def _list_sessions(prefix: str) -> list[str]:
    if not _server_has_any_session():
        return []
    result = _run(["list-sessions", "-F", "#{session_name}"], check=False)
    if result.returncode != 0:
        return []
    return [name for name in result.stdout.splitlines() if name.startswith(prefix)]


def _close_session(session_name: str) -> bool:
    """Close a managed session. Returns True if it existed and was closed."""
    if not _session_exists(session_name):
        return False
    _run(["kill-session", "-t", session_name])
    return True


# --------------------------------------------------------------------------
# Transport command construction
# --------------------------------------------------------------------------


def _require_binary(binary: str) -> None:
    if shutil.which(binary) is None:
        raise TerminalError(f"The '{binary}' binary is not available on this system.")


def _ssh_target(config: dict, default_port: int) -> tuple[str, int]:
    address = config.get("address")
    if not address:
        raise TerminalError("Device configuration is missing 'address'.")
    port = config.get("port", default_port)
    username = config.get("username")
    target = f"{username}@{address}" if username else str(address)
    return target, port


def _build_transport_command(device_config: dict) -> tuple[str, list[str]]:
    transport = device_config.get("transport")
    address = device_config.get("address")
    if not address:
        raise TerminalError("Device configuration is missing 'address'.")

    if transport == "ssh":
        _require_binary("ssh")
        target, port = _ssh_target(device_config, 22)
        command = ["ssh", "-p", str(port), target]

        jump_host = device_config.get("jump_host_config")
        if jump_host is not None:
            # Single-hop native OpenSSH ProxyJump (-J jump-target:jump-port).
            # No shell-hop automation: OpenSSH itself opens the second SSH
            # connection through the first, and the tmux pane still just
            # sees one interactive session to read/send against, exactly
            # like a direct connection.
            jump_target, jump_port = _ssh_target(jump_host, 22)
            command = ["ssh", "-J", f"{jump_target}:{jump_port}", "-p", str(port), target]

        return transport, command

    if transport == "telnet":
        _require_binary("telnet")
        port = device_config.get("port", 23)
        return transport, ["telnet", str(address), str(port)]

    raise TerminalError(f"Unsupported transport '{transport}'. Expected 'ssh' or 'telnet'.")


# --------------------------------------------------------------------------
# Public production API (used by the terminal_* MCP tools)
# --------------------------------------------------------------------------


def open_device_terminal(device_name: str, device_config: dict) -> dict:
    """Open (or reuse) the production terminal session for an active-topology device."""
    session_name = derive_production_session_name(device_name)
    transport, command = _build_transport_command(device_config)
    reused = _ensure_managed_session(session_name, command)
    return {
        "device": device_name,
        "session_name": session_name,
        "reused": reused,
        "transport": transport,
    }


def send_to_device(device_name: str, text: str | None, keys: list[str] | None, enter: bool) -> dict:
    """Send input to a device's production session: text, then keys, then Enter (in that order)."""
    session_name = derive_production_session_name(device_name)
    if text:
        _send_literal_text(session_name, text)
    if keys:
        _send_special_keys(session_name, keys)
    if enter:
        _send_enter(session_name)
    return {"device": device_name, "session_name": session_name}


def read_device(device_name: str, lines: int = DEFAULT_READ_LINES) -> dict:
    """Capture recent pane content from a device's production session."""
    session_name = derive_production_session_name(device_name)
    content = _capture_pane(session_name, lines)
    return {"device": device_name, "session_name": session_name, "content": content}


def list_device_sessions() -> list[dict]:
    """List managed production sessions (network-lab-device-* namespace only)."""
    sessions = []
    for session_name in _list_sessions(PRODUCTION_PREFIX):
        sessions.append(
            {
                "device": production_device_name(session_name),
                "session_name": session_name,
                "state": _pane_state(session_name),
            }
        )
    return sessions


def close_device_terminal(device_name: str) -> dict:
    """Close a device's production session. Never touches the validation namespace."""
    session_name = derive_production_session_name(device_name)
    closed = _close_session(session_name)
    return {"device": device_name, "session_name": session_name, "closed": closed}


# --------------------------------------------------------------------------
# Internal local-validation helpers
#
# Not exposed as MCP tools and not a public transport. These exist only so
# that local validation (Step 1 Section 55/56) can exercise the exact same
# session-management primitives used in production, against a safe local
# command instead of `ssh`/`telnet`.
# --------------------------------------------------------------------------


def open_validation_session(validation_id: str, command: list[str]) -> dict:
    session_name = derive_validation_session_name(validation_id)
    reused = _ensure_managed_session(session_name, command)
    return {"validation_id": validation_id, "session_name": session_name, "reused": reused}


def send_to_validation(validation_id: str, text: str | None, keys: list[str] | None, enter: bool) -> dict:
    session_name = derive_validation_session_name(validation_id)
    if text:
        _send_literal_text(session_name, text)
    if keys:
        _send_special_keys(session_name, keys)
    if enter:
        _send_enter(session_name)
    return {"validation_id": validation_id, "session_name": session_name}


def read_validation(validation_id: str, lines: int = DEFAULT_READ_LINES) -> str:
    session_name = derive_validation_session_name(validation_id)
    return _capture_pane(session_name, lines)


def validation_session_exists(validation_id: str) -> bool:
    return _session_exists(derive_validation_session_name(validation_id))


def list_validation_sessions() -> list[str]:
    return _list_sessions(VALIDATION_PREFIX)


def close_validation_session(validation_id: str) -> bool:
    session_name = derive_validation_session_name(validation_id)
    return _close_session(session_name)
