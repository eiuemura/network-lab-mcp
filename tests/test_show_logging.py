"""`show logging` / `show logging <device-id>` / `show logging <device-id>
<log-file>`: EXEC-only, read-only viewing of persistent terminal
transcripts (logs/terminal/<device-id>/<session-start>.log), integrated
through the same cli/grammar.py SSOT as every other command.

`terminal.LOGS_ROOT` is monkeypatched to an isolated tmp_path directory in
every test here -- nothing touches the real repository's logs/."""

from __future__ import annotations

import pytest

from network_lab_mcp import terminal
from network_lab_mcp.cli import config as cfgmod
from network_lab_mcp.cli import grammar
from network_lab_mcp.cli import main as climain


@pytest.fixture()
def isolated_logs(tmp_path, monkeypatch):
    logs_root = tmp_path / "logs" / "terminal"
    monkeypatch.setattr(terminal, "LOGS_ROOT", logs_root)
    return logs_root


def _write_log(logs_root, device_id, timestamp, content=""):
    device_dir = logs_root / device_id
    device_dir.mkdir(parents=True, exist_ok=True)
    (device_dir / f"{timestamp}.log").write_text(content)


# ---- h_show_logging (all devices) ----


def test_show_logging_with_no_log_directory(isolated_logs, lab_root, capsys):
    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "show logging")
    assert capsys.readouterr().out.strip() == "No terminal logs found."


def test_show_logging_with_empty_log_directory(isolated_logs, lab_root, capsys):
    isolated_logs.mkdir(parents=True)
    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "show logging")
    assert capsys.readouterr().out.strip() == "No terminal logs found."


def test_show_logging_summarizes_device_counts_and_total(isolated_logs, lab_root, capsys):
    """Step B.1: bare `show logging` was changed from a flat per-file
    listing to a per-device count summary (with a Total row) -- this
    test previously asserted the old flat-listing format, which is now
    only `show logging <device-id>`'s own behavior (see
    test_show_logging_device_multiple_sessions_newest_first below)."""
    _write_log(isolated_logs, "R1", "20260921T091500")
    _write_log(isolated_logs, "R2", "20260921T091505")
    _write_log(isolated_logs, "R1", "20260921T103210")
    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "show logging")
    lines = capsys.readouterr().out.strip().splitlines()
    assert any(line.split()[:2] == ["R1", "2"] for line in lines)
    assert any(line.split()[:2] == ["R2", "1"] for line in lines)
    assert any(line.split()[:2] == ["Total", "3"] for line in lines)


# ---- h_show_logging_device ----


def test_show_logging_device_no_logs(isolated_logs, lab_root, capsys):
    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "show logging R1")
    assert "No terminal logs found for device 'R1'." in capsys.readouterr().out


def test_show_logging_device_multiple_sessions_newest_first(isolated_logs, lab_root, capsys):
    _write_log(isolated_logs, "R1", "20260921T091500")
    _write_log(isolated_logs, "R1", "20260921T103210")
    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "show logging R1")
    lines = capsys.readouterr().out.strip().splitlines()
    data_lines = lines[2:]
    assert "20260921T103210.log" in data_lines[0]
    assert "20260921T091500.log" in data_lines[1]


def test_show_logging_invalid_device_is_harmless(isolated_logs, lab_root, capsys):
    _write_log(isolated_logs, "R1", "20260921T091500")
    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "show logging DOES-NOT-EXIST")
    out = capsys.readouterr().out
    assert "No terminal logs found for device 'DOES-NOT-EXIST'." in out


# ---- h_show_logging_device_file ----


def test_show_logging_device_file_shows_contents(isolated_logs, lab_root, capsys):
    _write_log(isolated_logs, "R1", "20260921T091500", "transcript line 1\ntranscript line 2\n")
    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "show logging R1 20260921T091500.log")
    assert capsys.readouterr().out == "transcript line 1\ntranscript line 2\n"


