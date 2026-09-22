"""Step 3 scope: IOS XR + IOS XE topology discovery over LLDP and CDP.

    committed active_access_info
        -> private bootstrap connection (terminal.open_bootstrap_terminal)
        -> per-type login + read-only collection (this module's minimal
           command runner): IOS XR gets LLDP + CDP; IOS XE gets CDP only
           (Step 3.6 Section 3 -- IOS XE has no LLDP support in this step)
        -> normalized neighbor observations (parse_lldp_neighbors /
           parse_cdp_neighbors), each tagged with its own `source`
        -> identity resolution (resolve_remote_identity, protocol-agnostic)
        -> multi-protocol link reconciliation (reconcile_links): the same
           physical link seen via both protocols (or reciprocally from both
           ends) becomes exactly one link; a local interface where LLDP and
           CDP disagree about the neighbor is reported as a conflict
           instead of silently picking one
        -> DiscoveryResult (in-memory only)

`discover_topology()` is the only entry point cli/main.py's `discover
topology` handler calls; everything else here is an implementation detail.
It never writes to disk and never mutates running-config or any committed
file -- the caller (cli/config.py) is responsible for turning a
DiscoveryResult into a topology *candidate*, exactly like any other
topology edit, and nothing here special-cases commit/clear/root/exit/end.

IOS XE login uses whatever transport the device's access-info specifies
(ssh or telnet). Telnet is unauthenticated-in-transit and unencrypted --
suitable only for isolated lab environments, never presented as a secure
transport (see README.md/docs/architecture.md).

Explicitly out of scope: NX-OS discovery, multi-hop jump hosts (telnet
devices cannot have a jump_host at all -- see lab.validate_device_jump_
host_references()), SNMP/NETCONF/RESTCONF, automatic topology activation/
commit, and a generic discovery/plugin framework."""

from __future__ import annotations

import concurrent.futures
import re
from dataclasses import dataclass, field

from network_lab_mcp import lab, terminal

LOGIN_TIMEOUT_SECONDS = 30
COMMAND_TIMEOUT_SECONDS = 25

# Step 3.3: bounds how many devices' bootstrap collection runs concurrently.
# Small and internal, not a public CLI/config knob (see task boundaries) --
# 8 comfortably covers real lab scale (a handful to a dozen managed
# devices) while keeping concurrent SSH/tmux session creation bounded
# rather than launching one thread per arbitrary target count.
DISCOVERY_MAX_WORKERS = 8

# Matches an IOS XR exec prompt, e.g. "RP/0/RP0/CPU0:APJC_JP_OSK_R1#", and
# captures the hostname -- this doubles as both "the prompt has returned"
# detection and the smallest reliable IOS XR hostname source (see
# "Local hostname collection" in docs/architecture.md): no separate
# `show running-config | include hostname` query is needed.
_IOSXR_PROMPT_RE = re.compile(r"RP/\S+/CPU\d+:(?P<hostname>[^#\s]+)#\s*$", re.MULTILINE)
# The password-prompt regex itself is shared SSOT (Step 3.5): see
# terminal.PASSWORD_PROMPT_RE's own docstring -- OpenSSH's client-side
# prompt text is identical regardless of caller, so there is exactly one
# place that recognizes it.
_LOGIN_WAIT_RE = re.compile(f"(?:{terminal.PASSWORD_PROMPT_RE.pattern})|(?:{_IOSXR_PROMPT_RE.pattern})")

# IOS XE (classic-IOS-style) exec prompt, e.g. "PAGENT#" or "PAGENT>" --
# unlike IOS XR's "RP/.../CPU0:hostname#" shape, IOS XE's own prompt *is*
# just the hostname, so the whole line must be exactly that (never matched
# against a mid-table CDP row, which always has other fields after the
# device ID on the same line -- see parse_cdp_neighbors()). A telnet/console
# login may also show a "Username:" prompt before "Password:" (Step 3.6
# Section 13); OpenSSH's own password prompt (terminal.PASSWORD_PROMPT_RE)
# is reused unchanged since it is transport-agnostic text matching, not an
# SSH-specific mechanism.
_IOSXE_PROMPT_RE = re.compile(r"^(?P<hostname>[\w.-]+)[#>]\s*$", re.MULTILINE)
_USERNAME_PROMPT_RE = re.compile(r"[Uu]sername:\s*$", re.MULTILINE)
_IOSXE_LOGIN_WAIT_RE = re.compile(
    f"(?:{_USERNAME_PROMPT_RE.pattern})|(?:{terminal.PASSWORD_PROMPT_RE.pattern})|(?:{_IOSXE_PROMPT_RE.pattern})"
)


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


