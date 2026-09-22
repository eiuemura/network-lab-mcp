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

import asyncio
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import yaml
from prompt_toolkit import PromptSession
from prompt_toolkit.application import Application, run_in_terminal
from prompt_toolkit.history import History
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.containers import Window
from prompt_toolkit.layout.controls import FormattedTextControl

import network_lab_mcp
from network_lab_mcp import discovery
from network_lab_mcp import lab
from network_lab_mcp import terminal
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
    indentation stripped from each line, and blank lines dropped.
    Everything else in a line -- internal spacing, punctuation, special
    characters inside a value -- is preserved exactly, so this must never
    be used on a single manually-typed line.

    A standalone "!" separator line (exactly "!" once stripped, as
    produced by the show-configuration renderers) is preserved here as a
    literal "!" entry rather than dropped -- execute_input_block() gives
    it its own narrow, mode-bounded meaning (see _apply_structural_bang())
    instead of treating it as an ordinary command line."""
    lines = []
    for raw_line in _normalize_newlines(text).split("\n"):
        content = raw_line.lstrip(" \t")
        stripped = content.strip()
        if not stripped:
            continue
        lines.append("!" if stripped == "!" else content)
    return lines


# Step A access-info paste round-trip: rendered access-info configuration
# closes every device/jump-host/definition block with a standalone "!"
# (see render_access_info_block()), so pasting it back must let "!" close
# the matching block -- otherwise a sibling "device R2" line right after
# "device R1"'s block is parsed while still inside R1's own submode and
# fails as an unknown command. This is deliberately NOT a generic "!" ==
# "exit" alias: it only applies inside a multi-line paste (never to a
# single manually-typed line, see execute_command_line()/run()), and only
# in these three access-info modes -- topology/scenario/reference paste
# behavior is unchanged, and a stray "!" at global configuration or EXEC
# is an explicit, deliberate no-op (never exit/end/quit, never terminates
# the CLI), so an imperfect copy/paste with one extra trailing "!" cannot
# silently leave configuration mode.
_STRUCTURAL_BANG_EXIT_MODES = ("access_device", "access_jump_host", "access_info")


def _apply_structural_bang(session: cfgmod.CliSession) -> None:
    if session.mode in _STRUCTURAL_BANG_EXIT_MODES:
        h_exit(session, {})


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
            # still recall the non-sensitive pasted commands. A "!"
            # separator is not itself a recallable command (see
            # _apply_structural_bang()), so it is excluded here exactly
            # like before this task's paste-round-trip fix.
            for line in _split_pasted_command_lines(string):
                if line != "!" and not _is_password_command(line):
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


def _no_definition_candidate_names(
    session: cfgmod.CliSession, kind: str, all_names: tuple[str, ...]
) -> tuple[str, ...]:
    """`no <kind> <name>` (Step C/D): see grammar.CliContext's
    `no_<kind>_candidate_names` docstring for the exact rule -- every
    stored name of this kind when nothing (of any kind) is dirty; only
    this exact identity if it is the one already-dirty definition
    (edit-in-progress or already pending deletion contributes nothing,
    since re-deleting it is not a new legal target); nothing at all if a
    *different* kind or name is dirty."""
    if session.definition_kind == kind and session.definition_dirty():
        if session.definition_candidate is None:
            return ()
        return (session.definition_name,)
    if session.definition_kind is not None and session.definition_dirty():
        return ()
    return tuple(all_names)


def _running_selectable_names(
    session: cfgmod.CliSession, kind: str, all_names: tuple[str, ...]
) -> tuple[str, ...]:
    """`config-running# <kind> <name>` selector (Step D): see
    grammar.CliContext's `running_<kind>_names` docstring -- every stored
    name of this kind, except one currently pending whole-definition
    deletion in the (separate) definition-editing candidate scope."""
    if session.definition_kind == kind and session.definition_dirty() and session.definition_candidate is None:
        return tuple(n for n in all_names if n != session.definition_name)
    return tuple(all_names)


def _monitor_terminal_target_names(session: cfgmod.CliSession) -> tuple[str, ...]:
    """`monitor terminal <device-id>` (Step 3.4/3.4a): every device eligible
    to be monitored right now -- see grammar.CliContext.monitor_terminal_
    device_ids's docstring for the exact rule. Used both to build that
    completion field and, independently, by h_monitor_terminal() to
    re-validate the typed device_id at execution time (completion hints
    are never trusted as authoritative, matching every other identifier
    in this CLI).

    Step 3.4a adds existing Discovery-session device IDs to the union: a
    device being discovered for the very first time may not yet be in the
    committed active topology (discover_topology()'s targets come from
    access-info, not the topology), so without this a brand-new device's
    live Discovery activity could never be monitored at all."""
    committed_devices: tuple[str, ...] = ()
    try:
        settings = lab.read_settings(session.lab_root)
        topology_name = lab.get_active_topology_name(settings)
        topology = lab.load_topology(topology_name, session.lab_root)
        committed_devices = tuple((topology.get("devices") or {}).keys())
    except lab.LabConfigError:
        pass
    session_devices = tuple(s["device"] for s in terminal.list_device_sessions())
    discovery_devices = tuple(terminal.list_discovery_device_ids())
    return tuple(dict.fromkeys((*committed_devices, *session_devices, *discovery_devices)))


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
    log_device_ids = tuple(terminal.list_logged_device_ids())
    log_files_by_device = {
        device_id: tuple(filename for _, filename in terminal.list_device_logs(device_id))
        for device_id in log_device_ids
    }
    try:
        committed_active_reference_names = tuple(lab.get_active_reference_names(lab.read_settings(lab_root)))
    except lab.LabConfigError:
        committed_active_reference_names = ()

    topology_names_list = tuple(lab.list_topology_names(lab_root))
    access_info_names_list = tuple(lab.list_access_info_names(lab_root))
    scenario_names_list = tuple(lab.list_scenario_names(lab_root))
    reference_names_list = tuple(lab.list_reference_names(lab_root))

    no_topology_candidate_names = _no_definition_candidate_names(session, "topology", topology_names_list)
    no_access_info_candidate_names = _no_definition_candidate_names(session, "access_info", access_info_names_list)
    no_scenario_candidate_names = _no_definition_candidate_names(session, "scenario", scenario_names_list)
    no_reference_candidate_names = _no_definition_candidate_names(session, "reference", reference_names_list)

    running_topology_names = _running_selectable_names(session, "topology", topology_names_list)
    running_access_info_names = _running_selectable_names(session, "access_info", access_info_names_list)
    running_scenario_names = _running_selectable_names(session, "scenario", scenario_names_list)
    running_reference_names = _running_selectable_names(session, "reference", reference_names_list)

    monitor_terminal_device_ids = _monitor_terminal_target_names(session)

    return grammar.CliContext(
        topology_names=topology_names_list,
        scenario_names=scenario_names_list,
        reference_names=reference_names_list,
        access_info_names=access_info_names_list,
        candidate_reference_names=candidate_references,
        topology_candidate_device_names=topology_device_names,
        access_info_candidate_device_names=access_info_device_names,
        access_info_candidate_jump_host_names=access_info_jump_host_names,
        log_device_ids=log_device_ids,
        log_files_by_device=log_files_by_device,
        committed_active_reference_names=committed_active_reference_names,
        no_topology_candidate_names=no_topology_candidate_names,
        no_access_info_candidate_names=no_access_info_candidate_names,
        no_scenario_candidate_names=no_scenario_candidate_names,
        no_reference_candidate_names=no_reference_candidate_names,
        running_topology_names=running_topology_names,
        running_access_info_names=running_access_info_names,
        running_scenario_names=running_scenario_names,
        running_reference_names=running_reference_names,
        monitor_terminal_device_ids=monitor_terminal_device_ids,
    )


# A device's 'jump_host' field is spelled 'jump-host' as a CLI keyword and
# in every rendered view; every other field is spelled identically in YAML
# and in the CLI.
_FIELD_DISPLAY_NAMES = {"jump_host": "jump-host"}


def _display_field_name(field_name: str) -> str:
    return _FIELD_DISPLAY_NAMES.get(field_name, field_name)


def render_topology_block(data: dict) -> list[str]:
    """Safe logical topology only: no address/transport/port/username/password.

    `links` has no structured CLI editing command (topology mode has no
    `link <a> <a-if> <b> <b-if>` command -- it is only ever set by
    `discover topology` or the external `edit`), so it is rendered as
    read-only review information, not as a re-typeable command line, under
    its own `links` block."""
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
    links = data.get("links") or []
    if links:
        lines.append(" links")
        for link in links:
            a = link.get("a", "?")
            b = link.get("b", "?")
            a_interface = link.get("a_interface")
            b_interface = link.get("b_interface")
            a_side = f"{a} {a_interface}" if a_interface else a
            b_side = f"{b} {b_interface}" if b_interface else b
            lines.append(f"  {a_side} <-> {b_side}")
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


_NONE_DISPLAY = "<none>"


def _running_config_lines(settings: dict) -> str:
    """`show running-config` selection summary (EXEC/global/running --
    one shared renderer). Step D.1: the `access-info` and `reference`
    sections always appear, even when empty, showing the display-only
    `<none>` marker instead of being silently omitted -- this makes an
    optional selection's absence explicit rather than ambiguous (was it
    never rendered, or genuinely unset?). `<none>` is rendering only: it
    is never written to settings.yaml, never a valid selector value, and
    never returned through candidate-diff (`show configuration`) or MCP
    state -- see _running_config_delta_lines() below, unchanged.

    `topology`/`scenario` are mandatory selections and keep their
    pre-existing behavior unchanged (a section is only appended if a
    name is actually present) -- Step D.1 does not introduce `<none>`
    for them; a missing mandatory value is not a state this renderer
    tries to make presentable, it is left exactly as before."""
    sections: list[tuple[str, list[str]]] = []
    access_info_name = settings.get("active_access_info")
    sections.append(("access-info", [access_info_name] if access_info_name else [_NONE_DISPLAY]))
    topology_name = settings.get("active_topology")
    if topology_name:
        sections.append(("topology", [topology_name]))
    scenario_name = settings.get("active_scenario")
    if scenario_name:
        sections.append(("scenario", [scenario_name]))
    references = settings.get("active_references") or []
    sections.append(("reference", list(references) if references else [_NONE_DISPLAY]))
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
    # links is a topology-level (not device-level) concept and any given
    # link necessarily names another device -- never another device's data
    # leaking into this one device's scoped view (see
    # render_topology_block()'s links section).
    scoped.pop("links", None)
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
    changed: a whole object present in `original_map` but absent from
    `candidate_map` (removed via `no <keyword> <name>`) as a single
    ` no <keyword> <name>` line, in committed order; then, for every object
    still in `candidate_map`, its own ' <keyword> <name>' / field lines /
    ' !' block if any field actually differs -- a brand-new object (absent
    from original_map) is diffed against {}, so all of its populated
    fields show up as "new". A candidate-only object that nets out
    identical to committed (e.g. removed then recreated identically, or
    never existed and was removed again) contributes nothing, by
    construction -- neither loop below renders it."""
    lines: list[str] = []
    for name in original_map:
        if name not in candidate_map:
            lines.append(f" no {keyword} {name}")
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
    if kind is None:
        return ""
    if candidate is None:
        # A whole definition is prospectively deleted (`no <kind> <name>`
        # -- see cfgmod.CliSession.remove_definition()). `original` is
        # always the real committed definition being removed here: a
        # brand-new, never-committed candidate is discarded outright by
        # remove_definition() instead of ever reaching this state, so
        # `original is None` alongside `candidate is None` never
        # represents a genuine deletion. Never render nested field-level
        # detail (and never the deleted definition's own content, which
        # for access-info may include plaintext credentials) -- only the
        # single whole-definition line. `kind.replace("_", "-")` recovers
        # the CLI keyword ("access_info" -> "access-info"; a no-op for
        # the other three kinds).
        if original is not None:
            return f"no {kind.replace('_', '-')} {name}"
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


