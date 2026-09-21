"""`delete logging all` / `delete logging <device> all` / `delete logging
<device> <log-file>`: EXEC-only, destructive terminal-log deletion (Step
B). Eligibility is derived by reusing the exact same stored-log
enumeration `show logging` uses (terminal.list_logged_device_ids() /
terminal.list_device_logs()), so what is deletable never drifts from what
is displayed.

The central safety invariant is device-level active-writer protection: a
production session (terminal.open_device_terminal()) and a Discovery
bootstrap session (terminal.open_bootstrap_terminal()) both attach
persistent pipe-pane logging to the very same logs/terminal/<device-id>/
directory, keyed only by device name -- so an active writer for a device
blocks deletion of ALL of that device's logs (not just a guessed "active"
one), and (transitively) blocks `delete logging all` entirely if any
targeted device is active. Real tmux sessions (mocked to a safe local
command, never ssh/telnet, in the same pattern as
test_terminal_logging.py) are used for the active-writer tests so the
actual detection mechanism is genuinely exercised, not merely asserted.

`terminal.LOGS_ROOT` is monkeypatched to an isolated tmp_path directory in
every test here -- the `isolated_logs` fixture itself asserts this
isolation before returning, so no destructive operation in this file can
reach the real repository's logs/terminal/."""

from __future__ import annotations

import time

import pytest

from network_lab_mcp import terminal
from network_lab_mcp.cli import config as cfgmod
from network_lab_mcp.cli import grammar
from network_lab_mcp.cli import main as climain

_REAL_REPO_LOGS_ROOT = terminal.LOGS_ROOT  # captured once, before any monkeypatch in this file


@pytest.fixture()
def isolated_logs(tmp_path, monkeypatch):
    logs_root = tmp_path / "logs" / "terminal"
    monkeypatch.setattr(terminal, "LOGS_ROOT", logs_root)
    # Step A's real-lab incident (see git history) happened because an
    # assumed redirection did not actually take effect. Prove it here,
    # explicitly, before any destructive operation below can run.
    assert terminal.LOGS_ROOT == logs_root
    assert terminal.LOGS_ROOT != _REAL_REPO_LOGS_ROOT
    assert tmp_path in terminal.LOGS_ROOT.parents
    assert _REAL_REPO_LOGS_ROOT not in terminal.LOGS_ROOT.parents
    return logs_root


def _write_log(logs_root, device_id, timestamp, content=""):
    device_dir = logs_root / device_id
    device_dir.mkdir(parents=True, exist_ok=True)
    (device_dir / f"{timestamp}.log").write_text(content)


def _wait_until(predicate, timeout=3.0, interval=0.1):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


# Fixed device names reserved for real-tmux active-writer tests in this
# file only (never R1/R2/... used by the synthetic-file tests, so a
# leaked session from a failed test can never be mistaken for synthetic
# fixture data).
_WRITER_TEST_DEVICE_IDS = ("DW1", "DW2", "DW3")


@pytest.fixture(autouse=True)
def _cleanup_sessions():
    yield
    for session_name in list(terminal.list_validation_sessions()):
        terminal.close_validation_session(session_name[len(terminal.VALIDATION_PREFIX) :])
    for device_id in _WRITER_TEST_DEVICE_IDS:
        terminal.close_device_terminal(device_id)
        terminal.close_bootstrap_terminal(device_id)


def _fake_transport(monkeypatch):
    monkeypatch.setattr(terminal, "_build_transport_command", lambda config, **kw: ("ssh", ["cat"]))


def _first_log_filename(logs_root, device_id):
    device_dir = logs_root / device_id
    assert _wait_until(lambda: device_dir.is_dir() and any(device_dir.iterdir()))
    return next(iter(device_dir.iterdir())).name


# ==========================================================================
# Grammar: `delete` (EXEC only), `delete logging`, dynamic providers, `?`,
# Tab, executable endpoints (Step B section 36)
# ==========================================================================


