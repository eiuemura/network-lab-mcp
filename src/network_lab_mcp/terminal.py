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
import shlex
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path

TMUX_SOCKET_NAME = "network-lab-mcp"

PRODUCTION_PREFIX = "network-lab-device-"
VALIDATION_PREFIX = "network-lab-validation-"
DISCOVERY_PREFIX = "network-lab-discovery-"
BOOTSTRAP_SESSION = "network-lab-bootstrap-initializer"

HISTORY_LIMIT = 20000
DEFAULT_READ_LINES = 100

# Repo checkout root, computed the same way lab.find_lab_root() computes its
# own repo-root-relative-to-this-file's-package-directory path -- kept
# independent (no import of network_lab_mcp.lab) so this module never
# depends on lab/ existing or being valid.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
LOGS_ROOT = _REPO_ROOT / "logs" / "terminal"

_SESSION_START_FORMAT = "%Y%m%dT%H%M%S"
# Group 1: the session-start timestamp prefix. Group 2 (optional): a
# same-second collision suffix (see _unique_log_path()).
_LOG_FILENAME_RE = re.compile(r"^(\d{8}T\d{6})(?:_(\d+))?\.log$")

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


def derive_discovery_session_name(device_name: str) -> str:
    """Derive the private, temporary Discovery bootstrap tmux session name.

    Structurally separate from both the production and validation
    namespaces (see module docstring) -- never exposed through any public
    MCP tool, and never confused with a topology device's production
    session even when discovering a device outside the active topology."""
    _validate_identifier(device_name, "Device name")
    return f"{DISCOVERY_PREFIX}{device_name}"


def is_production_session(session_name: str) -> bool:
    return session_name.startswith(PRODUCTION_PREFIX)


def is_validation_session(session_name: str) -> bool:
    return session_name.startswith(VALIDATION_PREFIX)


def is_discovery_session(session_name: str) -> bool:
    return session_name.startswith(DISCOVERY_PREFIX)


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


def _create_logged_session(session_name: str, device_name: str, command: list[str]) -> None:
    """Like _create_session(), but attaches persistent pipe-pane logging
    (see _start_session_logging()) *before* `command` (the ssh/telnet
    transport) starts running in the pane, instead of after -- so the
    earliest output (login banner, host-key message, the very first
    password prompt) is never lost to the log. The pane starts with a
    neutral shell (no command), which is what gives logging a moment to
    attach before `command` is typed into it and executed."""
    _run(["new-session", "-d", "-s", session_name, "-x", "220", "-y", "50"])
    _start_session_logging(session_name, device_name)
    _send_literal_text(session_name, shlex.join(command))
    _send_enter(session_name)


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


def _ensure_managed_session(session_name: str, command: list[str], *, log_device_name: str | None = None) -> bool:
    """Ensure a managed session exists, creating it with `command` if needed.

    Returns True if an existing session was reused, False if a new one was
    created. Existing sessions are never destroyed to reapply configuration.

    `log_device_name`, when given, creates the session via
    _create_logged_session() instead of _create_session() -- logging
    attached before `command` starts, rather than after. Reuse never
    touches logging either way (a reused session is already logging from
    when it was first created)."""
    bootstrap_created = _ensure_tmux_environment()
    reused = _session_exists(session_name)
    if not reused:
        if log_device_name is not None:
            _create_logged_session(session_name, log_device_name, command)
        else:
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


def _wait_for_pattern(
    session_name: str,
    pattern: "re.Pattern[str]",
    timeout: float,
    poll_interval: float = 0.3,
    baseline_text: str | None = None,
) -> str:
    """Poll pane content until `pattern` matches the tail of the captured
    text, or raise TerminalError on timeout. Returns the full captured pane
    text at the moment of the match. Used only by the private Discovery
    bootstrap path (see discovery.py) -- terminal_read()/terminal_send()
    remain a simple, unattended capture/send with no waiting loop.

    `baseline_text`, when given, is the pane content captured *before* the
    command that's now being waited on was sent. A match is only accepted
    once the captured text has actually changed from that baseline --
    otherwise a prompt already sitting in the pane from a *previous*
    command could satisfy `pattern` immediately, before the newly sent
    command has produced any output at all (a stale-prompt race). Omit it
    (the default) only when there is genuinely nothing prior to be stale
    relative to, e.g. a session that was just freshly created."""
    deadline = time.monotonic() + timeout
    last_text = baseline_text or ""
    while time.monotonic() < deadline:
        last_text = _capture_pane(session_name, HISTORY_LIMIT)
        if baseline_text is None or last_text != baseline_text:
            tail = "\n".join(last_text.splitlines()[-5:])
            if pattern.search(tail):
                return last_text
        time.sleep(poll_interval)
    raise TerminalError(
        f"Timed out after {timeout:.0f}s waiting for expected output on session '{session_name}'."
    )