def _resolve_login_password(device_id: str, device_config: dict, prompt_line: str) -> str:
    """Which password answers the current prompt.

    A thin, Discovery-specific wrapper around the shared
    terminal.resolve_target_password_prompt() (Step 3.5): the actual
    target-vs-jump-host attribution logic lives there once, reused
    identically by managed terminal_open()'s own private authentication,
    so a prompt that cannot be confidently attributed to either hop fails
    closed (DiscoveryError) the exact same way for both callers -- this
    function only supplies Discovery's own wording for that failure (see
    "Bounded ProxyJump limitation" in docs/architecture.md)."""
    outcome = terminal.resolve_target_password_prompt(device_config, prompt_line)
    if outcome.matched_target:
        return outcome.password
    if outcome.reason == "jump-host password prompt":
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
    if terminal.PASSWORD_PROMPT_RE.search(last_line):
        password = _resolve_login_password(device_id, device_config, last_line)
        terminal.send_to_bootstrap(device_id, password, None, True)
        text = terminal.wait_for_bootstrap_pattern(device_id, _IOSXR_PROMPT_RE, LOGIN_TIMEOUT_SECONDS)
    match = _IOSXR_PROMPT_RE.search(_last_nonblank_line(text))
    if not match:
        raise DiscoveryError(f"Device '{device_id}': did not reach an IOS XR prompt after login.")
    return match.group("hostname")


def _login_iosxe(device_id: str, device_config: dict) -> str:
    """IOS XE equivalent of _login(): the same bounded, at-most-one-
    password-send flow, but for classic-IOS-style login instead of IOS
    XR's. Works over either transport the device's access-info specifies
    (ssh or telnet, both already handled uniformly by terminal.
    open_bootstrap_terminal() -- see _build_transport_command()). A telnet
    device structurally cannot have a jump_host_config (lab.py's schema
    validation requires transport 'ssh' for that), so
    resolve_target_password_prompt()'s "no jump_host_config -> unambiguous"
    short-circuit already answers a telnet password prompt correctly with
    no telnet-specific attribution logic needed.

    Also answers at most one optional "Username:" prompt, which only some
    IOS XE login configurations show before "Password:"."""
    terminal.open_bootstrap_terminal(device_id, device_config)
    text = terminal.wait_for_bootstrap_pattern(device_id, _IOSXE_LOGIN_WAIT_RE, LOGIN_TIMEOUT_SECONDS)
    last_line = _last_nonblank_line(text)
    if _USERNAME_PROMPT_RE.search(last_line):
        username = device_config.get("username")
        if not username:
            raise DiscoveryError(
                f"Device '{device_id}': a username prompt appeared but no username is configured in "
                "the active access-info definition."
            )
        terminal.send_to_bootstrap(device_id, str(username), None, True)
        text = terminal.wait_for_bootstrap_pattern(
            device_id, _IOSXE_LOGIN_WAIT_RE, LOGIN_TIMEOUT_SECONDS, baseline_text=text
        )
        last_line = _last_nonblank_line(text)
    if terminal.PASSWORD_PROMPT_RE.search(last_line):
        password = _resolve_login_password(device_id, device_config, last_line)
        terminal.send_to_bootstrap(device_id, password, None, True)
        text = terminal.wait_for_bootstrap_pattern(device_id, _IOSXE_PROMPT_RE, LOGIN_TIMEOUT_SECONDS)
    match = _IOSXE_PROMPT_RE.search(_last_nonblank_line(text))
    if not match:
        raise DiscoveryError(f"Device '{device_id}': did not reach an IOS XE exec prompt after login.")
    return match.group("hostname")