def test_delete_parses_only_in_exec():
    # Grammar reachability is independent of filesystem state -- parsing
    # succeeds even with nothing eligible to delete yet; the handler is
    # what actually enumerates/rejects at execution time (see the
    # empty-state tests below).
    result = grammar.parse("exec", "delete logging all")
    assert result.ok
    assert result.action == "exec.delete_logging_all"
    for mode in ("global", "running", "topology", "device", "access_info", "access_device", "scenario", "reference"):
        result = grammar.parse(mode, "delete")
        assert not result.ok, mode
        assert result.error.kind == "unknown"


def test_delete_bare_help_lists_logging_no_cr():
    ctx = grammar.CliContext()
    result = grammar.help("exec", "delete ", ctx)
    assert [line.token for line in result.lines] == ["logging"]
    assert result.show_cr is False


def test_delete_inline_help_no_cr():
    ctx = grammar.CliContext()
    result = grammar.help("exec", "delete", ctx)
    assert [(line.token, line.description) for line in result.lines] == [("delete", "Delete stored information")]
    assert result.show_cr is False


def test_delete_logging_inline_help_no_cr():
    ctx = grammar.CliContext()
    result = grammar.help("exec", "delete logging", ctx)
    assert [(line.token, line.description) for line in result.lines] == [("logging", "Delete terminal logs")]
    assert result.show_cr is False


def test_delete_logging_bare_not_executable():
    result = grammar.parse("exec", "delete logging")
    assert not result.ok
    assert result.error.kind == "incomplete"


def test_delete_logging_device_bare_not_executable():
    result = grammar.parse("exec", "delete logging R1")
    assert not result.ok
    assert result.error.kind == "incomplete"


def test_delete_logging_all_parses():
    result = grammar.parse("exec", "delete logging all")
    assert result.ok
    assert result.action == "exec.delete_logging_all"
    assert result.args == {}


def test_delete_logging_device_all_parses():
    result = grammar.parse("exec", "delete logging R1 all")
    assert result.ok
    assert result.action == "exec.delete_logging_device_all"
    assert result.args == {"device_id": "R1"}


def test_delete_logging_device_file_parses():
    result = grammar.parse("exec", "delete logging R1 20260921T091500.log")
    assert result.ok
    assert result.action == "exec.delete_logging_device_file"
    assert result.args == {"device_id": "R1", "log_file": "20260921T091500.log"}


def test_delete_logging_bare_help_lists_all_and_known_devices():
    ctx = grammar.CliContext(log_device_ids=("R1", "R2"))
    result = grammar.help("exec", "delete logging ", ctx)
    assert [(line.token, line.description) for line in result.lines] == [
        ("all", "Delete all terminal logs"),
        ("R1", "Device with stored terminal logs"),
        ("R2", "Device with stored terminal logs"),
    ]
    assert result.show_cr is False


def test_delete_logging_all_inline_help_shows_cr():
    ctx = grammar.CliContext(log_device_ids=("R1",))
    result = grammar.help("exec", "delete logging all", ctx)
    assert [(line.token, line.description) for line in result.lines] == [("all", "Delete all terminal logs")]
    assert result.show_cr is True


def test_delete_logging_all_spaced_help_is_cr_only():
    ctx = grammar.CliContext()
    result = grammar.help("exec", "delete logging all ", ctx)
    assert result.lines == []
    assert result.show_cr is True


def test_delete_logging_device_exact_inline_help_no_cr():
    ctx = grammar.CliContext(log_device_ids=("R1", "R2"))
    result = grammar.help("exec", "delete logging R1", ctx)
    assert [(line.token, line.description) for line in result.lines] == [
        ("R1", "Device with stored terminal logs")
    ]
    assert result.show_cr is False  # bare "delete logging R1" is intentionally incomplete


def test_delete_logging_device_partial_help_has_no_cr():
    ctx = grammar.CliContext(log_device_ids=("R1", "R10"))
    result = grammar.help("exec", "delete logging R1", ctx)
    # "R1" is itself an exact device match here (not merely a partial
    # prefix of "R10"), so this is the exact-match branch above; use a
    # genuinely partial prefix instead.
    ctx2 = grammar.CliContext(log_device_ids=("R10",))
    result2 = grammar.help("exec", "delete logging R1", ctx2)
    assert [line.token for line in result2.lines] == ["R10"]
    assert result2.show_cr is False


