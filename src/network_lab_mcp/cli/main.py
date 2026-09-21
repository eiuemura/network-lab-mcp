"""Human-facing interactive Network Lab CLI.

Launched via ./run_cli.sh (`python3 -m network_lab_mcp.cli.main`). This is
the Human Configuration / Control Interface described in README.md: it
edits running-config (lab/settings.yaml: which topology/scenario/references
MCP uses), and topology/access-info/scenario/reference *definitions*,
through a candidate -> commit model, using an IOS XR-compatible interaction
style built on top of the command grammar in cli/grammar.py. It never
speaks the MCP stdio protocol and never performs network engineering
reasoning -- that remains Claude Code's job, driven by the committed
configuration this CLI produces.

Responsibilities kept here: the REPL, prompt rendering, mode transitions,
prompt_toolkit integration (completion/help key bindings, history, line
editing), command dispatch, output rendering, external-editor invocation,
and interactive confirmations. Candidate mutation and commit/clear
semantics live in cli/config.py; command grammar, abbreviation, completion,
and help all live in cli/grammar.py.
"""

from __future__ import annotations

import platform
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional

import yaml
from prompt_toolkit import PromptSession
from prompt_toolkit.application import run_in_terminal
from prompt_toolkit.history import History
from prompt_toolkit.key_binding import KeyBindings

import network_lab_mcp
from network_lab_mcp import lab
from network_lab_mcp.cli import config as cfgmod
from network_lab_mcp.cli import editor
from network_lab_mcp.cli import grammar

# Repo checkout root, for a best-effort `git rev-parse` in `show version`.
# Mirrors lab.find_lab_root()'s own repo-root computation; independent of it
# so `show version` never depends on lab/ existing or being valid.
_REPO_ROOT = Path(network_lab_mcp.__file__).resolve().parent.parent.parent


class _ExitCli(Exception):
    """Internal signal used only to unwind the REPL loop on EXEC exit/quit."""


# --------------------------------------------------------------------------
# Sensitive-command history masking
# --------------------------------------------------------------------------


def _is_password_command(line: str) -> bool:
    """Best-effort, deliberately conservative match: a bare password-setting
    command may be abbreviated (e.g. "pas cisco123"), so a raw secret must
    never enter this process's in-memory history even for an abbreviation.
    Over-matching (skipping a line that only looks like one) is safe; the
    only unsafe direction would be under-matching a real password command."""
    stripped = line.strip()
    if not stripped:
        return False
    first, _, rest = stripped.partition(" ")
    if not rest.strip():
        return False
    return bool(first) and "password".startswith(first.lower())


# --------------------------------------------------------------------------
# Multi-line configuration paste
# --------------------------------------------------------------------------


def _normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _split_pasted_command_lines(text: str) -> list[str]:
    """Split a pasted multi-line block into normalized physical command
    lines: CRLF/CR normalized to LF, leading configuration-display
    indentation stripped from each line, and blank lines / standalone "!"
    visual separators (as produced by the show-configuration renderers)
    dropped. Everything else in a line -- internal spacing, punctuation,
    special characters inside a value -- is preserved exactly, so this must
    never be used on a single manually-typed line."""
    lines = []
    for raw_line in _normalize_newlines(text).split("\n"):
        content = raw_line.lstrip(" \t")
        if not content.strip() or content.strip() == "!":
            continue
        lines.append(content)
    return lines


class MaskingHistory(History):
    """In-memory-only command history that never retains a raw password
    value. History is never written to disk, matching Step 2's requirement
    that history not persist across CLI process runs.

    `History.append_string()` unconditionally inserts into the in-memory
    `_loaded_strings` list that Up/Down navigation reads from -- overriding
    only `store_string()` (the persistent-backend hook) would not be enough,
    since that list is populated by `append_string()` itself. A sensitive
    line is therefore dropped before `append_string()` ever runs, so it
    never becomes reachable through history navigation at all."""

    def load_history_strings(self):
        return []

    def store_string(self, string: str) -> None:
        pass

    def append_string(self, string: str) -> None:
        if "\n" in _normalize_newlines(string):
            # A multi-line paste: prompt_toolkit hands back the whole
            # pasted block as one string containing embedded newlines.
            # Never store that raw block (it may contain a password on any
            # line) -- store each physical command line individually
            # instead, still excluding any password line, so Up/Down can
            # still recall the non-sensitive pasted commands.
            for line in _split_pasted_command_lines(string):
                if not _is_password_command(line):
                    super().append_string(line)
            return
        if _is_password_command(string):
            return
        super().append_string(string)


# --------------------------------------------------------------------------
# Prompt / context / rendering helpers
# --------------------------------------------------------------------------


def prompt_text(session: cfgmod.CliSession) -> str:
    if session.mode == "exec":
        return "network-lab# "
    if session.mode == "global":
        return "network-lab(config)# "
    if session.mode == "running":
        return "network-lab(config-running)# "
    if session.mode == "topology":
        return f"network-lab(config-topology-{session.definition_name})# "
    if session.mode == "device":
        return f"network-lab(config-device-{session.current_device_name})# "
    if session.mode == "access_info":
        return f"network-lab(config-access-info-{session.definition_name})# "
    if session.mode == "access_device":
        return f"network-lab(config-access-device-{session.current_device_name})# "
    if session.mode == "access_jump_host":
        return f"network-lab(config-access-jump-host-{session.current_jump_host_name})# "
    if session.mode == "scenario":
        return f"network-lab(config-scenario-{session.definition_name})# "
    if session.mode == "reference":
        return f"network-lab(config-reference-{session.definition_name})# "
    raise AssertionError(f"Unknown CLI mode: {session.mode!r}")