def _extract_command_output(full_text: str, command_text: str, prompt_re: re.Pattern) -> str:
    """Slice out one command's own output from the full pane transcript:
    everything after the line that echoes the command, up to (excluding)
    the trailing prompt line(s). `prompt_re` is the device-type-specific
    "end of output" prompt (IOS XR's or IOS XE's)."""
    lines = full_text.splitlines()
    needle = command_text.strip()
    start = 0
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].rstrip().endswith(needle):
            start = i + 1
            break
    end = len(lines)
    while end > start and (not lines[end - 1].strip() or prompt_re.search(lines[end - 1])):
        end -= 1
    return "\n".join(lines[start:end])


def _run_command(
    device_id: str,
    command_text: str,
    prompt_re: re.Pattern = _IOSXR_PROMPT_RE,
    timeout: float = COMMAND_TIMEOUT_SECONDS,
) -> str:
    """Send one command line and wait for the device's own prompt to
    return. `prompt_re` defaults to IOS XR's prompt (unchanged call sites);
    IOS XE collection passes _IOSXE_PROMPT_RE instead.

    Captures the pane *before* sending so wait_for_bootstrap_pattern() can
    require the pane to have actually changed before accepting a prompt
    match -- otherwise a prompt already sitting in the pane from the
    *previous* command could satisfy the wait immediately, before this
    command produced any output at all (a stale-prompt race)."""
    baseline = terminal.read_bootstrap(device_id)
    terminal.send_to_bootstrap(device_id, command_text, None, True)
    full_text = terminal.wait_for_bootstrap_pattern(device_id, prompt_re, timeout, baseline_text=baseline)
    return _extract_command_output(full_text, command_text, prompt_re)


def _bootstrap_collect(device_id: str, device_config: dict) -> dict:
    """IOS XR collection: log in, disable pagination, and collect the
    read-only commands -- `show version`/`show running-config` (unused
    downstream today, kept for diagnostic parity/future use) plus both
    neighbor-discovery protocols (Step 3.6 Section 3: IOS XR gets LLDP and
    CDP). CDP being unavailable/disabled is not a collection failure (see
    parse_cdp_neighbors()'s own lenient handling) -- only a login, other
    command, or timeout failure is (DiscoveryError). The caller is
    responsible for closing the bootstrap session either way."""
    try:
        hostname = _login(device_id, device_config)
        # Discovery sessions are temporary and closed right after
        # collection, so there is no need to restore terminal length
        # afterwards (see docs/architecture.md).
        _run_command(device_id, "terminal length 0")
        show_version = _run_command(device_id, "show version")
        show_running_config = _run_command(device_id, "show running-config")
        show_lldp_neighbors = _run_command(device_id, "show lldp neighbors")
        show_cdp_neighbors = _run_command(device_id, "show cdp neighbors")
    except terminal.TerminalError as exc:
        raise DiscoveryError(f"Device '{device_id}': {exc}") from exc
    return {
        "hostname": hostname,
        "show_version": show_version,
        "show_running_config": show_running_config,
        "show_lldp_neighbors": show_lldp_neighbors,
        "show_cdp_neighbors": show_cdp_neighbors,
    }


def _bootstrap_collect_iosxe(device_id: str, device_config: dict) -> dict:
    """IOS XE collection: log in and collect `show version` (diagnostic
    parity with the IOS XR path) plus `show cdp neighbors` -- IOS XE has no
    LLDP support in this step (Step 3.6 Section 3), and `show running-
    config` is skipped since it is unused downstream for IOS XR too (kept
    minimal per Section 11's "collect at minimum" framing). Fails closed
    (DiscoveryError) on any login, command, or timeout failure."""
    try:
        hostname = _login_iosxe(device_id, device_config)
        show_version = _run_command(device_id, "show version", _IOSXE_PROMPT_RE)
        show_cdp_neighbors = _run_command(device_id, "show cdp neighbors", _IOSXE_PROMPT_RE)
    except terminal.TerminalError as exc:
        raise DiscoveryError(f"Device '{device_id}': {exc}") from exc
    return {
        "hostname": hostname,
        "show_version": show_version,
        "show_cdp_neighbors": show_cdp_neighbors,
    }


