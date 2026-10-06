"""Candidate configuration session for the Network Lab CLI.

Two independent candidate scopes exist, and `clear`/`commit` always act on
both together:

- the running-config candidate (`settings_candidate`): which topology,
  scenario, and references MCP will use once committed.
- at most one definition candidate at a time (`definition_kind` /
  `definition_candidate`): a topology, access-info, scenario, or reference
  *definition* being created or edited. Selecting a different definition
  while the current one is dirty is blocked, mirroring the original
  topology-switch guard; moving into or out of running-config mode is not,
  since it is an independent scope.

Everything here is memory-only until commit() writes changed YAML to disk.
The validators in `network_lab_mcp.lab` remain the single source of
truth for structural validity; this module does not duplicate those rules.
It only adds the narrow, CLI-specific case-only topology-name collision
safety check, which is not a validator replacement.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from network_lab_mcp import discovery, lab


class ConfigError(Exception):
    """A clear, user-facing error raised while mutating candidate configuration."""


class CommitValidationError(Exception):
    """Raised when commit() validation fails. No disk writes have occurred."""

    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


def find_case_only_collision(entered_name: str, existing_names: list[str]) -> Optional[str]:
    """Return an existing name that differs from `entered_name` only by
    letter case, or None.

    This is a narrow safety check, not fuzzy matching: any other difference
    (including '-' vs '_' or added/removed characters) never triggers it,
    and an exact match is not a collision (it is a normal existing-object
    selection).
    """
    if entered_name in existing_names:
        return None
    folded = entered_name.casefold()
    for existing in existing_names:
        if existing != entered_name and existing.casefold() == folded:
            return existing
    return None


@dataclass
class TopologyPlan:
    kind: str  # "select_existing" | "case_collision" | "create_new"
    name: str
    existing: Optional[str] = None


def new_topology_data(name: str) -> dict:
    return {"name": name, "description": "", "devices": {}, "links": []}


def new_access_info_data(name: str) -> dict:
    return {"name": name, "devices": {}}


def new_scenario_data(name: str) -> dict:
    return {"name": name, "description": "", "objectives": []}


def new_reference_data(name: str) -> dict:
    return {"name": name, "description": "", "guidance": []}


DEVICE_FIELD_ORDER = ("type", "address", "transport", "port", "username", "password", "jump_host")
JUMP_HOST_FIELD_ORDER = ("type", "address", "transport", "port", "username", "password")

# Which config mode "exit" moves up to. Only "global" is missing here --
# its exit/end is a guarded jump straight to EXEC, handled separately.
_EXIT_PARENT_MODE = {
    "running": "global",
    "topology": "global",
    "access_info": "global",
    "scenario": "global",
    "reference": "global",
    "device": "topology",
    "access_device": "access_info",
    "access_jump_host": "access_info",
}

_DEFINITION_LOADERS: dict[str, tuple[Callable, Callable, Callable]] = {
    "topology": (lab.topology_exists, lab.load_topology, new_topology_data),
    "access_info": (lab.access_info_exists, lab.load_access_info, new_access_info_data),
    "scenario": (lab.scenario_exists, lab.load_scenario, new_scenario_data),
    "reference": (lab.reference_exists, lab.load_reference, new_reference_data),
}

_DEFINITION_VALIDATORS: dict[str, Callable] = {
    "topology": lab.validate_topology_data,
    "access_info": lab.validate_access_info_data,
    "scenario": lab.validate_scenario_data,
    "reference": lab.validate_reference_data,
}

_DEFINITION_WRITERS: dict[str, Callable] = {
    "topology": lab.write_topology,
    "access_info": lab.write_access_info,
    "scenario": lab.write_scenario,
    "reference": lab.write_reference,
}

# Every definition kind supports candidate deletion (`no <kind>
# <name>`, see remove_definition()/commit() below) -- one entry per kind,
# mirroring _DEFINITION_LOADERS/_DEFINITION_WRITERS exactly.
_DEFINITION_DELETERS: dict[str, Callable] = {
    "topology": lab.delete_topology,
    "access_info": lab.delete_access_info,
    "scenario": lab.delete_scenario,
    "reference": lab.delete_reference,
}

# Used by remove_definition() to check (before creating a deletion
# candidate) that `name` is an exact, existing, path-safe stored
# definition of the given kind -- the same check delete_*() itself
# re-applies at commit time (see lab.py's _stored_definition_is_deletable()).
_DEFINITION_DELETABILITY_CHECKS: dict[str, Callable] = {
    "topology": lab.topology_is_deletable,
    "access_info": lab.access_info_is_deletable,
    "scenario": lab.scenario_is_deletable,
    "reference": lab.reference_is_deletable,
}

# Human-readable kind labels for error messages, matching each kind's
# existing wording elsewhere in this module exactly (e.g.
# select_access_info()'s "Access information '<name>' does not exist.").
_DEFINITION_KIND_LABELS: dict[str, str] = {
    "topology": "Topology",
    "access_info": "Access information",
    "scenario": "Scenario",
    "reference": "Reference",
}


def load_committed_definition(kind: str, name: str, lab_root: Path) -> Optional[dict]:
    """Return the current committed-on-disk data for a definition of the
    given kind/name, or None if it does not exist yet.

    Always re-reads disk fresh -- never the in-memory candidate -- so a
    definition-scoped `show running-config` (see cli/main.py) reflects the
    real committed state, including one committed moments ago by this same
    session's own commit()."""
    exists_fn, load_fn, _ = _DEFINITION_LOADERS[kind]
    if not exists_fn(name, lab_root):
        return None
    return load_fn(name, lab_root)


