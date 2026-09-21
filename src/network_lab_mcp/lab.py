"""Loading of Network Lab MCP lab data: running-config selection, topology,
access-info, principles, scenarios, and references.

Lab YAML is intentionally re-read from disk on every call instead of being
cached at process startup, so that editing lab/settings.yaml (or any
committed definition file) takes effect on the next tool call without
restarting the MCP server.

Five kinds of data are deliberately kept separate (see README.md and
docs/architecture.md for the full model):

- running-config (lab/settings.yaml): which topology/scenario/references MCP
  currently uses. A *selection*, not a definition.
- access-info (lab/access-info/*.yaml): private device connection data
  (address/transport/port/username/password). Never exposed to Claude.
- topology (lab/topologies/*.yaml): safe logical topology (devices, device
  type, links). Exposed to Claude via get_active_topology().
- scenario (lab/scenarios/*.yaml): what Claude should do.
- reference (lab/references/*.yaml): reusable knowledge for Claude.
"""

from __future__ import annotations

import os
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


# --------------------------------------------------------------------------
# running-config (lab/settings.yaml): definition selection used by MCP
# --------------------------------------------------------------------------


def read_settings(lab_root: Path | None = None) -> dict:
    """Load the committed running-config (lab/settings.yaml) from disk."""
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


# --------------------------------------------------------------------------
# device.type: the fixed enum shared by topology and access-info
# --------------------------------------------------------------------------


DEVICE_TYPES: dict[str, str] = {
    "iosxr": "Cisco IOS XR",
    "iosxe": "Cisco IOS XE",
    "nxos": "Cisco NX-OS",
    # Not a network device Step 3 will run CDP/LLDP discovery against. `host`
    # is a normal registered topology node -- discovery is intentionally
    # skipped for it, not an error/"unsupported type" case.
    "host": "Generic host / endpoint",
}


def normalize_device_type(value: str) -> str:
    """Resolve `value` against the fixed DEVICE_TYPES enum and return the
    canonical lowercase keyword.

    Accepts an exact case-insensitive match or an unambiguous prefix
    abbreviation (mirroring fixed-keyword abbreviation elsewhere in the
    grammar); raises LabConfigError for an unknown or ambiguous value. This
    is the single validation primitive for `device.type`, shared by the
    Step 2 CLI's grammar-level `type` argument, topology YAML validation, and
    access-info YAML validation -- none of them duplicates this logic. Step 3
    topology discovery will dispatch platform-specific CDP/LLDP commands and
    parsers based on this field, so an unrecognized value must never reach a
    CLI candidate or committed YAML."""
    lowered = value.lower()
    if lowered in DEVICE_TYPES:
        return lowered
    matches = [key for key in DEVICE_TYPES if key.startswith(lowered)]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise LabConfigError(f"Ambiguous device type '{value}'. Matches: {', '.join(sorted(matches))}.")
    raise LabConfigError(f"Invalid device type '{value}'. Expected one of: {', '.join(sorted(DEVICE_TYPES))}.")


def validate_device_types(context_label: str, devices: dict) -> None:
    """Ensure each device's optional 'type' field, when present, is one of
    DEVICE_TYPES. A missing/empty 'type' is not itself an error here.

    Shared by topology and access-info validation (`context_label` is only
    used in error messages), so the enum's matching rules are never
    duplicated between the two."""
    for device_name, device in devices.items():
        raw_type = (device or {}).get("type")
        if raw_type in (None, ""):
            continue
        if not isinstance(raw_type, str):
            raise LabConfigError(f"{context_label} device '{device_name}' has an invalid 'type' value.")
        try:
            normalize_device_type(raw_type)
        except LabConfigError as exc:
            raise LabConfigError(f"{context_label} device '{device_name}': {exc}") from exc


# --------------------------------------------------------------------------
# topology (lab/topologies/*.yaml): safe logical topology, exposed to Claude
# --------------------------------------------------------------------------


# Private device-access fields that must live in access-info, never in
# topology, since topology is exposed to Claude via get_active_topology().
TOPOLOGY_ACCESS_FIELDS = ("address", "transport", "port", "username", "password")


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


def validate_topology_no_access_fields(topology_name: str, devices: dict) -> None:
    """Reject private device-access fields in topology data.

    Topology is safe logical data exposed to Claude via
    get_active_topology(); address/transport/port/username/password belong
    in access-info instead, which MCP never exposes."""
    for device_name, device in devices.items():
        present = sorted(field for field in TOPOLOGY_ACCESS_FIELDS if (device or {}).get(field) not in (None, ""))
        if present:
            raise LabConfigError(
                f"Topology '{topology_name}' device '{device_name}' contains private access field(s) "
                f"{', '.join(present)}; these belong in an access-info definition, not topology."
            )


