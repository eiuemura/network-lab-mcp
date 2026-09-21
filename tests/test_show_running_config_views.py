"""`show running-config <definition-type>` (EXEC only): a read-only
committed-only dereference of one active-definition selection.

    access-info / topology / scenario  -- single-selection, no <name>
    reference                          -- multi-select (ordered list);
                                           bare = all active, in order;
                                           <name> = one active member only

Bare `show running-config` (which definitions are active) is unchanged
and covered elsewhere (test_show_scopes.py) -- this file covers only the
new dereference views. All new renderers reuse the exact same functions
definition-mode `show running-config` already uses; nothing here builds
a second rendering system. Uses the isolated `lab_root` fixture only --
never the real repository lab/."""

from __future__ import annotations

import pytest

from network_lab_mcp import lab
from network_lab_mcp.cli import config as cfgmod
from network_lab_mcp.cli import grammar
from network_lab_mcp.cli import main as climain


def _settings(lab_root, **overrides):
    settings = lab.read_settings(lab_root)
    settings.update(overrides)
    lab.write_settings(settings, lab_root)


def _write_reference(lab_root, name, guidance=("x",)):
    lab.write_reference(name, {"name": name, "description": "", "guidance": list(guidance)}, lab_root)


# ---- bare `show running-config` is unchanged ----


def test_bare_show_running_config_unchanged(lab_root, capsys):
    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "show running-config")
    out = capsys.readouterr().out
    assert " access-info" in out
    assert " topology" in out
    assert " scenario" in out
    assert " reference" in out
    assert "sample_lab" in out  # the selection, not any definition's content


# ---- single-selection views ----


def test_show_running_config_access_info(lab_root, capsys):
    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "show running-config access-info")
    out = capsys.readouterr().out
    assert out.startswith("access-info sample_lab")
    assert "device R1" in out


def test_show_running_config_topology(lab_root, capsys):
    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "show running-config topology")
    out = capsys.readouterr().out
    assert out.startswith("topology sample_lab")
    assert "device R1" in out
    assert "device R2" in out


def test_show_running_config_scenario(lab_root, capsys):
    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "show running-config scenario")
    out = capsys.readouterr().out
    assert "name: sample" in out


def test_show_running_config_access_info_none_active(lab_root, capsys):
    _settings(lab_root, active_access_info=None)
    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "show running-config access-info")
    assert capsys.readouterr().out.strip() == "% No active access-info is configured."


def test_show_running_config_topology_missing_definition_fails_clearly(lab_root, capsys):
    _settings(lab_root, active_topology="does_not_exist")
    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "show running-config topology")
    assert capsys.readouterr().out.startswith("%")


def test_show_running_config_scenario_missing_definition_fails_clearly(lab_root, capsys):
    _settings(lab_root, active_scenario="does_not_exist")
    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "show running-config scenario")
    assert capsys.readouterr().out.startswith("%")


# ---- single-selection commands reject a trailing name (grammar-level) ----


@pytest.mark.parametrize(
    "command",
    [
        "show running-config access-info test_lab",
        "show running-config topology sample",
        "show running-config scenario sample",
    ],
)
def test_single_selection_views_reject_trailing_name(command):
    assert not grammar.parse("exec", command).ok


# ---- reference: zero / one / many ----


def test_reference_zero_active(lab_root, capsys):
    _settings(lab_root, active_references=[])
    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "show running-config reference")
    assert capsys.readouterr().out.strip() == "% No active references are configured."


def test_reference_zero_active_does_not_list_stored_files(lab_root, capsys):
    _write_reference(lab_root, "ospf_basics")
    _settings(lab_root, active_references=[])
    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "show running-config reference")
    out = capsys.readouterr().out
    assert "ospf_basics" not in out


