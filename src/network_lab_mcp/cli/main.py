"""Human-facing interactive Network Lab CLI.

Launched via ./run_cli.sh (`python3 -m network_lab_mcp.cli.main`). This is
the Human Configuration / Control Interface described in README.md: it edits
lab/settings.yaml and lab/topologies/*.yaml through a candidate -> commit
model, using an IOS XR-compatible interaction style built on top of the
command grammar in cli/grammar.py. It never speaks the MCP stdio protocol
and never performs network engineering reasoning -- that remains Claude
Code's job, driven by the committed configuration this CLI produces.

Responsibilities kept here: the REPL, prompt rendering, mode transitions,
prompt_toolkit integration (completion/help key bindings, history, line
editing), command dispatch, output rendering, and interactive confirmations.
Candidate mutation and commit/abort semantics live in cli/config.py; command
grammar, abbreviation, completion, and help all live in cli/grammar.py.
"""

from __future__ import annotations

import sys
from typing import Callable

from prompt_toolkit import PromptSession
from prompt_toolkit.application import run_in_terminal
from prompt_toolkit.history import History
from prompt_toolkit.key_binding import KeyBindings

from network_lab_mcp import lab
from network_lab_mcp.cli import config as cfgmod
from network_lab_mcp.cli import grammar

PASSWORD_MASK = "********"


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
    if session.mode == "topology":
        return f"network-lab(config-topology-{session.selected_topology_name})# "
    if session.mode == "device":
        return f"network-lab(config-device-{session.current_device_name})# "
    raise AssertionError(f"Unknown CLI mode: {session.mode!r}")


def build_context(session: cfgmod.CliSession) -> grammar.CliContext:
    lab_root = session.lab_root
    candidate_references: tuple[str, ...] = ()
    if session.settings_candidate is not None:
        candidate_references = tuple(session.settings_candidate.get("active_references") or [])
    device_names: tuple[str, ...] = ()
    if session.topology_candidate is not None:
        device_names = tuple((session.topology_candidate.get("devices") or {}).keys())
    return grammar.CliContext(
        topology_names=tuple(lab.list_topology_names(lab_root)),
        scenario_names=tuple(lab.list_scenario_names(lab_root)),
        reference_names=tuple(lab.list_reference_names(lab_root)),
        candidate_reference_names=candidate_references,
        topology_candidate_device_names=device_names,
    )


def _mask_device(device: dict) -> dict:
    masked = dict(device)
    if masked.get("password"):
        masked["password"] = PASSWORD_MASK
    return masked


def render_topology_block(data: dict) -> list[str]:
    lines = [f"topology {data.get('name', '')}"]
    description = data.get("description")
    if description:
        lines.append(f" description {description}")
    for device_name, device in (data.get("devices") or {}).items():
        lines.append(f" device {device_name}")
        masked = _mask_device(device or {})
        for field_name in cfgmod.DEVICE_FIELD_ORDER:
            value = masked.get(field_name)
            if value not in (None, ""):
                lines.append(f"  {field_name} {value}")
        lines.append(" !")
    lines.append("!")
    return lines


def render_running_config(session: cfgmod.CliSession) -> str:
    settings = lab.read_settings(session.lab_root)
    lines: list[str] = []
    topology_name = settings.get("active_topology")
    if topology_name:
        try:
            topology = lab.load_topology(topology_name, session.lab_root)
            lines.extend(render_topology_block(topology))
        except lab.LabConfigError as exc:
            lines.append(f"% {exc}")
    scenario_name = settings.get("active_scenario")
    if scenario_name:
        lines.append(f"scenario {scenario_name}")
    for reference in settings.get("active_references") or []:
        lines.append(f"reference {reference}")
    return "\n".join(lines)


