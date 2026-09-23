"""Step 3 scope: IOS XR + IOS XE + classic IOS topology discovery over LLDP
and CDP, enriched with stable Layer-3 interface context (IPv4 address +
VRF).

    committed active_access_info
        -> private bootstrap connection (terminal.open_bootstrap_terminal)
        -> per-type login + read-only collection (this module's minimal
           command runner): IOS XR gets LLDP + CDP + `show ipv4 interface
           brief`; IOS XE gets LLDP + CDP + `show vrf`/`show ip interface
           brief`; classic IOS gets CDP + `show vrf`/`show ip interface
           brief` (Step 3.7 Section 10 -- no classic-IOS LLDP in this step)
        -> normalized neighbor observations (parse_lldp_neighbors /
           parse_cdp_neighbors), each tagged with its own `source`
        -> identity resolution (resolve_remote_identity, protocol-agnostic)
        -> multi-protocol link reconciliation (reconcile_links): the same
           physical link seen via both protocols (or reciprocally from both
           ends) becomes exactly one link; a local interface where LLDP and
           CDP disagree about the neighbor is reported as a conflict
           instead of silently picking one
        -> per-device Layer-3 interface enrichment (optional, additive --
           see "L3 topology enrichment" below): ipv4_address + vrf only,
           never a prefix length, never operational (up/down) state
        -> DiscoveryResult (in-memory only)

`discover_topology()` is the only entry point cli/main.py's `discover
topology` handler calls; everything else here is an implementation detail.
It never writes to disk and never mutates running-config or any committed
file -- the caller (cli/config.py) is responsible for turning a
DiscoveryResult into a topology *candidate*, exactly like any other
topology edit, and nothing here special-cases commit/clear/root/exit/end.

IOS XE / classic IOS login uses whatever transport the device's
access-info specifies (ssh or telnet). Telnet is unauthenticated-in-transit
and unencrypted -- suitable only for isolated lab environments, never
presented as a secure transport (see README.md/docs/architecture.md).

L3 topology enrichment (Step 3.7) is deliberately additive, never a
Discovery blocker: neighbor discovery (LLDP/CDP) remains the primary,
required mechanism; if a device's optional L3 command(s) fail (transport
timeout or unrecognized output), that one device's L3 enrichment is
skipped (with a warning) while its LLDP/CDP-discovered links are kept.
Only a stable, directly observed IPv4 address + VRF are stored -- never a
prefix length, never inferred subnets, and never a link inferred from
address similarity (links come from LLDP/CDP or an explicit topology edit
only). A management-network address (the same address the selected
access-info uses to *reach* that device) is deliberately excluded from
topology L3 data -- see _build_device_interface_fields().

Explicitly out of scope: NX-OS discovery, multi-hop jump hosts (telnet
devices cannot have a jump_host at all -- see lab.validate_device_jump_
host_references()), SNMP/NETCONF/RESTCONF, automatic topology activation/
commit, IPv4 prefix-length/subnet inference, operational-state persistence,
and a generic discovery/plugin framework."""

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
# re.MULTILINE is required here (Step 3.8 finding): without it, the `$` in
# each combined sub-pattern only anchors to the true end of the whole
# captured string, not the end of each line -- meaning a match on a line
# that is *not* the very last line of the captured pane text (e.g. once
# something else has already been appended after it) silently fails. Both
# constituent patterns already carry their own re.MULTILINE individually,
# but that flag is lost when their `.pattern` text is combined into a new
# re.compile() call -- it must be re-applied on the combined pattern too.
_LOGIN_WAIT_RE = re.compile(
    f"(?:{terminal.PASSWORD_PROMPT_RE.pattern})|(?:{_IOSXR_PROMPT_RE.pattern})", re.MULTILINE
)