# Step D.1: purely explanatory footer for the exact `config-running# no ?`
# help context (grammar.is_bare_no_context() gates it). access-info and
# reference are the ONLY real grammar candidates under running-config's
# "no" -- topology/scenario never gained "no topology"/"no scenario" (both
# are mandatory running-config selections, unset via "topology <name>" /
# "scenario <name>" instead, never by removal). This table only explains
# that existing shape in prose; it is not itself parsed, completed, or
# stored anywhere -- see the semantic-drift tests in
# tests/test_running_config_selection_model.py that assert the real
# grammar still matches every claim made here.
_RUNNING_CONFIG_SELECTION_MODEL = """\
Running-config selection model:
Type         Selection     Can be unset  Behavior
-----------  ------------  ------------  --------------------------------------------
access-info  Single        Yes           Without it, terminal access and discovery
                                          are unavailable
topology     Single        No            Required; use "topology <name>" to switch
scenario     Single        No            Required; use "scenario <name>" to switch
reference    Multiple      Yes           Use "no reference <name>" to remove one"""


def _render_running_config_selection_model() -> str:
    return _RUNNING_CONFIG_SELECTION_MODEL


def _should_show_running_no_footer(mode: str, text_before: str) -> bool:
    """Gate for the Step D.1 footer: only the spaced `no ?` context, only
    in running mode. Factored out of the `?` key-binding so it is testable
    without prompt_toolkit machinery."""
    return mode == "running" and grammar.is_bare_no_context(mode, text_before)


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