# --------------------------------------------------------------------------
# LLDP + CDP parsing -- normalized observations only, no identity resolution
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class LldpObservation:
    """A single normalized neighbor observation from either protocol
    (`source` distinguishes them: "lldp" or "cdp") -- despite the name
    (kept to avoid an unnecessary rename of an already-public, widely
    tested type), this is the shared observation shape Step 3.6's CDP
    support reuses as-is rather than inventing a second, parallel type;
    see the `NeighborObservation` alias below for new code."""

    local_device_id: str
    local_interface: str
    remote_device_id_raw: str
    remote_port_id: str
    capabilities: tuple[str, ...] = ()
    source: str = "lldp"


# New CDP-facing code should spell it this way; both names are the exact
# same class.
NeighborObservation = LldpObservation

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

# CDP's own "Capability Codes" legend uses different letters/meanings than
# LLDP's (e.g. CDP's "T" is "Trans Bridge", not "telephone"; CDP's repeater
# code is lowercase "r", not "P") -- a separate mapping, not a reuse of
# LLDP's, to avoid mislabeling. The letter set here is a safe superset of
# what real Cisco CDP output uses; an unmapped letter is kept as-is rather
# than dropped (see _normalize_capability_chars()).
_CDP_CAPABILITY_CODES = {
    "R": "router",
    "T": "trans_bridge",
    "B": "source_route_bridge",
    "S": "switch",
    "H": "host",
    "I": "igmp",
    "r": "repeater",
    "P": "phone",
    "D": "remote",
    "C": "cvta",
    "M": "two_port_mac_relay",
}
# Every single-character CDP capability code, used only to distinguish a
# capability token (e.g. "S", "I") from the start of the Platform field
# during row parsing (see parse_cdp_neighbors()) -- not used for the
# mapping itself.
_CDP_CAPABILITY_LETTERS = frozenset(_CDP_CAPABILITY_CODES)

_TOTAL_ENTRIES_RE = re.compile(r"total entries displayed:\s*(\d+)", re.IGNORECASE)


def _normalize_capability_chars(raw: str, codes: dict[str, str]) -> tuple[str, ...]:
    return tuple(codes.get(ch, ch) for ch in raw.strip() if ch.strip())


def _normalize_capabilities(raw: str) -> tuple[str, ...]:
    return _normalize_capability_chars(raw, _CAPABILITY_CODES)


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


_CDP_HEADER_MARKER = "Local Intrfce"


def _cdp_is_capability_token(token: str) -> bool:
    return bool(token) and all(ch in _CDP_CAPABILITY_LETTERS for ch in token)