# Classic-IOS-style exec prompt, e.g. "PAGENT#" or "PAGENT>" -- shared by
# IOS XE *and* classic IOS (Step 3.7: the same login/prompt shape applies
# to both, so this is deliberately not named "_IOSXE_..." even though it
# was introduced for IOS XE in Step 3.6). Unlike IOS XR's
# "RP/.../CPU0:hostname#" shape, this prompt *is* just the hostname, so the
# whole line must be exactly that (never matched against a mid-table CDP
# row, which always has other fields after the device ID on the same line
# -- see parse_cdp_neighbors()). A telnet/console login may also show a
# "Username:" prompt before "Password:" (Step 3.6 Section 13); OpenSSH's
# own password prompt (terminal.PASSWORD_PROMPT_RE) is reused unchanged
# since it is transport-agnostic text matching, not an SSH-specific
# mechanism.
#
# Both now live in terminal.py (Step 3.8): managed terminal_open()'s own
# Telnet authentication needed the exact same two patterns, so they moved
# to the one shared lower-level module rather than being duplicated --
# these names are kept as aliases so nothing else in this file (or its
# tests) needs to change.
_IOS_STYLE_PROMPT_RE = terminal.IOS_STYLE_PROMPT_RE
_USERNAME_PROMPT_RE = terminal.USERNAME_PROMPT_RE
# re.MULTILINE re-applied on the combined pattern -- see _LOGIN_WAIT_RE's
# own comment above for why.
_IOS_STYLE_LOGIN_WAIT_RE = re.compile(
    f"(?:{_USERNAME_PROMPT_RE.pattern})|(?:{terminal.PASSWORD_PROMPT_RE.pattern})|(?:{_IOS_STYLE_PROMPT_RE.pattern})",
    re.MULTILINE,
)

# Real, documented Cisco text for "the command ran, but LLDP is
# administratively disabled" (Step 3.7 Section 13/56) -- this is NOT a
# transport/command failure (the prompt returns normally), and it is NOT a
# parser failure either; it must mean "zero LLDP observations", the same
# way parse_cdp_neighbors() already treats CDP-unavailable leniently.
_LLDP_UNAVAILABLE_RE = re.compile(r"LLDP is not enabled", re.IGNORECASE)


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
        terminal.send_secret_to_bootstrap(device_id, password)
        text = terminal.wait_for_bootstrap_pattern(device_id, _IOSXR_PROMPT_RE, LOGIN_TIMEOUT_SECONDS)
    match = _IOSXR_PROMPT_RE.search(_last_nonblank_line(text))
    if not match:
        raise DiscoveryError(f"Device '{device_id}': did not reach an IOS XR prompt after login.")
    return match.group("hostname")


def _login_ios_style(device_id: str, device_config: dict) -> str:
    """Shared classic-IOS-style login for both IOS XE *and* classic IOS
    (Step 3.7 Section 11 -- renamed from Step 3.6's IOS-XE-only
    `_login_iosxe()`, since the same login sequence genuinely applies to
    both and keeping the old name would now be misleading, not because the
    logic itself needed to change). The same bounded, at-most-one-
    password-send flow as _login(), but for classic-IOS-style login
    instead of IOS XR's. Works over either transport the device's
    access-info specifies (ssh or telnet, both already handled uniformly
    by terminal.open_bootstrap_terminal() -- see
    _build_transport_command()). A telnet device structurally cannot have
    a jump_host_config (lab.py's schema validation requires transport
    'ssh' for that), so resolve_target_password_prompt()'s "no
    jump_host_config -> unambiguous" short-circuit already answers a
    telnet password prompt correctly with no telnet-specific attribution
    logic needed.

    Also answers at most one optional "Username:" prompt, which only some
    IOS/IOS XE login configurations show before "Password:"."""
    terminal.open_bootstrap_terminal(device_id, device_config)
    text = terminal.wait_for_bootstrap_pattern(device_id, _IOS_STYLE_LOGIN_WAIT_RE, LOGIN_TIMEOUT_SECONDS)
    last_line = _last_nonblank_line(text)
    if _USERNAME_PROMPT_RE.search(last_line):
        username = device_config.get("username")
        if not username:
            raise DiscoveryError(
                f"Device '{device_id}': a username prompt appeared but no username is configured in "
                "the active access-info definition."
            )
        terminal.send_secret_to_bootstrap(device_id, str(username))
        text = terminal.wait_for_bootstrap_pattern(
            device_id, _IOS_STYLE_LOGIN_WAIT_RE, LOGIN_TIMEOUT_SECONDS, baseline_text=text
        )
        last_line = _last_nonblank_line(text)
    if terminal.PASSWORD_PROMPT_RE.search(last_line):
        password = _resolve_login_password(device_id, device_config, last_line)
        terminal.send_secret_to_bootstrap(device_id, password)
        text = terminal.wait_for_bootstrap_pattern(device_id, _IOS_STYLE_PROMPT_RE, LOGIN_TIMEOUT_SECONDS)
    match = _IOS_STYLE_PROMPT_RE.search(_last_nonblank_line(text))
    if not match:
        raise DiscoveryError(f"Device '{device_id}': did not reach an exec prompt after login.")
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


