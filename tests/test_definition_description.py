"""Definition-level `description` / `no description` for the CLI-managed
definitions (access-info, topology). Human-readable metadata only: it
follows the ordinary candidate/commit/clear and delete/restore lifecycle of
the definition itself. scenario/reference are YAML-managed and out of
scope. Uses only the isolated `lab_root` fixture."""

from __future__ import annotations

import pytest

from network_lab_mcp import lab
from network_lab_mcp.cli import config as cfgmod
from network_lab_mcp.cli import grammar
from network_lab_mcp.cli import main as climain

KINDS = [
    ("access_info", "access-info", lab.load_access_info),
    ("topology", "topology", lab.load_topology),
]
DESC = "Example description for a fictional lab"


def _session(lab_root):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    return session


def _run(session, line):
    climain.execute_command_line(session, line)


def _enter(session, kw):
    _run(session, f"{kw} sample_lab")
    assert session.definition_name == "sample_lab"


def _set_committed(lab_root, kind, loader, description):
    data = loader("sample_lab", lab_root)
    if description is None:
        data.pop("description", None)
    else:
        data["description"] = description
    getattr(lab, f"write_{kind}")("sample_lab", data, lab_root)


@pytest.mark.parametrize("kind,kw,loader", KINDS)
def test_set_is_candidate_only_then_commit_persists(lab_root, kind, kw, loader):
    _set_committed(lab_root, kind, loader, None)
    session = _session(lab_root)
    _enter(session, kw)
    _run(session, f"description {DESC}")
    assert "description" not in loader("sample_lab", lab_root) or loader("sample_lab", lab_root)["description"] != DESC
    assert f" description {DESC}" in climain.render_configuration_candidate(session)
    _run(session, "commit")
    assert loader("sample_lab", lab_root)["description"] == DESC
    # fresh process
    fresh = _session(lab_root)
    _enter(fresh, kw)
    assert f" description {DESC}" in climain.render_committed_definition(fresh)
    assert fresh.overall_dirty() is False


@pytest.mark.parametrize("kind,kw,loader", KINDS)
def test_overwrite_no_description_and_clear(lab_root, kind, kw, loader):
    _set_committed(lab_root, kind, loader, "A")
    session = _session(lab_root)
    _enter(session, kw)
    _run(session, "description B")
    assert session.overall_dirty()
    _run(session, "clear")
    assert session.definition_candidate["description"] == "A"
    assert loader("sample_lab", lab_root)["description"] == "A"
    _run(session, "no description")
    assert "description" not in session.definition_candidate
    assert loader("sample_lab", lab_root)["description"] == "A"  # not committed yet
    assert " no description" in climain.render_configuration_candidate(session)
    _run(session, "clear")
    assert session.definition_candidate["description"] == "A"
    _run(session, "no description")
    _run(session, "commit")
    assert not loader("sample_lab", lab_root).get("description")
    assert "description" not in climain.render_committed_definition(session)


@pytest.mark.parametrize("kind,kw,loader", KINDS)
def test_net_zero_mutations_are_clean(lab_root, kind, kw, loader):
    _set_committed(lab_root, kind, loader, "A")
    session = _session(lab_root)
    _enter(session, kw)
    _run(session, "description B")
    _run(session, "description A")
    assert session.definition_dirty() is False
    _set_committed(lab_root, kind, loader, None)
    session = _session(lab_root)
    _enter(session, kw)
    _run(session, "description A")
    _run(session, "no description")
    assert session.definition_dirty() is False


@pytest.mark.parametrize("kind,kw,loader", KINDS)
def test_empty_and_whitespace_rejected(lab_root, kind, kw, loader, capsys):
    session = _session(lab_root)
    _enter(session, kw)
    for line in ("description", "description    ", "description \t "):
        _run(session, line)
    assert "description" not in session.definition_candidate or session.definition_candidate["description"] == loader("sample_lab", lab_root).get("description")
    assert session.definition_dirty() is False
    assert grammar.parse(session.mode, "description   ").ok is False


@pytest.mark.parametrize("kind,kw,loader", KINDS)
def test_text_is_stripped_and_free_form(lab_root, kind, kw, loader):
    session = _session(lab_root)
    _enter(session, kw)
    _run(session, "description   Lab  with   spaces  ")
    assert session.definition_candidate["description"] == "Lab  with   spaces"


@pytest.mark.parametrize("kind,kw,loader", KINDS)
def test_existing_definition_without_description_loads_and_renders(lab_root, kind, kw, loader):
    _set_committed(lab_root, kind, loader, None)
    session = _session(lab_root)
    _enter(session, kw)
    assert "description" not in climain.render_committed_definition(session)
    _run(session, "commit")  # no-op, still valid