def parse_cdp_neighbors(raw_text: str, local_device_id: str) -> list[NeighborObservation]:
    """Parse `show cdp neighbors` output into normalized observations.

    Handles both real-world row shapes (Step 3.6 Section 4-5):
      - IOS XR-style, entirely on one line:
        "PAGENT          Gi0/0/0/10       144     R          Cisco 720 Gi0/0"
      - IOS/IOS XE-style, where a long/FQDN Device ID wraps onto its own
        line and the remaining fields follow on the *next* physical line:
        "external-switch.example.com"
        "                 Fas 0/0            159             S I   WS-C2960X Gig 1/0/16"

    Never assumes fixed column byte offsets. Since both Local Interface,
    Platform, and Port ID can each be multiple whitespace-separated tokens
    (e.g. "Fas 0/0", "ASR9K Ser", "Gig 1/0/16"), plain column-count
    splitting (as LLDP's parser uses) is not reliable here; instead, a
    field row is recognized by containing a bare-integer Holdtime token,
    which reliably splits "Device ID + Local Interface" (before it) from
    "Capability + Platform + Port ID" (after it), and only the Port ID
    field's own column start (read once from the header line) is needed to
    unambiguously split Platform from Port ID.

    Deliberately more lenient than parse_lldp_neighbors(): CDP is commonly
    disabled/unsupported on a given device, so a missing/unrecognized
    table header returns an empty list of observations rather than raising
    (Step 3.6 Section 10/37 -- CDP being unavailable must never fail the
    whole device's collection; contrast with LLDP, which is expected to
    always be available and so still fails closed on an unrecognized
    header). A malformed individual row is skipped, never fatal."""
    observations: list[NeighborObservation] = []
    in_table = False
    port_id_col: int | None = None
    pending_device_id: str | None = None

    for raw_line in raw_text.splitlines():
        line = raw_line.strip()
        if not in_table:
            if line.startswith("Device ID") and _CDP_HEADER_MARKER in line:
                idx = raw_line.find("Port ID")
                if idx != -1:
                    port_id_col = idx
                    in_table = True
            continue
        if not line:
            break

        tokens = list(re.finditer(r"\S+", raw_line))
        if not tokens:
            continue
        has_holdtime = any(m.group().isdigit() for m in tokens)
        if not has_holdtime:
            # A real wrapped Device-ID-only line (the row wrapped because
            # the Device ID was too long to fit before the Local Interface
            # column) is always exactly one token -- a real Device ID never
            # contains whitespace. A multi-token line with no Holdtime
            # anywhere is unrecognized/malformed input, not a wrapped
            # Device ID -- skip it rather than risk misattributing the
            # *next* row's fields to it.
            if len(tokens) == 1:
                pending_device_id = line
            continue

        if pending_device_id is not None:
            device_id = pending_device_id
            pending_device_id = None
            field_tokens = tokens
        else:
            device_id = tokens[0].group()
            field_tokens = tokens[1:]

        holdtime_idx = next((i for i, m in enumerate(field_tokens) if m.group().isdigit()), None)
        if holdtime_idx is None or holdtime_idx == 0:
            continue  # malformed row (no interface text before Holdtime) -- skip
        local_intf = " ".join(m.group() for m in field_tokens[:holdtime_idx])

        cap_tokens: list[str] = []
        for m in field_tokens[holdtime_idx + 1 :]:
            if not _cdp_is_capability_token(m.group()):
                break
            cap_tokens.append(m.group())
        capability_raw = " ".join(cap_tokens)

        port_id = raw_line[port_id_col:].strip() if port_id_col is not None else ""
        if not port_id:
            continue  # malformed row (no Port ID text) -- skip

        observations.append(
            NeighborObservation(
                local_device_id=local_device_id,
                local_interface=local_intf,
                remote_device_id_raw=device_id,
                remote_port_id=port_id,
                capabilities=_normalize_capability_chars(capability_raw, _CDP_CAPABILITY_CODES),
                source="cdp",
            )
        )

    if not in_table:
        return []
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
    resolved_observations: list[tuple[NeighborObservation, str]],
) -> tuple[list[ManagedLink], list[LinkConflict]]:
    """Deduplicate reciprocal, multi-protocol observations into physical
    links.

    `resolved_observations` is a list of (observation, resolved_remote_id)
    pairs from either protocol, already filtered to observations whose
    remote resolved uniquely to a managed device (see
    resolve_remote_identity()) -- the protocol that produced each
    observation is irrelevant to this function beyond its own `source`
    field, which callers may use for diagnostics. A physical link is keyed
    by its unordered pair of (device, interface) endpoints, so two parallel
    links between the same router pair on different interfaces stay
    distinct (section 46).

    Two conflict cases are both reported (never silently resolved by
    picking one side), and both fail closed only for the specific local
    interface(s) involved -- an unrelated link elsewhere still reconciles
    normally:
      1. A reciprocal pair that disagrees about the interface mapping
         (unchanged from before Step 3.6).
      2. (Step 3.6 Section 28/29) *One* local interface has more than one
         observation -- whether from different protocols (LLDP says one
         neighbor, CDP says a different one) or the same protocol
         producing incompatible rows -- that do not all agree on the same
         (remote device, remote interface). Corroborating observations
         (same protocol or not, same remote endpoint) are not a conflict;
         they collapse into the same single candidate link."""
    by_local_endpoint: dict[tuple[str, str], list[tuple[NeighborObservation, str]]] = {}
    for obs, remote_id in resolved_observations:
        by_local_endpoint.setdefault((obs.local_device_id, obs.local_interface), []).append((obs, remote_id))

    links: list[ManagedLink] = []
    conflicts: list[LinkConflict] = []
    # Endpoints are consumed by their own dict key (local_dev, local_intf),
    # never by a "canonical pair" derived from a claimed remote port --
    # in a conflict the two sides claim *different* remote ports, so only
    # marking each side's own key reliably prevents processing the same
    # physical endpoint twice.
    seen_endpoints: set[tuple[str, str]] = set()

    resolved_by_endpoint: dict[tuple[str, str], tuple[NeighborObservation, str]] = {}
    for endpoint, obs_list in by_local_endpoint.items():
        first_obs, first_remote_id = obs_list[0]
        agree = all(
            remote_id == first_remote_id and obs.remote_port_id == first_obs.remote_port_id
            for obs, remote_id in obs_list
        )
        if not agree:
            second_obs, _second_remote_id = next(
                (o, r) for o, r in obs_list if r != first_remote_id or o.remote_port_id != first_obs.remote_port_id
            )
            conflicts.append(LinkConflict(endpoint, endpoint, first_obs, second_obs))
            seen_endpoints.add(endpoint)
            continue
        resolved_by_endpoint[endpoint] = (first_obs, first_remote_id)

    for (local_dev, local_intf), (obs, remote_id) in resolved_by_endpoint.items():
        endpoint_a = (local_dev, local_intf)
        if endpoint_a in seen_endpoints:
            continue

        remote_intf = obs.remote_port_id
        endpoint_b = (remote_id, remote_intf)

        reverse = resolved_by_endpoint.get(endpoint_b)
        if reverse is None:
            # One-sided observation -- still a valid managed link.
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
    iosxe_target_count: int = 0
    cdp_observation_count: int = 0
    devices: dict[str, dict] = field(default_factory=dict)
    managed_links: list[ManagedLink] = field(default_factory=list)
    unresolved: list[UnresolvedNeighbor] = field(default_factory=list)
    conflicts: list[LinkConflict] = field(default_factory=list)
    identity_map: dict[str, str] = field(default_factory=dict)