def build_context(session: cfgmod.CliSession) -> grammar.CliContext:
    lab_root = session.lab_root
    candidate_references: tuple[str, ...] = ()
    if session.settings_candidate is not None:
        candidate_references = tuple(session.settings_candidate.get("active_references") or [])
    topology_device_names: tuple[str, ...] = ()
    access_info_device_names: tuple[str, ...] = ()
    access_info_jump_host_names: tuple[str, ...] = ()
    if session.definition_candidate is not None:
        if session.definition_kind == "topology":
            topology_device_names = tuple((session.definition_candidate.get("devices") or {}).keys())
        elif session.definition_kind == "access_info":
            access_info_device_names = tuple((session.definition_candidate.get("devices") or {}).keys())
            access_info_jump_host_names = tuple((session.definition_candidate.get("jump_hosts") or {}).keys())
    return grammar.CliContext(
        topology_names=tuple(lab.list_topology_names(lab_root)),
        scenario_names=tuple(lab.list_scenario_names(lab_root)),
        reference_names=tuple(lab.list_reference_names(lab_root)),
        access_info_names=tuple(lab.list_access_info_names(lab_root)),
        candidate_reference_names=candidate_references,
        topology_candidate_device_names=topology_device_names,
        access_info_candidate_device_names=access_info_device_names,
        access_info_candidate_jump_host_names=access_info_jump_host_names,
    )


# A device's 'jump_host' field is spelled 'jump-host' as a CLI keyword and
# in every rendered view; every other field is spelled identically in YAML
# and in the CLI.
_FIELD_DISPLAY_NAMES = {"jump_host": "jump-host"}


def _display_field_name(field_name: str) -> str:
    return _FIELD_DISPLAY_NAMES.get(field_name, field_name)


def render_topology_block(data: dict) -> list[str]:
    """Safe logical topology only: no address/transport/port/username/password."""
    lines = [f"topology {data.get('name', '')}"]
    description = data.get("description")
    if description:
        lines.append(f" description {description}")
    for device_name, device in (data.get("devices") or {}).items():
        lines.append(f" device {device_name}")
        device_type = (device or {}).get("type")
        if device_type not in (None, ""):
            lines.append(f"  type {device_type}")
        lines.append(" !")
    lines.append("!")
    return lines


def render_access_info_block(data: dict) -> list[str]:
    """Explicit local access-info rendering: passwords are shown in clear
    text here (this is a lab tool; see README.md "Password display
    policy") -- MCP tool results, logs, errors, `?`, completion, and
    history never go through this function and remain password-free."""
    lines = [f"access-info {data.get('name', '')}"]
    for jump_host_name, jump_host in (data.get("jump_hosts") or {}).items():
        lines.append(f" jump-host {jump_host_name}")
        for field_name in cfgmod.JUMP_HOST_FIELD_ORDER:
            value = (jump_host or {}).get(field_name)
            if value not in (None, ""):
                lines.append(f"  {_display_field_name(field_name)} {value}")
        lines.append(" !")
    for device_name, device in (data.get("devices") or {}).items():
        lines.append(f" device {device_name}")
        for field_name in cfgmod.DEVICE_FIELD_ORDER:
            value = (device or {}).get(field_name)
            if value not in (None, ""):
                lines.append(f"  {_display_field_name(field_name)} {value}")
        lines.append(" !")
    lines.append("!")
    return lines


def render_generic_definition(data: dict) -> str:
    """Scenario/reference: schema is intentionally not fixed yet (see
    docs/scenario_format.md), so the candidate mapping is shown as YAML."""
    return yaml.safe_dump(data, sort_keys=False, default_flow_style=False).rstrip("\n")


def _running_config_lines(settings: dict) -> str:
    sections: list[tuple[str, list[str]]] = []
    access_info_name = settings.get("active_access_info")
    if access_info_name:
        sections.append(("access-info", [access_info_name]))
    topology_name = settings.get("active_topology")
    if topology_name:
        sections.append(("topology", [topology_name]))
    scenario_name = settings.get("active_scenario")
    if scenario_name:
        sections.append(("scenario", [scenario_name]))
    references = settings.get("active_references") or []
    if references:
        sections.append(("reference", list(references)))
    lines: list[str] = []
    for label, values in sections:
        lines.append("!")
        lines.append(f" {label}")
        for value in values:
            lines.append(f"  {value}")
    lines.append("!")
    return "\n".join(lines)


def render_committed_running_config(session: cfgmod.CliSession) -> str:
    """`show running-config` in EXEC/global/running mode: the committed MCP
    running-config selection -- never a definition's own content. Not used
    once a topology/access-info/scenario/reference (or its device/jump-host
    submode) is the current context; see render_committed_definition() for
    that."""
    return _running_config_lines(lab.read_settings(session.lab_root))


# Nested submodes ("device" under topology, "access_device"/
# "access_jump_host" under access-info) show only the one sub-object being
# edited, not every device/jump-host in the parent definition --
# context-local, like the definition-level modes.
_DEVICE_SUBMODES = ("device", "access_device")


def _scoped_to_current_device(data: Optional[dict], device_name: Optional[str]) -> Optional[dict]:
    """Limit whole-definition `data` to just `device_name`'s entry, or None
    if that device isn't present (e.g. a brand-new, never-committed
    device) -- the caller renders None as "no output"."""
    if data is None or device_name is None:
        return None
    devices = data.get("devices") or {}
    if device_name not in devices:
        return None
    scoped = dict(data)
    scoped["devices"] = {device_name: devices[device_name]}
    scoped.pop("jump_hosts", None)
    return scoped


def _scoped_to_jump_host(data: Optional[dict], jump_host_name: Optional[str]) -> Optional[dict]:
    """Same idea as _scoped_to_current_device(), for a jump-host submode."""
    if data is None or jump_host_name is None:
        return None
    jump_hosts = data.get("jump_hosts") or {}
    if jump_host_name not in jump_hosts:
        return None
    scoped = dict(data)
    scoped["jump_hosts"] = {jump_host_name: jump_hosts[jump_host_name]}
    scoped.pop("devices", None)
    return scoped


