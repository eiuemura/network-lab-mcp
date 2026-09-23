"""Tests for lab.py's listing/existence helpers and
atomic YAML persistence."""

from __future__ import annotations

from network_lab_mcp import lab


def test_list_and_exists_helpers(lab_root):
    assert lab.list_topology_names(lab_root) == ["sample_lab"]
    assert set(lab.list_scenario_names(lab_root)) == {"sample", "failover_test"}
    assert set(lab.list_reference_names(lab_root)) == {"sample", "iosxr_basics"}

    assert lab.topology_exists("sample_lab", lab_root)
    assert not lab.topology_exists("does-not-exist", lab_root)
    assert lab.scenario_exists("failover_test", lab_root)
    assert not lab.scenario_exists("Failover_Test", lab_root)  # case-sensitive
    assert lab.reference_exists("iosxr_basics", lab_root)
    assert not lab.reference_exists("IOSXR_BASICS", lab_root)  # case-sensitive


def test_write_settings_atomic_roundtrip(lab_root):
    new_settings = {"active_topology": "sample_lab", "active_scenario": "failover_test", "active_references": []}
    lab.write_settings(new_settings, lab_root)
    assert lab.read_settings(lab_root) == new_settings
    assert not (lab_root / "settings.yaml.tmp").exists()


def test_write_topology_validates_before_write(lab_root):
    bad_data = {"name": "bad", "devices": {"": {}}, "links": []}
    import pytest

    with pytest.raises(lab.LabConfigError):
        lab.write_topology("bad", bad_data, lab_root)
    assert not (lab_root / "topologies" / "bad.yaml").exists()


def test_write_topology_creates_new_file(lab_root):
    data = {"name": "new_lab", "description": "", "devices": {}, "links": []}
    lab.write_topology("new_lab", data, lab_root)
    assert lab.load_topology("new_lab", lab_root) == data


def test_write_scenario_and_reference_roundtrip(lab_root):
    scenario_data = {"name": "new_scenario", "description": "", "objectives": []}
    lab.write_scenario("new_scenario", scenario_data, lab_root)
    assert lab.load_scenario("new_scenario", lab_root) == scenario_data

    reference_data = {"name": "new_reference", "description": "", "guidance": []}
    lab.write_reference("new_reference", reference_data, lab_root)
    assert lab.load_reference("new_reference", lab_root) == reference_data


def test_write_scenario_rejects_non_mapping(lab_root):
    import pytest

    with pytest.raises(lab.LabConfigError):
        lab.write_scenario("bad", ["not", "a", "mapping"], lab_root)
    assert not (lab_root / "scenarios" / "bad.yaml").exists()


def test_write_access_info_creates_new_file(lab_root):
    data = {"name": "lab_devices", "devices": {"R1": {"type": "iosxr", "address": "192.0.2.1"}}}
    lab.write_access_info("lab_devices", data, lab_root)
    assert lab.load_access_info("lab_devices", lab_root) == data