def _select_targets_by_type(access_data: dict, wanted_type: str) -> dict[str, dict]:
    """Discovery targets of one normalized device `type`. Any other type
    (including an unrecognized/missing one) is silently skipped, not an
    error -- only a definition with *zero* supported targets of any kind
    fails (see discover_topology())."""
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
        if normalized != wanted_type:
            continue
        resolved = dict(device_cfg)
        jump_ref = device_cfg.get("jump_host")
        if jump_ref and jump_ref in jump_hosts:
            resolved["jump_host_config"] = dict(jump_hosts[jump_ref])
        targets[device_id] = resolved
    return targets


def _select_iosxr_targets(access_data: dict) -> dict[str, dict]:
    return _select_targets_by_type(access_data, "iosxr")


def _select_iosxe_targets(access_data: dict) -> dict[str, dict]:
    return _select_targets_by_type(access_data, "iosxe")


def discover_topology(lab_root=None) -> DiscoveryResult:
    """Run the full Discovery flow against committed running-config's
    selected access-info and return an in-memory DiscoveryResult.

    Never touches the committed candidate/topology/settings -- turning this
    into a topology candidate is the caller's job (cli/config.py), exactly
    like any other topology edit. Conservative or nothing: if any supported
    target fails login/collection, the whole operation fails
    (DiscoveryError) before any bootstrap session is even considered for
    reconciliation -- there is no partial result.

    Supported targets (Step 3.6 Section 3): IOS XR (LLDP + CDP) and IOS XE
    (CDP only -- no LLDP support in this step). `nxos`/`host` and any
    unrecognized type are silently skipped, not failed; only zero supported
    targets of *either* kind fails."""
    lab_root = lab_root or lab.find_lab_root()
    access_info_name = resolve_default_topology_name(lab_root)
    if not lab.access_info_exists(access_info_name, lab_root):
        raise DiscoveryError(f"Selected access-info '{access_info_name}' does not exist.")
    access_data = lab.load_access_info(access_info_name, lab_root)

    iosxr_targets = _select_iosxr_targets(access_data)
    iosxe_targets = _select_iosxe_targets(access_data)
    if not iosxr_targets and not iosxe_targets:
        raise DiscoveryError(f"No supported IOS XR or IOS XE devices found in access-info '{access_info_name}'.")
    all_targets: dict[str, dict] = {**iosxr_targets, **iosxe_targets}

    # Step 3.3: one device's collection (login + its own read-only commands)
    # still runs strictly sequentially within its own worker -- only
    # *different* devices' collectors run concurrently, bounded by
    # DISCOVERY_MAX_WORKERS. Workers return a value (the per-type
    # _bootstrap_collect*'s dict) and touch only their own device's
    # bootstrap session; nothing here is mutated by more than one worker,
    # and no candidate/topology state is touched until every result has
    # been collected below.
    worker_count = min(len(all_targets), DISCOVERY_MAX_WORKERS)
    futures: dict[str, concurrent.futures.Future] = {}
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = {
                device_id: executor.submit(_bootstrap_collect, device_id, device_cfg)
                for device_id, device_cfg in iosxr_targets.items()
            }
            futures.update(
                {
                    device_id: executor.submit(_bootstrap_collect_iosxe, device_id, device_cfg)
                    for device_id, device_cfg in iosxe_targets.items()
                }
            )
        # The `with` block above only exits once every submitted future has
        # finished (successfully or not) -- so every bootstrap session below
        # is either fully collected or has already failed, never still
        # in flight, exactly like the previous sequential loop's own
        # try/finally guarantee.
    finally:
        for device_id in all_targets:
            terminal.close_bootstrap_terminal(device_id)

    # Deterministic aggregation and error attribution: always in original
    # target order (IOS XR targets, then IOS XE targets -- each preserving
    # its own access-info iteration order), never in whatever order the
    # thread pool happened to finish them -- so which device's failure
    # surfaces first, and the eventual candidate's own device/link
    # ordering, never depends on scheduling. The first target-order failure
    # is raised and stops aggregation immediately, matching the previous
    # sequential loop's own fail-fast behavior exactly (it also never
    # populated `collected` past the first failing device).
    collected: dict[str, dict] = {}
    for device_id in all_targets:
        try:
            collected[device_id] = futures[device_id].result()
        except DiscoveryError:
            raise
        except Exception as exc:
            # A worker crash (not a DiscoveryError) is a bug, not an
            # anticipated device/login/command failure -- still fails
            # Discovery closed, through the same bounded error type, without
            # leaking a raw traceback to the caller.
            raise DiscoveryError(f"Device '{device_id}': unexpected collection failure: {exc}") from exc

    identity_map = {device_id: info["hostname"] for device_id, info in collected.items()}

    resolved: list[tuple[NeighborObservation, str]] = []
    unresolved: list[UnresolvedNeighbor] = []
    observation_count = 0
    cdp_observation_count = 0
    for device_id, info in collected.items():
        observations: list[NeighborObservation] = []
        if device_id in iosxr_targets:
            try:
                lldp_observations = parse_lldp_neighbors(info["show_lldp_neighbors"], device_id)
            except LldpParseError as exc:
                raise DiscoveryError(str(exc)) from exc
            observation_count += len(lldp_observations)
            observations.extend(lldp_observations)
        cdp_observations = parse_cdp_neighbors(info.get("show_cdp_neighbors", ""), device_id)
        cdp_observation_count += len(cdp_observations)
        observations.extend(cdp_observations)

        for obs in observations:
            remote_id = resolve_remote_identity(obs.remote_device_id_raw, identity_map)
            if remote_id is not None and remote_id in all_targets:
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

    devices = {device_id: {"type": "iosxr"} for device_id in iosxr_targets}
    devices.update({device_id: {"type": "iosxe"} for device_id in iosxe_targets})

    return DiscoveryResult(
        access_info_name=access_info_name,
        default_topology_name=access_info_name,
        iosxr_target_count=len(iosxr_targets),
        connected_count=len(collected),
        observation_count=observation_count,
        iosxe_target_count=len(iosxe_targets),
        cdp_observation_count=cdp_observation_count,
        devices=devices,
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