def _scope_to_session_context(session: cfgmod.CliSession, data: Optional[dict]) -> Optional[dict]:
    if session.mode in _DEVICE_SUBMODES:
        return _scoped_to_current_device(data, session.current_device_name)
    if session.mode == "access_jump_host":
        return _scoped_to_jump_host(data, session.current_jump_host_name)
    return data


def _render_definition_block(kind: Optional[str], data: Optional[dict]) -> str:
    if kind is None or data is None:
        return ""
    if kind == "topology":
        return "\n".join(render_topology_block(data))
    if kind == "access_info":
        return "\n".join(render_access_info_block(data))
    return render_generic_definition(data)  # scenario / reference


def render_committed_definition(session: cfgmod.CliSession) -> str:
    """`show running-config` while a topology/access-info/scenario/
    reference definition (or its device/jump-host submode) is the current
    context: that same object's *full* committed-on-disk state, re-read
    fresh -- never the in-memory candidate -- so it is empty for a
    definition (or sub-object) that has never been committed, and reflects
    a commit made moments ago in this same session."""
    data = cfgmod.load_committed_definition(session.definition_kind, session.definition_name, session.lab_root)
    data = _scope_to_session_context(session, data)
    return _render_definition_block(session.definition_kind, data)


# --------------------------------------------------------------------------
# `show configuration` / bare `show`: UNCOMMITTED CHANGES ONLY (a bounded,
# pragmatic delta -- not a full candidate dump, and not a generic minimal
# recursive diff engine). See docs/cli_reference.md "show configuration" for
# the rationale.
# --------------------------------------------------------------------------


def _field_delta_lines(original_obj: dict, candidate_obj: dict, field_order: tuple[str, ...]) -> list[str]:
    """Field-level delta for structured scalar configuration (access-info/
    jump-host/topology-device fields): only changed fields are rendered,
    each as a "field value" (set/changed) or "no field" (cleared) line --
    unchanged fields are never repeated."""
    lines = []
    for field_name in field_order:
        old_value = original_obj.get(field_name)
        new_value = candidate_obj.get(field_name)
        if old_value == new_value:
            continue
        display_name = _display_field_name(field_name)
        if new_value not in (None, ""):
            lines.append(f"  {display_name} {new_value}")
        else:
            lines.append(f"  no {display_name}")
    return lines


def _named_objects_delta(keyword: str, original_map: dict, candidate_map: dict, field_order: tuple[str, ...]) -> list[str]:
    """Render only the named objects (devices/jump-hosts) that actually
    changed, each as its own ' <keyword> <name>' / field lines / ' !'
    block -- a brand-new object (absent from original_map) is diffed
    against {}, so all of its populated fields show up as "new"."""
    lines: list[str] = []
    for name, candidate_obj in candidate_map.items():
        original_obj = original_map.get(name) or {}
        field_lines = _field_delta_lines(original_obj, candidate_obj or {}, field_order)
        if field_lines:
            lines.append(f" {keyword} {name}")
            lines.extend(field_lines)
            lines.append(" !")
    return lines


def render_access_info_configuration_delta(name: str, original: Optional[dict], candidate: dict) -> str:
    original = original or {}
    body = _named_objects_delta("jump-host", original.get("jump_hosts") or {}, candidate.get("jump_hosts") or {}, cfgmod.JUMP_HOST_FIELD_ORDER)
    body += _named_objects_delta("device", original.get("devices") or {}, candidate.get("devices") or {}, cfgmod.DEVICE_FIELD_ORDER)
    if not body:
        return ""
    return "\n".join([f"access-info {name}", *body, "!"])


_TOPOLOGY_TRACKED_KEYS = ("name", "description", "devices")


def render_topology_configuration_delta(name: str, original: Optional[dict], candidate: dict) -> str:
    original = original or {}
    other_keys = (set(candidate) | set(original)) - set(_TOPOLOGY_TRACKED_KEYS)
    if any(candidate.get(k) != original.get(k) for k in other_keys):
        # A field the structured CLI has no representation for changed
        # (e.g. 'links', or something an external editor added) -- fall
        # back to the whole candidate block so nothing is silently lost,
        # per the bounded/pragmatic delta policy for open structures.
        return "\n".join(render_topology_block(candidate))

    lines: list[str] = []
    old_description = original.get("description")
    new_description = candidate.get("description")
    if old_description != new_description:
        lines.append(f" description {new_description}" if new_description else " no description")
    lines += _named_objects_delta("device", original.get("devices") or {}, candidate.get("devices") or {}, ("type",))
    if not lines:
        return ""
    return "\n".join([f"topology {name}", *lines, "!"])


def render_generic_configuration_delta(name: str, original: Optional[dict], candidate: dict) -> str:
    """Scenario/reference: open YAML with no structured CLI representation
    below the whole-document level, so the "changed object" is the whole
    (small) document -- an acceptable, bounded fallback, not a claim that
    every field in it changed."""
    if (original or {}) == candidate:
        return ""
    return render_generic_definition(candidate)


def _render_configuration_delta(kind: Optional[str], name: Optional[str], original: Optional[dict], candidate: Optional[dict]) -> str:
    if kind is None or candidate is None:
        return ""
    if kind == "topology":
        return render_topology_configuration_delta(name, original, candidate)
    if kind == "access_info":
        return render_access_info_configuration_delta(name, original, candidate)
    return render_generic_configuration_delta(name, original, candidate)