@pytest.mark.parametrize("kind,kw,loader", KINDS)
def test_create_description_commit(lab_root, kind, kw, loader):
    session = _session(lab_root)
    _run(session, f"{kw} brand_new")
    _run(session, f"description {DESC}")
    _run(session, "commit")
    assert loader("brand_new", lab_root)["description"] == DESC
    text = climain.render_committed_definition(session)
    assert text.count(f" description {DESC}") == 1


@pytest.mark.parametrize("kind,kw,loader", KINDS)
def test_uncommitted_delete_then_restore_restores_description(lab_root, kind, kw, loader):
    _set_committed(lab_root, kind, loader, "A")
    session = _session(lab_root)
    session.remove_definition(kind, "sample_lab")
    _run(session, f"{kw} sample_lab")
    assert session.definition_candidate["description"] == "A"
    assert session.definition_dirty() is False


@pytest.mark.parametrize("kind,kw,loader", KINDS)
def test_clear_after_candidate_delete_shows_description(lab_root, kind, kw, loader):
    _set_committed(lab_root, kind, loader, "A")
    session = _session(lab_root)
    session.remove_definition(kind, "sample_lab")
    session.clear()
    assert session.overall_dirty() is False
    assert loader("sample_lab", lab_root)["description"] == "A"


@pytest.mark.parametrize("kind,kw,loader", KINDS)
def test_committed_delete_then_recreate_does_not_resurrect(lab_root, kind, kw, loader):
    _set_committed(lab_root, kind, loader, "A")
    # a settings selection would block deletion; unselect first
    settings = lab.read_settings(lab_root)
    if kind == "access_info":
        settings.pop("active_access_info", None)
    else:
        lab.write_topology("other", {"name": "other", "description": "", "devices": {}, "links": []}, lab_root)
        settings["active_topology"] = "other"
    lab.write_settings(settings, lab_root)
    session = _session(lab_root)
    session.remove_definition(kind, "sample_lab")
    result = session.commit()
    assert result is True
    assert not getattr(lab, f"{kind}_exists")("sample_lab", lab_root)
    _run(session, f"{kw} sample_lab")
    assert not session.definition_candidate.get("description")
    _run(session, "commit")
    assert not loader("sample_lab", lab_root).get("description")


@pytest.mark.parametrize("kind,kw,loader", KINDS)
def test_help_inline_help_and_completion(lab_root, kind, kw, loader):
    mode = kind
    ctx = grammar.CliContext()
    tokens = {line.token: line.description for line in grammar.help(mode, "", ctx).lines}
    assert "description" in tokens
    no_tokens = [line.token for line in grammar.help(mode, "no ", ctx).lines]
    assert "description" in no_tokens
    assert "description" in grammar.complete(mode, "desc", ctx).candidates or grammar.complete(mode, "desc", ctx).completed
    assert grammar.parse(mode, "no description").action == f"{mode}.clear_description"
    assert grammar.parse(mode, "description x y").action == f"{mode}.description"
    assert grammar.parse(mode, "no description").ok


def test_global_running_config_shows_selected_descriptions(lab_root):
    _set_committed(lab_root, "access_info", lab.load_access_info, "Access desc")
    _set_committed(lab_root, "topology", lab.load_topology, "Topo desc")
    session = cfgmod.CliSession(lab_root)
    lines = climain.render_committed_running_config(session).splitlines()
    assert lines == [
        "!", " access-info", "  sample_lab", "   description Access desc",
        "!", " topology", "  sample_lab", "   description Topo desc",
        "!", " scenario", "  sample",
        "!", " reference", "  sample",
        "!",
    ]


def test_global_running_config_unchanged_without_descriptions(lab_root):
    _set_committed(lab_root, "access_info", lab.load_access_info, None)
    _set_committed(lab_root, "topology", lab.load_topology, None)
    lines = climain.render_committed_running_config(cfgmod.CliSession(lab_root)).splitlines()
    assert lines == [
        "!", " access-info", "  sample_lab",
        "!", " topology", "  sample_lab",
        "!", " scenario", "  sample",
        "!", " reference", "  sample",
        "!",
    ]


def test_exec_views_show_description(lab_root, capsys):
    _set_committed(lab_root, "access_info", lab.load_access_info, "Access desc")
    _set_committed(lab_root, "topology", lab.load_topology, "Topo desc")
    session = cfgmod.CliSession(lab_root)
    _run(session, "show running-config access-info")
    assert "\n description Access desc\n" in capsys.readouterr().out
    _run(session, "show running-config topology")
    assert "\n description Topo desc\n" in capsys.readouterr().out


def test_non_string_description_rejected_on_load():
    with pytest.raises(lab.LabConfigError):
        lab.validate_access_info_data("x", {"name": "x", "description": 5, "devices": {}})
    with pytest.raises(lab.LabConfigError):
        lab.validate_topology_data("x", {"name": "x", "description": ["a"], "devices": {}})


def test_scenario_and_reference_have_no_cli_description():
    for mode in ("scenario", "reference"):
        assert grammar.parse(mode, "description x").ok is False
