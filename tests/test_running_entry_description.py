"""Running-config *entry* descriptions (`config-running`):
`<kind> <name> description <text>` / `no <kind> <name> description` for the
ACTIVE access-info / topology / scenario / each active reference. The
description is metadata of the running entry (stored in settings.yaml under
`running_descriptions`), never of the definition, and never a selection.
Isolated `lab_root` fixture only."""

from __future__ import annotations

import pytest

from network_lab_mcp import lab
from network_lab_mcp.cli import config as cfgmod
from network_lab_mcp.cli import grammar
from network_lab_mcp.cli import main as climain

KINDS = [
    # kind, cli keyword, settings key, active name, other definition name
    ("access_info", "access-info", "active_access_info", "sample_lab", "other_ai"),
    ("topology", "topology", "active_topology", "sample_lab", "other_topo"),
    ("scenario", "scenario", "active_scenario", "sample", "other_sc"),
]


@pytest.fixture()
def root(lab_root):
    lab.write_access_info("other_ai", {"name": "other_ai", "devices": {}}, lab_root)
    lab.write_topology("other_topo", {"name": "other_topo", "description": "", "devices": {}, "links": []}, lab_root)
    lab.write_scenario("other_sc", {"name": "other_sc", "description": "", "objectives": []}, lab_root)
    for ref in ("ref_b", "ref_c"):
        lab.write_reference(ref, {"name": ref, "description": "", "guidance": []}, lab_root)
    return lab_root


