"""Tests for cli/editor.py: $VISUAL/$EDITOR/vim resolution order, the
candidate <-> temp-file <-> editor round trip, and error handling. A fake
editor script (see conftest.fake_editor) stands in for a real interactive
session so this suite never depends on one."""

from __future__ import annotations

import tempfile

import pytest

from network_lab_mcp.cli import editor


@pytest.fixture(autouse=True)
def _isolated_tempdir(tmp_path, monkeypatch):
    # edit_yaml_candidate() uses tempfile.mkstemp() with no explicit `dir`,
    # so it honors this module-global override -- keeps every test's
    # temp file (and cleanup check) inside pytest's own tmp_path.
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))


# ---- editor resolution order ----


def test_visual_takes_precedence_over_editor(monkeypatch):
    monkeypatch.setenv("VISUAL", "my-visual-editor")
    monkeypatch.setenv("EDITOR", "my-editor")
    assert editor.resolve_editor_command() == ["my-visual-editor"]


def test_editor_used_when_visual_unset(monkeypatch):
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.setenv("EDITOR", "my-editor")
    assert editor.resolve_editor_command() == ["my-editor"]


def test_vim_fallback_when_neither_set(monkeypatch):
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.delenv("EDITOR", raising=False)
    command = editor.resolve_editor_command()
    assert command[0] == "vim"
    assert "-c" in command  # syntax-on / filetype=yaml, added only for our own fallback


def test_explicit_editor_gets_no_injected_vim_options(monkeypatch):
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.setenv("EDITOR", "vim")
    # The operator explicitly chose vim themselves -- Network Lab MCP must
    # not silently add its own -c options in that case.
    assert editor.resolve_editor_command() == ["vim"]


def test_editor_with_arguments_is_split_safely(monkeypatch):
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.setenv("EDITOR", "code --wait")
    assert editor.resolve_editor_command() == ["code", "--wait"]


def test_malformed_editor_value_raises_clear_error(monkeypatch):
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.setenv("EDITOR", 'unterminated "quote')
    with pytest.raises(editor.EditorError):
        editor.resolve_editor_command()


# ---- candidate <-> temp file <-> editor round trip ----


def test_valid_yaml_edit_updates_candidate(monkeypatch, fake_editor):
    script = fake_editor("open(path, 'w').write('name: edited\\nobjectives: []\\n')")
    monkeypatch.setenv("EDITOR", f"python3 {script}")
    result = editor.edit_yaml_candidate({"name": "original", "objectives": ["x"]})
    assert result == {"name": "edited", "objectives": []}


def test_invalid_yaml_raises_and_leaves_no_temp_file(monkeypatch, fake_editor, tmp_path):
    script = fake_editor("open(path, 'w').write('name: [unterminated\\n')")
    monkeypatch.setenv("EDITOR", f"python3 {script}")
    with pytest.raises(editor.EditorError, match="line"):
        editor.edit_yaml_candidate({"name": "original"})
    assert list(tmp_path.glob("network-lab-mcp-*.yaml")) == []


def test_non_mapping_root_is_rejected(monkeypatch, fake_editor):
    script = fake_editor("open(path, 'w').write('- just\\n- a\\n- list\\n')")
    monkeypatch.setenv("EDITOR", f"python3 {script}")
    with pytest.raises(editor.EditorError, match="mapping"):
        editor.edit_yaml_candidate({"name": "original"})


def test_nonzero_exit_raises_and_candidate_untouched(monkeypatch, fake_editor):
    script = fake_editor("open(path, 'w').write('name: should-not-be-used\\n')\nsys.exit(3)")
    monkeypatch.setenv("EDITOR", f"python3 {script}")
    with pytest.raises(editor.EditorError, match="non-zero"):
        editor.edit_yaml_candidate({"name": "original"})


def test_semantic_no_change_round_trips_to_an_equal_mapping(monkeypatch, fake_editor):
    # Opening and saving without edits must not appear as a change to the
    # caller's dirty check (definition_candidate == definition_original).
    script = fake_editor("pass  # leave the file exactly as the caller wrote it")
    monkeypatch.setenv("EDITOR", f"python3 {script}")
    original = {"name": "original", "objectives": ["a", "b"]}
    result = editor.edit_yaml_candidate(original)
    assert result == original


def test_missing_editor_binary_raises_clear_error(monkeypatch):
    monkeypatch.setenv("EDITOR", "definitely-not-a-real-editor-binary")
    with pytest.raises(editor.EditorError, match="not found"):
        editor.edit_yaml_candidate({"name": "original"})


def test_temp_file_has_yaml_suffix_visible_to_editor(monkeypatch, fake_editor):
    script = fake_editor("assert path.endswith('.yaml'), path\nopen(path, 'w').write('name: ok\\n')")
    monkeypatch.setenv("EDITOR", f"python3 {script}")
    result = editor.edit_yaml_candidate({"name": "original"})
    assert result == {"name": "ok"}


def test_temp_file_cleaned_up_after_success(monkeypatch, fake_editor, tmp_path):
    script = fake_editor("open(path, 'w').write('name: ok\\n')")
    monkeypatch.setenv("EDITOR", f"python3 {script}")
    editor.edit_yaml_candidate({"name": "original"})
    assert list(tmp_path.glob("network-lab-mcp-*.yaml")) == []