def validate_topology_data(name: str, data: Any) -> None:
    """Validate an in-memory topology mapping using the same rules `load_topology()`
    applies to a freshly loaded file.

    This is the single validation primitive shared by the MCP load path, the
    Step 2 CLI commit path, and tests -- neither of the other callers
    duplicates these rules.
    """
    if not isinstance(data, dict):
        raise LabConfigError(f"Topology '{name}' data must be a YAML mapping.")
    devices = data.get("devices") or {}
    validate_topology_device_names(name, devices)
    validate_topology_no_access_fields(name, devices)
    validate_device_types(f"Topology '{name}'", devices)


def load_topology(name: str, lab_root: Path | None = None) -> dict:
    lab_root = lab_root or find_lab_root()
    path = lab_root / "topologies" / f"{name}.yaml"
    topology = _load_yaml(path, f"Topology '{name}'")
    validate_topology_data(name, topology)
    return topology


def write_topology(name: str, data: dict, lab_root: Path | None = None) -> None:
    """Persist a topology mapping to lab/topologies/<name>.yaml atomically.

    Validates with the same `validate_topology_data()` primitive used to load
    topologies, so an invalid candidate (including one still carrying
    private access fields) can never reach disk.
    """
    lab_root = lab_root or find_lab_root()
    validate_topology_data(name, data)
    _atomic_write_yaml(lab_root / "topologies" / f"{name}.yaml", data)


def list_topology_names(lab_root: Path | None = None) -> list[str]:
    """List the topology names available on disk (exact stored/file case)."""
    lab_root = lab_root or find_lab_root()
    return _list_yaml_stems(lab_root / "topologies")


def topology_exists(name: str, lab_root: Path | None = None) -> bool:
    lab_root = lab_root or find_lab_root()
    return (lab_root / "topologies" / f"{name}.yaml").is_file()


# --------------------------------------------------------------------------
# access-info (lab/access-info/*.yaml): private device access, never exposed
# --------------------------------------------------------------------------


# Single-hop OpenSSH ProxyJump only (see terminal.py): a jump host is always
# a generic endpoint, never a network device, and ProxyJump is SSH-only.
JUMP_HOST_TYPE = "host"


def validate_access_info_data(name: str, data: Any) -> None:
    """Validate an in-memory access-info mapping. Device names only need
    basic sanity here (non-empty strings) -- unlike topology, access-info
    device keys do not by themselves create terminal sessions, so they are
    not required to pass the topology session-name-collision check. The
    device.type enum is still validated through the same SSOT as topology."""
    if not isinstance(data, dict):
        raise LabConfigError(f"Access information '{name}' data must be a YAML mapping.")
    devices = data.get("devices") or {}
    if not isinstance(devices, dict):
        raise LabConfigError(f"Access information '{name}' has an invalid 'devices' section; expected a mapping.")
    for device_name in devices:
        if not isinstance(device_name, str) or not device_name.strip():
            raise LabConfigError(f"Access information '{name}' has a device with an empty or invalid name.")
    validate_device_types(f"Access information '{name}'", devices)


def load_access_info(name: str, lab_root: Path | None = None) -> dict:
    lab_root = lab_root or find_lab_root()
    path = lab_root / "access-info" / f"{name}.yaml"
    data = _load_yaml(path, f"Access information '{name}'")
    validate_access_info_data(name, data)
    return data


def write_access_info(name: str, data: dict, lab_root: Path | None = None) -> None:
    """Persist an access-info mapping to lab/access-info/<name>.yaml atomically."""
    lab_root = lab_root or find_lab_root()
    validate_access_info_data(name, data)
    _atomic_write_yaml(lab_root / "access-info" / f"{name}.yaml", data)


def list_access_info_names(lab_root: Path | None = None) -> list[str]:
    lab_root = lab_root or find_lab_root()
    return _list_yaml_stems(lab_root / "access-info")


def access_info_exists(name: str, lab_root: Path | None = None) -> bool:
    lab_root = lab_root or find_lab_root()
    return (lab_root / "access-info" / f"{name}.yaml").is_file()


def resolve_device_access(device_name: str, lab_root: Path | None = None) -> dict:
    """Search every committed access-info definition for `device_name` and
    return its private access data.

    This is a deliberately temporary, unscoped lookup (see
    "Known limitations" in README.md): access-info is not yet associated
    with a specific topology, so it is searched globally by exact device ID.
    Fails closed (LabConfigError) if the device ID is absent from every
    access-info definition, or present in more than one -- silently picking
    one would risk connecting to the wrong device. Credential values are
    never included in the raised error."""
    lab_root = lab_root or find_lab_root()
    matches: list[tuple[str, dict]] = []
    for access_info_name in list_access_info_names(lab_root):
        data = load_access_info(access_info_name, lab_root)
        devices = data.get("devices") or {}
        if device_name in devices:
            matches.append((access_info_name, devices[device_name] or {}))
    if not matches:
        raise LabConfigError(f"Access information for device '{device_name}' was not found.")
    if len(matches) > 1:
        raise LabConfigError(f"Access information for device '{device_name}' is ambiguous.")
    return matches[0][1]


# --------------------------------------------------------------------------
# scenario / reference: schema intentionally not fixed yet (see
# docs/scenario_format.md) -- minimal "valid YAML mapping" validation only
# --------------------------------------------------------------------------