def _running(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    climain.execute_command_line(session, "running")
    assert session.mode == "running"
    return session


def _run(session, line):
    climain.execute_command_line(session, line)


def _committed_desc(lab_root):
    return lab.read_settings(lab_root).get("running_descriptions")


@pytest.mark.parametrize("kind,kw,key,name,other", KINDS)
def test_set_overwrite_clear_commit_cycle(root, kind, kw, key, name, other):
    session = _running(root)
    _run(session, f"{kw} {name} description Local lab")
    assert session.settings_candidate["running_descriptions"][kind][name] == "Local lab"
    assert session.settings_candidate[key] == name  # selection untouched
    assert _committed_desc(root) is None  # candidate only
    assert session.overall_dirty()
    _run(session, f"{kw} {name} description   Overwritten  ")
    assert session.settings_candidate["running_descriptions"][kind][name] == "Overwritten"
    _run(session, "clear")
    assert not session.overall_dirty()
    _run(session, f"{kw} {name} description Kept")
    _run(session, "commit")
    assert _committed_desc(root) == {kind: {name: "Kept"}}
    fresh = cfgmod.CliSession(root)
    assert f"   description Kept" in climain.render_committed_running_config(fresh)
    session = _running(root)
    _run(session, f"no {kw} {name} description")
    assert session.settings_candidate[key] == name  # still active
    assert _committed_desc(root) == {kind: {name: "Kept"}}
    _run(session, "commit")
    assert _committed_desc(root) is None
    assert lab.read_settings(root)[key] == name


@pytest.mark.parametrize("kind,kw,key,name,other", KINDS)
def test_empty_and_whitespace_rejected(root, kind, kw, key, name, other):
    session = _running(root)
    for line in (f"{kw} {name} description", f"{kw} {name} description    "):
        _run(session, line)
    assert not session.overall_dirty()
    assert grammar.parse("running", f"{kw} {name} description   ").ok is False


@pytest.mark.parametrize("kind,kw,key,name,other", KINDS)
def test_inactive_entry_fails_closed(root, kind, kw, key, name, other, capsys):
    session = _running(root)
    _run(session, f"{kw} {other} description Nope")
    _run(session, f"no {kw} {other} description")
    assert session.settings_candidate[key] == name
    assert "running_descriptions" not in session.settings_candidate
    assert not session.overall_dirty()
    assert "not active in running-config" in capsys.readouterr().out


@pytest.mark.parametrize("kind,kw,key,name,other", KINDS)
def test_uncommitted_switch_then_restore_keeps_committed_description(root, kind, kw, key, name, other):
    session = _running(root)
    _run(session, f"{kw} {name} description A")
    _run(session, "commit")
    _run(session, f"{kw} {other}")
    assert "   description" not in "\n".join(climain.render_configuration_candidate(session).splitlines()[:0])
    _run(session, f"{kw} {name}")
    assert session.settings_candidate["running_descriptions"][kind][name] == "A"
    assert not session.overall_dirty()


@pytest.mark.parametrize("kind,kw,key,name,other", KINDS)
def test_committed_switch_then_reselect_does_not_resurrect(root, kind, kw, key, name, other):
    session = _running(root)
    _run(session, f"{kw} {name} description A")
    _run(session, "commit")
    _run(session, f"{kw} {other}")
    _run(session, "commit")
    assert _committed_desc(root) is None
    _run(session, f"{kw} {name}")
    assert "running_descriptions" not in climain.cfgmod.normalized_settings(session.settings_candidate)
    _run(session, "commit")
    assert _committed_desc(root) is None


@pytest.mark.parametrize("kind,kw,key,name,other", KINDS)
def test_net_zero_is_clean(root, kind, kw, key, name, other):
    session = _running(root)
    _run(session, f"{kw} {name} description A")
    _run(session, "commit")
    _run(session, f"{kw} {name} description B")
    assert session.overall_dirty()
    _run(session, f"{kw} {name} description A")
    assert not session.overall_dirty()
    _run(session, f"no {kw} {name} description")
    _run(session, "commit")
    _run(session, f"{kw} {name} description X")
    _run(session, f"no {kw} {name} description")
    assert not session.overall_dirty()


def test_access_info_no_description_does_not_deactivate_and_bare_no_still_does(root):
    session = _running(root)
    _run(session, "access-info sample_lab description A")
    _run(session, "no access-info sample_lab description")
    assert session.settings_candidate["active_access_info"] == "sample_lab"
    _run(session, "no access-info")
    assert "active_access_info" not in session.settings_candidate


def test_references_are_independent(root):
    session = _running(root)
    _run(session, "reference ref_b")
    _run(session, "reference ref_c")
    _run(session, "reference sample description S")
    _run(session, "reference ref_b description B")
    _run(session, "reference ref_c description C")
    _run(session, "commit")
    out = climain.render_committed_running_config(cfgmod.CliSession(root)).splitlines()
    assert out[-9:] == [
        "!", " reference", "  sample", "   description S", "  ref_b", "   description B", "  ref_c",
        "   description C", "!",
    ]
    session = _running(root)
    _run(session, "no reference ref_b description")
    assert session.settings_candidate["active_references"] == ["sample", "ref_b", "ref_c"]
    assert session.settings_candidate["running_descriptions"]["reference"] == {"sample": "S", "ref_c": "C"}
    _run(session, "commit")
    # uncommitted removal / re-add restores committed text
    _run(session, "no reference ref_c")
    _run(session, "reference ref_c")
    assert session.settings_candidate["running_descriptions"]["reference"]["ref_c"] == "C"
    _run(session, "clear")
    assert not session.overall_dirty()
    # committed removal / re-add does not resurrect
    _run(session, "no reference ref_c")
    _run(session, "commit")
    assert _committed_desc(root) == {"reference": {"sample": "S"}}
    _run(session, "reference ref_c")
    _run(session, "commit")
    assert _committed_desc(root) == {"reference": {"sample": "S"}}


def test_inactive_reference_not_auto_activated(root, capsys):
    session = _running(root)
    _run(session, "reference ref_b description X")
    assert session.settings_candidate["active_references"] == ["sample"]
    assert not session.overall_dirty()
    assert "not active" in capsys.readouterr().out


def test_active_definition_deletion_policy_unchanged_and_no_orphans(root, capsys):
    session = _running(root)
    _run(session, "access-info sample_lab description A")
    _run(session, "commit")
    _run(session, "root")
    _run(session, "no access-info sample_lab")
    _run(session, "commit")
    assert "active in running-config" in capsys.readouterr().out
    assert lab.access_info_exists("sample_lab", root)
    assert _committed_desc(root) == {"access_info": {"sample_lab": "A"}}
    # switch selection + delete in the same commit (existing policy)
    _run(session, "running")
    _run(session, "access-info other_ai")
    _run(session, "commit")
    assert _committed_desc(root) is None
    _run(session, "root")
    _run(session, "no access-info sample_lab")
    _run(session, "commit")
    assert not lab.access_info_exists("sample_lab", root)
    lab.write_access_info("sample_lab", {"name": "sample_lab", "devices": {}}, root)
    _run(session, "running")
    _run(session, "access-info sample_lab")
    _run(session, "commit")
    assert _committed_desc(root) is None


def test_rendering_unchanged_without_descriptions_and_independent_of_definitions(root):
    data = lab.load_topology("sample_lab", root)
    data["description"] = "DEFINITION TEXT"
    lab.write_topology("sample_lab", data, root)
    out = climain.render_committed_running_config(cfgmod.CliSession(root)).splitlines()
    assert out == ["!", " access-info", "  sample_lab", "!", " topology", "  sample_lab",
                   "!", " scenario", "  sample", "!", " reference", "  sample", "!"]
    session = _running(root)
    _run(session, "topology sample_lab description RUNNING TEXT")
    _run(session, "commit")
    out = "\n".join(climain.render_committed_running_config(cfgmod.CliSession(root)).splitlines())
    assert out.count("description") == 1 and "RUNNING TEXT" in out and "DEFINITION TEXT" not in out
    assert lab.load_topology("sample_lab", root)["description"] == "DEFINITION TEXT"
    assert "RUNNING" not in open(root / "topologies" / "sample_lab.yaml").read()


def test_stale_hand_edited_descriptions_not_rendered_or_kept(root):
    settings = lab.read_settings(root)
    settings["running_descriptions"] = {"topology": {"gone": "stale"}, "reference": {"sample": "R"}}
    lab.write_settings(settings, root)
    out = climain.render_committed_running_config(cfgmod.CliSession(root))
    assert "stale" not in out and "   description R" in out


def test_delta_rendering(root):
    session = _running(root)
    _run(session, "topology sample_lab description T")
    _run(session, "reference ref_b")
    _run(session, "reference ref_b description B")
    text = climain.render_configuration_candidate(session)
    assert " topology\n  sample_lab\n   description T" in text
    assert " reference\n  ref_b\n   description B" in text
    _run(session, "commit")
    _run(session, "no topology sample_lab description")
    assert climain.render_configuration_candidate(session) == "no topology sample_lab description"


def test_help_and_grammar_shape():
    ctx = grammar.CliContext()
    result = grammar.help("running", "topology sample_lab ", ctx)
    assert [l.token for l in result.lines] == ["description"] and result.show_cr
    assert [l.token for l in grammar.help("running", "topology sample_lab description ", ctx).lines] == ["<text>"]
    assert grammar.parse("running", "topology sample_lab").action == "running.topology"
    assert grammar.parse("running", "reference r").action == "running.reference_add"
    assert grammar.parse("running", "no reference r").action == "running.reference_remove"
    assert grammar.parse("running", "no access-info").action == "running.access_info_remove"
    assert grammar.parse("running", "no reference r description").action == "running.reference_clear_description"
    assert not grammar.parse("running", "no topology sample_lab").ok
    assert not grammar.parse("running", "no scenario sample").ok
    assert "description" in grammar.complete("running", "access-info sample_lab d", ctx).candidates or \
        grammar.complete("running", "access-info sample_lab d", ctx).completed


@pytest.mark.parametrize("mode", ["access_info", "access_device", "access_jump_host", "device", "scenario", "reference"])
def test_definition_modes_gain_no_description(mode):
    assert not grammar.parse(mode, "description x").ok
    assert not grammar.parse(mode, "no description").ok


def test_topology_definition_description_untouched(root):
    assert grammar.parse("topology", "description x").action == "topology.description"
    assert not grammar.parse("topology", "no description").ok


# ==========================================================================
# IOS XR-like executable-state inline help (shared grammar mechanism)
# ==========================================================================


def _help(mode, text):
    return grammar.help(mode, text, grammar.CliContext())


@pytest.mark.parametrize("kw", ["access-info", "topology", "scenario", "reference"])
def test_description_text_help_executable_state(kw):
    before = _help("running", f"{kw} x description ")
    assert [l.token for l in before.lines] == ["<text>"] and not before.show_cr
    for text in ("test", "arbitrary-valid-text", "SR-MPLS", "two words", "two words "):
        after = _help("running", f"{kw} x description {text}")
        assert [l.token for l in after.lines] == ["<text>"], text
        assert after.show_cr, text
    blank = _help("running", f"{kw} x description    ")
    assert not blank.show_cr
    assert grammar.parse("running", f"{kw} x description test").ok
    assert not grammar.parse("running", f"{kw} x description").ok
    assert not grammar.parse("running", f"{kw} x description   ").ok


@pytest.mark.parametrize("kw", ["topology", "scenario"])
def test_no_topology_scenario_help_exact_token(kw):
    prefix = _help("running", f"no {kw} x ")
    assert [l.token for l in prefix.lines] == ["description"] and not prefix.show_cr
    assert not grammar.parse("running", f"no {kw} x").ok
    exact = _help("running", f"no {kw} x description")
    assert exact.show_cr
    assert grammar.parse("running", f"no {kw} x description").ok
    # same rule as the other kinds' exact literal
    assert _help("running", "no reference x description").show_cr


def test_exact_literal_removal_executes_metadata_only(root):
    session = _running(root)
    for kw in ("topology", "scenario"):
        name = "sample_lab" if kw == "topology" else "sample"
        _run(session, f"{kw} {name} description D")
        _run(session, f"no {kw} {name} description")
        assert "running_descriptions" not in cfgmod.normalized_settings(session.settings_candidate)
        assert session.settings_candidate[f"active_{kw}"] == name


def test_selection_only_show_configuration_has_no_description_lines(root):
    session = _running(root)
    _run(session, "access-info other_ai")
    _run(session, "topology other_topo")
    _run(session, "scenario other_sc")
    _run(session, "reference ref_b")
    text = climain.render_configuration_candidate(session)
    assert text == "\n".join([
        "!", " access-info", "  other_ai", "!",
        "!", " topology", "  other_topo", "!",
        "!", " scenario", "  other_sc", "!",
        "!", " reference", "  ref_b", "!",
    ])
    assert "description" not in text
    _run(session, "commit")
    _run(session, "no reference ref_b")
    _run(session, "no access-info")
    assert climain.render_configuration_candidate(session) == "no access-info\nno reference ref_b"


# ==========================================================================
# Reserved-name collision: an entry literally named `description`
# ==========================================================================


def test_entry_named_description_is_unambiguous(root):
    lab.write_reference("description", {"name": "description", "description": "", "guidance": []}, root)
    lab.write_access_info("description", {"name": "description", "devices": {}}, root)
    parsed = grammar.parse("running", "reference description")
    assert parsed.ok and parsed.action == "running.reference_add" and parsed.args == {"name": "description"}
    parsed = grammar.parse("running", "reference description description Some running note")
    assert parsed.ok and parsed.action == "running.reference_description"
    assert parsed.args == {"name": "description", "text": "Some running note"}
    assert grammar.parse("running", "no reference description description").action == "running.reference_clear_description"
    assert grammar.parse("running", "no reference description").action == "running.reference_remove"

    session = _running(root)
    _run(session, "reference description")
    _run(session, "reference description description Some running note")
    assert session.settings_candidate["active_references"] == ["sample", "description"]
    _run(session, "access-info description")
    _run(session, "access-info description description Odd name")
    _run(session, "commit")
    out = climain.render_committed_running_config(cfgmod.CliSession(root))
    assert "  description\n   description Some running note" in out
    assert "  description\n   description Odd name" in out
    _run(session, "no reference description description")
    assert session.settings_candidate["active_references"] == ["sample", "description"]


def test_combined_selection_and_description_delta(root):
    session = _running(root)
    _run(session, "access-info sample_lab description Old")
    _run(session, "commit")
    _run(session, "access-info other_ai")
    _run(session, "access-info other_ai description New access")
    assert climain.render_configuration_candidate(session) == "!\n access-info\n  other_ai\n   description New access\n!"
    _run(session, "clear")
    _run(session, "access-info sample_lab description Newer")
    assert climain.render_configuration_candidate(session) == "!\n access-info\n  sample_lab\n   description Newer\n!"


def test_uncommitted_switch_and_back_after_commit_restores_and_commit_drops(root):
    session = _running(root)
    _run(session, "access-info sample_lab description A")
    _run(session, "commit")
    _run(session, "access-info other_ai")
    _run(session, "access-info sample_lab")
    assert not session.overall_dirty()
    assert climain.render_configuration_candidate(session) == ""
    _run(session, "access-info other_ai")
    _run(session, "commit")
    _run(session, "access-info sample_lab")
    assert "description" not in climain.render_configuration_candidate(session)
    assert cfgmod.normalized_settings(session.settings_candidate).get("running_descriptions") is None