# --------------------------------------------------------------------------
# `show running-config <definition-type>` (EXEC only) -- a read-only
# dereference of one committed active-definition selection. Always reads
# committed running-config + the committed definition fresh from disk
# (never the in-memory candidate), and always reuses the same renderers
# definition mode's own `show running-config` uses -- there is no second
# rendering system.
# --------------------------------------------------------------------------


def h_show_running_config_access_info(session: cfgmod.CliSession, args: dict) -> None:
    settings = lab.read_settings(session.lab_root)
    name = lab.get_active_access_info_name(settings)
    if not name:
        print("% No active access-info is configured.")
        return
    data = lab.load_access_info(name, session.lab_root)
    print("\n".join(render_access_info_block(data)))


def h_show_running_config_topology(session: cfgmod.CliSession, args: dict) -> None:
    settings = lab.read_settings(session.lab_root)
    name = lab.get_active_topology_name(settings)
    data = lab.load_topology(name, session.lab_root)
    print("\n".join(render_topology_block(data)))


def h_show_running_config_scenario(session: cfgmod.CliSession, args: dict) -> None:
    settings = lab.read_settings(session.lab_root)
    name = lab.get_active_scenario_name(settings)
    data = lab.load_scenario(name, session.lab_root)
    print(render_generic_definition(data))


def h_show_running_config_reference(session: cfgmod.CliSession, args: dict) -> None:
    settings = lab.read_settings(session.lab_root)
    names = lab.get_active_reference_names(settings)
    if not names:
        print("% No active references are configured.")
        return
    # Fail-closed and atomic: load_references() loads every name in
    # committed order and raises immediately if any one fails, before any
    # reference block is ever rendered -- never a partial view.
    references = lab.load_references(names, session.lab_root)
    print("\n!\n".join(render_generic_definition(reference) for reference in references))


def h_show_running_config_reference_name(session: cfgmod.CliSession, args: dict) -> None:
    name = args["name"]
    settings = lab.read_settings(session.lab_root)
    active_names = lab.get_active_reference_names(settings)
    if name not in active_names:
        print(f"% Reference '{name}' is not active in running-config.")
        return
    data = lab.load_reference(name, session.lab_root)
    print(render_generic_definition(data))


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
            "  show logging          (EXEC) List/view persistent terminal session logs",
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


# --------------------------------------------------------------------------
# `monitor terminal <device-id>` (EXEC only, Step 3.4/3.4a/3.4b) -- a live,
# read-only human view of whichever Network Lab MCP terminal session
# currently has priority for a device (managed > discovery > waiting; see
# terminal.capture_device_terminal_view()'s own docstring). Passive
# observation only: it never sends anything to a pane and never creates/
# closes a session, and it deliberately does not take the Step 3.3
# per-device lock (same rationale as before: observation is a plain read,
# and the lock is process-local anyway).
#
# Step 3.4b: the UI is `full_screen=False` (never the alternate screen
# buffer), so terminal activity already streamed to the human stays in
# the *normal* terminal emulator scrollback -- it is printed once via
# run_in_terminal() (the same "print permanently above a live area"
# mechanism the `?`/Tab key bindings already use elsewhere in this file)
# and never repainted. Only a small 3-line status block at the bottom
# (`Monitoring terminal <device> | Read-only | Source: ... | Status: ...
# | q: quit`, width-adaptive) is live/redrawn, via prompt_toolkit's own
# `renderer.erase()` on exit -- which erases only that live area, not the
# scrollback above it, so quitting cleanly restores `network-lab#`
# without touching anything already printed. Monitor lifetime remains
# independent of session lifetime: it survives WAITING/ENDED and source
# switches alike, resuming live output automatically -- only an explicit
# q/Q/Ctrl-C ends it.
# --------------------------------------------------------------------------

# Human-observation cadence: frequent enough to feel live, far below a
# busy loop (a handful of tmux subprocess calls per second at most, only
# while a human actually has a monitor open). Unchanged from Step 3.4.
_MONITOR_REFRESH_INTERVAL = 0.3

