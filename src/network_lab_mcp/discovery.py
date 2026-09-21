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


# --------------------------------------------------------------------------
# IOS XR LLDP parsing -- observations only, no identity resolution
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class LldpObservation:
    local_device_id: str
    local_interface: str
    remote_device_id_raw: str
    remote_port_id: str
    capabilities: tuple[str, ...] = ()
    source: str = "lldp"


_CAPABILITY_CODES = {
    "R": "router",
    "B": "bridge",
    "T": "telephone",
    "C": "docsis_cable_device",
    "W": "wlan_access_point",
    "P": "repeater",
    "S": "station",
    "O": "other",
}


def _normalize_capabilities(raw: str) -> tuple[str, ...]:
    return tuple(_CAPABILITY_CODES.get(ch, ch) for ch in raw.strip() if ch.strip())


def parse_lldp_neighbors(raw_text: str, local_device_id: str) -> list[LldpObservation]:
    """Parse `show lldp neighbors` output into normalized observations.

    Deliberately tolerant of real IOS XR output variation: the timestamp
    line and capability-codes legend are optional/ignored, column spacing
    is not depended on (each data row is split on whitespace, not fixed
    columns), and a trailing device prompt or a blank line ends the table
    just as reliably as "Total entries displayed: N". A row that doesn't
    split into exactly the 5 expected fields is skipped rather than raising
    -- this parser only ever produces raw observations, never resolves a
    remote Device ID to a logical managed device (see
    resolve_remote_identity() for that separate stage)."""
    observations: list[LldpObservation] = []
    in_table = False
    for raw_line in raw_text.splitlines():
        line = raw_line.strip()
        if not in_table:
            if line.startswith("Device ID") and "Local Intf" in line:
                in_table = True
            continue
        if not line:
            break
        if line.lower().startswith("total entries displayed"):
            break
        if _IOSXR_PROMPT_RE.search(line):
            break
        parts = line.split()
        if len(parts) != 5:
            continue
        device_id_raw, local_intf, _hold_time, capability_raw, port_id = parts
        observations.append(
            LldpObservation(
                local_device_id=local_device_id,
                local_interface=local_intf,
                remote_device_id_raw=device_id_raw,
                remote_port_id=port_id,
                capabilities=_normalize_capabilities(capability_raw),
            )
        )
    return observations


# --------------------------------------------------------------------------
# Identity resolution + link reconciliation
# --------------------------------------------------------------------------


def resolve_remote_identity(remote_device_id_raw: str, identity_map: dict[str, str]) -> str | None:
    """Resolve an LLDP remote Device ID to a logical managed device ID, or
    None if unresolved/ambiguous (fail closed -- never a guess).

    `identity_map` maps logical_device_id -> observed_hostname. Matching
    order, each step requiring a *unique* match:
      1. exact observed-hostname match
      2. case-normalized exact match
      3. short-name/FQDN-style alias match (the raw ID's segment before its
         first '.', compared case-insensitively against each hostname)

    No substring search, no fuzzy matching, and no inference from the
    logical device ID itself (e.g. never "R2" in device_id)."""

    def _unique(matches: list[str]) -> str | None:
        return matches[0] if len(matches) == 1 else None

    exact = [logical for logical, hostname in identity_map.items() if hostname == remote_device_id_raw]
    result = _unique(exact)
    if result is not None or len(exact) > 1:
        return result

    lowered = remote_device_id_raw.lower()
    case_insensitive = [logical for logical, hostname in identity_map.items() if hostname.lower() == lowered]
    result = _unique(case_insensitive)
    if result is not None or len(case_insensitive) > 1:
        return result

    short_name = remote_device_id_raw.split(".", 1)[0].lower()
    alias = [logical for logical, hostname in identity_map.items() if hostname.lower() == short_name]
    return _unique(alias)


@dataclass(frozen=True)
class ManagedLink:
    a_device: str
    a_interface: str
    b_device: str
    b_interface: str


@dataclass(frozen=True)
class LinkConflict:
    endpoint_a: tuple[str, str]
    endpoint_b: tuple[str, str]
    observation_a: LldpObservation
    observation_b: LldpObservation


@dataclass(frozen=True)
class UnresolvedNeighbor:
    remote_device_id_raw: str
    local_device_id: str
    local_interface: str
    remote_port_id: str
    capabilities: tuple[str, ...]


def reconcile_links(
    resolved_observations: list[tuple[LldpObservation, str]],
) -> tuple[list[ManagedLink], list[LinkConflict]]:
    """Deduplicate reciprocal LLDP observations into physical links.

    `resolved_observations` is a list of (observation, resolved_remote_id)
    pairs, already filtered to observations whose remote resolved uniquely
    to a managed device (see resolve_remote_identity()). A physical link is
    keyed by its unordered pair of (device, interface) endpoints, so two
    parallel links between the same router pair on different interfaces
    stay distinct (section 46). A reciprocal pair that disagrees about the
    interface mapping is reported as a conflict instead of silently
    picking one side; this only detects disagreement between two *managed,
    resolved* observations of each other, not a one-sided observation
    versus an unrelated/unresolved one on the same local interface."""
    by_local_endpoint: dict[tuple[str, str], tuple[LldpObservation, str]] = {}
    for obs, remote_id in resolved_observations:
        by_local_endpoint[(obs.local_device_id, obs.local_interface)] = (obs, remote_id)

    links: list[ManagedLink] = []
    conflicts: list[LinkConflict] = []
    # Endpoints are consumed by their own dict key (local_dev, local_intf),
    # never by a "canonical pair" derived from a claimed remote port --
    # in a conflict the two sides claim *different* remote ports, so only
    # marking each side's own key reliably prevents processing the same
    # physical endpoint twice.
    seen_endpoints: set[tuple[str, str]] = set()

    for (local_dev, local_intf), (obs, remote_id) in by_local_endpoint.items():
        endpoint_a = (local_dev, local_intf)
        if endpoint_a in seen_endpoints:
            continue

        remote_intf = obs.remote_port_id
        endpoint_b = (remote_id, remote_intf)

        reverse = by_local_endpoint.get(endpoint_b)
        if reverse is None:
            # One-sided LLDP observation -- still a valid managed link.
            links.append(ManagedLink(local_dev, local_intf, remote_id, remote_intf))
            seen_endpoints.add(endpoint_a)
            continue

        reverse_obs, reverse_remote_id = reverse
        if reverse_remote_id == local_dev and reverse_obs.remote_port_id == local_intf:
            links.append(ManagedLink(local_dev, local_intf, remote_id, remote_intf))
        else:
            conflicts.append(LinkConflict(endpoint_a, endpoint_b, obs, reverse_obs))
        seen_endpoints.add(endpoint_a)
        seen_endpoints.add(endpoint_b)

    return links, conflicts