def _run_command_tolerant(device_id: str, command_text: str, prompt_re: re.Pattern) -> str:
    """Like _run_command(), but for the *optional* L3 enrichment commands
    only (Step 3.7 Section 38): a transport-level failure (the prompt never
    returns -- e.g. an unsupported command that hangs rather than
    returning an error line, or a genuinely broken session) must not fail
    the whole device's collection just because L3 enrichment is best-
    effort. Returns "" on that failure instead of raising -- an empty
    string never looks like a recognized L3 table header, so the caller's
    own L3 parser will treat it as "L3 unavailable/unparseable for this
    device" (a warning, not a DiscoveryError) exactly like any other
    unrecognized L3 output. This is deliberately NOT used for LLDP/CDP or
    any of the existing required commands -- those keep failing the whole
    device closed, unchanged from Step 3.6."""
    try:
        return _run_command(device_id, command_text, prompt_re)
    except terminal.TerminalError:
        return ""


def _disable_terminal_paging(device_id: str, prompt_re: re.Pattern) -> None:
    """Send `terminal length 0` immediately after successful login and
    confirm the device prompt has returned before any Discovery show
    command is sent (Step 3.7a).

    This is the fix for a real observed failure: a C9200L (IOS XE) ran
    `show version` before pagination was disabled, its output stopped at
    the device's own `--More--` pager prompt (which never printed the
    expected exec prompt back), and the whole device's collection timed
    out and failed. Disabling the pager up front -- rather than teaching
    Discovery to detect and answer `--More--` -- keeps this a single,
    narrow, one-time step reusing the exact same send/wait-for-prompt
    primitive (`_run_command()`) every other command already uses, with
    no separate pager state machine.

    `terminal length 0` is sent exactly once per session (each collector
    calls this exactly once, right after login) and is a session-level
    EXEC setting only -- never persisted, never entered via configuration
    mode. Fails closed: if the prompt does not return, this raises
    (converted to DiscoveryError by the caller, exactly like any other
    login/command failure) with a message that identifies *this* phase
    specifically, and no Discovery show command is ever attempted while
    the terminal's page-length state is unknown."""
    try:
        _run_command(device_id, "terminal length 0", prompt_re)
    except terminal.TerminalError as exc:
        raise terminal.TerminalError(f"Timed out disabling terminal paging: {exc}") from exc


def _bootstrap_collect(device_id: str, device_config: dict) -> dict:
    """IOS XR collection: log in, disable pagination, and collect the
    read-only commands -- `show version` (identity/diagnostic parity),
    both neighbor-discovery protocols (IOS XR gets LLDP and CDP), and
    `show ipv4 interface brief` (L3 enrichment, collected tolerantly --
    see _run_command_tolerant()). `show running-config` is deliberately
    not collected: nothing in the Discovery result, parser, or
    reconciliation logic ever read it, so collecting the device's
    complete configuration -- which can be large and carries far more
    device-specific detail than Discovery needs -- would only persist
    unnecessary information into the terminal log for no benefit. CDP/L3
    being unavailable is not a collection failure -- only a login,
    LLDP/CDP command, or timeout failure is (DiscoveryError). The caller
    is responsible for closing the bootstrap session either way."""
    try:
        hostname = _login(device_id, device_config)
        # Discovery sessions are temporary and closed right after
        # collection, so there is no need to restore terminal length
        # afterwards (see docs/architecture.md).
        _disable_terminal_paging(device_id, _IOSXR_PROMPT_RE)
        show_version = _run_command(device_id, "show version")
        show_lldp_neighbors = _run_command(device_id, "show lldp neighbors")
        show_cdp_neighbors = _run_command(device_id, "show cdp neighbors")
        show_ipv4_interface_brief = _run_command_tolerant(device_id, "show ipv4 interface brief", _IOSXR_PROMPT_RE)
    except terminal.TerminalError as exc:
        raise DiscoveryError(f"Device '{device_id}': {exc}") from exc
    return {
        "hostname": hostname,
        "show_version": show_version,
        "show_lldp_neighbors": show_lldp_neighbors,
        "show_cdp_neighbors": show_cdp_neighbors,
        "show_ipv4_interface_brief": show_ipv4_interface_brief,
    }