def _running_config_delta_lines(original: dict, candidate: dict) -> list[str]:
    """Field-level delta for the running-config selection: only the
    selection(s) that actually changed, using the existing set/`no`
    rendering conventions -- unrelated unchanged selections are never
    repeated."""
    lines: list[str] = []
    for key, label in (
        ("active_access_info", "access-info"),
        ("active_topology", "topology"),
        ("active_scenario", "scenario"),
    ):
        old_value = original.get(key)
        new_value = candidate.get(key)
        if old_value == new_value:
            continue
        if new_value:
            lines.append("!")
            lines.append(f" {label}")
            lines.append(f"  {new_value}")
            lines.append("!")
        else:
            lines.append(f"no {label}")

    old_refs = original.get("active_references") or []
    new_refs = candidate.get("active_references") or []
    added_refs = [r for r in new_refs if r not in old_refs]
    removed_refs = [r for r in old_refs if r not in new_refs]
    if added_refs:
        lines.append("!")
        lines.append(" reference")
        for reference in added_refs:
            lines.append(f"  {reference}")
        lines.append("!")
    for reference in removed_refs:
        lines.append(f"no reference {reference}")
    return lines


def render_configuration_candidate(session: cfgmod.CliSession) -> str:
    """`show configuration` (and bare `show` in a configuration mode):
    **uncommitted changes only** in the current context -- never a full
    dump of the candidate. `running` mode shows the running-config
    selection delta; `global` mode aggregates that delta with the open
    definition's delta (each dirty scope rendered with its own type-aware
    delta renderer, not merged into one cross-definition diff); a
    definition mode (or its device/jump-host submode) shows only that
    object's delta, scoped to the current sub-object where applicable.
    Entering a new, still-empty object produces no output at all."""
    if session.mode == "running":
        return "\n".join(_running_config_delta_lines(session.committed_settings or {}, session.settings_candidate or {}))

    if session.mode == "global":
        parts = []
        settings_lines = _running_config_delta_lines(session.committed_settings or {}, session.settings_candidate or {})
        if settings_lines:
            parts.append("\n".join(settings_lines))
        if session.definition_kind is not None:
            original = cfgmod.load_committed_definition(session.definition_kind, session.definition_name, session.lab_root)
            text = _render_configuration_delta(session.definition_kind, session.definition_name, original, session.definition_candidate)
            if text:
                parts.append(text)
        return "\n".join(parts)

    kind = session.definition_kind
    if kind is None:
        return ""
    original = cfgmod.load_committed_definition(kind, session.definition_name, session.lab_root)
    original = _scope_to_session_context(session, original)
    candidate = _scope_to_session_context(session, session.definition_candidate)
    return _render_configuration_delta(kind, session.definition_name, original, candidate)


def print_help_result(result: grammar.HelpResult) -> None:
    for line in result.lines:
        print(f"  {line.token:<20} {line.description}")
    if result.show_cr:
        print("  <cr>")


def print_parse_error(error: grammar.ParseError) -> None:
    if error.kind == "unknown":
        print(f'% Unknown command: "{error.token}"')
    elif error.kind == "ambiguous":
        print(f'% Ambiguous command: "{error.token}"')
    elif error.kind == "incomplete":
        print("% Incomplete command.")
    elif error.kind == "invalid":
        if error.span is not None:
            print(" " * error.span[0] + "^")
        print("% Invalid input detected at '^' marker.")
        if error.detail:
            print(f"% {error.detail}")


def _confirm(message: str) -> bool:
    try:
        answer = input(message)
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return answer.strip().lower() in ("y", "yes")


def _do_commit(session: cfgmod.CliSession) -> None:
    """`commit` only saves the candidate -- it never changes `mode`. IOS
    XR-style navigation (`root`/`exit`/`end`) is how you leave a mode; a
    successful or no-op commit leaves you exactly where you were, so a
    freshly-committed object's mode (e.g. a device just created and
    committed) stays reachable immediately afterward."""
    try:
        changed = session.commit()
    except cfgmod.CommitValidationError as exc:
        for error in exc.errors:
            print(f"% {error}")
        return
    print("Commit complete." if changed else "No changes to commit.")


def _guarded_leave_configure(session: cfgmod.CliSession) -> None:
    if session.overall_dirty():
        print("% Uncommitted changes exist. Use 'commit' or 'clear'.")
        return
    session.reset_to_exec()


# --------------------------------------------------------------------------
# Command handlers -- dispatched by grammar action id
# --------------------------------------------------------------------------


def h_exec_configure(session: cfgmod.CliSession, args: dict) -> None:
    session.enter_configure()


def h_exec_exit(session: cfgmod.CliSession, args: dict) -> None:
    raise _ExitCli()


# `show running-config` means the committed MCP running-config selection
# only in these three modes; everywhere else it means the current
# topology/access-info/scenario/reference (or device submode) definition's
# own committed state instead (render_committed_definition()).
_MCP_SELECTION_MODES = ("exec", "global", "running")


def h_show_running_config(session: cfgmod.CliSession, args: dict) -> None:
    if session.mode in _MCP_SELECTION_MODES:
        print(render_committed_running_config(session))
        return
    text = render_committed_definition(session)
    if text:
        print(text)


def h_show_configuration(session: cfgmod.CliSession, args: dict) -> None:
    text = render_configuration_candidate(session)
    if text:
        print(text)