# How much recent context to print once when a source first becomes
# active (Step 3.4b Section 18) -- deliberately bounded (matches the
# existing "visible pane" convention, terminal.DEFAULT_READ_LINES), never
# the full multi-thousand-line tmux history.
_MONITOR_INITIAL_CONTEXT_LINES = terminal.DEFAULT_READ_LINES


@dataclass
class _MonitorStreamCursor:
    """Per-monitor-run incremental-output bookkeeping (Step 3.4b). Pure UI
    state, not a terminal.py concept: reset (a "new instance") whenever
    the observed source changes, or a same-named source's captured
    history no longer has the previously-seen prefix as its own prefix --
    the same-name case is exactly what happens when a session is closed
    and a new one is created under the identical name (a fresh tmux pane
    never carries over the old pane's history), so this needs no extra
    tmux identity query (pane_id/session_id): the content-prefix check
    already implies it. The one honestly-documented edge case this cannot
    distinguish from a real recreation is history-limit (20000-line)
    eviction truncating the same session's own history -- both are
    treated identically (a "resumed" instance, bounded fresh context),
    which never loses correctness for the human observer (everything
    already emitted already reached their terminal), only occasionally
    reprints a small bounded amount of already-seen tail content."""

    source: str = "none"
    lines_shown: int = 0
    last_full_lines: list[str] = field(default_factory=list)


def _monitor_stream_step(device_id: str, cursor: _MonitorStreamCursor) -> tuple[list[str], terminal.TerminalMonitorSnapshot]:
    """One observe-and-diff step: returns (new permanent lines to print,
    latest snapshot). Deliberately separate from the async poll loop
    below so every lifecycle/incremental-output case is directly
    unit-testable without any real time passing or prompt_toolkit
    machinery (same testability principle as Step 3.4's
    _render_monitor_view()). Mutates `cursor` in place; never touches
    tmux beyond the one read-only terminal.capture_device_terminal_view()
    call (full history, so burst output between polls -- up to the
    20000-line history-limit -- is never lost merely because it scrolled
    off the visible pane)."""
    snapshot = terminal.capture_device_terminal_view(device_id, lines=terminal.HISTORY_LIMIT)
    printable: list[str] = []

    if snapshot.status == "waiting":
        if cursor.source != "none":
            printable.append(f"[monitor] {cursor.source} terminal activity ended; waiting")
        cursor.source = "none"
        cursor.lines_shown = 0
        cursor.last_full_lines = []
        return printable, snapshot

    full_lines = snapshot.pane_text.split("\n") if snapshot.pane_text else []
    is_new_instance = (
        snapshot.source != cursor.source
        or full_lines[: cursor.lines_shown] != cursor.last_full_lines[: cursor.lines_shown]
    )

    if is_new_instance:
        if cursor.source == "none":
            printable.append(f"[monitor] {snapshot.source} terminal activity started")
        elif snapshot.source != cursor.source:
            printable.append(f"[monitor] switched to {snapshot.source} terminal activity")
        else:
            printable.append(f"[monitor] {snapshot.source} terminal activity resumed")
        start = max(0, len(full_lines) - _MONITOR_INITIAL_CONTEXT_LINES)
        printable.extend(full_lines[start:])
    elif len(full_lines) > cursor.lines_shown:
        printable.extend(full_lines[cursor.lines_shown :])
    # else: unchanged poll -- append nothing (Step 3.4b Section 33).

    cursor.source = snapshot.source
    cursor.lines_shown = len(full_lines)
    cursor.last_full_lines = full_lines
    return printable, snapshot


def _monitor_status_line(device_id: str, snapshot: terminal.TerminalMonitorSnapshot) -> str:
    parts = [f"Monitoring terminal {device_id}", "Read-only"]
    if snapshot.status == "waiting":
        parts.append("Status: waiting for terminal activity")
    else:
        parts.append(f"Source: {snapshot.source}")
        parts.append(f"Status: {snapshot.status}")
    parts.append("q: quit")
    return " | ".join(parts)


def _monitor_status_block(device_id: str, snapshot: terminal.TerminalMonitorSnapshot) -> str:
    """The small 3-line live status area (separator/content/separator),
    width-adaptive (Step 3.4b Section 6) -- never touches the managed/
    Discovery tmux pane's own geometry, only this local rendering."""
    width = shutil.get_terminal_size(fallback=(80, 24)).columns
    separator = "-" * width
    line = _monitor_status_line(device_id, snapshot)
    if len(line) > width:
        line = line[: max(0, width - 1)]
    return f"{separator}\n{line}\n{separator}"


async def _monitor_poll_loop(
    device_id: str, cursor: _MonitorStreamCursor, status_holder: list[str], refresh_interval: float, app: Application
) -> None:
    """Background task (Step 3.4b): polls, prints any new permanent
    activity via run_in_terminal() (the same mechanism the `?`/Tab key
    bindings already use to print above a live prompt_toolkit area), then
    updates the live status text and invalidates the display. Polls
    immediately on start (so the very first frame already reflects real
    state), then on `refresh_interval`. Automatically cancelled when the
    Application exits (prompt_toolkit's own create_background_task()
    contract) -- no manual stop flag needed."""
    while True:
        printable, snapshot = _monitor_stream_step(device_id, cursor)
        if printable:
            text = "\n".join(printable)
            await run_in_terminal(lambda text=text: print(text))
        status_holder[0] = _monitor_status_block(device_id, snapshot)
        app.invalidate()
        await asyncio.sleep(refresh_interval)