def _bootstrap_collect_iosxe(device_id: str, device_config: dict) -> dict:
    """IOS XE collection: log in, disable pagination (Step 3.7a -- see
    _disable_terminal_paging()'s own docstring for the real C9200L
    failure this fixes; Step 3.6/3.7 never did this for IOS XE at all),
    and collect `show version` (diagnostic parity with the IOS XR path),
    both neighbor-discovery protocols (Step 3.7 Section 13: IOS XE now
    also gets LLDP, in addition to CDP -- `% LLDP is not enabled` is
    handled by the caller, not here, exactly like CDP-unavailable already
    was), and the L3 enrichment commands `show vrf` + `show ip interface
    brief` (collected tolerantly -- see _run_command_tolerant()). `show
    running-config` is skipped since it is unused downstream for IOS XR
    too (kept minimal per Section 12's "collect at minimum" framing).
    Fails closed (DiscoveryError) on any login, paging, LLDP/CDP command,
    or timeout failure."""
    try:
        hostname = _login_ios_style(device_id, device_config)
        _disable_terminal_paging(device_id, _IOS_STYLE_PROMPT_RE)
        show_version = _run_command(device_id, "show version", _IOS_STYLE_PROMPT_RE)
        show_lldp_neighbors = _run_command(device_id, "show lldp neighbors", _IOS_STYLE_PROMPT_RE)
        show_cdp_neighbors = _run_command(device_id, "show cdp neighbors", _IOS_STYLE_PROMPT_RE)
        show_vrf = _run_command_tolerant(device_id, "show vrf", _IOS_STYLE_PROMPT_RE)
        show_ip_interface_brief = _run_command_tolerant(device_id, "show ip interface brief", _IOS_STYLE_PROMPT_RE)
    except terminal.TerminalError as exc:
        raise DiscoveryError(f"Device '{device_id}': {exc}") from exc
    return {
        "hostname": hostname,
        "show_version": show_version,
        "show_lldp_neighbors": show_lldp_neighbors,
        "show_cdp_neighbors": show_cdp_neighbors,
        "show_vrf": show_vrf,
        "show_ip_interface_brief": show_ip_interface_brief,
    }


