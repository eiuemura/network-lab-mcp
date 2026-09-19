"""Command grammar single source of truth for the Network Lab CLI.

For each CLI mode (EXEC, global configuration, topology configuration, device
configuration) this module builds one trie of fixed keywords and argument
slots. The same trie is used for parsing, unique fixed-keyword abbreviation
resolution, Tab/Ctrl-I completion, context-sensitive `?` help, dynamic
candidate lookup, `<cr>` eligibility, and invalid/incomplete/ambiguous
command reporting. There is no separate parser table, completion table, or
help table that could drift out of sync with this one.

Fixed CLI keywords (e.g. "configure", "topology", "transport") are matched
case-insensitively and support unique-prefix abbreviation, like IOS XR.
Object identifiers (topology/scenario/reference/device names) are matched
case-sensitively and are never abbreviated; that is enforced by never
attempting keyword-style matching against them at all -- an identifier
argument accepts whatever token the operator typed and defers existence
checks to the caller (see cli/config.py).

This module owns no mutable candidate/session state. Dynamic completion
providers are pure functions of an explicit, read-only CliContext supplied
by the caller (cli/main.py) on every call, so this module never imports
cli/config.py and cli/config.py never needs to import this module's runtime
state -- there is no grammar/config circular dependency.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Optional

# --------------------------------------------------------------------------
# Read-only runtime context for dynamic providers
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CliContext:
    """Read-only snapshot of CLI state used only for dynamic completion/help.

    Built fresh by cli/main.py before every parse/completion/help call.
    Dynamic providers must treat every field as read-only.
    """

    topology_names: tuple[str, ...] = ()
    scenario_names: tuple[str, ...] = ()
    reference_names: tuple[str, ...] = ()
    candidate_reference_names: tuple[str, ...] = ()
    topology_candidate_device_names: tuple[str, ...] = ()


Provider = Callable[[CliContext, str], list[str]]


@dataclass(frozen=True)
class ValidationOutcome:
    ok: bool
    normalized: Optional[str] = None
    error: Optional[str] = None


Validator = Callable[[str], ValidationOutcome]


def _ok(normalized: str) -> ValidationOutcome:
    return ValidationOutcome(True, normalized, None)


def _fail(message: str) -> ValidationOutcome:
    return ValidationOutcome(False, None, message)


def validate_freeform(value: str) -> ValidationOutcome:
    """Accept any token as-is. Used for object identifiers and free-form
    device fields (address/username/type/description); existence and
    semantic checks are the caller's responsibility, not the grammar's."""
    return _ok(value)


def validate_transport(value: str) -> ValidationOutcome:
    lowered = value.lower()
    if lowered in ("ssh", "telnet"):
        return _ok(lowered)
    return _fail("Invalid transport. Expected ssh or telnet.")


def validate_port(value: str) -> ValidationOutcome:
    if not value.isdigit():
        return _fail("Port must be between 1 and 65535.")
    port = int(value)
    if not (1 <= port <= 65535):
        return _fail("Port must be between 1 and 65535.")
    return _ok(str(port))


# --------------------------------------------------------------------------
# Dynamic providers (pure, read-only: provider(context, prefix) -> candidates)
# --------------------------------------------------------------------------


def provide_topology_names(ctx: CliContext, prefix: str) -> list[str]:
    return [n for n in ctx.topology_names if n.startswith(prefix)]


def provide_scenario_names(ctx: CliContext, prefix: str) -> list[str]:
    return [n for n in ctx.scenario_names if n.startswith(prefix)]


def provide_reference_names(ctx: CliContext, prefix: str) -> list[str]:
    return [n for n in ctx.reference_names if n.startswith(prefix)]


def provide_candidate_reference_names(ctx: CliContext, prefix: str) -> list[str]:
    return [n for n in ctx.candidate_reference_names if n.startswith(prefix)]


def provide_device_names(ctx: CliContext, prefix: str) -> list[str]:
    return [n for n in ctx.topology_candidate_device_names if n.startswith(prefix)]


def provide_transport_values(ctx: CliContext, prefix: str) -> list[str]:
    lowered = prefix.lower()
    return [v for v in ("ssh", "telnet") if v.startswith(lowered)]


# --------------------------------------------------------------------------
# Grammar tree
# --------------------------------------------------------------------------


@dataclass
class Argument:
    name: str
    description: str
    validate: Validator = validate_freeform
    provider: Optional[Provider] = None
    rest_of_line: bool = False
    hint: str = ""
    sensitive: bool = False
    enumerate_when_empty: bool = False
    value_help: dict[str, str] = field(default_factory=dict)

    def display_hint(self) -> str:
        return self.hint or f"<{self.name}>"


@dataclass
class CommandSpec:
    action: str
    summary: str


@dataclass
class Node:
    description: str = ""
    literal_children: dict[str, "Node"] = field(default_factory=dict)
    argument: Optional[Argument] = None
    argument_child: Optional["Node"] = None
    command: Optional[CommandSpec] = None

    def add_literal(self, keyword: str, description: str = "") -> "Node":
        key = keyword.lower()
        node = self.literal_children.get(key)
        if node is None:
            node = Node()
            self.literal_children[key] = node
        if description:
            node.description = description
        return node

    def add_argument(self, argument: Argument) -> "Node":
        self.argument = argument
        node = Node()
        self.argument_child = node
        return node

    def set_command(self, action: str, summary: str) -> None:
        self.command = CommandSpec(action, summary)


def _add_show_subtree(root: Node, mode: str, include_configuration: bool) -> None:
    show = root.add_literal("show", "Show information")
    running = show.add_literal("running-config", "Show committed lab configuration")
    running.set_command(f"{mode}.show_running_config", "Show committed lab configuration")
    if include_configuration:
        candidate = show.add_literal("configuration", "Show candidate configuration")
        candidate.set_command(f"{mode}.show_configuration", "Show candidate configuration")


def _add_common_subtree(root: Node, mode: str) -> None:
    commit = root.add_literal("commit", "Commit candidate configuration")
    commit.set_command(f"{mode}.commit", "Commit candidate configuration")
    abort = root.add_literal("abort", "Discard candidate configuration and return to EXEC")
    abort.set_command(f"{mode}.abort", "Discard candidate configuration and return to EXEC")
    end = root.add_literal("end", "Return to EXEC mode")
    end.set_command(f"{mode}.end", "Return to EXEC mode")
    exit_node = root.add_literal("exit", "Exit one configuration level")
    exit_node.set_command(f"{mode}.exit", "Exit one configuration level")
    help_node = root.add_literal("help", "Display help")
    help_node.set_command(f"{mode}.help", "Display help")


def _build_exec_root() -> Node:
    root = Node()
    configure = root.add_literal("configure", "Enter configuration mode")
    configure.set_command("exec.configure", "Enter configuration mode")
    terminal_alias = configure.add_literal("terminal", "Enter configuration mode")
    terminal_alias.set_command("exec.configure", "Enter configuration mode")

    _add_show_subtree(root, "exec", include_configuration=False)

    help_node = root.add_literal("help", "Display help")
    help_node.set_command("exec.help", "Display help")

    exit_node = root.add_literal("exit", "Exit the CLI")
    exit_node.set_command("exec.exit", "Exit the CLI")

    quit_node = root.add_literal("quit", "Exit the CLI")
    quit_node.set_command("exec.quit", "Exit the CLI")

    return root


def _build_global_root() -> Node:
    root = Node()

    topology_arg = Argument(
        "name",
        "Topology name",
        provider=provide_topology_names,
        hint="<name>",
    )
    topology_node = root.add_literal("topology", "Select or create a topology")
    topology_next = topology_node.add_argument(topology_arg)
    topology_next.set_command("global.topology", "Select or create a topology")

    scenario_arg = Argument(
        "name",
        "Scenario name",
        provider=provide_scenario_names,
        hint="<name>",
    )
    scenario_node = root.add_literal("scenario", "Select the active scenario")
    scenario_next = scenario_node.add_argument(scenario_arg)
    scenario_next.set_command("global.scenario", "Select the active scenario")

    reference_arg = Argument(
        "name",
        "Reference name",
        provider=provide_reference_names,
        hint="<name>",
    )
    reference_node = root.add_literal("reference", "Add an active reference")
    reference_next = reference_node.add_argument(reference_arg)
    reference_next.set_command("global.reference_add", "Add an active reference")

    no_node = root.add_literal("no", "Negate a configuration item")
    no_reference_node = no_node.add_literal("reference", "Remove an active reference")
    no_reference_arg = Argument(
        "name",
        "Currently active reference name",
        provider=provide_candidate_reference_names,
        hint="<name>",
    )
    no_reference_next = no_reference_node.add_argument(no_reference_arg)
    no_reference_next.set_command("global.reference_remove", "Remove an active reference")

    _add_show_subtree(root, "global", include_configuration=True)
    _add_common_subtree(root, "global")
    return root


def _build_topology_root() -> Node:
    root = Node()

    description_arg = Argument(
        "text",
        "Free-form topology description",
        rest_of_line=True,
        hint="<text>",
    )
    description_node = root.add_literal("description", "Set the topology description")
    description_next = description_node.add_argument(description_arg)
    description_next.set_command("topology.description", "Set the topology description")

    device_arg = Argument(
        "name",
        "Device name",
        provider=provide_device_names,
        hint="<name>",
    )
    device_node = root.add_literal("device", "Select or create a device")
    device_next = device_node.add_argument(device_arg)
    device_next.set_command("topology.device", "Select or create a device")

    _add_show_subtree(root, "topology", include_configuration=True)
    _add_common_subtree(root, "topology")
    return root


def _build_device_root() -> Node:
    root = Node()

    def add_field(
        keyword: str,
        description: str,
        action: str,
        *,
        validate: Validator = validate_freeform,
        provider: Optional[Provider] = None,
        hint: str = "<value>",
        sensitive: bool = False,
        enumerate_when_empty: bool = False,
        value_help: Optional[dict[str, str]] = None,
    ) -> None:
        argument = Argument(
            "value",
            description,
            validate=validate,
            provider=provider,
            hint=hint,
            sensitive=sensitive,
            enumerate_when_empty=enumerate_when_empty,
            value_help=value_help or {},
        )
        node = root.add_literal(keyword, description)
        next_node = node.add_argument(argument)
        next_node.set_command(action, description)

    add_field("type", "Set the device type", "device.set_type")
    add_field("address", "Set the device management address", "device.set_address")
    add_field(
        "transport",
        "Set the device transport",
        "device.set_transport",
        validate=validate_transport,
        provider=provide_transport_values,
        hint="<ssh|telnet>",
        enumerate_when_empty=True,
        value_help={"ssh": "Use SSH transport", "telnet": "Use Telnet transport"},
    )
    add_field(
        "port",
        "Set the device port",
        "device.set_port",
        validate=validate_port,
        hint="<1-65535>",
    )
    add_field("username", "Set the device username", "device.set_username")
    add_field(
        "password",
        "Set the device password",
        "device.set_password",
        sensitive=True,
        hint="<password>",
    )

    no_node = root.add_literal("no", "Negate a device field")
    for keyword, action, description in (
        ("username", "device.clear_username", "Clear the device username"),
        ("password", "device.clear_password", "Clear the device password"),
        ("port", "device.clear_port", "Clear the device port"),
    ):
        field_node = no_node.add_literal(keyword, description)
        field_node.set_command(action, description)

    _add_show_subtree(root, "device", include_configuration=True)
    _add_common_subtree(root, "device")
    return root


MODE_ROOTS: dict[str, Node] = {
    "exec": _build_exec_root(),
    "global": _build_global_root(),
    "topology": _build_topology_root(),
    "device": _build_device_root(),
}


# --------------------------------------------------------------------------
# Tokenization
# --------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"\S+")


def tokenize(line: str) -> list[tuple[str, int, int]]:
    """Split a line into (text, start, end) tokens. No quoting is supported;
    a free-form rest-of-line argument (e.g. `description`) is handled
    separately by consuming raw text instead of re-joining tokens."""
    return [(m.group(0), m.start(), m.end()) for m in _TOKEN_RE.finditer(line)]


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


@dataclass
class ParseError:
    kind: str  # "unknown" | "ambiguous" | "incomplete" | "invalid"
    token: str = ""
    span: Optional[tuple[int, int]] = None
    detail: str = ""


@dataclass
class ParseResult:
    ok: bool
    action: Optional[str] = None
    args: Optional[dict[str, str]] = None
    error: Optional[ParseError] = None


def _match_literal(node: Node, token: str) -> tuple[Optional[Node], list[str]]:
    """Resolve `token` against `node`'s fixed keywords: exact case-insensitive
    match wins immediately; otherwise a *unique* case-insensitive prefix
    match wins. Returns (matched_child, []) on a unique match, or
    (None, matches) where `matches` is empty (no match) or has 2+ entries
    (ambiguous)."""
    lowered = token.lower()
    if lowered in node.literal_children:
        return node.literal_children[lowered], []
    matches = [kw for kw in node.literal_children if kw.startswith(lowered)]
    if len(matches) == 1:
        return node.literal_children[matches[0]], []
    return None, matches


def parse(mode: str, line: str) -> ParseResult:
    tokens = tokenize(line)
    node = MODE_ROOTS[mode]
    args: dict[str, str] = {}

    i = 0
    while i < len(tokens):
        text, start, end = tokens[i]

        if node.argument is not None and not node.literal_children:
            argument = node.argument
            if argument.rest_of_line:
                value = line[start:]
                outcome = argument.validate(value)
                if not outcome.ok:
                    return ParseResult(False, error=ParseError("invalid", text, (start, len(line)), outcome.error or ""))
                args[argument.name] = outcome.normalized if outcome.normalized is not None else value
                node = node.argument_child
                i = len(tokens)
                break
            outcome = argument.validate(text)
            if not outcome.ok:
                return ParseResult(False, error=ParseError("invalid", text, (start, end), outcome.error or ""))
            args[argument.name] = outcome.normalized if outcome.normalized is not None else text
            node = node.argument_child
            i += 1
            continue

        if node.literal_children:
            child, matches = _match_literal(node, text)
            if child is not None:
                node = child
                i += 1
                continue
            if matches:
                return ParseResult(False, error=ParseError("ambiguous", text, (start, end)))
            if node.argument is not None:
                argument = node.argument
                outcome = argument.validate(text)
                if not outcome.ok:
                    return ParseResult(False, error=ParseError("invalid", text, (start, end), outcome.error or ""))
                args[argument.name] = outcome.normalized if outcome.normalized is not None else text
                node = node.argument_child
                i += 1
                continue
            if i == 0:
                return ParseResult(False, error=ParseError("unknown", text, (start, end)))
            return ParseResult(False, error=ParseError("invalid", text, (start, end)))

        # Dead end: this node accepts neither a further keyword nor an
        # argument, so any extra token here is invalid input.
        return ParseResult(False, error=ParseError("invalid", text, (start, end)))

    if node.command is not None:
        return ParseResult(True, action=node.command.action, args=args)
    return ParseResult(False, error=ParseError("incomplete"))


# --------------------------------------------------------------------------
# Completion and context-sensitive help
# --------------------------------------------------------------------------


@dataclass
class CompletionResult:
    candidates: list[str]
    replace_prefix: str


@dataclass
class HelpLine:
    token: str
    description: str


@dataclass
class HelpResult:
    lines: list[HelpLine]
    show_cr: bool
    partial: str


def split_for_completion(text_before_cursor: str) -> tuple[list[str], str]:
    """Split cursor-relative text into already-committed tokens and the
    partial token currently being typed (empty string if the cursor sits
    right after whitespace, i.e. at the start of a new token)."""
    tokens = tokenize(text_before_cursor)
    if text_before_cursor == "" or text_before_cursor[-1].isspace():
        return [t[0] for t in tokens], ""
    if not tokens:
        return [], ""
    return [t[0] for t in tokens[:-1]], tokens[-1][0]


def _walk_committed(mode: str, tokens: list[str]) -> Optional[Node]:
    """Walk already-committed tokens through the trie using the same
    resolution rules as parse(). Returns None if any committed token fails
    to resolve (broken prefix -> no completion/help context)."""
    node = MODE_ROOTS[mode]
    for text in tokens:
        if node.argument is not None and not node.literal_children:
            node = node.argument_child
            continue
        if node.literal_children:
            child, matches = _match_literal(node, text)
            if child is not None:
                node = child
                continue
            if node.argument is not None:
                node = node.argument_child
                continue
            return None
        return None
    return node


def complete(mode: str, text_before_cursor: str, ctx: CliContext) -> CompletionResult:
    committed, partial = split_for_completion(text_before_cursor)
    node = _walk_committed(mode, committed)
    if node is None:
        return CompletionResult([], partial)

    if node.argument is not None and not node.literal_children:
        argument = node.argument
        if argument.sensitive or argument.provider is None:
            return CompletionResult([], partial)
        return CompletionResult(sorted(argument.provider(ctx, partial)), partial)

    if node.literal_children:
        lowered = partial.lower()
        matches = [kw for kw in node.literal_children if kw.startswith(lowered)]
        return CompletionResult(matches, partial)

    return CompletionResult([], partial)


def help(mode: str, text_before_cursor: str, ctx: CliContext) -> HelpResult:
    committed, partial = split_for_completion(text_before_cursor)
    node = _walk_committed(mode, committed)
    if node is None:
        return HelpResult([], False, partial)

    if node.argument is not None and not node.literal_children:
        argument = node.argument
        lines: list[HelpLine] = []
        if argument.sensitive:
            lines.append(HelpLine(argument.display_hint(), argument.description))
        elif argument.enumerate_when_empty and partial == "":
            for value, description in argument.value_help.items():
                lines.append(HelpLine(value, description))
        elif partial != "" and argument.provider is not None:
            for value in sorted(argument.provider(ctx, partial)):
                lines.append(HelpLine(value, argument.description))
        else:
            lines.append(HelpLine(argument.display_hint(), argument.description))
        return HelpResult(lines, False, partial)

    lines = []
    lowered = partial.lower()
    for keyword, child in node.literal_children.items():
        if keyword.startswith(lowered):
            lines.append(HelpLine(keyword, child.description))
    show_cr = partial == "" and node.command is not None
    return HelpResult(lines, show_cr, partial)