# --------------------------------------------------------------------------
# Persistent terminal transcript logging (logs/terminal/<device-id>/*.log)
#
# tmux's own pipe-pane mechanism is the single source of truth for what
# gets logged -- this module never re-renders or duplicates pane content
# into a second log path. Logging is started once, right after a session
# is newly created (never on reuse, since the pipe is already attached to
# that pane for its whole lifetime); tmux pane state remains the runtime
# session SSOT and terminal_read() is unchanged -- these logs are a
# separate, persistent, write-only historical record.
# --------------------------------------------------------------------------


def _device_log_dir(device_name: str) -> Path:
    _validate_identifier(device_name, "Device name")
    return LOGS_ROOT / device_name


def _session_start_timestamp() -> str:
    return datetime.now().strftime(_SESSION_START_FORMAT)


def _unique_log_path(log_dir: Path, timestamp: str) -> Path:
    """The normal filename is `<timestamp>.log`. If two sessions for the
    same device start within the same second, that name would already
    exist -- append the smallest `_2`, `_3`, ... suffix that doesn't,
    rather than silently overwriting/appending to the earlier session's
    log."""
    candidate = log_dir / f"{timestamp}.log"
    suffix = 2
    while candidate.exists():
        candidate = log_dir / f"{timestamp}_{suffix}.log"
        suffix += 1
    return candidate


def _start_session_logging(session_name: str, device_name: str) -> Path:
    log_dir = _device_log_dir(device_name)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = _unique_log_path(log_dir, _session_start_timestamp())
    # -o: only start piping if this pane isn't already being piped (a no-op
    # on an already-logging pane, so this is safe to call unconditionally
    # right after session creation).
    _run(["pipe-pane", "-o", "-t", session_name, f"cat >> {shlex.quote(str(log_path))}"])
    return log_path


def list_logged_device_ids() -> list[str]:
    """Device IDs that have at least one log directory under logs/terminal/."""
    if not LOGS_ROOT.is_dir():
        return []
    return sorted(p.name for p in LOGS_ROOT.iterdir() if p.is_dir() and _NAME_RE.match(p.name))


def list_device_logs(device_name: str) -> list[tuple[datetime, str]]:
    """List (session_start, filename) pairs for one device, newest first.

    Returns an empty list for an unknown device or one with no logs yet --
    never raises for that; this is display-only, read-only data. Session
    Start is always derived from the `<timestamp>` prefix, even for a
    collision-suffixed filename (`<timestamp>_2.log`, ...) -- a same-
    second collision is broken by the numeric suffix (higher = later),
    not by filename string order."""
    if not _NAME_RE.match(device_name):
        return []
    log_dir = _device_log_dir(device_name)
    if not log_dir.is_dir():
        return []
    entries = []
    for path in log_dir.iterdir():
        if not path.is_file():
            continue
        match = _LOG_FILENAME_RE.match(path.name)
        if not match:
            continue
        try:
            started = datetime.strptime(match.group(1), _SESSION_START_FORMAT)
        except ValueError:
            continue
        collision_suffix = int(match.group(2)) if match.group(2) else 0
        entries.append((started, collision_suffix, path.name))
    entries.sort(key=lambda entry: (entry[0], entry[1]), reverse=True)
    return [(started, name) for started, _collision_suffix, name in entries]


def read_device_log(device_name: str, filename: str) -> str:
    """Read one device's log file by exact filename.

    Fails closed (TerminalError, never a silent guess) unless `device_name`
    is a valid identifier and `filename` is exactly one of that device's own
    already-listed log files -- this rejects path traversal (`../`, an
    absolute path, or any name not matching the fixed `<timestamp>.log`
    format) without needing to special-case those forms individually."""
    if not _NAME_RE.match(device_name):
        raise TerminalError(f"Device '{device_name}' has no terminal logs.")
    valid_filenames = {name for _, name in list_device_logs(device_name)}
    if filename not in valid_filenames:
        raise TerminalError(f"No log file '{filename}' for device '{device_name}'.")
    log_path = _device_log_dir(device_name) / filename
    return log_path.read_text(encoding="utf-8", errors="replace")