def test_reference_one_active_equals_single_reference_rendering(lab_root, capsys):
    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "show running-config reference")
    bare_out = capsys.readouterr().out
    climain.execute_command_line(session, "show running-config reference sample")
    named_out = capsys.readouterr().out
    assert bare_out == named_out
    assert "!" not in bare_out  # no multi-reference separator wrapper for a single reference


def test_reference_multiple_active_in_committed_order(lab_root, capsys):
    _write_reference(lab_root, "iosxr_basics")
    _write_reference(lab_root, "sr_mpls")
    _write_reference(lab_root, "evpn_basics")
    _settings(lab_root, active_references=["iosxr_basics", "sr_mpls", "evpn_basics"])

    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "show running-config reference")
    out = capsys.readouterr().out

    assert out.index("iosxr_basics") < out.index("sr_mpls") < out.index("evpn_basics")
    assert out.count("!") == 2  # one separator between each of the three blocks


def test_reference_multiple_active_not_sorted(lab_root, capsys):
    _write_reference(lab_root, "zzz_last")
    _write_reference(lab_root, "aaa_first")
    _settings(lab_root, active_references=["zzz_last", "aaa_first"])

    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "show running-config reference")
    out = capsys.readouterr().out
    assert out.index("zzz_last") < out.index("aaa_first")  # committed order, not alphabetical


def test_reference_multiple_uses_existing_single_reference_renderer_for_each_block(lab_root, capsys):
    _write_reference(lab_root, "iosxr_basics", guidance=["g1"])
    _write_reference(lab_root, "sr_mpls", guidance=["g2"])
    _settings(lab_root, active_references=["iosxr_basics", "sr_mpls"])

    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "show running-config reference iosxr_basics")
    solo_a = capsys.readouterr().out
    climain.execute_command_line(session, "show running-config reference sr_mpls")
    solo_b = capsys.readouterr().out

    climain.execute_command_line(session, "show running-config reference")
    combined = capsys.readouterr().out
    assert combined == solo_a.rstrip("\n") + "\n!\n" + solo_b


# ---- reference: one specific active member ----


def test_reference_name_active_renders_only_that_one(lab_root, capsys):
    _write_reference(lab_root, "iosxr_basics")
    _write_reference(lab_root, "sr_mpls")
    _settings(lab_root, active_references=["iosxr_basics", "sr_mpls"])

    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "show running-config reference iosxr_basics")
    out = capsys.readouterr().out
    assert "iosxr_basics" in out
    assert "sr_mpls" not in out


def test_reference_name_inactive_stored_reference_rejected(lab_root, capsys):
    _write_reference(lab_root, "iosxr_basics")
    _write_reference(lab_root, "ospf_basics")  # stored on disk, not active
    _settings(lab_root, active_references=["iosxr_basics"])

    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "show running-config reference ospf_basics")
    assert capsys.readouterr().out.strip() == "% Reference 'ospf_basics' is not active in running-config."


def test_reference_name_nonexistent_reference_rejected(lab_root, capsys):
    _settings(lab_root, active_references=["sample"])
    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "show running-config reference totally_unknown")
    assert capsys.readouterr().out.strip() == "% Reference 'totally_unknown' is not active in running-config."


def test_reference_name_case_sensitive(lab_root, capsys):
    _write_reference(lab_root, "IosXR_Basics")
    _settings(lab_root, active_references=["IosXR_Basics"])
    session = cfgmod.CliSession(lab_root)

    climain.execute_command_line(session, "show running-config reference IosXR_Basics")
    assert "IosXR_Basics" in capsys.readouterr().out

    climain.execute_command_line(session, "show running-config reference iosxr_basics")
    assert capsys.readouterr().out.strip() == "% Reference 'iosxr_basics' is not active in running-config."


# ---- candidate state must never leak into these committed views ----