def _resolve_git_commit() -> str:
    """Best-effort short commit hash for the checkout `show version` is
    running from. Never raises and never surfaces a raw git error: missing
    `git`, a non-repository checkout, or any other failure all collapse to
    "unavailable"."""
    try:
        result = subprocess.run(
            ["git", "-C", str(_REPO_ROOT), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return "unavailable"
    if result.returncode != 0:
        return "unavailable"
    return result.stdout.strip() or "unavailable"


def render_version_info() -> str:
    """Read-only software information: no candidate/dirty-state change, no
    access-info, no dependency on the active topology/scenario/reference."""
    return "\n".join(
        [
            "Network Lab MCP",
            "",
            f"  Version:       {network_lab_mcp.__version__}",
            f"  Release date:  {network_lab_mcp.__release_date__}",
            f"  Git commit:    {_resolve_git_commit()}",
            f"  Author:        {network_lab_mcp.__author__}",
            f"  License:       {network_lab_mcp.__license__}",
            f"  Python:        {platform.python_version()}",
        ]
    )


def render_quick_start() -> str:
    """Network Lab MCP's own Quick Start/usage help -- distinct from the
    IOS XR-style `?` context-sensitive syntax help, which is unaffected by
    this and lives entirely in the "?" key binding below."""
    return "\n".join(
        [
            "Network Lab MCP CLI",
            "",
            "Purpose:",
            "  Configure the lab definitions and settings used by Network Lab MCP.",
            "",
            "Typical workflow:",
            "  1. Configure private device access information (access-info).",
            "  2. Create or edit a topology.",
            "  3. Create or edit a scenario.",
            "  4. Create or edit references.",
            "  5. Select access-info/topology/scenario/references in running-config.",
            "  6. Commit the configuration.",
            "  7. Use Claude Code with Network Lab MCP.",
            "",
            "Configuration areas:",
            "  running-config   Select topology, scenario, and references used by MCP",
            "  access-info      Configure private device access information",
            "  topology         Create or edit the logical topology",
            "  scenario         Create or edit the task scenario",
            "  reference        Create or edit reusable knowledge",
            "",
            "Editing:",
            "  Topology, scenario, and reference definitions support external YAML editing.",
            "  Editor selection: $VISUAL -> $EDITOR -> vim",
            "",
            "CLI help:",
            "  Press '?' at any prompt for context-sensitive command help.",
            "",
            "Additional help:",
            "  help claude",
            "  help workflow",
            "  help editor",
            "  help cli",
        ]
    )


def render_help_claude() -> str:
    return "\n".join(
        [
            "Claude Code Integration",
            "",
            "Network Lab MCP exposes exactly seven MCP tools to Claude Code:",
            "  get_active_topology, get_execution_instructions, terminal_open,",
            "  terminal_send, terminal_read, terminal_list, terminal_close.",
            "",
            "Claude receives:",
            "  - the committed active topology (safe logical data only)",
            "  - the committed active scenario",
            "  - the committed active references",
            "",
            "Claude never receives:",
            "  - device passwords, usernames, or addresses",
            "  - any access-info definition, including jump-host details",
            "  - uncommitted candidate configuration",
            "",
            "Claude addresses a device by its logical device ID only (e.g. \"R1\").",
            "Network Lab MCP resolves the private connection details internally --",
            "including an optional single-hop SSH jump host, entirely inside",
            "terminal_open() -- and opens the terminal session; Claude never sees",
            "any of that resolution.",
            "",
            "Use running-config (see 'help workflow') to select which committed",
            "access-info/topology/scenario/references MCP uses and exposes to Claude.",
            "",
            "Getting started with Claude Code:",
            "  1. Install Network Lab MCP into an activated Python environment",
            "     (see README.md, \"Installation model\").",
            "  2. Register it once:",
            "       claude mcp add --scope user --transport stdio network-lab -- network-lab-mcp",
            "  3. Start Claude Code from your own task workspace (not this",
            "     repository) once Network Lab MCP has been configured and",
            "     committed.",
        ]
    )


def render_help_workflow() -> str:
    return "\n".join(
        [
            "Typical Workflow",
            "",
            "1. Configure access-info (device credentials, optional jump hosts).",
            "2. Create or edit topology.",
            "3. Create or edit scenario.",
            "4. Create or edit references.",
            "5. Select access-info/topology/scenario/references in running-config.",
            "6. Review uncommitted changes (show / show configuration).",
            "7. Commit.",
            "8. Use Claude Code.",
            "",
            "Definitions:",
            "  access-info     Private device connection information",
            "  topology        Safe logical network model",
            "  scenario        Task intent",
            "  reference       Reusable knowledge",
            "  running-config  Definitions currently used by MCP",
            "",
            "Topology can be created/edited with structured CLI commands or an",
            "external YAML editor ('edit'). A future Step 3 will add topology",
            "discovery as a third way to produce a topology candidate -- that",
            "discovery step is not implemented yet.",
        ]
    )


def render_help_editor() -> str:
    return "\n".join(
        [
            "External YAML Editor",
            "",
            "Available for topology, scenario, and reference definitions (not",
            "access-info or jump hosts, which stay on the structured CLI so",
            "credentials are never written to an external editor's temp file).",
            "",
            "Editor resolution order:",
            "  1. $VISUAL",
            "  2. $EDITOR",
            "  3. vim (fallback; no .vimrc required -- Network Lab MCP enables",
            "     basic YAML syntax highlighting itself only for this fallback,",
            "     never when you set $VISUAL/$EDITOR yourself)",
            "",
            "'edit' opens the current candidate in a secure, uniquely named",
            "temporary .yaml file. Saving and quitting the editor only updates",
            "the in-memory candidate; the real committed file is not changed",
            "until 'commit'. 'clear' discards the edit (and any other",
            "uncommitted change in the current configure session) without",
            "touching disk.",
        ]
    )


def render_help_cli() -> str:
    return "\n".join(
        [
            "CLI Usage",
            "",
            "  ?                     Context-sensitive help for the current position",
            "  Tab / Ctrl-I          Complete the current token",
            "  configure             Enter configuration mode",
            "  show                  Same as 'show configuration' (bare show)",
            "  show configuration    Uncommitted changes in the current context",
            "  show running-config   Committed state in the current context",
            "  commit                Save the candidate; stays in the current mode",
            "  root                  Jump to global configuration mode (candidate kept)",
            "  exit                  Move one configuration level up (candidate kept)",
            "  end                   Return to EXEC (blocked while uncommitted changes exist)",
            "  clear                 Discard uncommitted configure-session changes",
            "  Ctrl-C                Cancel the current input line only",
            "",
            "Pasting a multi-line configuration block is supported: each line runs in",
            "order as if typed manually, stopping at the first invalid line.",
            "",
            "'commit' never leaves the mode you were in -- use 'root'/'exit'/'end' to",
            "navigate. 'show running-config' means the committed MCP running-config",
            "selection in EXEC/global/running mode, and the current object's own",
            "committed state everywhere else.",
            "",
            "See docs/cli_reference.md for the full command reference.",
        ]
    )


_HELP_TOPIC_RENDERERS: dict[str, Callable[[], str]] = {
    "claude": render_help_claude,
    "workflow": render_help_workflow,
    "editor": render_help_editor,
    "cli": render_help_cli,
}


def h_help(session: cfgmod.CliSession, args: dict) -> None:
    print(render_quick_start())


def h_help_topic(session: cfgmod.CliSession, args: dict) -> None:
    print(_HELP_TOPIC_RENDERERS[args["topic"]]())


def h_show_version(session: cfgmod.CliSession, args: dict) -> None:
    print(render_version_info())


def h_commit(session: cfgmod.CliSession, args: dict) -> None:
    _do_commit(session)


def h_clear(session: cfgmod.CliSession, args: dict) -> None:
    session.clear()


def h_end(session: cfgmod.CliSession, args: dict) -> None:
    _guarded_leave_configure(session)


def h_root(session: cfgmod.CliSession, args: dict) -> None:
    """`root`: jump straight to global configuration mode from any nested
    submode, preserving candidate state -- never commits, never clears."""
    session.go_to_global()


# `exit` moves exactly one configuration level up, per cfgmod._EXIT_PARENT_MODE
# (the grammar/config single source of truth for the mode hierarchy -- no
# separate per-mode table to keep in sync here). A leaf submode's own
# current-object attribute is cleared on the way out.
_LEAF_MODE_CURRENT_ATTR = {
    "device": "current_device_name",
    "access_device": "current_device_name",
    "access_jump_host": "current_jump_host_name",
}


def h_exit(session: cfgmod.CliSession, args: dict) -> None:
    attr = _LEAF_MODE_CURRENT_ATTR.get(session.mode)
    if attr is not None:
        setattr(session, attr, None)
    session.mode = cfgmod._EXIT_PARENT_MODE[session.mode]


def h_edit(session: cfgmod.CliSession, args: dict) -> None:
    try:
        result = editor.edit_yaml_candidate(session.definition_candidate)
    except editor.EditorError as exc:
        print(f"% {exc}")
        return
    session.replace_definition_candidate(result)


# ---- global configuration mode ----


def h_global_running_config(session: cfgmod.CliSession, args: dict) -> None:
    session.mode = "running"


def h_global_access_info(session: cfgmod.CliSession, args: dict) -> None:
    name = args["name"]
    ok, message = session.can_switch_definition("access_info", name)
    if not ok:
        print(f"% {message}")
        return
    session.enter_access_info_definition(name)


def h_global_topology(session: cfgmod.CliSession, args: dict) -> None:
    name = args["name"]
    ok, message = session.can_switch_definition("topology", name)
    if not ok:
        print(f"% {message}")
        return
    plan = session.plan_topology_definition(name)
    if plan.kind == "case_collision":
        confirmed = _confirm(
            f"% An existing topology '{plan.existing}' differs only by letter case.\n"
            f"Create a separate topology named '{plan.name}'? [yes/no]: "
        )
        if not confirmed:
            return
        plan = cfgmod.TopologyPlan("create_new", plan.name)
    session.apply_topology_definition_plan(plan)


def h_global_scenario(session: cfgmod.CliSession, args: dict) -> None:
    name = args["name"]
    ok, message = session.can_switch_definition("scenario", name)
    if not ok:
        print(f"% {message}")
        return
    session.enter_scenario_definition(name)


def h_global_reference(session: cfgmod.CliSession, args: dict) -> None:
    name = args["name"]
    ok, message = session.can_switch_definition("reference", name)
    if not ok:
        print(f"% {message}")
        return
    session.enter_reference_definition(name)


# ---- running-config mode ----


def h_running_access_info(session: cfgmod.CliSession, args: dict) -> None:
    session.select_access_info(args["name"])


def h_running_access_info_remove(session: cfgmod.CliSession, args: dict) -> None:
    session.clear_access_info_selection()


def h_running_topology(session: cfgmod.CliSession, args: dict) -> None:
    session.select_topology(args["name"])


def h_running_scenario(session: cfgmod.CliSession, args: dict) -> None:
    session.set_scenario(args["name"])


def h_running_reference_add(session: cfgmod.CliSession, args: dict) -> None:
    session.add_reference(args["name"])


def h_running_reference_remove(session: cfgmod.CliSession, args: dict) -> None:
    session.remove_reference(args["name"])


# ---- topology definition mode ----


def h_topology_description(session: cfgmod.CliSession, args: dict) -> None:
    session.set_topology_description(args["text"])


def h_topology_device(session: cfgmod.CliSession, args: dict) -> None:
    session.enter_topology_device(args["name"])


# ---- topology device submode (safe fields only) ----


def h_device_set_type(session: cfgmod.CliSession, args: dict) -> None:
    session.set_device_field("type", args["value"])


# ---- access-info definition mode ----


def h_access_info_device(session: cfgmod.CliSession, args: dict) -> None:
    session.enter_access_info_device(args["name"])


def h_access_info_jump_host(session: cfgmod.CliSession, args: dict) -> None:
    session.enter_access_info_jump_host(args["name"])


# ---- access-info device submode (private connection fields) ----


def h_access_device_set_type(session: cfgmod.CliSession, args: dict) -> None:
    session.set_device_field("type", args["value"])


def h_access_device_set_address(session: cfgmod.CliSession, args: dict) -> None:
    session.set_device_field("address", args["value"])


def h_access_device_set_transport(session: cfgmod.CliSession, args: dict) -> None:
    session.set_device_field("transport", args["value"])


def h_access_device_set_port(session: cfgmod.CliSession, args: dict) -> None:
    session.set_device_field("port", int(args["value"]))


def h_access_device_set_username(session: cfgmod.CliSession, args: dict) -> None:
    session.set_device_field("username", args["value"])


def h_access_device_set_password(session: cfgmod.CliSession, args: dict) -> None:
    session.set_device_field("password", args["value"])


def h_access_device_clear_username(session: cfgmod.CliSession, args: dict) -> None:
    session.clear_device_field("username")


def h_access_device_clear_password(session: cfgmod.CliSession, args: dict) -> None:
    session.clear_device_field("password")


def h_access_device_clear_port(session: cfgmod.CliSession, args: dict) -> None:
    session.clear_device_field("port")


def h_access_device_set_jump_host(session: cfgmod.CliSession, args: dict) -> None:
    session.set_device_field("jump_host", args["value"])


def h_access_device_clear_jump_host(session: cfgmod.CliSession, args: dict) -> None:
    session.clear_device_field("jump_host")


# ---- access-info jump-host submode (single-hop OpenSSH ProxyJump endpoint) ----


def h_access_jump_host_set_type(session: cfgmod.CliSession, args: dict) -> None:
    session.set_jump_host_field("type", args["value"])


def h_access_jump_host_set_address(session: cfgmod.CliSession, args: dict) -> None:
    session.set_jump_host_field("address", args["value"])


def h_access_jump_host_set_transport(session: cfgmod.CliSession, args: dict) -> None:
    session.set_jump_host_field("transport", args["value"])


def h_access_jump_host_set_port(session: cfgmod.CliSession, args: dict) -> None:
    session.set_jump_host_field("port", int(args["value"]))


def h_access_jump_host_set_username(session: cfgmod.CliSession, args: dict) -> None:
    session.set_jump_host_field("username", args["value"])


def h_access_jump_host_set_password(session: cfgmod.CliSession, args: dict) -> None:
    session.set_jump_host_field("password", args["value"])


def h_access_jump_host_clear_username(session: cfgmod.CliSession, args: dict) -> None:
    session.clear_jump_host_field("username")


def h_access_jump_host_clear_password(session: cfgmod.CliSession, args: dict) -> None:
    session.clear_jump_host_field("password")


def h_access_jump_host_clear_port(session: cfgmod.CliSession, args: dict) -> None:
    session.clear_jump_host_field("port")


HANDLERS: dict[str, Callable[[cfgmod.CliSession, dict], None]] = {
    "exec.configure": h_exec_configure,
    "exec.show_running_config": h_show_running_config,
    "exec.show_version": h_show_version,
    "exec.help": h_help,
    "exec.help_topic": h_help_topic,
    "exec.exit": h_exec_exit,
    "exec.quit": h_exec_exit,
    "global.running_config": h_global_running_config,
    "global.access_info": h_global_access_info,
    "global.topology": h_global_topology,
    "global.scenario": h_global_scenario,
    "global.reference": h_global_reference,
    "global.exit": h_end,
    "running.access_info": h_running_access_info,
    "running.access_info_remove": h_running_access_info_remove,
    "running.topology": h_running_topology,
    "running.scenario": h_running_scenario,
    "running.reference_add": h_running_reference_add,
    "running.reference_remove": h_running_reference_remove,
    "topology.description": h_topology_description,
    "topology.device": h_topology_device,
    "topology.edit": h_edit,
    "device.set_type": h_device_set_type,
    "access_info.device": h_access_info_device,
    "access_info.jump_host": h_access_info_jump_host,
    "access_device.set_type": h_access_device_set_type,
    "access_device.set_address": h_access_device_set_address,
    "access_device.set_transport": h_access_device_set_transport,
    "access_device.set_port": h_access_device_set_port,
    "access_device.set_username": h_access_device_set_username,
    "access_device.set_password": h_access_device_set_password,
    "access_device.set_jump_host": h_access_device_set_jump_host,
    "access_device.clear_username": h_access_device_clear_username,
    "access_device.clear_password": h_access_device_clear_password,
    "access_device.clear_port": h_access_device_clear_port,
    "access_device.clear_jump_host": h_access_device_clear_jump_host,
    "access_jump_host.set_type": h_access_jump_host_set_type,
    "access_jump_host.set_address": h_access_jump_host_set_address,
    "access_jump_host.set_transport": h_access_jump_host_set_transport,
    "access_jump_host.set_port": h_access_jump_host_set_port,
    "access_jump_host.set_username": h_access_jump_host_set_username,
    "access_jump_host.set_password": h_access_jump_host_set_password,
    "access_jump_host.clear_username": h_access_jump_host_clear_username,
    "access_jump_host.clear_password": h_access_jump_host_clear_password,
    "access_jump_host.clear_port": h_access_jump_host_clear_port,
    "scenario.edit": h_edit,
    "reference.edit": h_edit,
}

# Commands shared verbatim by every configuration mode (show/clear/commit/
# end/help/exit). `exit`'s target per mode comes from
# cfgmod._EXIT_PARENT_MODE (the config module's own mode-hierarchy SSOT), so
# there is nothing mode-specific to list here even though the destination
# differs. `root` is omitted at "global" (see grammar.py's
# _add_common_subtree(include_root=False) -- global has no `root` node to
# dispatch). `show version` is deliberately EXEC-only (see grammar.py's
# _add_show_subtree), so it is not part of this shared registration either.
for _mode in (
    "global",
    "running",
    "topology",
    "device",
    "access_info",
    "access_device",
    "access_jump_host",
    "scenario",
    "reference",
):
    HANDLERS[f"{_mode}.show_running_config"] = h_show_running_config
    HANDLERS[f"{_mode}.show_configuration"] = h_show_configuration
    HANDLERS[f"{_mode}.commit"] = h_commit
    HANDLERS[f"{_mode}.clear"] = h_clear
    HANDLERS[f"{_mode}.end"] = h_end
    HANDLERS[f"{_mode}.help"] = h_help
    HANDLERS[f"{_mode}.help_topic"] = h_help_topic
    if _mode != "global":
        # global's "exit" is guarded (equivalent to "end"; see the literal
        # "global.exit": h_end entry above) since global is the top of the
        # configure-session hierarchy -- every other mode's "exit" moves up
        # one level via cfgmod._EXIT_PARENT_MODE, unguarded.
        HANDLERS[f"{_mode}.exit"] = h_exit
        HANDLERS[f"{_mode}.root"] = h_root


# --------------------------------------------------------------------------
# REPL
# --------------------------------------------------------------------------


def _make_key_bindings(session: cfgmod.CliSession) -> KeyBindings:
    kb = KeyBindings()

    @kb.add("?")
    def _(event) -> None:
        buffer = event.current_buffer
        text_before = buffer.document.text_before_cursor
        text_after = buffer.document.text_after_cursor
        result = grammar.help(session.mode, text_before, build_context(session))

        def _show() -> None:
            # Echo the "prompt + buffer + ?" line into scrollback first, the
            # same way a real terminal/IOS XR leaves a transcript of what was
            # actually pressed, before the help lines that answer it.
            print(f"{prompt_text(session)}{text_before}?{text_after}")
            print_help_result(result)

        if result.lines or result.show_cr:
            run_in_terminal(_show)
        else:
            event.app.output.bell()

    @kb.add("tab")
    def _(event) -> None:
        buffer = event.current_buffer
        text = buffer.document.text_before_cursor
        result = grammar.complete(session.mode, text, build_context(session))

        if len(result.candidates) == 1:
            candidate = result.candidates[0]
            if candidate != result.replace_prefix:
                buffer.delete_before_cursor(len(result.replace_prefix))
                buffer.insert_text(candidate)
            else:
                event.app.output.bell()
        elif len(result.candidates) > 1:
            def _show() -> None:
                print()
                for candidate in result.candidates:
                    print(f"  {candidate}")

            run_in_terminal(_show)
        else:
            event.app.output.bell()

    return kb


def execute_command_line(session: cfgmod.CliSession, line: str) -> bool:
    """Parse and execute exactly one physical command line against the
    session's current authoritative mode/candidate state, exactly as a
    manually typed line would be. Used both for ordinary single-line input
    and for each physical line of a multi-line paste: a line that changes
    mode or candidate state (device/exit/root/end/clear/commit/...) mutates
    `session` in place, so the *next* call always sees the resulting state
    -- there is no separate paste-side notion of "current mode". Returns
    True on success, False if a parse or handler error was printed.

    Propagates `_ExitCli` (EXEC `exit`/`quit`) uncaught, matching how a
    manually typed `exit` unwinds the REPL loop in `run()`."""
    result = grammar.parse(session.mode, line)
    if not result.ok:
        print_parse_error(result.error)
        return False

    handler = HANDLERS[result.action]
    try:
        handler(session, result.args or {})
    except (cfgmod.ConfigError, cfgmod.CommitValidationError, lab.LabConfigError, editor.EditorError) as exc:
        if isinstance(exc, cfgmod.CommitValidationError):
            for error in exc.errors:
                print(f"% {error}")
        else:
            print(f"% {exc}")
        return False
    return True


def execute_input_block(session: cfgmod.CliSession, text: str) -> None:
    """Execute one accepted REPL input, which is either a single manually
    typed command or a pasted multi-line configuration block (prompt_toolkit
    hands back pasted text as one string with embedded newlines). A
    single-line input is executed exactly as before, unchanged. A multi-line
    input is split into normalized physical command lines and executed
    sequentially through execute_command_line(), stopping at the first
    error -- lines already applied stay in the candidate (no rollback), and
    nothing here or in execute_command_line() ever calls commit implicitly.

    Propagates `_ExitCli` uncaught, so a pasted `exit`/`quit` at EXEC level
    unwinds the REPL loop exactly like a manually typed one."""
    if "\n" in _normalize_newlines(text):
        for line in _split_pasted_command_lines(text):
            if not execute_command_line(session, line):
                break
    else:
        execute_command_line(session, text)


def run() -> None:
    lab_root = lab.find_lab_root()
    session = cfgmod.CliSession(lab_root)
    history = MaskingHistory()
    key_bindings = _make_key_bindings(session)
    prompt_session: PromptSession = PromptSession(
        history=history,
        key_bindings=key_bindings,
        complete_while_typing=False,
    )

    print("Network Lab CLI. Type 'help' or press '?' for available commands.")

    while True:
        try:
            raw = prompt_session.prompt(prompt_text(session))
        except KeyboardInterrupt:
            # Cancel only the current partially typed input line; the
            # candidate configuration and current mode are untouched.
            continue
        except EOFError:
            if session.overall_dirty():
                print()
                print("% Uncommitted changes exist. Use 'commit' or 'clear'.")
                continue
            print()
            break

        if not raw.strip():
            continue

        try:
            execute_input_block(session, raw)
        except _ExitCli:
            break


def main() -> None:
    try:
        run()
    except lab.LabConfigError as exc:
        print(f"% {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