# --------------------------------------------------------------------------
# Terminal log deletion (Step B: EXEC `delete logging ...`)
#
# Eligibility is defined by reusing list_device_logs() -- the exact same
# enumeration `show logging` uses -- so there is no second log-discovery
# model; what can be deleted never drifts from what is displayed.
#
# Active-writer protection: both a normal production session
# (derive_production_session_name()) and a Discovery bootstrap session
# (derive_discovery_session_name()) attach persistent pipe-pane logging to
# the *same* logs/terminal/<device-id>/ directory, keyed by device name
# only -- see _create_logged_session()/open_device_terminal()/
# open_bootstrap_terminal(). Validation sessions never attach logging at
# all (open_validation_session() never passes log_device_name), so they
# are never a protection concern here. tmux's pipe-pane is attached once
# at session creation and never explicitly detached before the session is
# killed, so an existing session is treated as still actively writing.
#
# The current architecture does not record which exact log file a live
# session is piping to anywhere retrievable after creation (the path is
# computed once in _start_session_logging() and its return value is
# discarded by both callers) -- there is no session-to-log-path registry
# to consult. The only reliable, provable primitive is therefore
# device-level: "does a production or Discovery session for this exact
# device currently exist?". Protection is applied at that granularity,
# deliberately never guessing the exact active file from timestamp,
# filename, size, or modification time (see module docstring for why: it
# would be a guess, not a proof) -- an active device fails closed for
# individual-file deletion, device-all deletion, and (transitively)
# global-all deletion.
# --------------------------------------------------------------------------


def _device_has_active_session(device_name: str) -> bool:
    """True if a production or Discovery bootstrap session for this exact
    device currently exists -- the only two session kinds that ever
    attach persistent logging. This is a read-only check: it never
    creates, closes, or otherwise touches either session."""
    return _session_exists(derive_production_session_name(device_name)) or _session_exists(
        derive_discovery_session_name(device_name)
    )


def _eligible_log_files(device_name: str) -> list[Path]:
    """Resolved filesystem paths for device_name's own eligible log files,
    derived from list_device_logs() -- never a second enumeration. A
    symlink is excluded defensively: it is not a persistent log this
    subsystem itself wrote, so it is simply never an eligible deletion
    target (unlinking a symlink only ever removes the link itself, never
    a target it points to, but this keeps deletion scoped to exactly the
    regular files show logging already exposes)."""
    log_dir = _device_log_dir(device_name)
    paths = []
    for _started, filename in list_device_logs(device_name):
        path = log_dir / filename
        if path.is_symlink():
            continue
        paths.append(path)
    return paths


def _unlink_eligible_log(path: Path) -> None:
    """Delete exactly one already-resolved eligible log file. Defense in
    depth beyond the exact-enumeration match that produced `path`: refuse
    anything that is not a plain file confined under LOGS_ROOT (a
    belt-and-suspenders check; a path outside LOGS_ROOT or a non-regular
    file can never actually reach here through the exact-match callers
    below, since every path passed in was built from LOGS_ROOT / a
    listed device / a listed filename)."""
    resolved_root = LOGS_ROOT.resolve()
    resolved_path = path.resolve()
    if resolved_root != resolved_path and resolved_root not in resolved_path.parents:
        raise TerminalError("Refusing to delete a file outside the terminal log root.")
    if path.is_symlink() or not path.is_file():
        raise TerminalError("Refusing to delete a non-regular terminal log file.")
    path.unlink()


def delete_device_log_file(device_name: str, filename: str) -> None:
    """Delete exactly one eligible log file for one device. Raises
    TerminalError (deletes nothing) if the filename does not exactly
    match one of that device's own already-listed eligible logs, or if
    the device currently has an active writer (device-level fail-closed
    -- see module docstring)."""
    if not _NAME_RE.match(device_name):
        raise TerminalError(f"No log file '{filename}' for device '{device_name}'.")
    eligible = {path.name: path for path in _eligible_log_files(device_name)}
    if filename not in eligible:
        raise TerminalError(f"No log file '{filename}' for device '{device_name}'.")
    if _device_has_active_session(device_name):
        raise TerminalError(f"Cannot delete an active terminal log for device '{device_name}'.")
    _unlink_eligible_log(eligible[filename])


def delete_all_device_logs(device_name: str) -> int:
    """Delete every eligible log file for one device. Raises TerminalError
    (deletes nothing) if the device has no eligible logs at all, or if it
    currently has an active writer. Returns the number of files deleted."""
    if not _NAME_RE.match(device_name):
        raise TerminalError(f"No terminal logs found for device '{device_name}'.")
    targets = _eligible_log_files(device_name)
    if not targets:
        raise TerminalError(f"No terminal logs found for device '{device_name}'.")
    if _device_has_active_session(device_name):
        raise TerminalError(f"Cannot delete all logs for '{device_name}' while a terminal log is active.")
    for path in targets:
        _unlink_eligible_log(path)
    return len(targets)


