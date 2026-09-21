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


# OpenSSH's own interactive password prompt is always exactly
# "<user>@<host>'s password: " for whichever hop is currently
# authenticating -- stable, well-documented client-side text (not the
# remote device's own banner), used below to tell a jump-host prompt
# apart from the target device's own prompt without guessing.
_SSH_HOP_PASSWORD_PROMPT_RE = re.compile(r"(?P<hop_user>[^\s@]+)@(?P<hop_host>[^\s']+)'s password:\s*$")


def _resolve_login_password(device_id: str, device_config: dict, prompt_line: str) -> str:
    """Which password answers the current prompt.

    Direct SSH (no jump_host_config) is unambiguous: the one password
    prompt that can appear is always the target device's own.

    ProxyJump can show *two* separate password prompts in sequence (one
    per hop), and sending the wrong one to the wrong hop must never
    happen. This reads the prompting hop's own address out of OpenSSH's
    prompt text and only answers when it confidently matches the target
    device's own address; a prompt that matches the jump host's address,
    or that cannot be confidently attributed to either hop, fails closed
    (DiscoveryError) instead of guessing -- see "Bounded ProxyJump
    limitation" in docs/architecture.md. This is a deliberately bounded,
    Discovery-only limitation: it does not touch, and does not need to
    touch, terminal_open()'s shared connection-building code, since that
    path never automates password entry at all."""
    jump_host_config = device_config.get("jump_host_config")
    if not jump_host_config:
        return device_config.get("password") or ""

    hop_match = _SSH_HOP_PASSWORD_PROMPT_RE.search(prompt_line)
    hop_host = hop_match.group("hop_host") if hop_match else None
    if hop_host is not None and hop_host == str(device_config.get("address")):
        return device_config.get("password") or ""
    if hop_host is not None and hop_host == str(jump_host_config.get("address")):
        raise DiscoveryError(
            f"Device '{device_id}': the jump host prompted for an interactive password, which "
            "Discovery's automated bootstrap login does not support. Configure key/agent-based "
            "(non-interactive) SSH authentication for the jump host, or use direct SSH for Discovery."
        )
    raise DiscoveryError(
        f"Device '{device_id}': could not safely determine whether the password prompt belongs to the "
        "jump host or the target device; refusing to guess which credential to send. Configure "
        "key/agent-based (non-interactive) SSH authentication for the jump host."
    )


def _login(device_id: str, device_config: dict) -> str:
    """Open the bootstrap session, answer at most one password prompt, and
    return the resolved hostname once the IOS XR prompt is seen. Does not
    automate a host-key confirmation prompt (see
    terminal._build_transport_command()'s `accept_new_host_keys`, which
    avoids that prompt outright instead)."""
    terminal.open_bootstrap_terminal(device_id, device_config)
    text = terminal.wait_for_bootstrap_pattern(device_id, _LOGIN_WAIT_RE, LOGIN_TIMEOUT_SECONDS)
    last_line = _last_nonblank_line(text)
    if _PASSWORD_PROMPT_RE.search(last_line):
        password = _resolve_login_password(device_id, device_config, last_line)
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
    """Send one command line and wait for the IOS XR prompt to return.

    Captures the pane *before* sending so wait_for_bootstrap_pattern() can
    require the pane to have actually changed before accepting a prompt
    match -- otherwise a prompt already sitting in the pane from the
    *previous* command could satisfy the wait immediately, before this
    command produced any output at all (a stale-prompt race)."""
    baseline = terminal.read_bootstrap(device_id)
    terminal.send_to_bootstrap(device_id, command_text, None, True)
    full_text = terminal.wait_for_bootstrap_pattern(device_id, _IOSXR_PROMPT_RE, timeout, baseline_text=baseline)
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

_TOTAL_ENTRIES_RE = re.compile(r"total entries displayed:\s*(\d+)", re.IGNORECASE)


def _normalize_capabilities(raw: str) -> tuple[str, ...]:
    return tuple(_CAPABILITY_CODES.get(ch, ch) for ch in raw.strip() if ch.strip())