def test_reference_view_ignores_candidate_addition(lab_root, capsys):
    """The new EXEC-only views are grammar-unreachable while a candidate
    is dirty (there is no navigation path from a dirty configure session
    back to EXEC without `commit`/`clear` first -- `end`/`exit` are
    guarded exactly to prevent that). That itself is part of what keeps
    candidate state from leaking in; this calls the handler directly to
    additionally confirm it reads only committed state regardless."""
    _write_reference(lab_root, "sr_mpls")
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.mode = "running"
    climain.execute_command_line(session, "reference sr_mpls")  # candidate-only addition
    assert session.settings_candidate["active_references"] == ["sample", "sr_mpls"]

    # EXEC's `show running-config reference` genuinely cannot be reached
    # from here without committing or clearing first.
    assert not grammar.parse("running", "show running-config reference").ok

    climain.h_show_running_config_reference(session, {})
    out = capsys.readouterr().out
    assert "sr_mpls" not in out  # not committed yet

    climain.h_show_running_config_reference_name(session, {"name": "sr_mpls"})
    assert capsys.readouterr().out.strip() == "% Reference 'sr_mpls' is not active in running-config."


def test_reference_view_reflects_state_after_commit(lab_root, capsys):
    _write_reference(lab_root, "sr_mpls")
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.mode = "running"
    climain.execute_command_line(session, "reference sr_mpls")
    climain.execute_command_line(session, "commit")
    climain.execute_command_line(session, "end")  # clean now -- reachable normally
    assert session.mode == "exec"
    capsys.readouterr()

    climain.execute_command_line(session, "show running-config reference")
    out = capsys.readouterr().out
    assert "sample" in out and "sr_mpls" in out

    climain.execute_command_line(session, "show running-config reference sr_mpls")
    assert "sr_mpls" in capsys.readouterr().out


def test_topology_view_ignores_candidate_edit(lab_root, capsys):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.apply_topology_definition_plan(session.plan_topology_definition("sample_lab"))
    session.set_topology_description("uncommitted edit")

    climain.execute_command_line(session, "root")
    climain.execute_command_line(session, "show running-config topology")
    out = capsys.readouterr().out
    assert "uncommitted edit" not in out


# ---- multi-reference loading is fail-closed and atomic ----


def test_multi_reference_missing_active_reference_fails_without_partial_output(lab_root, capsys):
    _write_reference(lab_root, "valid_a")
    _write_reference(lab_root, "valid_b")
    _settings(lab_root, active_references=["valid_a", "missing_reference", "valid_b"])

    session = cfgmod.CliSession(lab_root)
    with pytest.raises(lab.LabConfigError):
        climain.h_show_running_config_reference(session, {})
    out = capsys.readouterr().out
    assert "valid_a" not in out  # no partial rendering happened before the failure


def test_multi_reference_invalid_active_reference_fails_without_partial_output(lab_root, capsys):
    _write_reference(lab_root, "valid_a")
    (lab_root / "references" / "invalid_reference.yaml").write_text("not-a-mapping\n- broken\n")
    _write_reference(lab_root, "valid_b")
    _settings(lab_root, active_references=["valid_a", "invalid_reference", "valid_b"])

    session = cfgmod.CliSession(lab_root)
    with pytest.raises(lab.LabConfigError):
        climain.h_show_running_config_reference(session, {})
    out = capsys.readouterr().out
    assert "valid_a" not in out


def test_individual_reference_failure_does_not_mutate_running_config(lab_root, capsys):
    (lab_root / "references" / "sample.yaml").write_text("not-a-mapping\n- broken\n")
    before = lab.read_settings(lab_root)

    session = cfgmod.CliSession(lab_root)
    with pytest.raises(lab.LabConfigError):
        climain.h_show_running_config_reference_name(session, {"name": "sample"})

    assert lab.read_settings(lab_root) == before


# ---- `?` and Tab completion (grammar-level, dynamic committed state) ----


def test_show_running_config_help_lists_definition_types_and_cr():
    ctx = grammar.CliContext()
    result = grammar.help("exec", "show running-config ", ctx)
    assert [line.token for line in result.lines] == ["access-info", "topology", "scenario", "reference"]
    assert result.show_cr