def _build_monitor_application(device_id: str, refresh_interval: float) -> Application:
    """Construct (but do not run) the monitor's Application -- separated
    from run_terminal_monitor() so tests can inspect its key bindings
    without entering the blocking event loop.

    `full_screen=False` (Step 3.4b): never the alternate screen buffer,
    so the terminal emulator's normal scrollback -- including whatever
    this run prints via _monitor_poll_loop()'s run_in_terminal() calls --
    is preserved after exit, unlike Step 3.4/3.4a's full-screen view."""
    kb = KeyBindings()

    @kb.add("q")
    @kb.add("Q")
    @kb.add("c-c")
    def _(event) -> None:
        # Exits only this Application (event.app), never the surrounding
        # CLI process/PromptSession -- see run_terminal_monitor().
        event.app.exit()

    status_holder = ["Monitoring terminal {}\nRead-only".format(device_id)]
    control = FormattedTextControl(text=lambda: status_holder[0])
    app = Application(
        layout=Layout(Window(content=control, height=3)),
        key_bindings=kb,
        full_screen=False,
    )
    cursor = _MonitorStreamCursor()
    app.pre_run_callables.append(
        lambda: app.create_background_task(
            _monitor_poll_loop(device_id, cursor, status_holder, refresh_interval, app)
        )
    )
    return app


def run_terminal_monitor(device_id: str, *, refresh_interval: float = _MONITOR_REFRESH_INTERVAL) -> None:
    """Blocking: runs the monitor until the user quits it (q/Q/Ctrl-C).
    Every other keystroke is simply unbound -- this Application's only
    control is a non-editable FormattedTextControl, so there is no text
    buffer for a stray key to be inserted into, and nothing here ever
    forwards a keystroke to tmux. Exiting erases only the live status
    area (prompt_toolkit's own renderer.erase() on exit) -- already
    streamed activity, printed as normal terminal output, is untouched --
    and adds nothing to CLI command history -- this loop never touches
    PromptSession/history at all."""
    _build_monitor_application(device_id, refresh_interval).run()


def h_monitor_terminal(session: cfgmod.CliSession, args: dict) -> None:
    _require_interactive(args)
    device_id = args["device_id"]
    if device_id not in _monitor_terminal_target_names(session):
        raise terminal.TerminalError(
            f"Device '{device_id}' is not in the active topology and has no existing terminal session."
        )
    run_terminal_monitor(device_id)


# --------------------------------------------------------------------------
# `show logging` (EXEC only) -- read-only terminal transcript log listing.
# --------------------------------------------------------------------------


def _render_log_table(columns: tuple[str, ...], rows: list[tuple[str, ...]]) -> str:
    if not rows:
        return "No terminal logs found."
    widths = [
        max(len(columns[i]), max((len(row[i]) for row in rows), default=0)) for i in range(len(columns))
    ]
    lines = [
        "  ".join(columns[i].ljust(widths[i]) for i in range(len(columns))),
        "  ".join("-" * widths[i] for i in range(len(columns))),
    ]
    for row in rows:
        lines.append("  ".join(row[i].ljust(widths[i]) for i in range(len(columns))))
    return "\n".join(lines)


def _format_session_start(started) -> str:
    return started.strftime("%Y-%m-%d %H:%M:%S")


def _render_log_summary_table(counts: list[tuple[str, int]]) -> str:
    """`show logging` bare (Step B.1 section 14-15): one row per valid
    device logging directory with its eligible log-file count -- an
    empty directory (e.g. after `delete logging <device> all`) is still
    shown, with 0, since it remains a meaningful `delete logging <device>
    directory` target -- plus a Total row (sum of eligible logs only,
    never a directory count) separated by its own dashed rule. Distinct
    from _render_log_table() (a flat per-file listing), which
    `show logging <device>` still uses unchanged."""
    columns = ("Device", "Log Files")
    body = [(device, str(count)) for device, count in counts]
    footer = ("Total", str(sum(count for _device, count in counts)))
    widths = [max(len(columns[i]), max(len(row[i]) for row in body + [footer])) for i in range(len(columns))]
    separator = "  ".join("-" * widths[i] for i in range(len(columns)))
    lines = ["  ".join(columns[i].ljust(widths[i]) for i in range(len(columns))), separator]
    lines.extend("  ".join(row[i].ljust(widths[i]) for i in range(len(columns))) for row in body)
    lines.append(separator)
    lines.append("  ".join(footer[i].ljust(widths[i]) for i in range(len(columns))))
    return "\n".join(lines)


def h_show_logging(session: cfgmod.CliSession, args: dict) -> None:
    """Bare `show logging` (Step B.1a: restored to its pre-Step-B.1
    behavior -- see commit 972046b): a flat listing of every device's
    persistent log files, newest first, exactly as `show logging` meant
    before Step B.1 briefly overloaded it with the summary table now
    available explicitly as `show logging summary`."""
    rows = [
        (device_id, _format_session_start(started), filename)
        for device_id in sorted(terminal.list_logged_device_ids())
        for started, filename in terminal.list_device_logs(device_id)
    ]
    rows.sort(key=lambda row: row[1], reverse=True)
    print(_render_log_table(("Device", "Session Start", "Log File"), rows))


def h_show_logging_summary(session: cfgmod.CliSession, args: dict) -> None:
    device_ids = sorted(terminal.list_logged_device_ids())
    if not device_ids:
        print("No terminal logs found.")
        return
    counts = [(device_id, len(terminal.list_device_logs(device_id))) for device_id in device_ids]
    print(_render_log_summary_table(counts))


def h_show_logging_device(session: cfgmod.CliSession, args: dict) -> None:
    device_id = args["device_id"]
    rows = [(_format_session_start(started), filename) for started, filename in terminal.list_device_logs(device_id)]
    if not rows:
        print(f"No terminal logs found for device '{device_id}'.")
        return
    print(_render_log_table(("Session Start", "Log File"), rows))


def h_show_logging_device_file(session: cfgmod.CliSession, args: dict) -> None:
    content = terminal.read_device_log(args["device_id"], args["log_file"])
    if content:
        print(content, end="" if content.endswith("\n") else "\n")


