"""Tests for cli/main.py's Quick Start help, help topics, and `show
version` rendering. Grammar-level parsing/completion/help for these
commands is covered in test_grammar.py; this file covers the rendered
content and the read-only, side-effect-free nature of `show version`."""

from __future__ import annotations

import subprocess

import pytest

from network_lab_mcp import lab
from network_lab_mcp.cli import config as cfgmod
from network_lab_mcp.cli import main as climain


# ---- Quick Start / help topics ----


def test_quick_start_mentions_all_configuration_areas_and_topics():
    text = climain.render_quick_start()
    for area in ("running-config", "access-info", "topology", "scenario", "reference"):
        assert area in text
    for topic in ("help claude", "help workflow", "help editor", "help cli"):
        assert topic in text
    assert "Network Lab MCP CLI" in text


def test_quick_start_does_not_read_like_a_bare_command_list():
    text = climain.render_quick_start()
    # The old bare-`help` output was just a list of "<token>  <description>"
    # rows; the new one must not merely reproduce that.
    assert "Purpose:" in text
    assert "Typical workflow:" in text


def test_help_claude_mentions_seven_tools_and_privacy_boundary():
    text = climain.render_help_claude()
    assert "seven" in text.lower()
    for tool in (
        "get_active_topology",
        "get_execution_instructions",
        "terminal_open",
        "terminal_send",
        "terminal_read",
        "terminal_list",
        "terminal_close",
    ):
        assert tool in text
    assert "password" in text.lower()
    assert "access-info" in text
    # Reuses the exact, already-verified registration command from
    # README.md rather than inventing a new one.
    assert "claude mcp add --scope user --transport stdio network-lab -- network-lab-mcp" in text


def test_help_claude_command_matches_readme():
    with open("README.md", encoding="utf-8") as handle:
        readme = handle.read()
    assert "claude mcp add --scope user --transport stdio network-lab -- network-lab-mcp" in readme


def test_help_workflow_lists_ordered_steps_and_mentions_discovery_deferred():
    text = climain.render_help_workflow()
    assert "1. Configure access-info." in text
    assert "8. Use Claude Code." in text
    assert "discovery" in text.lower()
    assert "not implemented yet" in text.lower()


def test_help_editor_describes_resolution_order_and_candidate_flow():
    text = climain.render_help_editor()
    assert "$VISUAL" in text and "$EDITOR" in text and "vim" in text
    assert "topology, scenario, and reference" in text.lower() or "topology" in text
    assert "commit" in text.lower()
    assert "clear" in text.lower()
    assert ".vimrc" in text


def test_help_cli_is_short_and_does_not_duplicate_full_reference():
    text = climain.render_help_cli()
    assert "?" in text
    assert "clear" in text
    assert "commit" in text
    # Concise: much shorter than the full cli_reference.md.
    with open("docs/cli_reference.md", encoding="utf-8") as handle:
        full_reference = handle.read()
    assert len(text) < len(full_reference) / 5


def test_help_topic_dispatch_table_covers_every_grammar_topic():
    from network_lab_mcp.cli import grammar

    assert set(climain._HELP_TOPIC_RENDERERS) == set(grammar.HELP_TOPICS)


# ---- show version ----


def test_render_version_info_contains_all_required_fields():
    text = climain.render_version_info()
    assert "Network Lab MCP" in text
    assert "Version:" in text
    assert "Release date:" in text
    assert "Git commit:" in text
    assert "Author:" in text
    assert "License:" in text
    assert "Python:" in text


def test_version_matches_expected_release_metadata():
    import network_lab_mcp

    assert network_lab_mcp.__version__ == "0.1.0"
    assert network_lab_mcp.__release_date__ == "2026-09-20"
    assert network_lab_mcp.__author__ == "Eitaro Uemura"
    assert network_lab_mcp.__license__ == "GNU General Public License v3.0"


def test_python_version_is_dynamic_not_hardcoded():
    import platform

    text = climain.render_version_info()
    assert platform.python_version() in text
    assert "3.13.x" not in text  # never a hard-coded placeholder


def test_git_commit_resolves_dynamically_when_available(monkeypatch):
    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args, 0, stdout="abc1234\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert climain._resolve_git_commit() == "abc1234"


def test_git_commit_unavailable_when_git_binary_missing(monkeypatch):
    def fake_run(*args, **kwargs):
        raise FileNotFoundError("git not found")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert climain._resolve_git_commit() == "unavailable"


def test_git_commit_unavailable_on_nonzero_exit(monkeypatch):
    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args, 128, stdout="", stderr="fatal: not a git repository")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert climain._resolve_git_commit() == "unavailable"


def test_git_commit_unavailable_on_timeout(monkeypatch):
    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="git", timeout=2)

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert climain._resolve_git_commit() == "unavailable"


def test_show_version_does_not_raise_when_metadata_missing(monkeypatch):
    monkeypatch.setattr("network_lab_mcp.__version__", "unavailable")
    monkeypatch.setattr("network_lab_mcp.__author__", "unavailable")
    monkeypatch.setattr("network_lab_mcp.__license__", "unavailable")
    text = climain.render_version_info()
    assert "unavailable" in text


# ---- show version: no side effects ----


def test_show_version_handler_does_not_touch_candidate_state(lab_root, capsys):
    session = cfgmod.CliSession(lab_root)
    session.enter_configure()
    session.set_scenario("failover_test")
    dirty_before = session.overall_dirty()
    mode_before = session.mode
    climain.h_show_version(session, {})
    assert session.overall_dirty() == dirty_before
    assert session.mode == mode_before
    assert session.definition_kind is None
    captured = capsys.readouterr()
    assert "Network Lab MCP" in captured.out


def test_show_version_works_in_exec_mode_without_configure(lab_root, monkeypatch, capsys):
    monkeypatch.setattr(lab, "find_lab_root", lambda: lab_root)
    session = cfgmod.CliSession(lab_root)
    assert session.mode == "exec"
    climain.h_show_version(session, {})
    captured = capsys.readouterr()
    assert "Version:" in captured.out
    assert session.mode == "exec"


# ---- packaging / license metadata ----


def test_pyproject_declares_expected_version_author_license():
    # Avoid a hard tomllib dependency (stdlib only since Python 3.11, while
    # this project supports >=3.10); a plain text check is enough here.
    with open("pyproject.toml", encoding="utf-8") as handle:
        text = handle.read()
    assert 'version = "0.1.0"' in text
    assert 'name = "Eitaro Uemura"' in text
    assert 'text = "GNU General Public License v3.0"' in text


def test_license_file_exists_and_is_gpl_v3():
    with open("LICENSE", encoding="utf-8") as handle:
        text = handle.read()
    assert "GNU GENERAL PUBLIC LICENSE" in text
    assert "Version 3, 29 June 2007" in text


def test_readme_documents_license():
    with open("README.md", encoding="utf-8") as handle:
        readme = handle.read()
    assert "GNU General Public License v3.0" in readme
