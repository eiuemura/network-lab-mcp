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