def test_show_logging_device_file_invalid_file_reports_error(isolated_logs, lab_root, capsys):
    _write_log(isolated_logs, "R1", "20260921T091500")
    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "show logging R1 20260921T999999.log")
    assert capsys.readouterr().out.startswith("%")


@pytest.mark.parametrize("traversal", ["../../etc/passwd", "..%2Fsecret.log"])
def test_show_logging_device_file_rejects_path_traversal(isolated_logs, lab_root, capsys, traversal):
    _write_log(isolated_logs, "R1", "20260921T091500")
    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, f"show logging R1 {traversal}")
    assert capsys.readouterr().out.startswith("%")


# ---- EXEC-only ----


def test_show_logging_not_available_outside_exec():
    ctx = grammar.CliContext()
    for mode in ("global", "running", "topology", "device", "access_info", "access_device", "scenario", "reference"):
        result = grammar.parse(mode, "show logging")
        assert not result.ok, mode


def test_show_logging_parses_in_exec():
    result = grammar.parse("exec", "show logging")
    assert result.ok
    assert result.action == "exec.show_logging"


# ---- grammar `?` / <cr> / Tab completion ----


def test_show_help_lists_logging_with_cr():
    ctx = grammar.CliContext()
    result = grammar.help("exec", "show ", ctx)
    tokens = [line.token for line in result.lines]
    assert "logging" in tokens


def test_show_logging_help_lists_known_devices_and_cr():
    ctx = grammar.CliContext(log_device_ids=("R1", "R2"))
    result = grammar.help("exec", "show logging ", ctx)
    assert [line.token for line in result.lines] == ["R1", "R2"]
    assert result.show_cr


def test_show_logging_device_help_lists_files_and_cr():
    ctx = grammar.CliContext(log_files_by_device={"R1": ("20260921T091500.log", "20260921T103210.log")})
    result = grammar.help("exec", "show logging R1 ", ctx)
    assert [line.token for line in result.lines] == ["20260921T091500.log", "20260921T103210.log"]
    assert result.show_cr


def test_show_logging_device_file_help_is_cr_only():
    ctx = grammar.CliContext(log_files_by_device={"R1": ("20260921T091500.log",)})
    result = grammar.help("exec", "show logging R1 20260921T091500.log ", ctx)
    assert result.lines == []
    assert result.show_cr


def test_show_logging_tab_completion_devices_and_files():
    ctx = grammar.CliContext(log_device_ids=("R1", "R2"), log_files_by_device={"R1": ("20260921T091500.log",)})
    assert grammar.complete("exec", "show logging R", ctx).candidates == ["R1", "R2"]
    assert grammar.complete("exec", "show logging R1 ", ctx).candidates == ["20260921T091500.log"]


# ---- collision-suffixed filenames stay compatible with show logging ----


def test_show_logging_lists_collision_suffixed_files(isolated_logs, lab_root, capsys):
    _write_log(isolated_logs, "R1", "20260921T091500")
    device_dir = isolated_logs / "R1"
    (device_dir / "20260921T091500_2.log").write_text("second session\n")
    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "show logging R1")
    lines = capsys.readouterr().out.strip().splitlines()
    filenames = {line.split()[-1] for line in lines[2:]}
    assert filenames == {"20260921T091500.log", "20260921T091500_2.log"}


def test_show_logging_device_file_reads_collision_suffixed_filename(isolated_logs, lab_root, capsys):
    device_dir = isolated_logs / "R1"
    device_dir.mkdir(parents=True)
    (device_dir / "20260921T091500_2.log").write_text("collision transcript\n")
    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "show logging R1 20260921T091500_2.log")
    assert capsys.readouterr().out == "collision transcript\n"


def test_show_logging_tab_completion_includes_collision_suffixed_filenames():
    ctx = grammar.CliContext(log_files_by_device={"R1": ("20260921T091500.log", "20260921T091500_2.log")})
    candidates = grammar.complete("exec", "show logging R1 ", ctx).candidates
    assert set(candidates) == {"20260921T091500.log", "20260921T091500_2.log"}