class LldpParseError(Exception):
    """Raised when `show lldp neighbors` output cannot be recognized as
    valid IOS XR LLDP structure, or when its own declared entry count
    disagrees with what was actually parsed -- this is the fail-closed
    signal that keeps a failed/unrecognized command (e.g. "% Invalid input
    detected...") from ever being silently treated as a legitimate
    zero-neighbor response."""


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
    resolve_remote_identity() for that separate stage).

    Raises LldpParseError (never returns a misleading empty list) when:
    - the "Device ID ... Local Intf ..." table header is never recognized
      at all (e.g. invalid/unrecognized command output such as
      "% Invalid input detected..." -- structurally indistinguishable from
      a legitimate zero-neighbor response unless this is checked); or
    - a "Total entries displayed: N" line is present and disagrees with
      the number of rows actually parsed.

    A legitimate zero-neighbor response (header present, no data rows, and
    either no "Total entries displayed" line or one that says 0) still
    returns an empty list successfully."""
    observations: list[LldpObservation] = []
    in_table = False
    declared_total: int | None = None
    for raw_line in raw_text.splitlines():
        line = raw_line.strip()
        if not in_table:
            if line.startswith("Device ID") and "Local Intf" in line:
                in_table = True
            continue
        if not line:
            break
        total_match = _TOTAL_ENTRIES_RE.match(line)
        if total_match:
            declared_total = int(total_match.group(1))
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
    if not in_table:
        raise LldpParseError(
            f"Device '{local_device_id}': 'show lldp neighbors' output was not recognized as valid "
            "IOS XR LLDP structure (no 'Device ID ... Local Intf' table header found)."
        )
    if declared_total is not None and declared_total != len(observations):
        raise LldpParseError(
            f"Device '{local_device_id}': 'Total entries displayed: {declared_total}' does not match "
            f"{len(observations)} parsed neighbor row(s)."
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


# --------------------------------------------------------------------------
# Top-level orchestration -- builds an in-memory DiscoveryResult
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class DiscoveryResult:
    access_info_name: str
    default_topology_name: str
    iosxr_target_count: int
    connected_count: int
    observation_count: int
    devices: dict[str, dict] = field(default_factory=dict)
    managed_links: list[ManagedLink] = field(default_factory=list)
    unresolved: list[UnresolvedNeighbor] = field(default_factory=list)
    conflicts: list[LinkConflict] = field(default_factory=list)
    identity_map: dict[str, str] = field(default_factory=dict)


def _select_iosxr_targets(access_data: dict) -> dict[str, dict]:
    """Supported Discovery targets: type iosxr only. `type host` is
    silently skipped (not an error); iosxe/nxos are unsupported for this
    phase and are also skipped, not failed -- only a mixed definition with
    *zero* iosxr targets fails (see discover_topology())."""
    targets: dict[str, dict] = {}
    devices = access_data.get("devices") or {}
    jump_hosts = access_data.get("jump_hosts") or {}
    for device_id, device_cfg in devices.items():
        device_cfg = device_cfg or {}
        raw_type = device_cfg.get("type")
        if not raw_type:
            continue
        try:
            normalized = lab.normalize_device_type(str(raw_type))
        except lab.LabConfigError:
            continue
        if normalized != "iosxr":
            continue
        resolved = dict(device_cfg)
        jump_ref = device_cfg.get("jump_host")
        if jump_ref and jump_ref in jump_hosts:
            resolved["jump_host_config"] = dict(jump_hosts[jump_ref])
        targets[device_id] = resolved
    return targets


def discover_topology(lab_root=None) -> DiscoveryResult:
    """Run the full Discovery flow against committed running-config's
    selected access-info and return an in-memory DiscoveryResult.

    Never touches the committed candidate/topology/settings -- turning this
    into a topology candidate is the caller's job (cli/config.py), exactly
    like any other topology edit. Conservative or nothing: if any supported
    IOS XR target fails login/collection, the whole operation fails
    (DiscoveryError) before any bootstrap session is even considered for
    reconciliation -- there is no partial result."""
    lab_root = lab_root or lab.find_lab_root()
    access_info_name = resolve_default_topology_name(lab_root)
    if not lab.access_info_exists(access_info_name, lab_root):
        raise DiscoveryError(f"Selected access-info '{access_info_name}' does not exist.")
    access_data = lab.load_access_info(access_info_name, lab_root)

    iosxr_targets = _select_iosxr_targets(access_data)
    if not iosxr_targets:
        raise DiscoveryError(f"No supported IOS XR devices found in access-info '{access_info_name}'.")

    collected: dict[str, dict] = {}
    try:
        for device_id, device_cfg in iosxr_targets.items():
            collected[device_id] = _bootstrap_collect(device_id, device_cfg)
    finally:
        for device_id in iosxr_targets:
            terminal.close_bootstrap_terminal(device_id)

    identity_map = {device_id: info["hostname"] for device_id, info in collected.items()}

    resolved: list[tuple[LldpObservation, str]] = []
    unresolved: list[UnresolvedNeighbor] = []
    observation_count = 0
    for device_id, info in collected.items():
        try:
            observations = parse_lldp_neighbors(info["show_lldp_neighbors"], device_id)
        except LldpParseError as exc:
            raise DiscoveryError(str(exc)) from exc
        observation_count += len(observations)
        for obs in observations:
            remote_id = resolve_remote_identity(obs.remote_device_id_raw, identity_map)
            if remote_id is not None and remote_id in iosxr_targets:
                resolved.append((obs, remote_id))
            else:
                unresolved.append(
                    UnresolvedNeighbor(
                        remote_device_id_raw=obs.remote_device_id_raw,
                        local_device_id=obs.local_device_id,
                        local_interface=obs.local_interface,
                        remote_port_id=obs.remote_port_id,
                        capabilities=obs.capabilities,
                    )
                )

    links, conflicts = reconcile_links(resolved)

    return DiscoveryResult(
        access_info_name=access_info_name,
        default_topology_name=access_info_name,
        iosxr_target_count=len(iosxr_targets),
        connected_count=len(collected),
        observation_count=observation_count,
        devices={device_id: {"type": "iosxr"} for device_id in iosxr_targets},
        managed_links=links,
        unresolved=unresolved,
        conflicts=conflicts,
        identity_map=identity_map,
    )


def resolve_default_topology_name(lab_root=None) -> str:
    """The default Discovery target topology name: the committed selected
    access-info definition's own name (section 53) -- same-basename is
    only this default, never a runtime requirement (section 54). Exposed
    separately from discover_topology() so a caller (the CLI) can check
    candidate-switch safety *before* running the real, expensive Discovery
    flow, not just after."""
    lab_root = lab_root or lab.find_lab_root()
    settings = lab.read_settings(lab_root)
    access_info_name = lab.get_active_access_info_name(settings)
    if not access_info_name:
        raise DiscoveryError("No access-info is selected in running-config.")
    return access_info_name


def _topology_link_key(link: dict) -> tuple:
    endpoint_a = (link.get("a"), link.get("a_interface"))
    endpoint_b = (link.get("b"), link.get("b_interface"))
    return tuple(sorted((endpoint_a, endpoint_b)))


def build_topology_devices_and_links(result: DiscoveryResult, existing_candidate: dict) -> tuple[dict, list]:
    """Merge a DiscoveryResult's managed devices/links into whatever
    devices/links already exist in `existing_candidate` (a brand-new empty
    topology or an already-committed/candidate one). Conservative: never
    removes an existing device or link, and never duplicates a link that's
    already present (compared by its unordered (device, interface)
    endpoint pair) -- see "Existing target topology behavior" (section 60).
    Pure/no I/O: the caller (cli/config.py) is responsible for actually
    writing the result into `session.definition_candidate`."""
    devices = dict(existing_candidate.get("devices") or {})
    for device_id, fields in result.devices.items():
        merged = dict(devices.get(device_id) or {})
        merged.update(fields)
        devices[device_id] = merged

    links = list(existing_candidate.get("links") or [])
    seen_keys = {_topology_link_key(link) for link in links}
    for managed_link in result.managed_links:
        link_dict = {
            "a": managed_link.a_device,
            "a_interface": managed_link.a_interface,
            "b": managed_link.b_device,
            "b_interface": managed_link.b_interface,
        }
        key = _topology_link_key(link_dict)
        if key in seen_keys:
            continue
        links.append(link_dict)
        seen_keys.add(key)

    return devices, links