def test_show_running_config_reference_help_lists_only_active_names_and_cr():
    ctx = grammar.CliContext(committed_active_reference_names=("iosxr_basics", "sr_mpls"))
    result = grammar.help("exec", "show running-config reference ", ctx)
    assert [line.token for line in result.lines] == ["iosxr_basics", "sr_mpls"]
    assert result.show_cr


def test_show_running_config_reference_help_excludes_inactive_and_candidate_names():
    # Only committed_active_reference_names feeds this provider -- an
    # inactive stored reference or a candidate-only addition must not
    # appear just because some *other* context field happens to list it.
    ctx = grammar.CliContext(
        committed_active_reference_names=("iosxr_basics",),
        reference_names=("iosxr_basics", "ospf_basics"),  # every stored file
        candidate_reference_names=("iosxr_basics", "candidate_only"),
    )
    result = grammar.help("exec", "show running-config reference ", ctx)
    assert [line.token for line in result.lines] == ["iosxr_basics"]


def test_show_running_config_reference_tab_completion_active_only():
    ctx = grammar.CliContext(committed_active_reference_names=("iosxr_basics", "sr_mpls"))
    assert grammar.complete("exec", "show running-config reference ", ctx).candidates == [
        "iosxr_basics",
        "sr_mpls",
    ]
    assert grammar.complete("exec", "show running-config reference i", ctx).candidates == ["iosxr_basics"]


def test_build_context_populates_committed_active_reference_names(lab_root):
    _write_reference(lab_root, "sr_mpls")
    _settings(lab_root, active_references=["sample", "sr_mpls"])
    session = cfgmod.CliSession(lab_root)
    ctx = climain.build_context(session)
    assert ctx.committed_active_reference_names == ("sample", "sr_mpls")


# ---- fixed-keyword parsing / abbreviation / ambiguity ----


def test_fixed_keyword_abbreviation_for_definition_types():
    for command, action in (
        ("show running-config access", "exec.show_running_config_access_info"),
        ("show running-config top", "exec.show_running_config_topology"),
        ("show running-config scen", "exec.show_running_config_scenario"),
        ("show running-config ref", "exec.show_running_config_reference"),
    ):
        result = grammar.parse("exec", command)
        assert result.ok, command
        assert result.action == action, command


def test_unknown_definition_type_keyword_rejected():
    result = grammar.parse("exec", "show running-config bogus")
    assert not result.ok


def test_reference_name_is_not_abbreviated_or_prefix_matched(lab_root, capsys):
    _write_reference(lab_root, "iosxr_basics")
    _settings(lab_root, active_references=["iosxr_basics"])
    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "show running-config reference iosxr")
    assert capsys.readouterr().out.strip() == "% Reference 'iosxr' is not active in running-config."


# ---- definition-mode renderer equivalence ----


def test_definition_mode_reference_view_matches_exec_running_config_reference_view(lab_root, capsys):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.enter_reference_definition("sample")
    climain.execute_command_line(session, "show running-config")
    definition_mode_out = capsys.readouterr().out

    climain.execute_command_line(session, "root")
    climain.execute_command_line(session, "end")
    climain.execute_command_line(session, "show running-config reference sample")
    exec_out = capsys.readouterr().out

    assert definition_mode_out == exec_out


def test_definition_mode_topology_view_matches_exec_running_config_topology_view(lab_root, capsys):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.apply_topology_definition_plan(session.plan_topology_definition("sample_lab"))
    climain.execute_command_line(session, "show running-config")
    definition_mode_out = capsys.readouterr().out

    climain.execute_command_line(session, "root")
    climain.execute_command_line(session, "end")
    climain.execute_command_line(session, "show running-config topology")
    exec_out = capsys.readouterr().out

    assert definition_mode_out == exec_out