def render_candidate_configuration(session: cfgmod.CliSession) -> str:
    lines: list[str] = []
    settings = session.settings_candidate or {}
    topology_name = settings.get("active_topology")
    if session.topology_candidate is not None:
        lines.extend(render_topology_block(session.topology_candidate))
    else:
        if topology_name:
            lines.append(f"active_topology {topology_name}")
        lines.append("! No topology edit context is currently selected.")
    scenario_name = settings.get("active_scenario")
    if scenario_name:
        lines.append(f"scenario {scenario_name}")
    for reference in settings.get("active_references") or []:
        lines.append(f"reference {reference}")
    return "\n".join(lines)


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
    try:
        changed = session.commit()
    except cfgmod.CommitValidationError as exc:
        for error in exc.errors:
            print(f"% {error}")
        return
    print("Commit complete." if changed else "No changes to commit.")
    session.mode = "exec"


def _guarded_leave_configure(session: cfgmod.CliSession) -> None:
    if session.overall_dirty():
        print("% Uncommitted changes exist. Use 'commit' or 'abort'.")
        return
    session.reset_to_exec()


# --------------------------------------------------------------------------
# Command handlers -- dispatched by grammar action id
# --------------------------------------------------------------------------


def h_exec_configure(session: cfgmod.CliSession, args: dict) -> None:
    session.enter_configure()


def h_exec_show_running_config(session: cfgmod.CliSession, args: dict) -> None:
    print(render_running_config(session))


def h_exec_help(session: cfgmod.CliSession, args: dict) -> None:
    print_help_result(grammar.help(session.mode, "", build_context(session)))


def h_exec_exit(session: cfgmod.CliSession, args: dict) -> None:
    raise _ExitCli()


def h_global_topology(session: cfgmod.CliSession, args: dict) -> None:
    name = args["name"]
    ok, message = session.can_switch_topology()
    if not ok:
        print(f"% {message}")
        return
    plan = session.plan_topology_selection(name)
    if plan.kind == "case_collision":
        confirmed = _confirm(
            f"% An existing topology '{plan.existing}' differs only by letter case.\n"
            f"Create a separate topology named '{plan.name}'? [yes/no]: "
        )
        if not confirmed:
            return
        plan = cfgmod.TopologyPlan("create_new", plan.name)
    session.apply_topology_plan(plan)


def h_global_scenario(session: cfgmod.CliSession, args: dict) -> None:
    session.set_scenario(args["name"])


def h_global_reference_add(session: cfgmod.CliSession, args: dict) -> None:
    session.add_reference(args["name"])


def h_global_reference_remove(session: cfgmod.CliSession, args: dict) -> None:
    session.remove_reference(args["name"])


def h_show_configuration(session: cfgmod.CliSession, args: dict) -> None:
    print(render_candidate_configuration(session))


def h_show_running_config(session: cfgmod.CliSession, args: dict) -> None:
    print(render_running_config(session))


def h_commit(session: cfgmod.CliSession, args: dict) -> None:
    _do_commit(session)


def h_abort(session: cfgmod.CliSession, args: dict) -> None:
    session.abort()


def h_end(session: cfgmod.CliSession, args: dict) -> None:
    _guarded_leave_configure(session)


def h_global_exit(session: cfgmod.CliSession, args: dict) -> None:
    _guarded_leave_configure(session)


def h_help(session: cfgmod.CliSession, args: dict) -> None:
    print_help_result(grammar.help(session.mode, "", build_context(session)))


def h_topology_description(session: cfgmod.CliSession, args: dict) -> None:
    session.set_topology_description(args["text"])


def h_topology_device(session: cfgmod.CliSession, args: dict) -> None:
    session.enter_device(args["name"])


def h_topology_exit(session: cfgmod.CliSession, args: dict) -> None:
    session.current_device_name = None
    session.mode = "global"


def h_device_set_type(session: cfgmod.CliSession, args: dict) -> None:
    session.set_device_field("type", args["value"])


def h_device_set_address(session: cfgmod.CliSession, args: dict) -> None:
    session.set_device_field("address", args["value"])


def h_device_set_transport(session: cfgmod.CliSession, args: dict) -> None:
    session.set_device_field("transport", args["value"])