# ---- delete logging (Step B / Step B.1, EXEC only) ----
#
# terminal.py owns enumeration/preflight/manifest/deletion (DeletionPlan,
# build_*_deletion_plan(), apply_deletion_plan()) and never reads
# interactive input; this module owns the [y/N] confirmation and message
# text only (Step B.1 section 50 boundary). terminal.TerminalError is
# already caught generically by execute_command_line() and printed as
# "% <message>", so a preflight rejection needs no handling here.


def _plural(count: int, singular: str, plural: str) -> str:
    return singular if count == 1 else plural


def _read_confirmation_line() -> str:
    """Thin wrapper around input() (no prompt argument -- the prompt is
    always printed separately by _confirm_delete(), so tests can observe
    it via captured stdout regardless of how the answer itself is
    supplied). Isolated so tests can supply a deterministic answer
    without a real interactive stdin, and so Ctrl-C/EOF handling lives in
    exactly one place. Uses plain input(), not prompt_toolkit's own
    PromptSession/history, so a confirmation answer is never inserted
    into command history and never confused with a pasted physical line
    (see h_delete_logging_*'s _require_interactive() check, which runs
    before this is ever reached)."""
    return input()


def _confirm_delete(message: str) -> bool:
    """[y/N] confirmation for one destructive delete logging operation.
    Enter alone (or any of n/N) is No -- never destructive by omission.
    Anything else that isn't y/Y/n/N re-prompts instead of being silently
    interpreted either way. Ctrl-C/EOF cancel safely, matching the
    pre-existing topology case-collision confirmation's own convention
    (see _confirm() above) without touching that separate helper."""
    prompt = f"{message} [y/N]: "
    while True:
        print(prompt, end="", flush=True)
        try:
            answer = _read_confirmation_line().strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return False
        if answer in ("y", "Y"):
            return True
        if answer in ("n", "N", ""):
            return False
        print("Please enter y or n.")


def _require_interactive(args: dict) -> None:
    """`delete logging` requires a real [y/N] answer, which a multi-line
    paste can never safely supply (see execute_input_block()'s
    `interactive` flag) -- fail closed instead of either blocking the
    paste on stdin or silently treating the next pasted line as the
    answer."""
    if not args.get("_interactive", True):
        raise terminal.TerminalError(
            "delete logging requires interactive confirmation and cannot be run from multi-line paste."
        )


def _confirm_and_apply(initial_plan, message: str, build_plan) -> "terminal.DeletionPlan | None":
    """Shared confirm-then-re-preflight-then-apply flow for every
    destructive delete logging command (Step B.1 sections 10-12/51-52).
    `initial_plan` is what was already built (and used to compute
    `message`); `build_plan` is called again only after the user
    confirms, to catch anything that changed while they were deciding
    (a new log, a newly active writer, new directory contents) -- if the
    freshly-built plan differs at all from `initial_plan`, nothing is
    deleted and the caller is told to retry. Returns the applied plan, or
    None if the user declined or the target state changed underneath
    them (either way, a message has already been printed)."""
    if not _confirm_delete(message):
        print("Delete cancelled.")
        return None
    reconfirmed_plan = build_plan()
    if reconfirmed_plan != initial_plan:
        print("% Logging state changed while waiting for confirmation.")
        print("% No logs were deleted. Retry the command.")
        return None
    terminal.apply_deletion_plan(reconfirmed_plan)
    return reconfirmed_plan


def h_delete_logging_device_file(session: cfgmod.CliSession, args: dict) -> None:
    _require_interactive(args)
    device_id = args["device_id"]
    log_file = args["log_file"]
    build_plan = lambda: terminal.build_file_deletion_plan(device_id, log_file)
    plan = build_plan()
    message = f"Delete terminal log {device_id}/{log_file}?"
    if _confirm_and_apply(plan, message, build_plan) is not None:
        print(f"Deleted terminal log {device_id}/{log_file}.")


def h_delete_logging_device_all(session: cfgmod.CliSession, args: dict) -> None:
    _require_interactive(args)
    device_id = args["device_id"]
    build_plan = lambda: terminal.build_device_all_deletion_plan(device_id)
    plan = build_plan()
    count = len(plan.files)
    message = f"Delete all {count} terminal {_plural(count, 'log', 'logs')} for {device_id}?"
    if _confirm_and_apply(plan, message, build_plan) is not None:
        print(f"Deleted {count} terminal {_plural(count, 'log', 'logs')} for {device_id}.")


def h_delete_logging_device_directory(session: cfgmod.CliSession, args: dict) -> None:
    _require_interactive(args)
    device_id = args["device_id"]
    build_plan = lambda: terminal.build_device_directory_deletion_plan(device_id)
    plan = build_plan()
    count = len(plan.files)
    if count == 0:
        message = f"Delete empty logging directory for {device_id}?"
        success = f"Deleted logging directory for {device_id}."
    else:
        message = (
            f"Delete all {count} terminal {_plural(count, 'log', 'logs')} "
            f"and logging directory for {device_id}?"
        )
        success = (
            f"Deleted {count} terminal {_plural(count, 'log', 'logs')} "
            f"and logging directory for {device_id}."
        )
    if _confirm_and_apply(plan, message, build_plan) is not None:
        print(success)


def h_delete_logging_all(session: cfgmod.CliSession, args: dict) -> None:
    _require_interactive(args)
    build_plan = terminal.build_global_all_deletion_plan
    plan = build_plan()
    count = len(plan.files)
    message = f"Delete all {count} terminal {_plural(count, 'log', 'logs')}?"
    if _confirm_and_apply(plan, message, build_plan) is not None:
        print(f"Deleted {count} terminal {_plural(count, 'log', 'logs')}.")


def h_delete_logging_all_directory(session: cfgmod.CliSession, args: dict) -> None:
    _require_interactive(args)
    build_plan = terminal.build_global_directory_deletion_plan
    plan = build_plan()
    file_count = len(plan.files)
    dir_count = len(plan.directories)
    message = (
        f"Delete all {file_count} terminal {_plural(file_count, 'log', 'logs')} and "
        f"{dir_count} device log {_plural(dir_count, 'directory', 'directories')}?"
    )
    if _confirm_and_apply(plan, message, build_plan) is not None:
        print(
            f"Deleted {file_count} terminal {_plural(file_count, 'log', 'logs')} and "
            f"{dir_count} device log {_plural(dir_count, 'directory', 'directories')}."
        )


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