def _validate_mapping(kind: str, name: str, data: Any) -> None:
    if not isinstance(data, dict):
        raise LabConfigError(f"{kind} '{name}' data must be a YAML mapping.")


def validate_scenario_data(name: str, data: Any) -> None:
    _validate_mapping("Scenario", name, data)


def validate_reference_data(name: str, data: Any) -> None:
    _validate_mapping("Reference", name, data)


def load_principles(lab_root: Path | None = None) -> Any:
    lab_root = lab_root or find_lab_root()
    return _load_yaml(lab_root / "principles.yaml", "Principles")


def load_scenario(name: str, lab_root: Path | None = None) -> Any:
    lab_root = lab_root or find_lab_root()
    data = _load_yaml(lab_root / "scenarios" / f"{name}.yaml", f"Scenario '{name}'")
    _validate_mapping("Scenario", name, data)
    return data


def write_scenario(name: str, data: dict, lab_root: Path | None = None) -> None:
    lab_root = lab_root or find_lab_root()
    validate_scenario_data(name, data)
    _atomic_write_yaml(lab_root / "scenarios" / f"{name}.yaml", data)


def list_scenario_names(lab_root: Path | None = None) -> list[str]:
    lab_root = lab_root or find_lab_root()
    return _list_yaml_stems(lab_root / "scenarios")


def scenario_exists(name: str, lab_root: Path | None = None) -> bool:
    lab_root = lab_root or find_lab_root()
    return (lab_root / "scenarios" / f"{name}.yaml").is_file()


def load_reference(name: str, lab_root: Path | None = None) -> Any:
    lab_root = lab_root or find_lab_root()
    data = _load_yaml(lab_root / "references" / f"{name}.yaml", f"Reference '{name}'")
    _validate_mapping("Reference", name, data)
    return data


def load_references(names: list[str], lab_root: Path | None = None) -> list[Any]:
    lab_root = lab_root or find_lab_root()
    return [load_reference(name, lab_root) for name in names]


def write_reference(name: str, data: dict, lab_root: Path | None = None) -> None:
    lab_root = lab_root or find_lab_root()
    validate_reference_data(name, data)
    _atomic_write_yaml(lab_root / "references" / f"{name}.yaml", data)


def list_reference_names(lab_root: Path | None = None) -> list[str]:
    lab_root = lab_root or find_lab_root()
    return _list_yaml_stems(lab_root / "references")


def reference_exists(name: str, lab_root: Path | None = None) -> bool:
    lab_root = lab_root or find_lab_root()
    return (lab_root / "references" / f"{name}.yaml").is_file()


# --------------------------------------------------------------------------
# MCP-facing reads: committed running-config selection only, never candidate
# state, never access-info
# --------------------------------------------------------------------------


def get_active_topology() -> dict:
    """Reload the running-config selection and the active topology from disk
    and return them together. Never includes access-info: this is the safe
    logical topology Claude is allowed to see."""
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
    """Verify `device_name` exists in the active topology, then resolve its
    private access information from committed access-info definitions.

    Returns (topology_name, access_info_dict) -- never topology data itself,
    since terminal connectivity needs address/transport/username/password,
    which topology no longer carries. Raises LabConfigError (fail closed)
    when the device is not present in the active topology, when access
    information for it is missing or ambiguous (see
    resolve_device_access()), or when the topology's and access-info's
    device.type disagree once both are normalized through the shared
    DEVICE_TYPES SSOT."""
    active = get_active_topology()
    topology_devices = active["topology"].get("devices") or {}
    topology_device = topology_devices.get(device_name)
    if topology_device is None:
        raise LabConfigError(
            f"Device '{device_name}' is not present in active topology '{active['active_topology']}'."
        )
    access = resolve_device_access(device_name)

    topology_type = (topology_device or {}).get("type")
    access_type = (access or {}).get("type")
    if topology_type and access_type:
        if normalize_device_type(str(topology_type)) != normalize_device_type(str(access_type)):
            raise LabConfigError(
                f"Device type mismatch for '{device_name}' between topology and access information."
            )

    return active["active_topology"], access


# --------------------------------------------------------------------------
# Generic listing / atomic write helpers
# --------------------------------------------------------------------------


def _list_yaml_stems(directory: Path) -> list[str]:
    if not directory.is_dir():
        return []
    return sorted(p.stem for p in directory.glob("*.yaml"))


def _atomic_write_yaml(path: Path, data: Any) -> None:
    """Write YAML atomically: write to a sibling temp file, flush, then replace.

    This avoids ever leaving a partially written committed YAML file behind,
    and avoids touching the target file at all when the caller decides not to
    write (see the write_*() callers in cli/config.py, which only call this
    when a scope is actually dirty).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(data, handle, sort_keys=False, default_flow_style=False)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_path, path)


def write_settings(settings: dict, lab_root: Path | None = None) -> None:
    """Persist a running-config selection mapping to lab/settings.yaml atomically."""
    lab_root = lab_root or find_lab_root()
    _atomic_write_yaml(lab_root / "settings.yaml", settings)