def delete_all_logs() -> int:
    """Delete every eligible terminal log across every device. Preflights
    the WHOLE operation before deleting anything: if ANY device targeted
    by this call (one with at least one eligible log) currently has an
    active writer, nothing at all is deleted -- not even the logs of
    devices that are themselves inactive. Raises TerminalError either way
    (active-writer rejection, or nothing to delete); returns the count
    deleted on success."""
    all_targets: list[Path] = []
    active_devices: list[str] = []
    for device_name in list_logged_device_ids():
        targets = _eligible_log_files(device_name)
        if not targets:
            continue
        if _device_has_active_session(device_name):
            active_devices.append(device_name)
            continue
        all_targets.extend(targets)
    if active_devices:
        raise TerminalError(
            "Cannot delete all terminal logs while terminal logs are active. "
            "Close or wait for the active sessions first."
        )
    if not all_targets:
        raise TerminalError("No terminal logs found.")
    for path in all_targets:
        _unlink_eligible_log(path)
    return len(all_targets)


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


def _build_transport_command(device_config: dict, *, accept_new_host_keys: bool = False) -> tuple[str, list[str]]:
    """`accept_new_host_keys` is used only by the private Discovery bootstrap
    path (see discovery.py): Discovery is an unattended flow with no human
    to answer an interactive host-key confirmation prompt, so it passes
    `-o StrictHostKeyChecking=accept-new` (still verifies/records the key,
    it just never blocks on a fresh one) instead of automating that prompt.
    terminal_open() (human/Claude-driven) never sets this -- its behavior is
    completely unchanged."""
    transport = device_config.get("transport")
    address = device_config.get("address")
    if not address:
        raise TerminalError("Device configuration is missing 'address'.")

    if transport == "ssh":
        _require_binary("ssh")
        target, port = _ssh_target(device_config, 22)
        host_key_opts = (
            ["-o", "StrictHostKeyChecking=accept-new", "-o", "BatchMode=no"] if accept_new_host_keys else []
        )
        command = ["ssh", *host_key_opts, "-p", str(port), target]

        jump_host = device_config.get("jump_host_config")
        if jump_host is not None:
            # Single-hop native OpenSSH ProxyJump (-J jump-target:jump-port).
            # No shell-hop automation: OpenSSH itself opens the second SSH
            # connection through the first, and the tmux pane still just
            # sees one interactive session to read/send against, exactly
            # like a direct connection.
            jump_target, jump_port = _ssh_target(jump_host, 22)
            command = ["ssh", *host_key_opts, "-J", f"{jump_target}:{jump_port}", "-p", str(port), target]

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
    reused = _ensure_managed_session(session_name, command, log_device_name=device_name)
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
# Private Discovery bootstrap connectivity (see discovery.py)
#
# Not exposed as an MCP tool and not reachable through terminal_open()'s
# public device-namespace/active-topology restriction, which stays
# completely unchanged. This reuses the exact same session primitives and
# _build_transport_command() (direct SSH and single-hop ProxyJump alike) as
# production, in a structurally separate namespace so a Discovery session
# can never be mistaken for, list alongside, or be closed by any public
# terminal_* tool call.
# --------------------------------------------------------------------------


def open_bootstrap_terminal(device_name: str, device_config: dict) -> dict:
    """Open a private, temporary session for Discovery only. Always creates
    a fresh session (Discovery never reuses a prior bootstrap session -- see
    close_bootstrap_terminal(), which callers must use once collection for
    that device finishes)."""
    session_name = derive_discovery_session_name(device_name)
    _close_session(session_name)  # never reuse a stale bootstrap session
    transport, command = _build_transport_command(device_config, accept_new_host_keys=True)
    _ensure_managed_session(session_name, command, log_device_name=device_name)
    return {"device": device_name, "session_name": session_name, "transport": transport}


def send_to_bootstrap(device_name: str, text: str | None, keys: list[str] | None, enter: bool) -> None:
    session_name = derive_discovery_session_name(device_name)
    if text:
        _send_literal_text(session_name, text)
    if keys:
        _send_special_keys(session_name, keys)
    if enter:
        _send_enter(session_name)


def wait_for_bootstrap_pattern(
    device_name: str, pattern: "re.Pattern[str]", timeout: float, baseline_text: str | None = None
) -> str:
    session_name = derive_discovery_session_name(device_name)
    return _wait_for_pattern(session_name, pattern, timeout, baseline_text=baseline_text)


def read_bootstrap(device_name: str, lines: int = HISTORY_LIMIT) -> str:
    session_name = derive_discovery_session_name(device_name)
    return _capture_pane(session_name, lines)


def close_bootstrap_terminal(device_name: str) -> bool:
    return _close_session(derive_discovery_session_name(device_name))


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