def h_device_set_port(session: cfgmod.CliSession, args: dict) -> None:
    session.set_device_field("port", int(args["value"]))


def h_device_set_username(session: cfgmod.CliSession, args: dict) -> None:
    session.set_device_field("username", args["value"])


def h_device_set_password(session: cfgmod.CliSession, args: dict) -> None:
    session.set_device_field("password", args["value"])


def h_device_clear_username(session: cfgmod.CliSession, args: dict) -> None:
    session.clear_device_field("username")


def h_device_clear_password(session: cfgmod.CliSession, args: dict) -> None:
    session.clear_device_field("password")


def h_device_clear_port(session: cfgmod.CliSession, args: dict) -> None:
    session.clear_device_field("port")


def h_device_exit(session: cfgmod.CliSession, args: dict) -> None:
    session.current_device_name = None
    session.mode = "topology"


HANDLERS: dict[str, Callable[[cfgmod.CliSession, dict], None]] = {
    "exec.configure": h_exec_configure,
    "exec.show_running_config": h_exec_show_running_config,
    "exec.help": h_exec_help,
    "exec.exit": h_exec_exit,
    "exec.quit": h_exec_exit,
    "global.topology": h_global_topology,
    "global.scenario": h_global_scenario,
    "global.reference_add": h_global_reference_add,
    "global.reference_remove": h_global_reference_remove,
    "global.show_running_config": h_show_running_config,
    "global.show_configuration": h_show_configuration,
    "global.commit": h_commit,
    "global.abort": h_abort,
    "global.end": h_end,
    "global.exit": h_global_exit,
    "global.help": h_help,
    "topology.description": h_topology_description,
    "topology.device": h_topology_device,
    "topology.show_running_config": h_show_running_config,
    "topology.show_configuration": h_show_configuration,
    "topology.commit": h_commit,
    "topology.abort": h_abort,
    "topology.end": h_end,
    "topology.exit": h_topology_exit,
    "topology.help": h_help,
    "device.set_type": h_device_set_type,
    "device.set_address": h_device_set_address,
    "device.set_transport": h_device_set_transport,
    "device.set_port": h_device_set_port,
    "device.set_username": h_device_set_username,
    "device.set_password": h_device_set_password,
    "device.clear_username": h_device_clear_username,
    "device.clear_password": h_device_clear_password,
    "device.clear_port": h_device_clear_port,
    "device.show_running_config": h_show_running_config,
    "device.show_configuration": h_show_configuration,
    "device.commit": h_commit,
    "device.abort": h_abort,
    "device.end": h_end,
    "device.exit": h_device_exit,
    "device.help": h_help,
}


# --------------------------------------------------------------------------
# REPL
# --------------------------------------------------------------------------


def _make_key_bindings(session: cfgmod.CliSession) -> KeyBindings:
    kb = KeyBindings()

    @kb.add("?")
    def _(event) -> None:
        buffer = event.current_buffer
        text = buffer.document.text_before_cursor
        result = grammar.help(session.mode, text, build_context(session))

        def _show() -> None:
            print()
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
            line = prompt_session.prompt(prompt_text(session))
        except KeyboardInterrupt:
            # Cancel only the current partially typed input line; the
            # candidate configuration and current mode are untouched.
            continue
        except EOFError:
            if session.overall_dirty():
                print()
                print("% Uncommitted changes exist. Use 'commit' or 'abort'.")
                continue
            print()
            break

        if not line.strip():
            continue

        result = grammar.parse(session.mode, line)
        if not result.ok:
            print_parse_error(result.error)
            continue

        handler = HANDLERS[result.action]
        try:
            handler(session, result.args or {})
        except _ExitCli:
            break
        except (cfgmod.ConfigError, cfgmod.CommitValidationError, lab.LabConfigError) as exc:
            if isinstance(exc, cfgmod.CommitValidationError):
                for error in exc.errors:
                    print(f"% {error}")
            else:
                print(f"% {exc}")


def main() -> None:
    try:
        run()
    except lab.LabConfigError as exc:
        print(f"% {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