def _group_unresolved_neighbors(unresolved: list) -> dict:
    grouped: dict = {}
    for obs in unresolved:
        grouped.setdefault(obs.remote_device_id_raw, []).append(obs)
    return grouped


def render_discovery_summary(result: "discovery.DiscoveryResult") -> str:
    grouped_unresolved = _group_unresolved_neighbors(result.unresolved)
    lines = [
        "Discovery complete.",
        "",
        f"  Access-info:          {result.access_info_name}",
        f"  IOS XR targets:       {result.iosxr_target_count}",
        f"  IOS XE targets:       {result.iosxe_target_count}",
        f"  IOS targets:          {result.ios_target_count}",
        f"  Connected:            {result.connected_count}",
        f"  LLDP observations:    {result.observation_count}",
        f"  CDP observations:     {result.cdp_observation_count}",
        f"  Managed links:        {len(result.managed_links)}",
        f"  Unresolved neighbors: {len(grouped_unresolved)}",
        f"  L3 enrichment:        {result.l3_enriched_device_count}/{result.connected_count} devices, "
        f"{result.l3_interface_count} interfaces",
        f"  Topology candidate:   {result.default_topology_name}",
    ]
    if result.l3_warnings:
        lines.append("")
        lines.append(f"L3 enrichment warnings ({len(result.l3_warnings)}):")
        for warning in result.l3_warnings:
            lines.append(f"  {warning}")
    if result.conflicts:
        lines.append("")
        lines.append(f"Protocol/link reconciliation conflicts ({len(result.conflicts)}, not added):")
        for conflict in result.conflicts:
            a_dev, a_intf = conflict.endpoint_a
            b_dev, b_intf = conflict.endpoint_b
            if conflict.endpoint_a == conflict.endpoint_b:
                lines.append(
                    f"  {a_dev} {a_intf}: conflicting neighbor observations "
                    f"({conflict.observation_a.source} -> {conflict.observation_a.remote_device_id_raw} "
                    f"vs {conflict.observation_b.source} -> {conflict.observation_b.remote_device_id_raw})"
                )
            else:
                lines.append(f"  {a_dev} {a_intf} <-> {b_dev} {b_intf}: inconsistent reciprocal observation")
    if grouped_unresolved:
        lines.append("")
        lines.append("Unresolved neighbors:")
        for raw_id, observations in grouped_unresolved.items():
            lines.append(f"  {raw_id}")
            for obs in observations:
                capability = ",".join(obs.capabilities) if obs.capabilities else "-"
                lines.append(
                    f"    {obs.local_device_id} {obs.local_interface} -> {obs.remote_port_id} ({capability})"
                )
    return "\n".join(lines)


def h_global_discover_topology(session: cfgmod.CliSession, args: dict) -> None:
    target_name = discovery.resolve_default_topology_name(session.lab_root)
    ok, message = session.can_switch_definition("topology", target_name)
    if not ok:
        print(f"% {message}")
        return
    print(f"Discovering topology from access-info '{target_name}'...")
    result = discovery.discover_topology(session.lab_root)
    session.apply_discovery_result(result)
    print(render_discovery_summary(result))


def h_global_access_info(session: cfgmod.CliSession, args: dict) -> None:
    name = args["name"]
    ok, message = session.can_switch_definition("access_info", name)
    if not ok:
        print(f"% {message}")
        return
    session.enter_access_info_definition(name)


def h_global_no_access_info(session: cfgmod.CliSession, args: dict) -> None:
    session.remove_definition("access_info", args["name"])


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


def h_global_no_topology(session: cfgmod.CliSession, args: dict) -> None:
    session.remove_topology_definition(args["name"])


def h_global_scenario(session: cfgmod.CliSession, args: dict) -> None:
    name = args["name"]
    ok, message = session.can_switch_definition("scenario", name)
    if not ok:
        print(f"% {message}")
        return
    session.enter_scenario_definition(name)


def h_global_no_scenario(session: cfgmod.CliSession, args: dict) -> None:
    session.remove_definition("scenario", args["name"])


def h_global_reference(session: cfgmod.CliSession, args: dict) -> None:
    name = args["name"]
    ok, message = session.can_switch_definition("reference", name)
    if not ok:
        print(f"% {message}")
        return
    session.enter_reference_definition(name)


def h_global_no_reference(session: cfgmod.CliSession, args: dict) -> None:
    session.remove_definition("reference", args["name"])


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


def h_access_info_remove_device(session: cfgmod.CliSession, args: dict) -> None:
    session.remove_device(args["name"])


def h_access_info_remove_jump_host(session: cfgmod.CliSession, args: dict) -> None:
    session.remove_jump_host(args["name"])


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


def h_access_device_clear_type(session: cfgmod.CliSession, args: dict) -> None:
    session.clear_device_field("type")


def h_access_device_clear_address(session: cfgmod.CliSession, args: dict) -> None:
    session.clear_device_field("address")


def h_access_device_clear_transport(session: cfgmod.CliSession, args: dict) -> None:
    session.clear_device_field("transport")


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


def h_access_jump_host_clear_type(session: cfgmod.CliSession, args: dict) -> None:
    session.clear_jump_host_field("type")


def h_access_jump_host_clear_address(session: cfgmod.CliSession, args: dict) -> None:
    session.clear_jump_host_field("address")


def h_access_jump_host_clear_transport(session: cfgmod.CliSession, args: dict) -> None:
    session.clear_jump_host_field("transport")


def h_access_jump_host_clear_username(session: cfgmod.CliSession, args: dict) -> None:
    session.clear_jump_host_field("username")


