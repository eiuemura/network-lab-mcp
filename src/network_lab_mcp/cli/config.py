"""Candidate configuration session for the Network Lab CLI.

Owns the settings candidate, the topology candidate, scoped dirty-state
evaluation, and the candidate -> committed commit/abort boundary. Everything
here is memory-only until commit() writes changed YAML to disk.

Step 1's topology/device validation in `network_lab_mcp.lab` remains the
single source of truth for structural validity; this module does not
duplicate those rules. It only adds the narrow, CLI-specific case-only
topology-name collision safety check, which is not a validator replacement.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

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


DEVICE_FIELD_ORDER = ("type", "address", "transport", "port", "username", "password")


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
        self.selected_topology_name: Optional[str] = None
        self.topology_original: Optional[dict] = None
        self.topology_candidate: Optional[dict] = None
        self.current_device_name: Optional[str] = None

    # ---- scoped dirty state ----

    def settings_dirty(self) -> bool:
        return self.settings_candidate is not None and self.settings_candidate != self.committed_settings

    def topology_dirty(self) -> bool:
        return self.topology_original is not None and self.topology_candidate != self.topology_original

    def new_topology_dirty(self) -> bool:
        return self.selected_topology_name is not None and self.topology_original is None

    def overall_dirty(self) -> bool:
        return self.settings_dirty() or self.topology_dirty() or self.new_topology_dirty()

    # ---- configure entry / full exit ----

    def enter_configure(self) -> None:
        self.committed_settings = lab.read_settings(self.lab_root)
        self.settings_candidate = copy.deepcopy(self.committed_settings)
        self.selected_topology_name = None
        self.topology_original = None
        self.topology_candidate = None
        self.current_device_name = None
        self.mode = "global"

    def reset_to_exec(self) -> None:
        """Return to EXEC, discarding all candidate state. By construction,
        EXEC mode always implies overall_dirty() is False."""
        self.mode = "exec"
        self.committed_settings = None
        self.settings_candidate = None
        self.selected_topology_name = None
        self.topology_original = None
        self.topology_candidate = None
        self.current_device_name = None

    # ---- topology selection ----

    def can_switch_topology(self) -> tuple[bool, Optional[str]]:
        if self.selected_topology_name is None:
            return True, None
        if self.topology_dirty() or self.new_topology_dirty():
            return False, "Uncommitted topology changes exist. Use 'commit' or 'abort' before switching topology."
        return True, None

    def plan_topology_selection(self, name: str) -> TopologyPlan:
        if self.selected_topology_name == name:
            return TopologyPlan("select_existing", name)
        existing_names = lab.list_topology_names(self.lab_root)
        if name in existing_names:
            return TopologyPlan("select_existing", name)
        collision = find_case_only_collision(name, existing_names)
        if collision is not None:
            return TopologyPlan("case_collision", name, collision)
        return TopologyPlan("create_new", name)

    def apply_topology_plan(self, plan: TopologyPlan) -> None:
        if plan.kind == "case_collision":
            raise ConfigError("A case-only topology collision must be confirmed before it can be applied.")
        if plan.kind == "select_existing":
            if not (self.selected_topology_name == plan.name and self.topology_candidate is not None):
                data = lab.load_topology(plan.name, self.lab_root)
                self.selected_topology_name = plan.name
                self.topology_original = copy.deepcopy(data)
                self.topology_candidate = copy.deepcopy(data)
        else:  # create_new
            self.selected_topology_name = plan.name
            self.topology_original = None
            self.topology_candidate = new_topology_data(plan.name)
        self.settings_candidate["active_topology"] = plan.name
        self.current_device_name = None
        self.mode = "topology"

    # ---- topology-mode mutation ----

    def set_topology_description(self, text: str) -> None:
        self.topology_candidate["description"] = text

    def enter_device(self, name: str) -> None:
        devices = self.topology_candidate.setdefault("devices", {})
        if name not in devices:
            devices[name] = {}
        self.current_device_name = name
        self.mode = "device"

    # ---- device-mode mutation ----

    def _device(self) -> dict:
        return self.topology_candidate["devices"][self.current_device_name]

    def set_device_field(self, field_name: str, value) -> None:
        self._device()[field_name] = value

    def clear_device_field(self, field_name: str) -> None:
        self._device().pop(field_name, None)

    # ---- scenario / reference selection ----

    def set_scenario(self, name: str) -> None:
        if not lab.scenario_exists(name, self.lab_root):
            raise ConfigError(f"Scenario '{name}' does not exist.")
        self.settings_candidate["active_scenario"] = name

    def add_reference(self, name: str) -> None:
        refs = self.settings_candidate.setdefault("active_references", [])
        if name in refs:
            raise ConfigError(f"Reference '{name}' is already active.")
        if not lab.reference_exists(name, self.lab_root):
            raise ConfigError(f"Reference '{name}' does not exist.")
        refs.append(name)

    def remove_reference(self, name: str) -> None:
        refs = self.settings_candidate.setdefault("active_references", [])
        if name not in refs:
            raise ConfigError(f"Reference '{name}' is not active.")
        refs.remove(name)

    # ---- commit / abort ----

    def commit(self) -> bool:
        """Validate and persist the candidate. Returns True if anything was
        written, False for a no-op ("No changes to commit."). Raises
        CommitValidationError (no disk writes at all) on validation failure."""
        if not self.overall_dirty():
            return False

        errors: list[str] = []
        settings = self.settings_candidate
        target_topology = settings.get("active_topology")
        topology_being_written = (
            self.selected_topology_name is not None
            and self.selected_topology_name == target_topology
            and (self.topology_dirty() or self.new_topology_dirty())
        )

        if not target_topology:
            errors.append("Settings is missing a valid active topology.")
        elif not topology_being_written and not lab.topology_exists(target_topology, self.lab_root):
            errors.append(f"Topology '{target_topology}' does not exist.")

        if topology_being_written:
            try:
                lab.validate_topology_data(self.selected_topology_name, self.topology_candidate)
            except lab.LabConfigError as exc:
                errors.append(str(exc))

        scenario = settings.get("active_scenario")
        if not scenario:
            errors.append("Settings is missing a valid active scenario.")
        elif not lab.scenario_exists(scenario, self.lab_root):
            errors.append(f"Scenario '{scenario}' does not exist.")

        for reference in settings.get("active_references") or []:
            if not lab.reference_exists(reference, self.lab_root):
                errors.append(f"Reference '{reference}' does not exist.")

        if errors:
            raise CommitValidationError(errors)

        if topology_being_written:
            lab.write_topology(self.selected_topology_name, self.topology_candidate, self.lab_root)
            self.topology_original = copy.deepcopy(self.topology_candidate)

        if self.settings_dirty():
            lab.write_settings(self.settings_candidate, self.lab_root)
            self.committed_settings = copy.deepcopy(self.settings_candidate)

        return True

    def abort(self) -> None:
        self.reset_to_exec()
