"""Step 3 initial scope: IOS XR + LLDP topology discovery.

    committed active_access_info
        -> private bootstrap connection (terminal.open_bootstrap_terminal)
        -> IOS XR login + `show version`/`show running-config`/
           `show lldp neighbors` (this module's minimal command runner)
        -> IOS XR LLDP parsing (parse_lldp_neighbors)
        -> identity resolution (resolve_remote_identity)
        -> link reconciliation (reconcile_links)
        -> DiscoveryResult (in-memory only)

`discover_topology()` is the only entry point cli/main.py's `discover
topology` handler calls; everything else here is an implementation detail.
It never writes to disk and never mutates running-config or any committed
file -- the caller (cli/config.py) is responsible for turning a
DiscoveryResult into a topology *candidate*, exactly like any other
topology edit, and nothing here special-cases commit/clear/root/exit/end.

Explicitly out of scope for this initial implementation (see README.md/
docs/architecture.md for the full list): IOS XE/NX-OS discovery, CDP,
multi-hop jump hosts, SNMP/NETCONF/RESTCONF, automatic topology
activation/commit, and a generic discovery/plugin framework.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from network_lab_mcp import lab, terminal

LOGIN_TIMEOUT_SECONDS = 30
COMMAND_TIMEOUT_SECONDS = 25

# Matches an IOS XR exec prompt, e.g. "RP/0/RP0/CPU0:APJC_JP_OSK_R1#", and
# captures the hostname -- this doubles as both "the prompt has returned"
# detection and the smallest reliable IOS XR hostname source (see
# "Local hostname collection" in docs/architecture.md): no separate
# `show running-config | include hostname` query is needed.
_IOSXR_PROMPT_RE = re.compile(r"RP/\S+/CPU\d+:(?P<hostname>[^#\s]+)#\s*$", re.MULTILINE)
_PASSWORD_PROMPT_RE = re.compile(r"[Pp]assword:\s*$", re.MULTILINE)
_LOGIN_WAIT_RE = re.compile(f"(?:{_PASSWORD_PROMPT_RE.pattern})|(?:{_IOSXR_PROMPT_RE.pattern})")


class DiscoveryError(Exception):
    """Fail-closed error for the whole `discover topology` operation."""


# --------------------------------------------------------------------------
# Private bootstrap connectivity + minimal command runner
# --------------------------------------------------------------------------


def _last_nonblank_line(text: str) -> str:
    for line in reversed(text.splitlines()):
        if line.strip():
            return line
    return ""


def _login(device_id: str, device_config: dict) -> str:
    """Open the bootstrap session, answer at most one password prompt, and
    return the resolved hostname once the IOS XR prompt is seen. Does not
    automate a host-key confirmation prompt (see
    terminal._build_transport_command()'s `accept_new_host_keys`, which
    avoids that prompt outright instead)."""
    terminal.open_bootstrap_terminal(device_id, device_config)
    text = terminal.wait_for_bootstrap_pattern(device_id, _LOGIN_WAIT_RE, LOGIN_TIMEOUT_SECONDS)
    if _PASSWORD_PROMPT_RE.search(_last_nonblank_line(text)):
        password = device_config.get("password") or ""
        terminal.send_to_bootstrap(device_id, password, None, True)
        text = terminal.wait_for_bootstrap_pattern(device_id, _IOSXR_PROMPT_RE, LOGIN_TIMEOUT_SECONDS)
    match = _IOSXR_PROMPT_RE.search(_last_nonblank_line(text))
    if not match:
        raise DiscoveryError(f"Device '{device_id}': did not reach an IOS XR prompt after login.")
    return match.group("hostname")


def _extract_command_output(full_text: str, command_text: str) -> str:
    """Slice out one command's own output from the full pane transcript:
    everything after the line that echoes the command, up to (excluding)
    the trailing prompt line(s)."""
    lines = full_text.splitlines()
    needle = command_text.strip()
    start = 0
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].rstrip().endswith(needle):
            start = i + 1
            break
    end = len(lines)
    while end > start and (not lines[end - 1].strip() or _IOSXR_PROMPT_RE.search(lines[end - 1])):
        end -= 1
    return "\n".join(lines[start:end])


def _run_command(device_id: str, command_text: str, timeout: float = COMMAND_TIMEOUT_SECONDS) -> str:
    terminal.send_to_bootstrap(device_id, command_text, None, True)
    full_text = terminal.wait_for_bootstrap_pattern(device_id, _IOSXR_PROMPT_RE, timeout)
    return _extract_command_output(full_text, command_text)


def _bootstrap_collect(device_id: str, device_config: dict) -> dict:
    """Log in, disable pagination, and collect the three required
    read-only commands. Fails closed (DiscoveryError) on any login,
    command, or timeout failure -- the caller is responsible for closing
    the bootstrap session either way."""
    try:
        hostname = _login(device_id, device_config)
        # Discovery sessions are temporary and closed right after
        # collection, so there is no need to restore terminal length
        # afterwards (see docs/architecture.md).
        _run_command(device_id, "terminal length 0")
        show_version = _run_command(device_id, "show version")
        show_running_config = _run_command(device_id, "show running-config")
        show_lldp_neighbors = _run_command(device_id, "show lldp neighbors")
    except terminal.TerminalError as exc:
        raise DiscoveryError(f"Device '{device_id}': {exc}") from exc
    return {
        "hostname": hostname,
        "show_version": show_version,
        "show_running_config": show_running_config,
        "show_lldp_neighbors": show_lldp_neighbors,
    }