def h_access_jump_host_clear_password(session: cfgmod.CliSession, args: dict) -> None:
    session.clear_jump_host_field("password")


def h_access_jump_host_clear_port(session: cfgmod.CliSession, args: dict) -> None:
    session.clear_jump_host_field("port")


HANDLERS: dict[str, Callable[[cfgmod.CliSession, dict], None]] = {
    "exec.configure": h_exec_configure,
    "exec.show_running_config": h_show_running_config,
    "exec.show_running_config_access_info": h_show_running_config_access_info,
    "exec.show_running_config_topology": h_show_running_config_topology,
    "exec.show_running_config_scenario": h_show_running_config_scenario,
    "exec.show_running_config_reference": h_show_running_config_reference,
    "exec.show_running_config_reference_name": h_show_running_config_reference_name,
    "exec.show_version": h_show_version,
    "exec.monitor_terminal": h_monitor_terminal,
    "exec.show_logging": h_show_logging,
    "exec.show_logging_summary": h_show_logging_summary,
    "exec.show_logging_device": h_show_logging_device,
    "exec.show_logging_device_file": h_show_logging_device_file,
    "exec.delete_logging_all": h_delete_logging_all,
    "exec.delete_logging_all_directory": h_delete_logging_all_directory,
    "exec.delete_logging_device_all": h_delete_logging_device_all,
    "exec.delete_logging_device_directory": h_delete_logging_device_directory,
    "exec.delete_logging_device_file": h_delete_logging_device_file,
    "exec.help": h_help,
    "exec.help_topic": h_help_topic,
    "exec.exit": h_exec_exit,
    "exec.quit": h_exec_exit,
    "global.running_config": h_global_running_config,
    "global.discover_topology": h_global_discover_topology,
    "global.access_info": h_global_access_info,
    "global.topology": h_global_topology,
    "global.no_topology": h_global_no_topology,
    "global.no_access_info": h_global_no_access_info,
    "global.no_scenario": h_global_no_scenario,
    "global.no_reference": h_global_no_reference,
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
    "access_info.remove_device": h_access_info_remove_device,
    "access_info.remove_jump_host": h_access_info_remove_jump_host,
    "access_device.set_type": h_access_device_set_type,
    "access_device.set_address": h_access_device_set_address,
    "access_device.set_transport": h_access_device_set_transport,
    "access_device.set_port": h_access_device_set_port,
    "access_device.set_username": h_access_device_set_username,
    "access_device.set_password": h_access_device_set_password,
    "access_device.set_jump_host": h_access_device_set_jump_host,
    "access_device.clear_type": h_access_device_clear_type,
    "access_device.clear_address": h_access_device_clear_address,
    "access_device.clear_transport": h_access_device_clear_transport,
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
    "access_jump_host.clear_type": h_access_jump_host_clear_type,
    "access_jump_host.clear_address": h_access_jump_host_clear_address,
    "access_jump_host.clear_transport": h_access_jump_host_clear_transport,
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
            # Step D.1: the running-config selection model footer is scoped
            # to exactly the spaced "no ?" context in running mode -- never
            # the attached "no?", never any other mode/token. Presentation
            # only: it adds no grammar candidate and cannot affect Tab
            # completion, parsing, or command history.
            if _should_show_running_no_footer(session.mode, text_before):
                print()
                print(_render_running_config_selection_model())

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


def execute_command_line(session: cfgmod.CliSession, line: str, *, interactive: bool = True) -> bool:
    """Parse and execute exactly one physical command line against the
    session's current authoritative mode/candidate state, exactly as a
    manually typed line would be. Used both for ordinary single-line input
    and for each physical line of a multi-line paste: a line that changes
    mode or candidate state (device/exit/root/end/clear/commit/...) mutates
    `session` in place, so the *next* call always sees the resulting state
    -- there is no separate paste-side notion of "current mode". Returns
    True on success, False if a parse or handler error was printed.

    `interactive` (default True, matching every pre-existing call site)
    is only ever False for a physical line inside a multi-line paste (see
    execute_input_block()) -- it is threaded into the handler's `args` as
    `"_interactive"`, a synthetic key every handler except the delete
    logging ones ignores, so a confirmation-requiring command can fail
    closed instead of ever blocking on stdin (or worse, misreading the
    next pasted line as its answer) when run from a paste.

    Propagates `_ExitCli` (EXEC `exit`/`quit`) uncaught, matching how a
    manually typed `exit` unwinds the REPL loop in `run()`."""
    result = grammar.parse(session.mode, line)
    if not result.ok:
        print_parse_error(result.error)
        return False

    handler = HANDLERS[result.action]
    args = dict(result.args or {})
    args["_interactive"] = interactive
    try:
        handler(session, args)
    except (
        cfgmod.ConfigError,
        cfgmod.CommitValidationError,
        lab.LabConfigError,
        editor.EditorError,
        terminal.TerminalError,
        discovery.DiscoveryError,
    ) as exc:
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
    single-line input is executed exactly as before, unchanged -- a single
    manually typed "!" is not special-cased here at all and reaches
    execute_command_line()/grammar.parse() exactly as before this task. A
    multi-line input is split into normalized physical command lines and
    executed sequentially, stopping at the first error -- lines already
    applied stay in the candidate (no rollback), and nothing here or in
    execute_command_line() ever calls commit implicitly. A "!" physical
    line is the one exception to "every line goes through
    execute_command_line()": it is dispatched to
    _apply_structural_bang() instead, per session.mode *at that point* in
    the sequence -- same authoritative re-read-after-every-line model as
    every other mode-changing line here.

    Propagates `_ExitCli` uncaught, so a pasted `exit`/`quit` at EXEC level
    unwinds the REPL loop exactly like a manually typed one."""
    if "\n" in _normalize_newlines(text):
        for line in _split_pasted_command_lines(text):
            if line == "!":
                _apply_structural_bang(session)
                continue
            if not execute_command_line(session, line, interactive=False):
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
