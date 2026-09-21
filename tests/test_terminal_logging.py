"""Persistent terminal transcript logging (logs/terminal/<device-id>/*.log).

Uses real tmux sessions (in the isolated network-lab-mcp tmux server, via
the same validation-session namespace other Step 1 tests use) so pipe-pane
behavior is genuinely exercised, but never a real router -- sessions run a
safe local command instead of ssh/telnet. `terminal.LOGS_ROOT` is
monkeypatched to a temporary directory so nothing here ever touches the
real repository's logs/."""

from __future__ import annotations

import time

import pytest

from network_lab_mcp import terminal


@pytest.fixture()
def isolated_logs(tmp_path, monkeypatch):
    logs_root = tmp_path / "logs" / "terminal"
    monkeypatch.setattr(terminal, "LOGS_ROOT", logs_root)
    return logs_root


def _open_validation(validation_id: str, script: str) -> str:
    result = terminal.open_validation_session(validation_id, ["bash", "-c", script])
    return result["session_name"]


def _wait_until(predicate, timeout=3.0, interval=0.1):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


@pytest.fixture(autouse=True)
def _cleanup_validation_sessions():
    yield
    for session_name in list(terminal.list_validation_sessions()):
        validation_id = session_name[len(terminal.VALIDATION_PREFIX) :]
        terminal.close_validation_session(validation_id)


def test_pipe_pane_creates_expected_log_file_and_captures_output(isolated_logs):
    # A small delay before the first output mirrors a real SSH connection's
    # handshake time, giving pipe-pane a realistic window to attach before
    # anything is printed (tmux pipe-pane never replays pane history).
    session_name = _open_validation("logtest-basic", "sleep 0.3; echo HELLO-LOG; sleep 2")
    terminal._start_session_logging(session_name, "R9")

    log_dir = isolated_logs / "R9"
    assert _wait_until(lambda: log_dir.is_dir() and any(log_dir.iterdir()))

    files = list(log_dir.iterdir())
    assert len(files) == 1
    assert terminal._LOG_FILENAME_RE.match(files[0].name)

    assert _wait_until(lambda: "HELLO-LOG" in files[0].read_text())

    terminal.close_validation_session("logtest-basic")


def test_multiple_devices_get_separate_log_directories(isolated_logs):
    s1 = _open_validation("logtest-multi-a", "sleep 2")
    s2 = _open_validation("logtest-multi-b", "sleep 2")
    terminal._start_session_logging(s1, "R9")
    terminal._start_session_logging(s2, "R10")

    assert _wait_until(lambda: list((isolated_logs / "R9").glob("*.log")) != [])
    assert _wait_until(lambda: list((isolated_logs / "R10").glob("*.log")) != [])

    terminal.close_validation_session("logtest-multi-a")
    terminal.close_validation_session("logtest-multi-b")


def test_reused_session_does_not_start_a_second_log_file(isolated_logs, monkeypatch):
    monkeypatch.setattr(terminal, "_build_transport_command", lambda config, **kw: ("ssh", ["cat"]))
    terminal.open_device_terminal("R9", {"transport": "ssh", "address": "192.0.2.1"})
    terminal.open_device_terminal("R9", {"transport": "ssh", "address": "192.0.2.1"})  # reused

    log_dir = isolated_logs / "R9"
    assert _wait_until(lambda: list(log_dir.glob("*.log")) != [])
    assert len(list(log_dir.iterdir())) == 1

    terminal.close_device_terminal("R9")


def test_terminal_read_behavior_is_unaffected_by_logging(isolated_logs):
    session_name = _open_validation("logtest-read", "cat")
    terminal._start_session_logging(session_name, "R9")
    terminal._send_literal_text(session_name, "ping")
    terminal._send_enter(session_name)

    assert _wait_until(lambda: "ping" in terminal._capture_pane(session_name, 100))

    terminal.close_validation_session("logtest-read")


def test_sent_text_is_not_duplicated_into_a_second_application_log(isolated_logs):
    session_name = _open_validation("logtest-dup", "cat")
    terminal._start_session_logging(session_name, "R9")
    for value in ("one", "two", "three"):
        terminal._send_literal_text(session_name, value)
        terminal._send_enter(session_name)

    log_dir = isolated_logs / "R9"
    assert _wait_until(lambda: log_dir.is_dir() and any(log_dir.iterdir()))
    # Exactly one log file for this device, regardless of how many sends
    # happened on the pane -- there is no separate per-send application log.
    assert len(list(log_dir.iterdir())) == 1

    terminal.close_validation_session("logtest-dup")


# ---- listing / reading (used by `show logging`) ----


def test_list_logged_device_ids_empty_when_no_logs_dir(isolated_logs):
    assert terminal.list_logged_device_ids() == []


def test_list_device_logs_empty_for_unknown_device(isolated_logs):
    assert terminal.list_device_logs("R9") == []


def test_list_device_logs_newest_first(isolated_logs):
    device_dir = isolated_logs / "R9"
    device_dir.mkdir(parents=True)
    (device_dir / "20260101T090000.log").write_text("old\n")
    (device_dir / "20260101T100000.log").write_text("new\n")

    entries = terminal.list_device_logs("R9")
    assert [name for _, name in entries] == ["20260101T100000.log", "20260101T090000.log"]
    assert terminal.list_logged_device_ids() == ["R9"]


def test_read_device_log_returns_exact_content(isolated_logs):
    device_dir = isolated_logs / "R9"
    device_dir.mkdir(parents=True)
    (device_dir / "20260101T090000.log").write_text("transcript content\n")

    assert terminal.read_device_log("R9", "20260101T090000.log") == "transcript content\n"


def test_read_device_log_rejects_unknown_filename(isolated_logs):
    device_dir = isolated_logs / "R9"
    device_dir.mkdir(parents=True)
    (device_dir / "20260101T090000.log").write_text("x")

    with pytest.raises(terminal.TerminalError):
        terminal.read_device_log("R9", "20260101T999999.log")


@pytest.mark.parametrize(
    "traversal",
    ["../../../etc/passwd", "../secret.log", "/etc/passwd", "..\\..\\secret.log"],
)
def test_read_device_log_rejects_path_traversal(isolated_logs, traversal):
    device_dir = isolated_logs / "R9"
    device_dir.mkdir(parents=True)
    (device_dir / "20260101T090000.log").write_text("x")

    with pytest.raises(terminal.TerminalError):
        terminal.read_device_log("R9", traversal)


def test_read_device_log_rejects_invalid_device_name(isolated_logs):
    with pytest.raises(terminal.TerminalError):
        terminal.read_device_log("../etc", "20260101T090000.log")
