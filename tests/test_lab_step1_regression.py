"""Step 1 regression tests: existing lab.py behavior must be unaffected by
the Step 2 additions (validate_topology_data() wraps the same rules that
validate_topology_device_names() always enforced)."""

from __future__ import annotations

import pytest

from network_lab_mcp import lab


def test_load_topology_valid(lab_root):
    topology = lab.load_topology("sample_lab", lab_root)
    assert topology["name"] == "sample_lab"
    assert set(topology["devices"]) == {"R1", "R2"}


def test_load_topology_rejects_invalid_device_name(lab_root):
    (lab_root / "topologies" / "broken.yaml").write_text(
        "name: broken\ndevices:\n  '':\n    type: iosxr\nlinks: []\n",
        encoding="utf-8",
    )
    with pytest.raises(lab.LabConfigError):
        lab.load_topology("broken", lab_root)


# ---- device.type enum ----


def test_normalize_device_type_accepts_exact_and_case_insensitive():
    assert lab.normalize_device_type("iosxr") == "iosxr"
    assert lab.normalize_device_type("IOSXE") == "iosxe"
    assert lab.normalize_device_type("NxOs") == "nxos"
    assert lab.normalize_device_type("host") == "host"
    assert lab.normalize_device_type("HOST") == "host"
    assert lab.normalize_device_type("Host") == "host"


def test_normalize_device_type_accepts_unambiguous_abbreviation():
    assert lab.normalize_device_type("nx") == "nxos"
    assert lab.normalize_device_type("h") == "host"


def test_normalize_device_type_rejects_ambiguous_abbreviation():
    with pytest.raises(lab.LabConfigError, match="Ambiguous"):
        lab.normalize_device_type("ios")


def test_normalize_device_type_rejects_unknown_value():
    with pytest.raises(lab.LabConfigError, match="Invalid device type"):
        lab.normalize_device_type("junos")


def test_load_topology_accepts_all_supported_device_types(lab_root):
    for device_type in ("iosxr", "iosxe", "nxos", "host"):
        path = lab_root / "topologies" / f"types_{device_type}.yaml"
        path.write_text(
            f"name: types_{device_type}\ndevices:\n  R1:\n    type: {device_type}\nlinks: []\n",
            encoding="utf-8",
        )
        topology = lab.load_topology(f"types_{device_type}", lab_root)
        assert topology["devices"]["R1"]["type"] == device_type


def test_load_topology_allows_missing_device_type(lab_root):
    (lab_root / "topologies" / "no_type.yaml").write_text(
        "name: no_type\ndevices:\n  R1:\n    address: 192.0.2.1\nlinks: []\n",
        encoding="utf-8",
    )
    topology = lab.load_topology("no_type", lab_root)
    assert "type" not in topology["devices"]["R1"]


def test_load_topology_rejects_unsupported_device_type(lab_root):
    for index, bad_type in enumerate(("junos", "linux", "router", "switch", "generic", "none")):
        name = f"bad_type_{index}"
        (lab_root / "topologies" / f"{name}.yaml").write_text(
            f"name: {name}\ndevices:\n  R1:\n    type: {bad_type}\nlinks: []\n",
            encoding="utf-8",
        )
        with pytest.raises(lab.LabConfigError, match="Invalid device type"):
            lab.load_topology(name, lab_root)


def test_get_active_topology_and_execution_instructions(lab_root, monkeypatch):
    monkeypatch.setattr(lab, "find_lab_root", lambda: lab_root)
    active = lab.get_active_topology()
    assert active["active_topology"] == "sample_lab"
    assert "R1" in active["topology"]["devices"]

    instructions = lab.get_execution_instructions()
    assert instructions["scenario"]["name"] == "sample"
    assert [r["name"] for r in instructions["references"]] == ["sample"]


def test_get_device(lab_root, monkeypatch):
    monkeypatch.setattr(lab, "find_lab_root", lambda: lab_root)
    topology_name, device = lab.get_device("R1")
    assert topology_name == "sample_lab"
    assert device["address"] == "192.0.2.11"

    with pytest.raises(lab.LabConfigError):
        lab.get_device("does-not-exist")
