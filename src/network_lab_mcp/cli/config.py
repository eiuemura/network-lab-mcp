"""Candidate configuration session for the Network Lab CLI.

Two independent candidate scopes exist, and `clear`/`commit` always act on
both together:

- the running-config candidate (`settings_candidate`): which topology,
  scenario, and references MCP will use once committed.
- at most one definition candidate at a time (`definition_kind` /
  `definition_candidate`): a topology, access-info, scenario, or reference
  *definition* being created or edited. Selecting a different definition
  while the current one is dirty is blocked, exactly like Step 2's original
  topology-switch guard; moving into or out of running-config mode is not,
  since it is an independent scope.

Everything here is memory-only until commit() writes changed YAML to disk.
Step 1's validators in `network_lab_mcp.lab` remain the single source of
truth for structural validity; this module does not duplicate those rules.
It only adds the narrow, CLI-specific case-only topology-name collision
safety check, which is not a validator replacement.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from network_lab_mcp import lab


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


DEVICE_FIELD_ORDER = ("type", "address", "transport", "port", "username", "password")

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

    # ---- scoped dirty state ----

    def settings_dirty(self) -> bool:
        return self.settings_candidate is not None and self.settings_candidate != self.committed_settings

    def definition_dirty(self) -> bool:
        if self.definition_kind is None:
            return False
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
            else:
                self.definition_candidate = copy.deepcopy(self.definition_original)
                if self.current_device_name is not None:
                    devices = self.definition_candidate.get("devices") or {}
                    if self.current_device_name not in devices:
                        self.current_device_name = None
        self._reconcile_mode_after_clear()

    def _reconcile_mode_after_clear(self) -> None:
        """After clear(), fall back to the nearest still-valid parent mode
        instead of lingering in a submode whose target no longer exists."""
        if self.definition_kind is None and self.mode in ("topology", "device", "access_info", "access_device", "scenario", "reference"):
            self.mode = "global"
            self.current_device_name = None
            return
        if self.mode in ("device", "access_device") and self.current_device_name is None:
            self.mode = "topology" if self.mode == "device" else "access_info"

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

    # ---- running-config selection (topology/scenario/references MCP uses) ----

    def _definition_will_exist(self, kind: str, exists_fn: Callable, name: str) -> bool:
        """True if `name` is already committed, or is the definition
        currently being created/edited in this same configure session --
        selecting a definition you are actively authoring (not yet
        committed) is allowed, since running-config and definition editing
        are independent candidate scopes that can be open at once."""
        return bool(exists_fn(name, self.lab_root) or (self.definition_kind == kind and self.definition_name == name))

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

        if definition_being_written:
            try:
                _DEFINITION_VALIDATORS[self.definition_kind](self.definition_name, self.definition_candidate)
            except lab.LabConfigError as exc:
                errors.append(str(exc))

        def _will_exist(kind: str, exists_fn: Callable, name: Optional[str]) -> bool:
            if not name:
                return False
            if exists_fn(name, self.lab_root):
                return True
            return definition_being_written and self.definition_kind == kind and self.definition_name == name

        settings = self.settings_candidate
        target_topology = settings.get("active_topology")
        if not target_topology:
            errors.append("Running configuration is missing a valid active topology.")
        elif not _will_exist("topology", lab.topology_exists, target_topology):
            errors.append(f"Topology '{target_topology}' does not exist.")

        scenario = settings.get("active_scenario")
        if not scenario:
            errors.append("Running configuration is missing a valid active scenario.")
        elif not _will_exist("scenario", lab.scenario_exists, scenario):
            errors.append(f"Scenario '{scenario}' does not exist.")

        for reference in settings.get("active_references") or []:
            if not _will_exist("reference", lab.reference_exists, reference):
                errors.append(f"Reference '{reference}' does not exist.")

        if errors:
            raise CommitValidationError(errors)

        if definition_being_written:
            _DEFINITION_WRITERS[self.definition_kind](self.definition_name, self.definition_candidate, self.lab_root)
            self.definition_original = copy.deepcopy(self.definition_candidate)

        if self.settings_dirty():
            lab.write_settings(self.settings_candidate, self.lab_root)
            self.committed_settings = copy.deepcopy(self.settings_candidate)

        return True