def test_delete_logging_unknown_device_inline_help_empty():
    ctx = grammar.CliContext(log_device_ids=("R1",))
    result = grammar.help("exec", "delete logging R9", ctx)
    assert result.lines == []
    assert result.show_cr is False


def test_delete_logging_device_spaced_help_lists_all_and_files():
    ctx = grammar.CliContext(log_files_by_device={"R1": ("20260921T091500.log", "20260921T103210.log")})
    result = grammar.help("exec", "delete logging R1 ", ctx)
    assert [(line.token, line.description) for line in result.lines] == [
        ("all", "Delete all terminal logs for this device"),
        ("20260921T091500.log", "Terminal log"),
        ("20260921T103210.log", "Terminal log"),
    ]
    assert result.show_cr is False


def test_delete_logging_device_all_inline_help_shows_cr():
    ctx = grammar.CliContext()
    result = grammar.help("exec", "delete logging R1 all", ctx)
    assert [(line.token, line.description) for line in result.lines] == [
        ("all", "Delete all terminal logs for this device")
    ]
    assert result.show_cr is True


def test_delete_logging_device_file_exact_inline_help_shows_cr():
    ctx = grammar.CliContext(log_files_by_device={"R1": ("20260921T091500.log",)})
    result = grammar.help("exec", "delete logging R1 20260921T091500.log", ctx)
    assert [(line.token, line.description) for line in result.lines] == [
        ("20260921T091500.log", "Terminal log")
    ]
    assert result.show_cr is True


def test_delete_logging_device_file_spaced_help_is_cr_only():
    ctx = grammar.CliContext(log_files_by_device={"R1": ("20260921T091500.log",)})
    result = grammar.help("exec", "delete logging R1 20260921T091500.log ", ctx)
    assert result.lines == []
    assert result.show_cr is True


def test_delete_logging_tab_completion_devices_and_all():
    ctx = grammar.CliContext(log_device_ids=("R1", "R2"))
    candidates = grammar.complete("exec", "delete logging ", ctx).candidates
    assert set(candidates) == {"all", "R1", "R2"}


def test_delete_logging_device_tab_completion_files_and_all():
    ctx = grammar.CliContext(log_files_by_device={"R1": ("20260921T091500.log",)})
    candidates = grammar.complete("exec", "delete logging R1 ", ctx).candidates
    assert set(candidates) == {"all", "20260921T091500.log"}


def test_delete_unknown_device_or_file_does_not_wildcard_match():
    ctx = grammar.CliContext(log_device_ids=("R1",), log_files_by_device={"R1": ("20260921T091500.log",)})
    result = grammar.help("exec", "delete logging R9", ctx)
    assert result.lines == []
    result2 = grammar.help("exec", "delete logging R1 does-not-exist.log", ctx)
    assert result2.lines == []


def test_delete_keyword_no_new_abbreviation_ambiguity():
    result = grammar.parse("exec", "d logging all")
    assert result.ok
    assert result.action == "exec.delete_logging_all"
    ctx = grammar.CliContext()
    tokens = [line.token for line in grammar.help("exec", "", ctx).lines]
    assert tokens == ["configure", "show", "delete", "help", "exit", "quit"]


# ==========================================================================
# Single-file deletion (Step B section 37)
# ==========================================================================


def test_delete_single_log_file(isolated_logs, lab_root, capsys):
    _write_log(isolated_logs, "R1", "20260921T090000", "a")
    _write_log(isolated_logs, "R1", "20260921T100000", "b")
    _write_log(isolated_logs, "R2", "20260921T110000", "c")
    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "delete logging R1 20260921T090000.log")
    assert capsys.readouterr().out.strip() == "Deleted terminal log R1/20260921T090000.log."
    assert [name for _, name in terminal.list_device_logs("R1")] == ["20260921T100000.log"]
    assert [name for _, name in terminal.list_device_logs("R2")] == ["20260921T110000.log"]