class CliSession:
    """Mutable candidate-configuration state for one CLI process lifetime.

    Not imported by cli/grammar.py; cli/main.py builds a read-only
    grammar.CliContext from this session's data on every parse/completion/
    help call instead.
    """

    def __init__(self, lab_root: Path):
        self.lab_root = lab_root
        self.mode = "exec"
        self.committed_settings: Optional[dict] = None
        self.settings_candidate: Optional[dict] = None
        self.definition_kind: Optional[str] = None  # "topology" | "access_info" | "scenario" | "reference"
        self.definition_name: Optional[str] = None
        self.definition_original: Optional[dict] = None  # None => new/unsaved
        self.definition_candidate: Optional[dict] = None
        self.current_device_name: Optional[str] = None
        self.current_jump_host_name: Optional[str] = None

    # ---- scoped dirty state ----

    def settings_dirty(self) -> bool:
        return self.settings_candidate is not None and self.settings_candidate != self.committed_settings

    def definition_dirty(self) -> bool:
        if self.definition_kind is None:
            return False
        if self.definition_candidate is None:
            # A whole definition is prospectively deleted (currently only
            # topology definitions support this -- see
            # remove_topology_definition()). Always dirty: the final
            # state ("absent") always differs from a real committed
            # original -- remove_topology_definition() never leaves this
            # state set for a never-committed (definition_original is
            # None) candidate, which it discards outright instead.
            return True
        if self.definition_original is None:
            return True  # brand-new, never-committed definition
        return self.definition_candidate != self.definition_original

    def overall_dirty(self) -> bool:
        return self.settings_dirty() or self.definition_dirty()

    # ---- configure entry / full exit ----

    def enter_configure(self) -> None:
        self.committed_settings = lab.read_settings(self.lab_root)
        self.settings_candidate = copy.deepcopy(self.committed_settings)
        self.definition_kind = None
        self.definition_name = None
        self.definition_original = None
        self.definition_candidate = None
        self.current_device_name = None
        self.current_jump_host_name = None
        self.mode = "global"

    def reset_to_exec(self) -> None:
        """Return to EXEC, discarding all candidate state. By construction,
        EXEC mode always implies overall_dirty() is False."""
        self.mode = "exec"
        self.committed_settings = None
        self.settings_candidate = None
        self.definition_kind = None
        self.definition_name = None
        self.definition_original = None
        self.definition_candidate = None
        self.current_device_name = None
        self.current_jump_host_name = None

    def clear(self) -> None:
        """Discard every uncommitted change in the current configure
        session -- both the running-config candidate and the definition
        candidate -- restoring committed state. Never writes disk, never
        returns to EXEC. A brand-new (never-committed) definition candidate
        is discarded entirely, not "reset to empty"."""
        if self.committed_settings is not None:
            self.settings_candidate = copy.deepcopy(self.committed_settings)
        if self.definition_kind is not None:
            if self.definition_original is None:
                self.definition_kind = None
                self.definition_name = None
                self.definition_candidate = None
                self.current_device_name = None
                self.current_jump_host_name = None
            else:
                self.definition_candidate = copy.deepcopy(self.definition_original)
                if self.current_device_name is not None:
                    devices = self.definition_candidate.get("devices") or {}
                    if self.current_device_name not in devices:
                        self.current_device_name = None
                if self.current_jump_host_name is not None:
                    jump_hosts = self.definition_candidate.get("jump_hosts") or {}
                    if self.current_jump_host_name not in jump_hosts:
                        self.current_jump_host_name = None
        self._reconcile_mode_after_clear()

    def _reconcile_mode_after_clear(self) -> None:
        """After clear(), fall back to the nearest still-valid parent mode
        instead of lingering in a submode whose target no longer exists."""
        if self.definition_kind is None and self.mode in (
            "topology",
            "device",
            "access_info",
            "access_device",
            "access_jump_host",
            "scenario",
            "reference",
        ):
            self.mode = "global"
            self.current_device_name = None
            self.current_jump_host_name = None
            return
        if self.mode in ("device", "access_device") and self.current_device_name is None:
            self.mode = "topology" if self.mode == "device" else "access_info"
        if self.mode == "access_jump_host" and self.current_jump_host_name is None:
            self.mode = "access_info"

    # ---- definition switching guard (shared by topology/access-info/scenario/reference) ----

    def can_switch_definition(self, kind: str, name: str) -> tuple[bool, Optional[str]]:
        if self.definition_kind is None:
            return True, None
        if (self.definition_kind, self.definition_name) == (kind, name):
            return True, None
        if self.definition_dirty():
            return False, "Uncommitted changes exist. Use 'commit' or 'clear' before switching."
        return True, None

    def _enter_definition(self, kind: str, name: str, mode: str) -> None:
        exists_fn, load_fn, new_data_fn = _DEFINITION_LOADERS[kind]
        if not (self.definition_kind == kind and self.definition_name == name and self.definition_candidate is not None):
            if exists_fn(name, self.lab_root):
                data = load_fn(name, self.lab_root)
                self.definition_original = copy.deepcopy(data)
                self.definition_candidate = copy.deepcopy(data)
            else:
                self.definition_original = None
                self.definition_candidate = new_data_fn(name)
            self.definition_kind = kind
            self.definition_name = name
        self.current_device_name = None
        self.mode = mode

    # ---- topology definition (with the case-only collision safeguard) ----

    def plan_topology_definition(self, name: str) -> TopologyPlan:
        if self.definition_kind == "topology" and self.definition_name == name:
            return TopologyPlan("select_existing", name)
        existing_names = lab.list_topology_names(self.lab_root)
        if name in existing_names:
            return TopologyPlan("select_existing", name)
        collision = find_case_only_collision(name, existing_names)
        if collision is not None:
            return TopologyPlan("case_collision", name, collision)
        return TopologyPlan("create_new", name)

    def apply_topology_definition_plan(self, plan: TopologyPlan) -> None:
        if plan.kind == "case_collision":
            raise ConfigError("A case-only topology collision must be confirmed before it can be applied.")
        self._enter_definition("topology", plan.name, "topology")

    def remove_definition(self, kind: str, name: str) -> None:
        """`no <kind> <name>` (global configuration only): candidate
        deletion of one stored definition -- never an immediate
        filesystem operation (see commit()). Shared by all four
        definition kinds (topology/access_info/scenario/reference).
        Participates in the same "at most one dirty definition candidate
        at a time" rule as every other definition mutation
        (can_switch_definition()); deletion counts as a definition
        mutation just like editing, so a different dirty definition (of
        *any* kind) blocks it, and a pending deletion blocks
        editing/creating/deleting a *different* definition, exactly like
        the existing definition-switching guard. `topology test_lab` and
        `access-info test_lab` are different candidate identities
        (`definition_kind` + `definition_name`) even though the name
        string matches.

        The candidate represents deletion as `definition_candidate is
        None` while `definition_kind`/`definition_name`/
        `definition_original` stay set to the real committed definition
        being removed -- reusing `_enter_definition()`'s existing reload
        guard (`definition_candidate is not None`) means re-entering the
        *same* definition (e.g. `topology <name>`) automatically reloads
        and restores it, cancelling the pending deletion, with no
        changes needed there. `clear()`'s existing "restore from
        definition_original" branch cancels it the same way -- this
        never re-reads or re-renders a deleted definition's own content
        (private access-info fields included) beyond what `load_fn`
        itself needs to restore the candidate.

        Deleting the definition *currently open as a brand-new, never-
        committed candidate* (`definition_original is None`) is instead a
        net-zero cancellation: the whole candidate slot is discarded
        outright, exactly like `clear()` already treats that case, since
        there is nothing real to mark as prospectively absent."""
        if self.definition_kind == kind and self.definition_name == name:
            if self.definition_original is None:
                self.definition_kind = None
                self.definition_name = None
                self.definition_candidate = None
                self.current_device_name = None
                self.current_jump_host_name = None
                return
            self.definition_candidate = None
            self.current_device_name = None
            self.current_jump_host_name = None
            return
        ok, message = self.can_switch_definition(kind, name)
        if not ok:
            raise ConfigError(message)
        if not _DEFINITION_DELETABILITY_CHECKS[kind](name, self.lab_root):
            raise ConfigError(f"{_DEFINITION_KIND_LABELS[kind]} '{name}' does not exist.")
        _, load_fn, _ = _DEFINITION_LOADERS[kind]
        self.definition_original = load_fn(name, self.lab_root)
        self.definition_candidate = None
        self.definition_kind = kind
        self.definition_name = name
        self.current_device_name = None
        self.current_jump_host_name = None

    def remove_topology_definition(self, name: str) -> None:
        """`no topology <name>` -- see remove_definition(), the shared
        implementation this delegates to. Kept as its own method since
        existing tests/callers already use this exact name."""
        self.remove_definition("topology", name)

    def apply_discovery_result(self, result: "discovery.DiscoveryResult") -> None:
        """Turn a completed Discovery run into a topology candidate, using
        exactly the same open/create/merge machinery as a manually typed
        `topology <name>`: an existing committed topology's candidate base
        (description, unrelated devices/links) is preserved, and only the
        newly discovered managed devices/links are added -- never applied
        partially, and never itself committed or selected as
        active_topology (that stays the operator's explicit next step)."""
        plan = self.plan_topology_definition(result.default_topology_name)
        self.apply_topology_definition_plan(plan)
        devices, links = discovery.build_topology_devices_and_links(result, self.definition_candidate)
        self.definition_candidate["devices"] = devices
        self.definition_candidate["links"] = links

    def enter_access_info_definition(self, name: str) -> None:
        self._enter_definition("access_info", name, "access_info")

    def enter_scenario_definition(self, name: str) -> None:
        self._enter_definition("scenario", name, "scenario")

    def enter_reference_definition(self, name: str) -> None:
        self._enter_definition("reference", name, "reference")

    def replace_definition_candidate(self, data: dict) -> None:
        """Install `data` (typically the result of an external-editor round
        trip) as the definition candidate, after validating it with the
        same SSOT validator commit() will use -- an invalid edit never
        reaches the candidate."""
        _DEFINITION_VALIDATORS[self.definition_kind](self.definition_name, data)
        self.definition_candidate = data

    # ---- topology-mode mutation ----

    def set_topology_description(self, text: str) -> None:
        self.definition_candidate["description"] = text

    # ---- navigation: `root` jumps straight to global config, preserving
    # candidate state (never commits, never clears) ----

    def go_to_global(self) -> None:
        self.current_device_name = None
        self.current_jump_host_name = None
        self.mode = "global"

    # ---- device sub-editing, shared by topology (safe fields only) and
    # access-info (private connection fields) ----

    def enter_topology_device(self, name: str) -> None:
        devices = self.definition_candidate.setdefault("devices", {})
        if name not in devices:
            devices[name] = {}
        self.current_device_name = name
        self.mode = "device"

    def enter_access_info_device(self, name: str) -> None:
        devices = self.definition_candidate.setdefault("devices", {})
        if name not in devices:
            devices[name] = {}
        self.current_device_name = name
        self.mode = "access_device"

    def _device(self) -> dict:
        return self.definition_candidate["devices"][self.current_device_name]

    def set_device_field(self, field_name: str, value) -> None:
        self._device()[field_name] = value

    def clear_device_field(self, field_name: str) -> None:
        self._device().pop(field_name, None)

    def remove_device(self, name: str) -> None:
        """`no device <name>`: remove the whole device object from the
        candidate only. Never cascades to topology, tmux, Discovery, or any
        other definition -- access-info and topology are separate models."""
        devices = self.definition_candidate.get("devices") or {}
        if name not in devices:
            raise ConfigError(f"Device '{name}' does not exist.")
        del devices[name]

    # ---- jump-host sub-editing (access-info only: a reusable single-hop
    # OpenSSH ProxyJump endpoint, referenced by name from a device's own
    # optional 'jump_host' field) ----

    def enter_access_info_jump_host(self, name: str) -> None:
        jump_hosts = self.definition_candidate.setdefault("jump_hosts", {})
        if name not in jump_hosts:
            jump_hosts[name] = {}
        self.current_jump_host_name = name
        self.mode = "access_jump_host"

    def _jump_host(self) -> dict:
        return self.definition_candidate["jump_hosts"][self.current_jump_host_name]

    def set_jump_host_field(self, field_name: str, value) -> None:
        self._jump_host()[field_name] = value

    def clear_jump_host_field(self, field_name: str) -> None:
        self._jump_host().pop(field_name, None)

    def remove_jump_host(self, name: str) -> None:
        """`no jump-host <name>`: remove the whole jump-host object from the
        candidate only. Deliberately does not cascade -- a device still
        referencing this jump host is left as-is (a dangling reference for
        commit's existing validate_device_jump_host_references() to catch);
        fixing that reference is the operator's explicit next step, never
        an automatic side effect of this command."""
        jump_hosts = self.definition_candidate.get("jump_hosts") or {}
        if name not in jump_hosts:
            raise ConfigError(f"Jump host '{name}' does not exist.")
        del jump_hosts[name]

    # ---- running-config selection (topology/scenario/references MCP uses) ----

    def _definition_will_exist(self, kind: str, exists_fn: Callable, name: str) -> bool:
        """True if `name` is already committed, or is the definition
        currently being created/edited in this same configure session --
        selecting a definition you are actively authoring (not yet
        committed) is allowed, since running-config and definition editing
        are independent candidate scopes that can be open at once."""
        return bool(exists_fn(name, self.lab_root) or (self.definition_kind == kind and self.definition_name == name))

    def select_access_info(self, name: str) -> None:
        if not self._definition_will_exist("access_info", lab.access_info_exists, name):
            raise ConfigError(f"Access information '{name}' does not exist.")
        self.settings_candidate["active_access_info"] = name

    def clear_access_info_selection(self) -> None:
        """`no access-info`: remove the selection from the candidate
        (omission, not a sentinel value like "none"/"disabled") -- a
        legitimate fail-closed state once committed, not an error."""
        self.settings_candidate.pop("active_access_info", None)

    def select_topology(self, name: str) -> None:
        if not self._definition_will_exist("topology", lab.topology_exists, name):
            raise ConfigError(f"Topology '{name}' does not exist.")
        self.settings_candidate["active_topology"] = name

    def set_scenario(self, name: str) -> None:
        if not self._definition_will_exist("scenario", lab.scenario_exists, name):
            raise ConfigError(f"Scenario '{name}' does not exist.")
        self.settings_candidate["active_scenario"] = name

    def add_reference(self, name: str) -> None:
        refs = self.settings_candidate.setdefault("active_references", [])
        if name in refs:
            raise ConfigError(f"Reference '{name}' is already selected.")
        if not self._definition_will_exist("reference", lab.reference_exists, name):
            raise ConfigError(f"Reference '{name}' does not exist.")
        refs.append(name)

    def remove_reference(self, name: str) -> None:
        refs = self.settings_candidate.setdefault("active_references", [])
        if name not in refs:
            raise ConfigError(f"Reference '{name}' is not currently selected.")
        refs.remove(name)

    # ---- commit ----

    def commit(self) -> bool:
        """Validate and persist the candidate. Returns True if anything was
        written, False for a no-op ("No changes to commit."). Raises
        CommitValidationError (no disk writes at all) on validation failure.

        Definition files are written before settings.yaml, since the
        running-config selection may point at a definition just created or
        edited in this same commit."""
        if not self.overall_dirty():
            return False

        errors: list[str] = []
        definition_being_written = self.definition_kind is not None and self.definition_dirty()
        definition_being_deleted = definition_being_written and self.definition_candidate is None

        if definition_being_written and not definition_being_deleted:
            try:
                _DEFINITION_VALIDATORS[self.definition_kind](self.definition_name, self.definition_candidate)
            except lab.LabConfigError as exc:
                errors.append(str(exc))

        def _will_exist(kind: str, exists_fn: Callable, name: Optional[str]) -> bool:
            if not name:
                return False
            if definition_being_deleted and self.definition_kind == kind and self.definition_name == name:
                # This exact commit is what removes it -- never treated
                # as "will exist" even though the file is still on disk
                # right now (deletion hasn't happened yet).
                return False
            if exists_fn(name, self.lab_root):
                return True
            return definition_being_written and self.definition_kind == kind and self.definition_name == name

        def _existence_error(kind: str, exists_fn: Callable, name: Optional[str]) -> Optional[str]:
            """Shared by every active-reference check below: None if
            `name` is empty (an optional selection left unset) or will
            genuinely exist after this commit; otherwise a clear error --
            distinguishing "does not exist at all" from "this exact
            commit is what is deleting it" (never a silent cascade)."""
            if not name or _will_exist(kind, exists_fn, name):
                return None
            label = _DEFINITION_KIND_LABELS[kind]
            if definition_being_deleted and self.definition_kind == kind and self.definition_name == name:
                return f"Cannot remove {label.lower()} '{name}' because it is active in running-config."
            return f"{label} '{name}' does not exist."

        settings = self.settings_candidate

        access_info_name = settings.get("active_access_info")
        error = _existence_error("access_info", lab.access_info_exists, access_info_name)
        if error:
            errors.append(error)

        target_topology = settings.get("active_topology")
        if not target_topology:
            errors.append("Running configuration is missing a valid active topology.")
        else:
            error = _existence_error("topology", lab.topology_exists, target_topology)
            if error:
                errors.append(error)

        scenario = settings.get("active_scenario")
        if not scenario:
            errors.append("Running configuration is missing a valid active scenario.")
        else:
            error = _existence_error("scenario", lab.scenario_exists, scenario)
            if error:
                errors.append(error)

        for reference in settings.get("active_references") or []:
            error = _existence_error("reference", lab.reference_exists, reference)
            if error:
                errors.append(error)

        if errors:
            raise CommitValidationError(errors)

        if definition_being_written:
            if definition_being_deleted:
                _DEFINITION_DELETERS[self.definition_kind](self.definition_name, self.lab_root)
                self.definition_kind = None
                self.definition_name = None
                self.definition_original = None
                self.definition_candidate = None
            else:
                _DEFINITION_WRITERS[self.definition_kind](self.definition_name, self.definition_candidate, self.lab_root)
                self.definition_original = copy.deepcopy(self.definition_candidate)

        if self.settings_dirty():
            lab.write_settings(self.settings_candidate, self.lab_root)
            self.committed_settings = copy.deepcopy(self.settings_candidate)

        return True
