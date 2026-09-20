"""Tests for context-aware `show running-config` / `show configuration`
scoping, and the EXEC-only restriction on `show version`.

General rule: in EXEC/global/running mode, `show running-config` is the
committed MCP running-config selection (unchanged). In every other mode
(a topology/access-info/scenario/reference definition, or its nested
device submode), `show running-config` means "what is currently committed
for *this* object" (re-read fresh from disk, empty if never committed) and
`show configuration` means "what am I currently editing" (the candidate,
scoped to the current device inside a device submode)."""

from __future__ import annotations

from network_lab_mcp import lab
from network_lab_mcp.cli import config as cfgmod
from network_lab_mcp.cli import main as climain


# ---- EXEC / global / running: unchanged MCP-selection semantics ----


def test_exec_show_running_config_is_mcp_selection(lab_root):
    session = cfgmod.CliSession(lab_root)
    text = climain.render_committed_running_config(session)
    assert "topology" in text and "sample_lab" in text
    assert "scenario" in text and "sample" in text


def test_global_mode_show_running_config_unaffected_by_open_definition(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.apply_topology_definition_plan(session.plan_topology_definition("brand_new"))
    session.mode = "global"  # as if the operator `exit`-ed back up
    climain.h_show_running_config(session, {})
    # Still the MCP selection, not the open (unrelated) topology definition.
    text = climain.render_committed_running_config(session)
    assert "sample_lab" in text


def test_running_mode_show_running_config_is_committed_mcp_selection(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.mode = "running"
    session.select_topology("sample_lab")  # no-op, already committed value
    text = climain.render_committed_running_config(session)
    assert "sample_lab" in text


def test_running_mode_show_configuration_is_candidate_selection(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.mode = "running"
    session.apply_topology_definition_plan(session.plan_topology_definition("test"))  # unrelated definition edit
    session.mode = "running"
    session.select_topology("sample_lab")
    text = climain.render_configuration_candidate(session)
    assert "sample_lab" in text


# ---- show version: EXEC only ----


def test_show_version_grammar_rejected_outside_exec():
    from network_lab_mcp.cli import grammar

    for mode in ("global", "running", "topology", "device", "access_info", "access_device", "scenario", "reference"):
        result = grammar.parse(mode, "show version")
        assert not result.ok, mode


# ---- new (never-committed) access-info definition ----


def test_new_access_info_running_config_is_empty(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.enter_access_info_definition("test")
    assert climain.render_committed_definition(session) == ""


def test_new_access_info_configuration_shows_only_candidate(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.enter_access_info_definition("test")
    text = climain.render_configuration_candidate(session)
    assert "sample_lab" not in text  # never another definition's data
    assert "test" in text


def test_new_access_info_device_running_config_empty_until_commit(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.enter_access_info_definition("test")
    session.enter_access_info_device("R1")
    session.set_device_field("type", "iosxr")
    session.set_device_field("address", "192.0.2.50")
    session.set_device_field("password", "s3cret")

    assert climain.render_committed_definition(session) == ""
    candidate_text = climain.render_configuration_candidate(session)
    assert "R1" in candidate_text
    assert "192.0.2.50" in candidate_text
    assert "s3cret" not in candidate_text  # masked even in the candidate view
    assert "********" in candidate_text

    session.commit()
    committed_text = climain.render_committed_definition(session)
    assert "R1" in committed_text
    assert "192.0.2.50" in committed_text
    assert "s3cret" not in committed_text


# ---- existing access-info definition ----


def test_existing_access_info_running_config_vs_configuration(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.enter_access_info_definition("sample_lab")
    session.enter_access_info_device("R1")
    session.set_device_field("address", "192.0.2.99")

    committed_text = climain.render_committed_definition(session)
    candidate_text = climain.render_configuration_candidate(session)
    assert "192.0.2.11" in committed_text  # original committed address
    assert "192.0.2.99" not in committed_text
    assert "192.0.2.99" in candidate_text  # modified candidate address
    assert "********" in committed_text and "********" in candidate_text


def test_clear_restores_committed_view_in_configuration(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.enter_access_info_definition("sample_lab")
    session.enter_access_info_device("R1")
    session.set_device_field("address", "192.0.2.99")
    assert "192.0.2.99" in climain.render_configuration_candidate(session)

    session.clear()
    assert not lab.access_info_exists("test", lab_root)  # unrelated: sanity
    assert "192.0.2.99" not in climain.render_configuration_candidate(session)
    assert "192.0.2.11" in climain.render_configuration_candidate(session)


def test_clear_on_brand_new_definition_leaves_no_disk_write_and_falls_back(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.enter_access_info_definition("test")
    session.enter_access_info_device("R1")
    session.set_device_field("address", "192.0.2.50")
    session.clear()
    assert session.mode == "global"
    assert session.definition_kind is None
    assert not lab.access_info_exists("test", lab_root)


# ---- topology ----


def test_new_topology_running_config_is_empty(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.apply_topology_definition_plan(session.plan_topology_definition("brand_new"))
    assert climain.render_committed_definition(session) == ""


def test_existing_topology_running_config_vs_configuration(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.apply_topology_definition_plan(session.plan_topology_definition("sample_lab"))
    session.set_topology_description("Edited description.")

    committed_text = climain.render_committed_definition(session)
    candidate_text = climain.render_configuration_candidate(session)
    assert "Edited description." not in committed_text
    assert "Edited description." in candidate_text
    # No access fields, links preserved on the committed side.
    assert "address" not in committed_text and "password" not in committed_text


def test_topology_device_mode_scopes_to_one_device(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.apply_topology_definition_plan(session.plan_topology_definition("sample_lab"))
    session.enter_topology_device("R1")
    committed_text = climain.render_committed_definition(session)
    assert "R1" in committed_text
    assert "R2" not in committed_text  # scoped away, even though R2 exists in the topology


def test_new_topology_device_running_config_empty(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.apply_topology_definition_plan(session.plan_topology_definition("sample_lab"))
    session.enter_topology_device("BRAND_NEW_DEVICE")
    session.set_device_field("type", "host")
    assert climain.render_committed_definition(session) == ""
    assert "BRAND_NEW_DEVICE" in climain.render_configuration_candidate(session)


# ---- scenario / reference ----


def test_new_scenario_running_config_is_empty(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.enter_scenario_definition("brand_new_scenario")
    assert climain.render_committed_definition(session) == ""
    assert "brand_new_scenario" in climain.render_configuration_candidate(session)


def test_existing_scenario_running_config_vs_configuration(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.enter_scenario_definition("failover_test")
    session.definition_candidate["description"] = "Edited."
    committed_text = climain.render_committed_definition(session)
    candidate_text = climain.render_configuration_candidate(session)
    assert "Edited." not in committed_text
    assert "Edited." in candidate_text


def test_new_reference_running_config_is_empty(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.enter_reference_definition("brand_new_reference")
    assert climain.render_committed_definition(session) == ""


def test_scenario_edit_with_fake_editor_then_show_scopes(lab_root, monkeypatch, fake_editor):
    from network_lab_mcp.cli import main as climain2

    script = fake_editor(
        "import yaml\n"
        "data = yaml.safe_load(open(path))\n"
        "data['description'] = 'edited via fake editor'\n"
        "yaml.safe_dump(data, open(path, 'w'))\n"
    )
    monkeypatch.setenv("EDITOR", f"python3 {script}")
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.enter_scenario_definition("failover_test")
    climain2.h_edit(session, {})
    committed_text = climain.render_committed_definition(session)
    candidate_text = climain.render_configuration_candidate(session)
    assert "edited via fake editor" not in committed_text
    assert "edited via fake editor" in candidate_text


# ---- after commit, running-config reflects the new committed state ----


def test_after_commit_running_config_shows_newly_committed_state(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.apply_topology_definition_plan(session.plan_topology_definition("brand_new"))
    session.set_topology_description("Just committed.")
    session.commit()
    assert "Just committed." in climain.render_committed_definition(session)