def test_delete_nonexistent_exact_filename_deletes_nothing(isolated_logs, lab_root, capsys):
    _write_log(isolated_logs, "R1", "20260921T090000", "a")
    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "delete logging R1 does-not-exist.log")
    assert capsys.readouterr().out.startswith("%")
    assert [name for _, name in terminal.list_device_logs("R1")] == ["20260921T090000.log"]


# ==========================================================================
# Device-all deletion (Step B section 38)
# ==========================================================================


def test_delete_all_logs_for_one_device(isolated_logs, lab_root, capsys):
    _write_log(isolated_logs, "R1", "20260921T090000", "a")
    _write_log(isolated_logs, "R1", "20260921T100000", "b")
    _write_log(isolated_logs, "R2", "20260921T110000", "c")
    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "delete logging R1 all")
    assert capsys.readouterr().out.strip() == "Deleted 2 terminal logs for R1."
    assert terminal.list_device_logs("R1") == []
    assert [name for _, name in terminal.list_device_logs("R2")] == ["20260921T110000.log"]


def test_delete_all_for_empty_or_nonexistent_device(isolated_logs, lab_root, capsys):
    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "delete logging R9 all")
    assert capsys.readouterr().out.strip() == "% No terminal logs found for device 'R9'."
    assert not (isolated_logs / "R9").exists()


def test_delete_all_for_a_device_already_emptied_by_deletion(isolated_logs, lab_root, capsys):
    _write_log(isolated_logs, "R1", "20260921T090000", "a")
    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "delete logging R1 all")
    capsys.readouterr()
    climain.execute_command_line(session, "delete logging R1 all")  # retry: nothing left
    assert capsys.readouterr().out.strip() == "% No terminal logs found for device 'R1'."


# ==========================================================================
# Global-all deletion (Step B section 39)
# ==========================================================================


def test_delete_all_logs_globally(isolated_logs, lab_root, capsys):
    _write_log(isolated_logs, "R1", "20260921T090000", "a")
    _write_log(isolated_logs, "R2", "20260921T100000", "b")
    _write_log(isolated_logs, "R2", "20260921T110000", "c")
    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "delete logging all")
    assert capsys.readouterr().out.strip() == "Deleted 3 terminal logs."
    # Directory cleanup is explicitly out of scope (Step B section 15) --
    # the per-device *log files* are gone, which is the actual invariant.
    assert terminal.list_device_logs("R1") == []
    assert terminal.list_device_logs("R2") == []


def test_delete_all_logs_globally_with_zero_logs_is_safe(isolated_logs, lab_root, capsys):
    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "delete logging all")
    assert capsys.readouterr().out.strip() == "% No terminal logs found."


def test_delete_all_logs_globally_with_empty_dir_is_safe(isolated_logs, lab_root, capsys):
    isolated_logs.mkdir(parents=True)
    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "delete logging all")
    assert capsys.readouterr().out.strip() == "% No terminal logs found."


# ==========================================================================
# Path traversal / symlink / unknown-file safety (Step B sections 43/44/46)
# ==========================================================================


def test_delete_logging_device_path_traversal_rejected(isolated_logs, lab_root, capsys):
    _write_log(isolated_logs, "R1", "20260921T090000", "a")
    outside = isolated_logs.parent / "outside-device-dir.log"
    outside.write_text("do not delete me")
    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "delete logging ../outside all")
    assert capsys.readouterr().out.startswith("%")
    assert outside.exists()
    assert [name for _, name in terminal.list_device_logs("R1")] == ["20260921T090000.log"]


@pytest.mark.parametrize("traversal", ["../outside.log", "../../outside.log", "/etc/passwd"])
def test_delete_logging_filename_path_traversal_rejected(isolated_logs, lab_root, capsys, traversal, tmp_path):
    _write_log(isolated_logs, "R1", "20260921T090000", "a")
    outside = tmp_path / "outside.log"
    outside.write_text("do not delete me")
    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, f"delete logging R1 {traversal}")
    assert capsys.readouterr().out.startswith("%")
    assert outside.exists()
    assert [name for _, name in terminal.list_device_logs("R1")] == ["20260921T090000.log"]


