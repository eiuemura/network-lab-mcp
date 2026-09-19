"""Loading of Network Lab MCP lab data: settings, topology, principles, scenarios, and references.

Lab YAML is intentionally re-read from disk on every call instead of being
cached at process startup, so that editing lab/settings.yaml takes effect on
the next tool call without restarting the MCP server.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from network_lab_mcp import terminal


class LabConfigError(Exception):
    """A clear, user-facing error in the lab configuration or lab data."""


def find_lab_root() -> Path:
    """Resolve the lab/ directory owned by this repository checkout.

    Step 1 only supports a local editable installation, so the lab root is
    always the sibling lab/ directory of the source checkout providing this
    module. This must not depend on the caller's current working directory,
    since Claude Code is normally started from a separate task workspace.
    """
    package_dir = Path(__file__).resolve().parent
    repo_root = package_dir.parent.parent
    lab_root = repo_root / "lab"
    if not lab_root.is_dir():
        raise LabConfigError(
            f"Lab root not found at '{lab_root}'. Network Lab MCP requires a local "
            "editable installation ('pip install -e .') where the repository "
            "checkout owns the lab/ directory. Non-editable/wheel installation "
            "is not a supported configuration in Step 1."
        )
    return lab_root


def _load_yaml(path: Path, what: str) -> Any:
    if not path.is_file():
        raise LabConfigError(f"{what} not found at '{path}'.")
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
    except yaml.YAMLError as exc:
        raise LabConfigError(f"{what} at '{path}' is not valid YAML: {exc}") from exc
    if data is None:
        raise LabConfigError(f"{what} at '{path}' is empty.")
    return data


def read_settings(lab_root: Path | None = None) -> dict:
    """Load lab/settings.yaml from disk, with a clear error if it is missing."""
    lab_root = lab_root or find_lab_root()
    settings_path = lab_root / "settings.yaml"
    example_path = lab_root / "settings.example.yaml"
    if not settings_path.is_file():
        raise LabConfigError(
            f"Local settings file not found at '{settings_path}'. Create it once "
            f"by copying the tracked template, e.g.: cp {example_path} {settings_path}"
        )
    settings = _load_yaml(settings_path, "Settings")
    if not isinstance(settings, dict):
        raise LabConfigError(f"Settings at '{settings_path}' must be a YAML mapping.")
    return settings


def get_active_topology_name(settings: dict) -> str:
    name = settings.get("active_topology")
    if not name or not isinstance(name, str):
        raise LabConfigError("Settings is missing a valid 'active_topology' value.")
    return name


def get_active_scenario_name(settings: dict) -> str:
    name = settings.get("active_scenario")
    if not name or not isinstance(name, str):
        raise LabConfigError("Settings is missing a valid 'active_scenario' value.")
    return name


def get_active_reference_names(settings: dict) -> list[str]:
    names = settings.get("active_references") or []
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        raise LabConfigError("Settings 'active_references' must be a list of strings.")
    return names


def validate_topology_device_names(topology_name: str, devices: dict) -> None:
    """Ensure every device name maps to a safe and unambiguous terminal session name."""
    if not isinstance(devices, dict):
        raise LabConfigError(
            f"Topology '{topology_name}' has an invalid 'devices' section; expected a mapping."
        )
    seen_sessions: dict[str, str] = {}
    for device_name in devices:
        if not isinstance(device_name, str) or not device_name.strip():
            raise LabConfigError(f"Topology '{topology_name}' has a device with an empty or invalid name.")
        try:
            session_name = terminal.derive_production_session_name(device_name)
        except terminal.TerminalError as exc:
            raise LabConfigError(
                f"Topology '{topology_name}' device '{device_name}' cannot be mapped "
                f"to a safe terminal session name: {exc}"
            ) from exc
        collision = seen_sessions.get(session_name)
        if collision is not None and collision != device_name:
            raise LabConfigError(
                f"Topology '{topology_name}' devices '{collision}' and '{device_name}' "
                f"both resolve to the terminal session '{session_name}'."
            )
        seen_sessions[session_name] = device_name


def load_topology(name: str, lab_root: Path | None = None) -> dict:
    lab_root = lab_root or find_lab_root()
    path = lab_root / "topologies" / f"{name}.yaml"
    topology = _load_yaml(path, f"Topology '{name}'")
    if not isinstance(topology, dict):
        raise LabConfigError(f"Topology '{name}' at '{path}' must be a YAML mapping.")
    devices = topology.get("devices") or {}
    validate_topology_device_names(name, devices)
    return topology


def load_principles(lab_root: Path | None = None) -> Any:
    lab_root = lab_root or find_lab_root()
    return _load_yaml(lab_root / "principles.yaml", "Principles")


def load_scenario(name: str, lab_root: Path | None = None) -> Any:
    lab_root = lab_root or find_lab_root()
    return _load_yaml(lab_root / "scenarios" / f"{name}.yaml", f"Scenario '{name}'")


def load_references(names: list[str], lab_root: Path | None = None) -> list[Any]:
    lab_root = lab_root or find_lab_root()
    return [_load_yaml(lab_root / "references" / f"{name}.yaml", f"Reference '{name}'") for name in names]


def get_active_topology() -> dict:
    """Reload settings and the active topology from disk and return them together."""
    lab_root = find_lab_root()
    settings = read_settings(lab_root)
    topology_name = get_active_topology_name(settings)
    topology = load_topology(topology_name, lab_root)
    return {"active_topology": topology_name, "topology": topology}


def get_execution_instructions() -> dict:
    """Reload principles, the active scenario, and active references from disk."""
    lab_root = find_lab_root()
    settings = read_settings(lab_root)
    scenario_name = get_active_scenario_name(settings)
    reference_names = get_active_reference_names(settings)
    principles = load_principles(lab_root)
    scenario = load_scenario(scenario_name, lab_root)
    references = load_references(reference_names, lab_root)
    return {
        "principles": principles,
        "scenario": {"name": scenario_name, "content": scenario},
        "references": [
            {"name": name, "content": content} for name, content in zip(reference_names, references)
        ],
    }


def get_device(device_name: str) -> tuple[str, dict]:
    """Reload settings and the active topology, then return one device's configuration.

    Returns a (topology_name, device_config) tuple. Raises LabConfigError when
    the device is not present in the active topology.
    """
    active = get_active_topology()
    devices = active["topology"].get("devices") or {}
    device = devices.get(device_name)
    if device is None:
        raise LabConfigError(
            f"Device '{device_name}' is not present in active topology '{active['active_topology']}'."
        )
    return active["active_topology"], device
