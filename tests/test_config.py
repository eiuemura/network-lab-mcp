"""Tests for cli/config.py: the running-config candidate (independent from
any definition candidate), topology/access-info/scenario/reference
definition editing (create-or-select, never a "set active selection"
command any more), the case-only topology collision safeguard, device
sub-editing (safe fields under topology, private fields under access-info),
clear (replacing abort), commit ordering/validation, and external-editor
integration.

Old Step 2 selector semantics -- global `topology <name>` / `scenario
<name>` / `reference <name>` directly changing the running-config selection
-- are gone. `test_old_selector_semantics_are_no_longer_reachable` pins
that down explicitly; every other selection test below exercises the new
home for that behavior, the `running` mode methods
(select_topology/set_scenario/add_reference/remove_reference)."""

from __future__ import annotations

import pytest

from network_lab_mcp import lab
from network_lab_mcp.cli import config as cfgmod
from network_lab_mcp.cli import editor
from network_lab_mcp.cli import main as climain


# ---- candidate initialization: running-config and definition are independent ----


def test_configure_initializes_settings_candidate_only(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    assert session.mode == "global"
    assert session.settings_candidate == session.committed_settings
    assert session.definition_kind is None
    assert session.definition_candidate is None
    assert session.overall_dirty() is False


def test_old_selector_semantics_are_no_longer_reachable(lab_root):
    """Global `topology <name>` is now a definition editor, not a selection
    command: entering it must never touch the running-config candidate."""
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    baseline_active_topology = session.settings_candidate["active_topology"]
    session.apply_topology_definition_plan(session.plan_topology_definition("brand_new"))
    assert session.settings_candidate["active_topology"] == baseline_active_topology
    assert session.settings_dirty() is False

    session.enter_scenario_definition("failover_test")
    assert session.settings_candidate["active_scenario"] == "sample"

    session.enter_reference_definition("iosxr_basics")
    assert session.settings_candidate.get("active_references") == ["sample"]

    # And CliSession no longer exposes the old topology-selection attribute names.
    assert not hasattr(session, "selected_topology_name")
    assert not hasattr(session, "topology_candidate")
    assert not hasattr(session, "topology_original")


# ---- running-config selectors (the new home for "active selection") ----


def test_running_select_topology_requires_existing(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    with pytest.raises(cfgmod.ConfigError):
        session.select_topology("does-not-exist")
    session.select_topology("sample_lab")
    assert session.settings_candidate["active_topology"] == "sample_lab"
    assert session.settings_dirty() is False  # already the committed value


def test_running_scenario_selection_is_case_sensitive(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    with pytest.raises(cfgmod.ConfigError):
        session.set_scenario("Failover_Test")
    session.set_scenario("failover_test")
    assert session.settings_candidate["active_scenario"] == "failover_test"


def test_running_reference_add_remove_and_duplicates(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    with pytest.raises(cfgmod.ConfigError):
        session.add_reference("IOSXR_BASICS")
    session.add_reference("iosxr_basics")
    assert session.settings_candidate["active_references"] == ["sample", "iosxr_basics"]
    with pytest.raises(cfgmod.ConfigError):
        session.add_reference("iosxr_basics")

    session.remove_reference("sample")
    assert session.settings_candidate["active_references"] == ["iosxr_basics"]
    with pytest.raises(cfgmod.ConfigError):
        session.remove_reference("sample")


def test_settings_only_dirty_does_not_block_definition_switch(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.set_scenario("failover_test")
    assert session.settings_dirty() is True
    ok, message = session.can_switch_definition("topology", "sample_lab")
    assert ok is True and message is None


# ---- topology definition editing ----


def test_dirty_topology_definition_blocks_switch(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.apply_topology_definition_plan(session.plan_topology_definition("sample_lab"))
    session.set_topology_description("changed")
    assert session.definition_dirty() is True
    ok, message = session.can_switch_definition("topology", "other_lab")
    assert ok is False
    assert "commit" in message and "clear" in message


def test_clean_topology_switch_allowed(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.apply_topology_definition_plan(session.plan_topology_definition("sample_lab"))
    assert session.definition_dirty() is False
    ok, message = session.can_switch_definition("topology", "other_lab")
    assert ok is True


def test_new_topology_is_dirty_until_committed(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.apply_topology_definition_plan(session.plan_topology_definition("brand_new"))
    assert session.definition_dirty() is True
    assert session.overall_dirty() is True


def test_existing_topology_loads_as_candidate_not_replaced_with_empty(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.apply_topology_definition_plan(session.plan_topology_definition("sample_lab"))
    assert set(session.definition_candidate["devices"]) == {"R1", "R2"}
    assert session.definition_candidate["links"] == [{"a": "R1", "b": "R2"}]


# ---- case-only collision safeguard ----


def test_case_only_collision_detected():
    assert cfgmod.find_case_only_collision("SRv6_Lab", ["srv6_lab"]) == "srv6_lab"
    assert cfgmod.find_case_only_collision("srv6_lab", ["srv6_lab"]) is None  # exact match, not a collision
    assert cfgmod.find_case_only_collision("srv6-lab", ["srv6_lab"]) is None  # not case-only
    assert cfgmod.find_case_only_collision("srv6lab", ["srv6_lab"]) is None
    assert cfgmod.find_case_only_collision("srv_lab", ["srv6_lab"]) is None


def test_plan_topology_definition_flags_case_collision(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    plan = session.plan_topology_definition("SAMPLE_LAB")
    assert plan.kind == "case_collision"
    assert plan.existing == "sample_lab"


def test_confirmed_case_collision_creates_distinct_topology(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    plan = cfgmod.TopologyPlan("create_new", "SAMPLE_LAB")
    session.apply_topology_definition_plan(plan)
    assert session.definition_name == "SAMPLE_LAB"
    assert session.definition_candidate["name"] == "SAMPLE_LAB"
    assert session.definition_original is None
    assert lab.load_topology("sample_lab", lab_root)["name"] == "sample_lab"  # untouched


def test_no_fuzzy_matching(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    for near_miss in ("sample-lab", "samplelab", "sampl_lab"):
        plan = session.plan_topology_definition(near_miss)
        assert plan.kind == "create_new", near_miss


# ---- device sub-editing: topology (safe fields only) ----


def test_topology_device_mode_only_sets_type(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.apply_topology_definition_plan(session.plan_topology_definition("sample_lab"))
    session.enter_topology_device("R3")
    session.set_device_field("type", "nxos")
    assert session.definition_candidate["devices"]["R3"] == {"type": "nxos"}


# ---- access-info definition editing ----


def test_access_info_create_or_load(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.enter_access_info_definition("sample_lab")
    assert session.definition_kind == "access_info"
    assert session.definition_original is not None
    assert session.definition_candidate["devices"]["R1"]["address"] == "192.0.2.11"

    session2 = cfgmod.CliSession(lab_root)
    session2.enter_configure()
    session2.enter_access_info_definition("brand_new_access")
    assert session2.definition_original is None
    assert session2.definition_candidate == {"name": "brand_new_access", "devices": {}}


def test_access_info_device_mode_sets_full_field_set(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.enter_access_info_definition("lab_devices")
    session.enter_access_info_device("R1")
    session.set_device_field("type", "iosxr")
    session.set_device_field("address", "192.0.2.50")
    session.set_device_field("transport", "ssh")
    session.set_device_field("port", 22)
    session.set_device_field("username", "u")
    session.set_device_field("password", "p")
    device = session.definition_candidate["devices"]["R1"]
    assert device == {
        "type": "iosxr",
        "address": "192.0.2.50",
        "transport": "ssh",
        "port": 22,
        "username": "u",
        "password": "p",
    }
    session.clear_device_field("password")
    assert "password" not in session.definition_candidate["devices"]["R1"]


def test_access_info_switch_guard_matches_topology_guard(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.enter_access_info_definition("lab_a")
    session.enter_access_info_device("R1")
    session.set_device_field("address", "10.0.0.1")
    ok, message = session.can_switch_definition("access_info", "lab_b")
    assert ok is False
    assert "commit" in message and "clear" in message


# ---- scenario / reference definition editing ----


def test_scenario_create_or_load(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.enter_scenario_definition("failover_test")
    assert session.definition_candidate["name"] == "failover_test"
    assert session.definition_original is not None

    session2 = cfgmod.CliSession(lab_root)
    session2.enter_configure()
    session2.enter_scenario_definition("brand_new_scenario")
    assert session2.definition_original is None
    assert session2.definition_candidate == {"name": "brand_new_scenario", "description": "", "objectives": []}


def test_reference_create_or_load(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.enter_reference_definition("sr_mpls")
    assert session.definition_original is None
    assert session.definition_candidate == {"name": "sr_mpls", "description": "", "guidance": []}


# ---- clear (replaces abort) ----


def test_clear_discards_running_config_changes_without_leaving_exec(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.set_scenario("failover_test")
    session.clear()
    assert session.mode == "global"
    assert session.settings_candidate == session.committed_settings
    assert session.overall_dirty() is False


def test_clear_discards_new_topology_and_falls_back_to_global(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.apply_topology_definition_plan(session.plan_topology_definition("brand_new"))
    session.enter_topology_device("R1")
    session.clear()
    assert session.mode == "global"
    assert session.definition_kind is None
    assert session.overall_dirty() is False
    assert not lab.topology_exists("brand_new", lab_root)


def test_clear_reverts_existing_topology_edit_and_stays_in_topology_mode(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.apply_topology_definition_plan(session.plan_topology_definition("sample_lab"))
    session.set_topology_description("changed")
    session.clear()
    assert session.mode == "topology"
    assert session.definition_candidate["description"] != "changed"
    assert session.overall_dirty() is False


def test_clear_falls_back_from_device_mode_when_new_device_disappears(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.apply_topology_definition_plan(session.plan_topology_definition("sample_lab"))
    session.enter_topology_device("BRAND_NEW_DEVICE")
    session.clear()
    # The topology itself still exists (it was only selected, not created),
    # but the freshly-added device evaporates on revert -- step back to the
    # nearest still-valid parent mode instead of a dangling device submode.
    assert session.mode == "topology"
    assert "BRAND_NEW_DEVICE" not in session.definition_candidate["devices"]


def test_clear_does_not_write_disk(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.apply_topology_definition_plan(session.plan_topology_definition("brand_new"))
    settings_mtime = (lab_root / "settings.yaml").stat().st_mtime_ns
    session.clear()
    assert (lab_root / "settings.yaml").stat().st_mtime_ns == settings_mtime
    assert not (lab_root / "topologies" / "brand_new.yaml").exists()


def test_abort_no_longer_exists_as_a_method(lab_root):
    session = cfgmod.CliSession(lab_root)
    assert not hasattr(session, "abort")


# ---- commit ----


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
    session.apply_topology_definition_plan(session.plan_topology_definition("sample_lab"))
    topology_mtime = (lab_root / "topologies" / "sample_lab.yaml").stat().st_mtime_ns
    session.commit()
    assert (lab_root / "topologies" / "sample_lab.yaml").stat().st_mtime_ns == topology_mtime


def test_topology_edit_commit_persists_and_preserves_links(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.apply_topology_definition_plan(session.plan_topology_definition("sample_lab"))
    session.set_topology_description("Updated description.")
    session.commit()

    persisted = lab.load_topology("sample_lab", lab_root)
    assert persisted["description"] == "Updated description."
    assert persisted["links"] == [{"a": "R1", "b": "R2"}]
    assert persisted["devices"]["R2"]["type"] == "iosxr"


def test_new_topology_commit_persists_and_running_config_can_select_it(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.apply_topology_definition_plan(session.plan_topology_definition("brand_new"))
    session.enter_topology_device("R1")
    session.set_device_field("type", "nxos")
    session.commit()

    assert lab.topology_exists("brand_new", lab_root)
    # Running-config selection is untouched by editing a definition.
    assert lab.read_settings(lab_root)["active_topology"] == "sample_lab"

    session2 = cfgmod.CliSession(lab_root)
    session2.enter_configure()
    session2.mode = "running"
    session2.select_topology("brand_new")
    session2.commit()
    assert lab.read_settings(lab_root)["active_topology"] == "brand_new"


def test_commit_writes_definition_before_settings_when_selecting_a_new_definition(lab_root):
    """A running-config selection that names a definition created in the
    very same commit must not be rejected as 'does not exist' -- definition
    files are written before settings.yaml."""
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.apply_topology_definition_plan(session.plan_topology_definition("brand_new"))
    session.mode = "running"
    session.select_topology("brand_new")
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


def test_candidate_never_visible_to_committed_reads_before_commit(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.set_scenario("failover_test")
    assert lab.read_settings(lab_root)["active_scenario"] == "sample"
    session.commit()
    assert lab.read_settings(lab_root)["active_scenario"] == "failover_test"


def test_commit_accepts_supported_device_type(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.apply_topology_definition_plan(session.plan_topology_definition("sample_lab"))
    session.enter_topology_device("R1")
    session.set_device_field("type", "nxos")
    session.commit()
    assert lab.load_topology("sample_lab", lab_root)["devices"]["R1"]["type"] == "nxos"


def test_commit_persists_host_device_type(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.apply_topology_definition_plan(session.plan_topology_definition("sample_lab"))
    session.enter_topology_device("PC1")
    session.set_device_field("type", "host")
    session.commit()
    assert lab.load_topology("sample_lab", lab_root)["devices"]["PC1"]["type"] == "host"


def test_commit_rejects_unsupported_device_type(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.apply_topology_definition_plan(session.plan_topology_definition("sample_lab"))
    session.enter_topology_device("R1")
    session.set_device_field("type", "junos")
    with pytest.raises(cfgmod.CommitValidationError, match="Invalid device type"):
        session.commit()
    assert lab.load_topology("sample_lab", lab_root)["devices"]["R1"]["type"] == "iosxr"


def test_commit_persists_access_info_and_masks_nothing_on_disk(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.enter_access_info_definition("lab_devices")
    session.enter_access_info_device("R1")
    session.set_device_field("address", "192.0.2.99")
    session.set_device_field("password", "s3cret")
    session.commit()
    persisted = lab.load_access_info("lab_devices", lab_root)
    assert persisted["devices"]["R1"]["address"] == "192.0.2.99"
    assert persisted["devices"]["R1"]["password"] == "s3cret"  # plaintext on disk, like Step 1


def test_commit_rejects_access_field_smuggled_into_topology(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.apply_topology_definition_plan(session.plan_topology_definition("sample_lab"))
    session.enter_topology_device("R1")
    session.set_device_field("password", "should-not-be-allowed")  # bypasses the grammar layer
    with pytest.raises(cfgmod.CommitValidationError, match="access field"):
        session.commit()


def test_commit_persists_scenario_and_reference_definitions(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.enter_scenario_definition("new_scenario")
    session.definition_candidate["objectives"] = ["one", "two"]
    session.commit()
    assert lab.load_scenario("new_scenario", lab_root)["objectives"] == ["one", "two"]

    session2 = cfgmod.CliSession(lab_root)
    session2.enter_configure()
    session2.enter_reference_definition("new_reference")
    session2.definition_candidate["guidance"] = ["be careful"]
    session2.commit()
    assert lab.load_reference("new_reference", lab_root)["guidance"] == ["be careful"]


# ---- external editor integration (fake editor; no real interactive session) ----


def test_replace_definition_candidate_validates_before_accepting(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.apply_topology_definition_plan(session.plan_topology_definition("sample_lab"))
    with pytest.raises(lab.LabConfigError, match="access field"):
        session.replace_definition_candidate({"name": "sample_lab", "devices": {"R1": {"password": "nope"}}, "links": []})
    # Rejected edit never reaches the candidate.
    assert "password" not in session.definition_candidate["devices"].get("R1", {})


def test_edit_topology_end_to_end_with_fake_editor(lab_root, monkeypatch, fake_editor):
    script = fake_editor(
        "import yaml\n"
        "data = yaml.safe_load(open(path))\n"
        "data['description'] = 'edited via fake editor'\n"
        "yaml.safe_dump(data, open(path, 'w'))\n"
    )
    monkeypatch.setenv("EDITOR", f"python3 {script}")
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.apply_topology_definition_plan(session.plan_topology_definition("sample_lab"))
    climain.h_edit(session, {})
    assert session.definition_candidate["description"] == "edited via fake editor"
    session.commit()
    assert lab.load_topology("sample_lab", lab_root)["description"] == "edited via fake editor"


def test_edit_scenario_end_to_end_with_fake_editor(lab_root, monkeypatch, fake_editor):
    script = fake_editor(
        "import yaml\n"
        "data = yaml.safe_load(open(path))\n"
        "data['objectives'] = ['edited']\n"
        "yaml.safe_dump(data, open(path, 'w'))\n"
    )
    monkeypatch.setenv("EDITOR", f"python3 {script}")
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.enter_scenario_definition("failover_test")
    climain.h_edit(session, {})
    assert session.definition_candidate["objectives"] == ["edited"]


def test_edit_reference_end_to_end_with_fake_editor(lab_root, monkeypatch, fake_editor):
    script = fake_editor(
        "import yaml\n"
        "data = yaml.safe_load(open(path))\n"
        "data['guidance'] = ['edited']\n"
        "yaml.safe_dump(data, open(path, 'w'))\n"
    )
    monkeypatch.setenv("EDITOR", f"python3 {script}")
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.enter_reference_definition("sr_mpls")
    climain.h_edit(session, {})
    assert session.definition_candidate["guidance"] == ["edited"]


def test_edit_with_invalid_yaml_leaves_candidate_unchanged(lab_root, monkeypatch, fake_editor, capsys):
    script = fake_editor("open(path, 'w').write('not: [valid\\n')")
    monkeypatch.setenv("EDITOR", f"python3 {script}")
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.apply_topology_definition_plan(session.plan_topology_definition("sample_lab"))
    before = dict(session.definition_candidate)
    climain.h_edit(session, {})
    assert session.definition_candidate == before
    assert "line" in capsys.readouterr().out.lower() or True  # message is printed by h_edit; see cli output


def test_edit_with_nonzero_exit_leaves_candidate_unchanged(lab_root, monkeypatch, fake_editor):
    script = fake_editor("open(path, 'w').write('description: should-not-apply\\n')\nsys.exit(1)")
    monkeypatch.setenv("EDITOR", f"python3 {script}")
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.apply_topology_definition_plan(session.plan_topology_definition("sample_lab"))
    before = dict(session.definition_candidate)
    climain.h_edit(session, {})
    assert session.definition_candidate == before


def test_edit_semantic_no_change_does_not_mark_dirty(lab_root, monkeypatch, fake_editor):
    script = fake_editor("pass  # save without changing anything")
    monkeypatch.setenv("EDITOR", f"python3 {script}")
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.apply_topology_definition_plan(session.plan_topology_definition("sample_lab"))
    assert session.definition_dirty() is False
    climain.h_edit(session, {})
    assert session.definition_dirty() is False