def test_delete_logging_rejects_symlink_and_leaves_target_untouched(isolated_logs, lab_root, capsys, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("precious")
    device_dir = isolated_logs / "R1"
    device_dir.mkdir(parents=True)
    symlink_path = device_dir / "20260921T090000.log"
    symlink_path.symlink_to(outside)

    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "delete logging R1 20260921T090000.log")
    assert capsys.readouterr().out.startswith("%")
    assert outside.read_text() == "precious"
    assert symlink_path.is_symlink()  # the symlink itself was never touched either


def test_delete_all_device_logs_ignores_symlinked_entry(isolated_logs, lab_root, capsys, tmp_path):
    outside = tmp_path / "outside2.txt"
    outside.write_text("precious2")
    device_dir = isolated_logs / "R1"
    device_dir.mkdir(parents=True)
    (device_dir / "20260921T090000.log").write_text("real log")
    (device_dir / "20260921T100000.log").symlink_to(outside)

    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "delete logging R1 all")
    assert capsys.readouterr().out.strip() == "Deleted 1 terminal logs for R1."
    assert outside.read_text() == "precious2"
    assert (device_dir / "20260921T100000.log").is_symlink()
    assert not (device_dir / "20260921T090000.log").exists()


def test_delete_all_device_logs_preserves_unknown_files(isolated_logs, lab_root, capsys):
    _write_log(isolated_logs, "R1", "20260921T090000", "a")
    device_dir = isolated_logs / "R1"
    (device_dir / "notes.txt").write_text("keep me")
    (device_dir / "subdirectory").mkdir()

    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "delete logging R1 all")
    assert (device_dir / "notes.txt").read_text() == "keep me"
    assert (device_dir / "subdirectory").is_dir()
    assert not (device_dir / "20260921T090000.log").exists()


def test_delete_all_logs_globally_preserves_unrelated_files(isolated_logs, lab_root, capsys):
    _write_log(isolated_logs, "R1", "20260921T090000", "a")
    (isolated_logs / "R1" / "unrelated.txt").write_text("keep me too")
    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "delete logging all")
    assert (isolated_logs / "R1" / "unrelated.txt").read_text() == "keep me too"


# ==========================================================================
# `show logging` / completion consistency after deletion (Step B section
# 29/30/31)
# ==========================================================================


def test_show_logging_reflects_deletion_immediately(isolated_logs, lab_root, capsys):
    _write_log(isolated_logs, "R1", "20260921T090000", "a")
    _write_log(isolated_logs, "R2", "20260921T100000", "b")
    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "delete logging R1 all")
    capsys.readouterr()
    climain.execute_command_line(session, "show logging")
    out = capsys.readouterr().out
    assert "R1" not in out
    assert "R2" in out


def test_dynamic_help_reflects_deletion_immediately(isolated_logs, lab_root):
    _write_log(isolated_logs, "R1", "20260921T090000", "a")
    _write_log(isolated_logs, "R1", "20260921T100000", "b")
    session = cfgmod.CliSession(lab_root)
    ctx_before = climain.build_context(session)
    assert set(ctx_before.log_files_by_device.get("R1", ())) == {"20260921T090000.log", "20260921T100000.log"}

    climain.execute_command_line(session, "delete logging R1 20260921T090000.log")

    ctx_after = climain.build_context(session)
    assert set(ctx_after.log_files_by_device.get("R1", ())) == {"20260921T100000.log"}


