"""Network Lab MCP stdio server.

Exposes exactly seven tools to Claude Code:

    get_active_topology()
    get_execution_instructions()
    terminal_open()
    terminal_send()
    terminal_read()
    terminal_list()
    terminal_close()

This process communicates over stdio, and stdout is reserved for MCP protocol
traffic. Diagnostic logging goes to stderr only; nothing is ever printed to
stdout.
"""

from __future__ import annotations

import logging
import sys

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from network_lab_mcp import lab, terminal

logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s network-lab-mcp: %(message)s")
logger = logging.getLogger("network_lab_mcp")

mcp = MCPServer("network-lab-mcp", version="0.1.0")


@mcp.tool()
def get_active_topology() -> dict:
    """Return the currently active lab topology: where the work is performed and
    which devices and links exist. Settings and the topology YAML are reloaded
    from disk on every call, so editing lab/settings.yaml takes effect
    immediately without restarting this server."""
    try:
        return lab.get_active_topology()
    except lab.LabConfigError as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool()
def get_execution_instructions() -> dict:
    """Return the operating principles, the active scenario, and the active
    reference knowledge for the current task: how to behave, what to
    accomplish, and what reusable knowledge is available. Reloaded from disk
    on every call."""
    try:
        return lab.get_execution_instructions()
    except lab.LabConfigError as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool()
def terminal_open(device: str) -> dict:
    """Open (or reuse) a terminal session for a device in the active topology,
    launching ssh or telnet inside a dedicated tmux environment. The active
    topology is reloaded from disk before opening the session. Login prompts,
    passwords, and host-key confirmations are handled interactively via
    terminal_read()/terminal_send(), not automated by this tool."""
    try:
        _, device_config = lab.get_device(device)
        return terminal.open_device_terminal(device, device_config)
    except (lab.LabConfigError, terminal.TerminalError) as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool()
def terminal_send(
    device: str,
    text: str | None = None,
    keys: list[str] | None = None,
    enter: bool = False,
) -> dict:
    """Send input to a device's open terminal session.

    Execution order is fixed and deterministic: if `text` is given it is sent
    literally first, then any `keys` (e.g. "C-c", "Tab", "Up") are sent in the
    supplied order, then Enter is sent last if `enter` is true. Nothing is
    deduplicated: passing keys=["Enter"] together with enter=true sends Enter
    twice. The `text` value is never logged or echoed back, since it may
    contain credentials."""
    try:
        return terminal.send_to_device(device, text, keys, enter)
    except terminal.TerminalError as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool()
def terminal_read(device: str, lines: int = terminal.DEFAULT_READ_LINES) -> dict:
    """Capture recent terminal output for a device, so the caller can inspect
    the current prompt, command output, a password/interactive prompt, paging
    state, or unexpected errors. `lines` limits how much recent scrollback is
    returned (default 100); the underlying tmux session retains a much larger
    history buffer."""
    try:
        return terminal.read_device(device, lines)
    except terminal.TerminalError as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool()
def terminal_list() -> dict:
    """List managed production terminal sessions for active-topology devices
    only. Local validation sessions are never included, regardless of whether
    a device happens to be named similarly to a validation identifier."""
    return {"sessions": terminal.list_device_sessions()}


@mcp.tool()
def terminal_close(device: str) -> dict:
    """Close a device's managed production terminal session. Only ever targets
    the production session namespace; cannot affect validation sessions."""
    try:
        return terminal.close_device_terminal(device)
    except terminal.TerminalError as exc:
        raise ToolError(str(exc)) from exc


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
