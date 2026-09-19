"""Tests for cli/config.py: candidate session, scoped dirty state, topology
selection (including the case-only collision safeguard), commit/abort, and
Step 1 validator reuse."""

from __future__ import annotations

import pytest

from network_lab_mcp import lab
from network_lab_mcp.cli import config as cfgmod


def test_configure_initializes_settings_candidate_only(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    assert session.mode == "global"
    assert session.settings_candidate == session.committed_settings
    assert session.selected_topology_name is None
    assert session.topology_candidate is None
    assert session.overall_dirty() is False


def test_settings_only_dirty_does_not_block_topology_switch(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.set_scenario("failover_test")
    assert session.settings_dirty() is True
    assert session.topology_dirty() is False
    ok, message = session.can_switch_topology()
    assert ok is True and message is None


def test_dirty_topology_blocks_switch(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.apply_topology_plan(session.plan_topology_selection("sample_lab"))
    session.set_topology_description("changed")
    assert session.topology_dirty() is True
    ok, message = session.can_switch_topology()
    assert ok is False
    assert "commit" in message


def test_clean_topology_switch_allowed(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.apply_topology_plan(session.plan_topology_selection("sample_lab"))
    assert session.topology_dirty() is False
    ok, message = session.can_switch_topology()
    assert ok is True


def test_new_topology_is_dirty_until_committed(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.apply_topology_plan(session.plan_topology_selection("brand_new"))
    assert session.new_topology_dirty() is True
    assert session.overall_dirty() is True


# ---- case-only collision safeguard ----


def test_case_only_collision_detected():
    assert cfgmod.find_case_only_collision("SRv6_Lab", ["srv6_lab"]) == "srv6_lab"
    assert cfgmod.find_case_only_collision("srv6_lab", ["srv6_lab"]) is None  # exact match, not a collision
    assert cfgmod.find_case_only_collision("srv6-lab", ["srv6_lab"]) is None  # not case-only
    assert cfgmod.find_case_only_collision("srv6lab", ["srv6_lab"]) is None
    assert cfgmod.find_case_only_collision("srv_lab", ["srv6_lab"]) is None


def test_plan_topology_selection_flags_case_collision(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    plan = session.plan_topology_selection("SAMPLE_LAB")
    assert plan.kind == "case_collision"
    assert plan.existing == "sample_lab"


def test_case_collision_plan_does_not_mutate_candidate(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.set_scenario("failover_test")
    baseline_active_topology = session.settings_candidate["active_topology"]
    plan = session.plan_topology_selection("SAMPLE_LAB")
    assert plan.kind == "case_collision"
    # Merely planning must not touch any candidate state.
    assert session.settings_candidate["active_topology"] == baseline_active_topology
    assert session.selected_topology_name is None
    assert session.topology_candidate is None


def test_confirmed_case_collision_creates_distinct_topology(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    plan = cfgmod.TopologyPlan("create_new", "SAMPLE_LAB")
    session.apply_topology_plan(plan)
    assert session.selected_topology_name == "SAMPLE_LAB"
    assert session.topology_candidate["name"] == "SAMPLE_LAB"
    assert session.topology_original is None
    # The existing, differently-cased topology is untouched.
    assert lab.load_topology("sample_lab", lab_root)["name"] == "sample_lab"


def test_no_fuzzy_matching(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    for near_miss in ("sample-lab", "samplelab", "sampl_lab"):
        plan = session.plan_topology_selection(near_miss)
        assert plan.kind == "create_new", near_miss


# ---- object identifier case sensitivity ----


def test_scenario_selection_is_case_sensitive(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    with pytest.raises(cfgmod.ConfigError):
        session.set_scenario("Failover_Test")
    session.set_scenario("failover_test")
    assert session.settings_candidate["active_scenario"] == "failover_test"


def test_reference_selection_is_case_sensitive(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    with pytest.raises(cfgmod.ConfigError):
        session.add_reference("IOSXR_BASICS")
    session.add_reference("iosxr_basics")


def test_duplicate_reference_rejected(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.add_reference("iosxr_basics")
    with pytest.raises(cfgmod.ConfigError):
        session.add_reference("iosxr_basics")


# ---- commit / abort ----


def test_no_change_commit_writes_nothing(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    settings_mtime = (lab_root / "settings.yaml").stat().st_mtime_ns
    changed = session.commit()
    assert changed is False
    assert (lab_root / "settings.yaml").stat().st_mtime_ns == settings_mtime


def test_settings_only_commit_does_not_rewrite_topology(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.set_scenario("failover_test")
    topology_mtime = (lab_root / "topologies" / "sample_lab.yaml").stat().st_mtime_ns
    changed = session.commit()
    assert changed is True
    assert (lab_root / "topologies" / "sample_lab.yaml").stat().st_mtime_ns == topology_mtime
    assert lab.read_settings(lab_root)["active_scenario"] == "failover_test"


def test_clean_topology_selection_commit_does_not_rewrite_topology(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.apply_topology_plan(session.plan_topology_selection("sample_lab"))
    topology_mtime = (lab_root / "topologies" / "sample_lab.yaml").stat().st_mtime_ns
    session.commit()
    assert (lab_root / "topologies" / "sample_lab.yaml").stat().st_mtime_ns == topology_mtime


def test_topology_edit_commit_persists_and_preserves_links(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.apply_topology_plan(session.plan_topology_selection("sample_lab"))
    session.enter_device("R1")
    session.set_device_field("password", "STEP2_TEST_SECRET_12345")
    session.commit()

    persisted = lab.load_topology("sample_lab", lab_root)
    assert persisted["devices"]["R1"]["password"] == "STEP2_TEST_SECRET_12345"
    # Untouched semantic data (links, R2) must survive.
    assert persisted["links"] == [{"a": "R1", "b": "R2"}]
    assert persisted["devices"]["R2"]["address"] == "192.0.2.12"


def test_commit_accepts_supported_device_type(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.apply_topology_plan(session.plan_topology_selection("sample_lab"))
    session.enter_device("R1")
    session.set_device_field("type", "nxos")
    session.commit()
    assert lab.load_topology("sample_lab", lab_root)["devices"]["R1"]["type"] == "nxos"


def test_commit_rejects_unsupported_device_type(lab_root):
    # Reaches lab.validate_topology_data() at commit time even when a value
    # bypasses the CLI grammar's own type validator, proving the check is
    # not duplicated only on the interactive input path.
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.apply_topology_plan(session.plan_topology_selection("sample_lab"))
    session.enter_device("R1")
    session.set_device_field("type", "junos")
    with pytest.raises(cfgmod.CommitValidationError, match="Invalid device type"):
        session.commit()
    assert lab.load_topology("sample_lab", lab_root)["devices"]["R1"]["type"] == "iosxr"


def test_new_topology_commit_persists_settings_and_topology(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.apply_topology_plan(session.plan_topology_selection("brand_new"))
    session.enter_device("R1")
    session.set_device_field("transport", "ssh")
    session.commit()

    assert lab.topology_exists("brand_new", lab_root)
    assert lab.read_settings(lab_root)["active_topology"] == "brand_new"


def test_failed_commit_leaves_disk_unchanged(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.settings_candidate["active_scenario"] = "does-not-exist"
    settings_before = lab.read_settings(lab_root)
    with pytest.raises(cfgmod.CommitValidationError):
        session.commit()
    assert lab.read_settings(lab_root) == settings_before
    assert session.overall_dirty() is True  # candidate retained


def test_abort_discards_candidate_and_writes_nothing(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.apply_topology_plan(session.plan_topology_selection("brand_new"))
    session.abort()
    assert session.mode == "exec"
    assert session.overall_dirty() is False
    assert not lab.topology_exists("brand_new", lab_root)


def test_candidate_never_visible_to_committed_reads_before_commit(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.set_scenario("failover_test")
    # Simulate the MCP server's own read path: it must still see the old value.
    assert lab.read_settings(lab_root)["active_scenario"] == "sample"
    session.commit()
    assert lab.read_settings(lab_root)["active_scenario"] == "failover_test"