def test_device_log_files_no_longer_advertised_once_all_are_gone(isolated_logs, lab_root):
    """Step B section 31: once R1 has zero eligible log files, `delete
    logging R1 ?` no longer offers any filename for it. R1's *directory*
    may still be listed by `delete logging ?` (list_logged_device_ids()
    is directory-existence-based, pre-existing/unchanged show logging
    behavior, and directory cleanup is explicitly out of scope -- section
    15/31's own escape clause), but attempting to delete it again
    correctly reports nothing left to delete (see
    test_delete_all_for_empty_or_nonexistent_device's exact wording)."""
    _write_log(isolated_logs, "R1", "20260921T090000", "a")
    _write_log(isolated_logs, "R2", "20260921T100000", "b")
    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "delete logging R1 all")
    ctx = climain.build_context(session)
    assert ctx.log_files_by_device.get("R1", ()) == ()
    assert set(ctx.log_files_by_device.get("R2", ())) == {"20260921T100000.log"}


# ==========================================================================
# Active-writer safety: normal production terminal session (Step B
# section 40), using a real (mocked-command) tmux session
# ==========================================================================


def test_active_normal_terminal_blocks_individual_file_deletion(isolated_logs, lab_root, monkeypatch, capsys):
    _fake_transport(monkeypatch)
    terminal.open_device_terminal("DW1", {"transport": "ssh", "address": "192.0.2.1"})
    filename = _first_log_filename(isolated_logs, "DW1")

    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, f"delete logging DW1 {filename}")
    assert capsys.readouterr().out.strip() == "% Cannot delete an active terminal log for device 'DW1'."
    assert (isolated_logs / "DW1" / filename).exists()


def test_active_normal_terminal_blocks_device_all(isolated_logs, lab_root, monkeypatch, capsys):
    _fake_transport(monkeypatch)
    terminal.open_device_terminal("DW1", {"transport": "ssh", "address": "192.0.2.1"})
    _first_log_filename(isolated_logs, "DW1")

    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "delete logging DW1 all")
    assert capsys.readouterr().out.strip() == "% Cannot delete all logs for 'DW1' while a terminal log is active."
    assert any((isolated_logs / "DW1").iterdir())


def test_inactive_device_deletion_proceeds_while_another_device_is_active(isolated_logs, lab_root, monkeypatch, capsys):
    _fake_transport(monkeypatch)
    terminal.open_device_terminal("DW1", {"transport": "ssh", "address": "192.0.2.1"})
    _first_log_filename(isolated_logs, "DW1")
    _write_log(isolated_logs, "R2", "20260921T090000", "inactive")

    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "delete logging R2 all")
    assert capsys.readouterr().out.strip() == "Deleted 1 terminal logs for R2."
    assert terminal.list_device_logs("R2") == []
    assert any((isolated_logs / "DW1").iterdir())  # untouched, still active


def test_active_normal_terminal_never_closed_by_rejected_deletion(isolated_logs, lab_root, monkeypatch, capsys):
    _fake_transport(monkeypatch)
    terminal.open_device_terminal("DW1", {"transport": "ssh", "address": "192.0.2.1"})
    _first_log_filename(isolated_logs, "DW1")

    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "delete logging DW1 all")  # rejected
    assert terminal._session_exists(terminal.derive_production_session_name("DW1"))


# ==========================================================================
# Discovery bootstrap active-writer safety (Step B section 41), using the
# actual open_bootstrap_terminal() writer path -- never a renamed
# production session.
# ==========================================================================


def test_discovery_bootstrap_active_log_blocks_individual_deletion(isolated_logs, lab_root, monkeypatch, capsys):
    _fake_transport(monkeypatch)
    terminal.open_bootstrap_terminal("DW1", {"transport": "ssh", "address": "192.0.2.1"})
    filename = _first_log_filename(isolated_logs, "DW1")

    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, f"delete logging DW1 {filename}")
    assert capsys.readouterr().out.strip() == "% Cannot delete an active terminal log for device 'DW1'."
    assert (isolated_logs / "DW1" / filename).exists()


def test_discovery_bootstrap_active_log_blocks_device_all(isolated_logs, lab_root, monkeypatch, capsys):
    _fake_transport(monkeypatch)
    terminal.open_bootstrap_terminal("DW1", {"transport": "ssh", "address": "192.0.2.1"})
    _first_log_filename(isolated_logs, "DW1")

    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "delete logging DW1 all")
    assert capsys.readouterr().out.strip() == "% Cannot delete all logs for 'DW1' while a terminal log is active."
    assert any((isolated_logs / "DW1").iterdir())