def _bootstrap_collect_ios(device_id: str, device_config: dict) -> dict:
    """Classic IOS collection (Step 3.7 Section 12, deliberately minimal --
    preserving the same "collect at minimum" principle Step 3.6 used for
    IOS XE): log in, disable pagination (Step 3.7a), `show version`, `show
    cdp neighbors` (classic IOS has no LLDP support in this step --
    Section 10), and the L3 enrichment commands `show vrf` + `show ip
    interface brief` (tolerant). No `show running-config` (unused
    downstream). Fails closed (DiscoveryError) on any login, paging, CDP
    command, or timeout failure."""
    try:
        hostname = _login_ios_style(device_id, device_config)
        _disable_terminal_paging(device_id, _IOS_STYLE_PROMPT_RE)
        show_version = _run_command(device_id, "show version", _IOS_STYLE_PROMPT_RE)
        show_cdp_neighbors = _run_command(device_id, "show cdp neighbors", _IOS_STYLE_PROMPT_RE)
        show_vrf = _run_command_tolerant(device_id, "show vrf", _IOS_STYLE_PROMPT_RE)
        show_ip_interface_brief = _run_command_tolerant(device_id, "show ip interface brief", _IOS_STYLE_PROMPT_RE)
    except terminal.TerminalError as exc:
        raise DiscoveryError(f"Device '{device_id}': {exc}") from exc
    return {
        "hostname": hostname,
        "show_version": show_version,
        "show_cdp_neighbors": show_cdp_neighbors,
        "show_vrf": show_vrf,
        "show_ip_interface_brief": show_ip_interface_brief,
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
# L3 interface enrichment (Step 3.7): ipv4_address + vrf only, per
# interface -- additive topology context, never a Discovery blocker, never
# a link/connectivity source. See the module docstring's "L3 topology
# enrichment" paragraph for the overall design.
# --------------------------------------------------------------------------


class L3ParseError(Exception):
    """Raised when an L3 enrichment command's output cannot be recognized
    at all (its own table header is missing) -- the fail-closed signal
    that keeps a failed/unsupported command from being silently treated as
    "zero interfaces". Caught by discover_topology() and turned into a
    per-device skip + warning (Section 38/39), never a whole-Discovery
    failure -- unlike LldpParseError, which still fails the whole
    operation."""


# A conservative, deliberately small set of known Cisco interface-name
# abbreviations, each mapped to its one canonical full name (Step 3.7
# Section 32) -- used only to let `show vrf`'s (possibly abbreviated)
# Interfaces column match `show ip interface brief`'s (full-name) Interface
# column for the same physical interface. An unrecognized prefix is left
# completely unchanged (never guessed) -- this is intentionally not a
# general-purpose interface-alias framework, just the smallest lookup
# needed to reconcile these two specific commands' output.
_INTERFACE_TYPE_ALIASES: dict[str, str] = {
    "gi": "GigabitEthernet",
    "gig": "GigabitEthernet",
    "gigabitethernet": "GigabitEthernet",
    "fa": "FastEthernet",
    "fas": "FastEthernet",
    "fastethernet": "FastEthernet",
    "te": "TenGigabitEthernet",
    "tengigabitethernet": "TenGigabitEthernet",
    "fo": "FortyGigabitEthernet",
    "fortygigabitethernet": "FortyGigabitEthernet",
    "hu": "HundredGigE",
    "hundredgige": "HundredGigE",
    "tw": "TwoGigabitEthernet",
    "twogigabitethernet": "TwoGigabitEthernet",
    "et": "Ethernet",
    "eth": "Ethernet",
    "ethernet": "Ethernet",
    "vl": "Vlan",
    "vlan": "Vlan",
    "lo": "Loopback",
    "loopback": "Loopback",
    "po": "Port-channel",
    "port-channel": "Port-channel",
    "se": "Serial",
    "serial": "Serial",
    "bv": "BVI",
    "bvi": "BVI",
    "tu": "Tunnel",
    "tunnel": "Tunnel",
}
_INTERFACE_PREFIX_RE = re.compile(r"^([A-Za-z-]+)(.*)$")

_DEFAULT_VRF = "default"


def _canonicalize_interface_name(raw: str) -> str:
    """Expand a KNOWN interface-type abbreviation (e.g. "Gi0/0",
    "Fas 0/0") to its one canonical full name ("GigabitEthernet0/0",
    "FastEthernet0/0"), preserving the slot/port/sub-interface suffix
    (including a dotted sub-interface, e.g. ".2000") unchanged. An
    embedded space (as CDP sometimes renders, e.g. "Fas 0/0") is removed
    only as part of a *recognized* rewrite. An unrecognized prefix is
    returned completely unchanged (including any embedded space) -- never
    over-normalized."""
    cleaned = raw.strip().replace(" ", "")
    match = _INTERFACE_PREFIX_RE.match(cleaned)
    if not match:
        return raw.strip()
    prefix, rest = match.group(1), match.group(2)
    canonical = _INTERFACE_TYPE_ALIASES.get(prefix.lower())
    if canonical is None:
        return raw.strip()
    return f"{canonical}{rest}"


def parse_ipv4_interface_brief(raw_text: str) -> dict[str, tuple[str | None, str]]:
    """Parse IOS XR's `show ipv4 interface brief` into
    {interface: (ipv4_address_or_None, vrf_name)}. Only Interface/
    IP-Address/Vrf-Name are read; Status/Protocol are ignored entirely
    (Step 3.7 Section 29) regardless of how many words they take (e.g.
    "Shutdown" vs. a multi-word status) -- Vrf-Name is always the *last*
    whitespace token and IP-Address is always the second, which is robust
    to that variation without depending on a fixed token count.

    "unassigned" becomes None (Section 35) -- never the literal string,
    never a fabricated address. Raises L3ParseError only when the
    "Interface ... IP-Address ... Vrf-Name" header itself is never found
    (e.g. an unsupported/mistyped command) -- a malformed individual row is
    skipped, never fatal, and a header-found-but-zero-rows response
    returns an empty dict successfully."""
    interfaces: dict[str, tuple[str | None, str]] = {}
    in_table = False
    for raw_line in raw_text.splitlines():
        line = raw_line.strip()
        if not in_table:
            if "Interface" in line and "IP-Address" in line and "Vrf-Name" in line:
                in_table = True
            continue
        if not line:
            break
        tokens = line.split()
        if len(tokens) < 4:
            continue  # malformed row -- skip
        interface_name, ip_address = tokens[0], tokens[1]
        vrf_name = tokens[-1]
        interfaces[interface_name] = (None if ip_address.lower() == "unassigned" else ip_address, vrf_name)
    if not in_table:
        raise L3ParseError(
            "'show ipv4 interface brief' output was not recognized as valid IOS XR structure "
            "(no 'Interface ... IP-Address ... Vrf-Name' table header found)."
        )
    return interfaces


def parse_ip_interface_brief(raw_text: str) -> dict[str, str | None]:
    """Parse classic IOS / IOS XE's `show ip interface brief` into
    {canonical_interface_name: ipv4_address_or_None}. Only Interface/
    IP-Address are read (the first two whitespace tokens); OK?/Method/
    Status/Protocol are ignored entirely regardless of their own word
    count (Status can legitimately be the two-word "administratively
    down", which would otherwise make a fixed-token-count split brittle --
    never needing to parse it at all sidesteps that entirely).

    "unassigned" becomes None (Section 35). Raises L3ParseError only when
    the "Interface ... IP-Address" header itself is never found."""
    interfaces: dict[str, str | None] = {}
    in_table = False
    for raw_line in raw_text.splitlines():
        line = raw_line.strip()
        if not in_table:
            if "Interface" in line and "IP-Address" in line:
                in_table = True
            continue
        if not line:
            break
        tokens = line.split()
        if len(tokens) < 2:
            continue  # malformed row -- skip
        interface_name = _canonicalize_interface_name(tokens[0])
        ip_address = tokens[1]
        interfaces[interface_name] = None if ip_address.lower() == "unassigned" else ip_address
    if not in_table:
        raise L3ParseError(
            "'show ip interface brief' output was not recognized as valid structure "
            "(no 'Interface ... IP-Address' table header found)."
        )
    return interfaces


def parse_show_vrf(raw_text: str) -> dict[str, str]:
    """Parse classic IOS / IOS XE's `show vrf` into
    {canonical_interface_name: vrf_name} -- only non-default VRF
    membership; an interface never listed here is assumed `default` by the
    caller (Section 32/36), never guessed here.

    The Name/Default-RD/Protocols/Interfaces columns are not fixed-width
    (e.g. "<not set>" is itself two whitespace tokens), so each row is read
    as: VRF name = the *first* token, interface = the *last* token,
    tolerating any number of tokens in between. Some IOS versions wrap a
    VRF's additional interfaces onto their own continuation line with the
    VRF-name column blank -- recognized here as a line containing *only*
    an interface token, associated with the immediately preceding VRF
    name.

    Raises L3ParseError only when the "Name ... Interfaces" header itself
    is never found; a header-found-but-zero-non-default-VRF response
    returns an empty dict successfully (this is the common case: most
    interfaces belong to the default VRF, which never appears here)."""
    vrf_by_interface: dict[str, str] = {}
    in_table = False
    pending_vrf_name: str | None = None
    for raw_line in raw_text.splitlines():
        line = raw_line.strip()
        if not in_table:
            if "Name" in line and "Interfaces" in line:
                in_table = True
            continue
        if not line:
            break
        tokens = line.split()
        if len(tokens) == 1:
            if pending_vrf_name is not None:
                vrf_by_interface[_canonicalize_interface_name(tokens[0])] = pending_vrf_name
            continue
        vrf_name = tokens[0]
        interface_name = tokens[-1]
        vrf_by_interface[_canonicalize_interface_name(interface_name)] = vrf_name
        pending_vrf_name = vrf_name
    if not in_table:
        raise L3ParseError(
            "'show vrf' output was not recognized as valid structure (no 'Name ... Interfaces' "
            "table header found)."
        )
    return vrf_by_interface


def _combine_ios_style_l3(
    ip_brief: dict[str, str | None], vrf_by_interface: dict[str, str]
) -> dict[str, tuple[str | None, str]]:
    """Combine classic IOS/IOS XE's two required L3 sources (Section 31):
    every interface `show ip interface brief` reports, paired with its VRF
    from `show vrf` if listed there, else the literal `default` (Section
    36) -- never guessed as default when `show vrf` itself failed to
    parse (the caller only reaches this once *both* sources parsed
    successfully; see discover_topology())."""
    return {name: (ipv4, vrf_by_interface.get(name, _DEFAULT_VRF)) for name, ipv4 in ip_brief.items()}


def _build_device_interface_fields(
    raw_l3: dict[str, tuple[str | None, str]], management_address: str | None
) -> dict[str, dict | None]:
    """Turn one device's raw (interface -> (ipv4_or_None, vrf)) observation
    into the topology candidate's own {interface: {ipv4_address, vrf} |
    None} shape (Section 40/41): a real, non-management-address
    observation becomes a dict to set; an explicitly-observed `unassigned`
    interface, or one whose address matches the access-info connection
    address for this same device (Section 27 -- never persist the
    management address into topology L3 data), becomes an explicit `None`
    removal signal so build_topology_devices_and_links() can safely drop
    any stale prior value for that same interface without erasing
    anything else."""
    fields: dict[str, dict | None] = {}
    for raw_name, (ipv4_address, vrf) in raw_l3.items():
        canonical = _canonicalize_interface_name(raw_name)
        if ipv4_address is None or ipv4_address == management_address:
            fields[canonical] = None
        else:
            fields[canonical] = {"ipv4_address": ipv4_address, "vrf": vrf}
    return fields


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
    ios_target_count: int = 0
    l3_enriched_device_count: int = 0
    l3_interface_count: int = 0
    l3_warnings: list[str] = field(default_factory=list)
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


def _select_ios_targets(access_data: dict) -> dict[str, dict]:
    return _select_targets_by_type(access_data, "ios")


def discover_topology(lab_root=None) -> DiscoveryResult:
    """Run the full Discovery flow against committed running-config's
    selected access-info and return an in-memory DiscoveryResult.

    Never touches the committed candidate/topology/settings -- turning this
    into a topology candidate is the caller's job (cli/config.py), exactly
    like any other topology edit. Conservative or nothing: if any supported
    target fails login/collection, the whole operation fails
    (DiscoveryError) before any bootstrap session is even considered for
    reconciliation -- there is no partial result.

    Supported targets (Step 3.7 Section 3/10): IOS XR (LLDP + CDP), IOS XE
    (LLDP + CDP), and classic IOS (CDP only). `nxos`/`host` and any
    unrecognized type are silently skipped, not failed; only zero supported
    targets of *any* kind fails. L3 enrichment (IPv4 + VRF context) is
    collected per device on top of neighbor discovery, additively -- see
    the module docstring."""
    lab_root = lab_root or lab.find_lab_root()
    access_info_name = resolve_default_topology_name(lab_root)
    if not lab.access_info_exists(access_info_name, lab_root):
        raise DiscoveryError(f"Selected access-info '{access_info_name}' does not exist.")
    access_data = lab.load_access_info(access_info_name, lab_root)

    iosxr_targets = _select_iosxr_targets(access_data)
    iosxe_targets = _select_iosxe_targets(access_data)
    ios_targets = _select_ios_targets(access_data)
    if not iosxr_targets and not iosxe_targets and not ios_targets:
        raise DiscoveryError(
            f"No supported IOS XR, IOS XE, or IOS devices found in access-info '{access_info_name}'."
        )
    all_targets: dict[str, dict] = {**iosxr_targets, **iosxe_targets, **ios_targets}

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
            futures.update(
                {
                    device_id: executor.submit(_bootstrap_collect_ios, device_id, device_cfg)
                    for device_id, device_cfg in ios_targets.items()
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
        elif device_id in iosxe_targets:
            # Step 3.7 Section 13/15: "% LLDP is not enabled" (or any other
            # unrecognized response) means zero LLDP observations, not a
            # device/parser failure -- IOS XE's LLDP support is optional/
            # commonly disabled, unlike IOS XR's. Bypassing the strict
            # parser entirely for this known signal keeps
            # parse_lldp_neighbors() itself, and IOS XR's own still-strict
            # behavior, completely unchanged.
            lldp_text = info.get("show_lldp_neighbors", "")
            if not _LLDP_UNAVAILABLE_RE.search(lldp_text):
                try:
                    lldp_observations = parse_lldp_neighbors(lldp_text, device_id)
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
    devices.update({device_id: {"type": "ios"} for device_id in ios_targets})

    # L3 enrichment (Step 3.7): additive, per-device, never a Discovery
    # blocker -- an L3ParseError here only skips *this device's*
    # enrichment (with a warning), never the whole operation, and never
    # touches its already-collected LLDP/CDP links above.
    l3_warnings: list[str] = []
    l3_enriched_device_count = 0
    l3_interface_count = 0
    for device_id, info in collected.items():
        management_address = all_targets[device_id].get("address")
        try:
            if device_id in iosxr_targets:
                raw_l3 = parse_ipv4_interface_brief(info.get("show_ipv4_interface_brief", ""))
            else:
                ip_brief = parse_ip_interface_brief(info.get("show_ip_interface_brief", ""))
                vrf_by_interface = parse_show_vrf(info.get("show_vrf", ""))
                raw_l3 = _combine_ios_style_l3(ip_brief, vrf_by_interface)
        except L3ParseError as exc:
            l3_warnings.append(f"Device '{device_id}': L3 enrichment skipped: {exc}")
            continue
        interface_fields = _build_device_interface_fields(raw_l3, management_address)
        devices[device_id]["interfaces"] = interface_fields
        l3_enriched_device_count += 1
        l3_interface_count += sum(1 for value in interface_fields.values() if value is not None)

    return DiscoveryResult(
        access_info_name=access_info_name,
        default_topology_name=access_info_name,
        iosxr_target_count=len(iosxr_targets),
        connected_count=len(collected),
        observation_count=observation_count,
        iosxe_target_count=len(iosxe_targets),
        cdp_observation_count=cdp_observation_count,
        ios_target_count=len(ios_targets),
        l3_enriched_device_count=l3_enriched_device_count,
        l3_interface_count=l3_interface_count,
        l3_warnings=l3_warnings,
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
    writing the result into `session.definition_candidate`.

    `fields["interfaces"]` (Step 3.7 L3 enrichment), when present, is
    merged *per interface*, never with a blanket top-level dict.update()
    like every other field: a plain update() would silently replace the
    entire existing interfaces mapping, discarding L3 data for any
    interface not re-observed this run (Section 40 -- absence/failure must
    never erase previous data). Each interface's own value is either a
    dict to set/overwrite, or `None` -- an explicit removal signal (Section
    41, from an interface explicitly re-observed as `unassigned` or
    excluded as a management address) that safely drops just that one
    interface's stale entry. A device with no L3 result at all this run
    (enrichment skipped/failed) simply has no "interfaces" key in
    `fields`, so its existing interfaces are left completely untouched."""
    devices = dict(existing_candidate.get("devices") or {})
    for device_id, fields in result.devices.items():
        merged = dict(devices.get(device_id) or {})
        new_interfaces = fields.get("interfaces")
        other_fields = {key: value for key, value in fields.items() if key != "interfaces"}
        merged.update(other_fields)
        if new_interfaces is not None:
            merged_interfaces = dict(merged.get("interfaces") or {})
            for interface_name, interface_value in new_interfaces.items():
                if interface_value is None:
                    merged_interfaces.pop(interface_name, None)
                else:
                    merged_interfaces[interface_name] = interface_value
            merged["interfaces"] = merged_interfaces
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