def test_discovery_bootstrap_active_log_blocks_global_all(isolated_logs, lab_root, monkeypatch, capsys):
    _fake_transport(monkeypatch)
    terminal.open_bootstrap_terminal("DW1", {"transport": "ssh", "address": "192.0.2.1"})
    _first_log_filename(isolated_logs, "DW1")
    _write_log(isolated_logs, "R2", "20260921T090000", "b")

    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "delete logging all")
    assert capsys.readouterr().out.strip() == (
        "% Cannot delete all terminal logs while terminal logs are active. "
        "Close or wait for the active sessions first."
    )
    assert any((isolated_logs / "DW1").iterdir())
    assert [name for _, name in terminal.list_device_logs("R2")] == ["20260921T090000.log"]


def test_unrelated_device_deletion_proceeds_while_discovery_is_active(isolated_logs, lab_root, monkeypatch, capsys):
    _fake_transport(monkeypatch)
    terminal.open_bootstrap_terminal("DW1", {"transport": "ssh", "address": "192.0.2.1"})
    _first_log_filename(isolated_logs, "DW1")
    _write_log(isolated_logs, "R2", "20260921T090000", "b")

    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "delete logging R2 all")
    assert capsys.readouterr().out.strip() == "Deleted 1 terminal logs for R2."
    assert any((isolated_logs / "DW1").iterdir())  # Discovery's log untouched


def test_deletion_resumes_after_discovery_session_closes(isolated_logs, lab_root, monkeypatch, capsys):
    _fake_transport(monkeypatch)
    terminal.open_bootstrap_terminal("DW1", {"transport": "ssh", "address": "192.0.2.1"})
    _first_log_filename(isolated_logs, "DW1")
    terminal.close_bootstrap_terminal("DW1")

    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "delete logging DW1 all")
    assert capsys.readouterr().out.strip() == "Deleted 1 terminal logs for DW1."
    assert terminal.list_device_logs("DW1") == []


def test_discovery_active_never_closed_by_rejected_deletion(isolated_logs, lab_root, monkeypatch, capsys):
    _fake_transport(monkeypatch)
    terminal.open_bootstrap_terminal("DW1", {"transport": "ssh", "address": "192.0.2.1"})
    _first_log_filename(isolated_logs, "DW1")

    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "delete logging DW1 all")  # rejected
    assert terminal._session_exists(terminal.derive_discovery_session_name("DW1"))


# ==========================================================================
# Bulk atomicity: preflight failure deletes zero files (Step B section 42)
# ==========================================================================


def test_global_all_atomicity_with_active_normal_terminal(isolated_logs, lab_root, monkeypatch, capsys):
    _write_log(isolated_logs, "R1", "20260921T090000", "a")
    _fake_transport(monkeypatch)
    terminal.open_device_terminal("DW1", {"transport": "ssh", "address": "192.0.2.1"})
    _first_log_filename(isolated_logs, "DW1")

    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "delete logging all")
    assert capsys.readouterr().out.startswith("%")
    assert [name for _, name in terminal.list_device_logs("R1")] == ["20260921T090000.log"]
    assert any((isolated_logs / "DW1").iterdir())


def test_global_all_atomicity_with_active_discovery(isolated_logs, lab_root, monkeypatch, capsys):
    _write_log(isolated_logs, "R1", "20260921T090000", "a")
    _fake_transport(monkeypatch)
    terminal.open_bootstrap_terminal("DW1", {"transport": "ssh", "address": "192.0.2.1"})
    _first_log_filename(isolated_logs, "DW1")

    session = cfgmod.CliSession(lab_root)
    climain.execute_command_line(session, "delete logging all")
    assert capsys.readouterr().out.startswith("%")
    assert [name for _, name in terminal.list_device_logs("R1")] == ["20260921T090000.log"]
    assert any((isolated_logs / "DW1").iterdir())
