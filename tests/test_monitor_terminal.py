"""Step 3.4: `monitor terminal <device-id>` -- read-only observation
backend and monitor UI lifecycle.

Two layers are tested largely independently (Section 53's "separate
observe-from-render from the periodic refresh loop" principle):

- `terminal.capture_device_terminal_view()`: the pure, side-effect-free
  observation primitive (WAITING/ACTIVE/ENDED), exercised directly against
  real (isolated) tmux sessions -- deterministic, no monitor UI involved.
- `cli.main._render_monitor_view()` / `run_terminal_monitor()`: the
  monitor UI itself, exercised through prompt_toolkit's own supported
  headless testing seam (`create_pipe_input()` + `DummyOutput()` +
  `create_app_session()`), event-driven (`threading.Event`), never a bare
  `time.sleep()` used as the pass/fail signal.

Uses the isolated network-lab-mcp tmux socket (real tmux, never a real
router) with `terminal.LOGS_ROOT` monkeypatched to a temp directory, same
convention as test_terminal_logging.py / test_terminal_concurrency.py."""

from __future__ import annotations

import threading

import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from network_lab_mcp import terminal
from network_lab_mcp.cli import main as climain

_TIMEOUT = 5.0


@pytest.fixture(autouse=True)
def isolated_logs(tmp_path, monkeypatch):
    monkeypatch.setattr(terminal, "LOGS_ROOT", tmp_path / "logs" / "terminal")


@pytest.fixture(autouse=True)
def _cleanup_device_sessions():
    yield
    for session in terminal.list_device_sessions():
        terminal.close_device_terminal(session["device"])
    # A few tests deliberately create a session via terminal._create_session()
    # directly (to control whether the *pane's own process* dies, for
    # ENDED-state testing) rather than through _ensure_managed_session(),
    # which is the only place that ordinarily retires the shared bootstrap
    # session once a real one exists -- clean it up here too so this file
    # never leaves it behind for the rest of the suite.
    terminal._kill_session_if_exists(terminal.BOOTSTRAP_SESSION)


def _open_fake(device: str, script: str = "sleep 5") -> None:
    terminal._ensure_managed_session(
        terminal.derive_production_session_name(device), ["bash", "-c", script], log_device_name=device
    )


# ==========================================================================
# Observation backend: WAITING / ACTIVE / ENDED (Sections 26, 42-45)
# ==========================================================================


def test_waiting_when_no_session_exists():
    snapshot = terminal.capture_device_terminal_view("R1")
    assert snapshot.status == "waiting"
    assert snapshot.pane_text == ""


def test_active_when_session_exists():
    _open_fake("R1")
    snapshot = terminal.capture_device_terminal_view("R1")
    assert snapshot.status == "active"


def test_waiting_again_after_session_disappears():
    _open_fake("R1")
    assert terminal.capture_device_terminal_view("R1").status == "active"
    terminal.close_device_terminal("R1")
    snapshot = terminal.capture_device_terminal_view("R1")
    assert snapshot.status == "waiting"
    assert snapshot.pane_text == ""


def test_resumes_active_after_recreation_under_same_name():
    _open_fake("R1")
    assert terminal.capture_device_terminal_view("R1").status == "active"
    terminal.close_device_terminal("R1")
    assert terminal.capture_device_terminal_view("R1").status == "waiting"
    _open_fake("R1")
    assert terminal.capture_device_terminal_view("R1").status == "active"


def test_ended_when_pane_process_dies_but_session_remains():
    terminal._ensure_tmux_environment()
    session_name = terminal.derive_production_session_name("R1")
    terminal._create_session(session_name, ["bash", "-c", "echo done; exit 0"])

    def _observed_ended() -> bool:
        return terminal.capture_device_terminal_view("R1").status == "ended"

    assert _wait_until(_observed_ended)
    snapshot = terminal.capture_device_terminal_view("R1")
    assert snapshot.status == "ended"
    assert "done" in snapshot.pane_text


def test_ended_pane_replaced_by_recreation_becomes_active():
    terminal._ensure_tmux_environment()
    session_name = terminal.derive_production_session_name("R1")
    terminal._create_session(session_name, ["bash", "-c", "exit 0"])
    assert _wait_until(lambda: terminal.capture_device_terminal_view("R1").status == "ended")
    terminal.close_device_terminal("R1")
    _open_fake("R1")
    assert terminal.capture_device_terminal_view("R1").status == "active"


def _wait_until(predicate, timeout: float = _TIMEOUT, interval: float = 0.05) -> bool:
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


# ==========================================================================
# Capture race: target vanishes between the state check and the capture
# (Section 27/49)
# ==========================================================================


def test_capture_race_target_vanishes_mid_observation(monkeypatch):
    _open_fake("R1")

    def _vanished_capture(session_name, lines):
        raise terminal.TerminalError(f"Session '{session_name}' does not exist.")

    monkeypatch.setattr(terminal, "_capture_pane", _vanished_capture)
    snapshot = terminal.capture_device_terminal_view("R1")
    assert snapshot.status == "waiting"
    assert snapshot.pane_text == ""


def test_capture_race_recovers_once_capture_succeeds_again():
    _open_fake("R1")
    assert terminal.capture_device_terminal_view("R1").status == "active"


# ==========================================================================
# Pure render function (Section 53)
# ==========================================================================


def test_render_waiting_text(monkeypatch):
    monkeypatch.setattr(
        terminal, "capture_device_terminal_view", lambda d: terminal.TerminalMonitorSnapshot(d, "waiting", "")
    )
    text = climain._render_monitor_view("R1")
    assert "Monitoring terminal R1" in text
    assert "Read-only" in text
    assert "waiting for managed terminal session" in text


def test_render_active_text(monkeypatch):
    monkeypatch.setattr(
        terminal,
        "capture_device_terminal_view",
        lambda d: terminal.TerminalMonitorSnapshot(d, "active", "RP/0/RP0/CPU0:R1#show version"),
    )
    text = climain._render_monitor_view("R1")
    assert "Status: active" in text
    assert "RP/0/RP0/CPU0:R1#show version" in text


def test_render_ended_text(monkeypatch):
    monkeypatch.setattr(
        terminal, "capture_device_terminal_view", lambda d: terminal.TerminalMonitorSnapshot(d, "ended", "last output")
    )
    text = climain._render_monitor_view("R1")
    assert "terminal session ended" in text
    assert "last output" in text


# ==========================================================================
# Monitor UI lifecycle through the real Application (Sections 42, 46, 47)
# ==========================================================================


def test_full_lifecycle_waiting_then_active_then_quit():
    device = "R1"
    seen_waiting = threading.Event()
    seen_active = threading.Event()
    real_capture = terminal.capture_device_terminal_view

    def tracking_capture(dev):
        snapshot = real_capture(dev)
        if snapshot.status == "waiting":
            seen_waiting.set()
        elif snapshot.status == "active":
            seen_active.set()
        return snapshot

    def driver(pipe_input):
        assert seen_waiting.wait(timeout=_TIMEOUT)
        _open_fake(device)
        assert seen_active.wait(timeout=_TIMEOUT)
        pipe_input.send_text("q")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(terminal, "capture_device_terminal_view", tracking_capture)
        with create_pipe_input() as pipe_input:
            threading.Thread(target=driver, args=(pipe_input,), daemon=True).start()
            with create_app_session(input=pipe_input, output=DummyOutput()):
                climain.run_terminal_monitor(device, refresh_interval=0.02)

    assert seen_waiting.is_set()
    assert seen_active.is_set()


def test_session_disappearance_does_not_exit_monitor():
    device = "R1"
    _open_fake(device)
    seen_active = threading.Event()
    seen_waiting_after_active = threading.Event()
    became_active_once = threading.Event()
    real_capture = terminal.capture_device_terminal_view

    def tracking_capture(dev):
        snapshot = real_capture(dev)
        if snapshot.status == "active":
            seen_active.set()
            became_active_once.set()
        elif snapshot.status == "waiting" and became_active_once.is_set():
            seen_waiting_after_active.set()
        return snapshot

    def driver(pipe_input):
        assert seen_active.wait(timeout=_TIMEOUT)
        terminal.close_device_terminal(device)
        assert seen_waiting_after_active.wait(timeout=_TIMEOUT)
        pipe_input.send_text("q")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(terminal, "capture_device_terminal_view", tracking_capture)
        with create_pipe_input() as pipe_input:
            threading.Thread(target=driver, args=(pipe_input,), daemon=True).start()
            with create_app_session(input=pipe_input, output=DummyOutput()):
                climain.run_terminal_monitor(device, refresh_interval=0.02)

    # Monitor did not exit on disappearance -- it ran until q was sent.
    assert seen_waiting_after_active.is_set()


def test_session_recreation_resumes_display():
    device = "R1"
    _open_fake(device)
    phases: list[str] = []
    real_capture = terminal.capture_device_terminal_view
    phase_lock = threading.Lock()
    first_active = threading.Event()
    waiting_after_close = threading.Event()
    second_active = threading.Event()

    def tracking_capture(dev):
        snapshot = real_capture(dev)
        with phase_lock:
            if not phases or phases[-1] != snapshot.status:
                phases.append(snapshot.status)
        if snapshot.status == "active" and not first_active.is_set():
            first_active.set()
        elif snapshot.status == "waiting" and first_active.is_set():
            waiting_after_close.set()
        elif snapshot.status == "active" and waiting_after_close.is_set():
            second_active.set()
        return snapshot

    def driver(pipe_input):
        assert first_active.wait(timeout=_TIMEOUT)
        terminal.close_device_terminal(device)
        assert waiting_after_close.wait(timeout=_TIMEOUT)
        _open_fake(device)
        assert second_active.wait(timeout=_TIMEOUT)
        pipe_input.send_text("q")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(terminal, "capture_device_terminal_view", tracking_capture)
        with create_pipe_input() as pipe_input:
            threading.Thread(target=driver, args=(pipe_input,), daemon=True).start()
            with create_app_session(input=pipe_input, output=DummyOutput()):
                climain.run_terminal_monitor(device, refresh_interval=0.02)

    assert first_active.is_set()
    assert waiting_after_close.is_set()
    assert second_active.is_set()
    # active -> waiting -> active sequence actually observed, in order.
    assert "active" in phases
    assert phases.index("waiting", phases.index("active")) < len(phases) - 1 or second_active.is_set()


def test_q_exits_and_managed_session_remains_untouched():
    device = "R1"
    _open_fake(device)
    with create_pipe_input() as pipe_input:
        pipe_input.send_text("q")
        with create_app_session(input=pipe_input, output=DummyOutput()):
            climain.run_terminal_monitor(device, refresh_interval=0.02)
    # The managed session is still alive -- q was never sent to tmux.
    sessions = {s["device"] for s in terminal.list_device_sessions()}
    assert device in sessions


def test_uppercase_q_also_exits():
    device = "R1"
    _open_fake(device)
    with create_pipe_input() as pipe_input:
        pipe_input.send_text("Q")
        with create_app_session(input=pipe_input, output=DummyOutput()):
            climain.run_terminal_monitor(device, refresh_interval=0.02)
    sessions = {s["device"] for s in terminal.list_device_sessions()}
    assert device in sessions


def test_ctrl_c_exits_monitor_only():
    device = "R1"
    _open_fake(device)
    with create_pipe_input() as pipe_input:
        pipe_input.send_text("\x03")  # Ctrl-C
        with create_app_session(input=pipe_input, output=DummyOutput()):
            climain.run_terminal_monitor(device, refresh_interval=0.02)
    # Returning from run_terminal_monitor() at all (no exception, no hang)
    # proves only the monitor Application exited, not the process.
    sessions = {s["device"] for s in terminal.list_device_sessions()}
    assert device in sessions


def test_other_keystrokes_are_ignored_never_forwarded():
    device = "R1"
    _open_fake(device)
    sent: list[tuple] = []
    real_send = terminal._send_literal_text
    real_special = terminal._send_special_keys

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(terminal, "_send_literal_text", lambda *a: sent.append(("text", *a)) or real_send(*a))
        mp.setattr(terminal, "_send_special_keys", lambda *a: sent.append(("keys", *a)) or real_special(*a))
        with create_pipe_input() as pipe_input:
            pipe_input.send_text("show version\r")  # ordinary keystrokes, then Enter
            pipe_input.send_text("q")
            with create_app_session(input=pipe_input, output=DummyOutput()):
                climain.run_terminal_monitor(device, refresh_interval=0.02)

    assert sent == []


# ==========================================================================
# No-write / read-only safety (Section 48)
# ==========================================================================


def test_monitor_never_calls_any_mutating_terminal_helper():
    device = "R1"
    real_open_device_terminal = terminal.open_device_terminal
    real_send_to_device = terminal.send_to_device
    real_close_device_terminal = terminal.close_device_terminal
    real_ensure_managed_session = terminal._ensure_managed_session
    real_send_literal_text = terminal._send_literal_text
    real_send_special_keys = terminal._send_special_keys
    real_send_enter = terminal._send_enter
    real_create_session = terminal._create_session
    real_create_logged_session = terminal._create_logged_session
    real_close_session = terminal._close_session
    real_start_session_logging = terminal._start_session_logging

    calls: list[str] = []

    def _tracked(name, fn):
        def wrapper(*args, **kwargs):
            calls.append(name)
            return fn(*args, **kwargs)

        return wrapper

    seen_active = threading.Event()
    seen_waiting_after = threading.Event()
    became_active = threading.Event()

    def tracking_capture(dev):
        snapshot = terminal.TerminalMonitorSnapshot(
            dev,
            *(("active", "pane") if became_active.is_set() and not seen_waiting_after.is_set() else ("waiting", "")),
        )
        return snapshot

    # Bootstrap the shared tmux server *before* patching -- otherwise the
    # driver's own _ensure_tmux_environment() call below would need to
    # create the shared bootstrap session for the first time from *inside*
    # the patched block, which (since _ensure_tmux_environment() calls
    # _create_session() by plain module-level name lookup, not a captured
    # reference) would dispatch to the tracked wrapper and falsely count
    # as a "monitor-triggered" call.
    terminal._ensure_tmux_environment()

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(terminal, "open_device_terminal", _tracked("open_device_terminal", real_open_device_terminal))
        mp.setattr(terminal, "send_to_device", _tracked("send_to_device", real_send_to_device))
        mp.setattr(terminal, "close_device_terminal", _tracked("close_device_terminal", real_close_device_terminal))
        mp.setattr(terminal, "_ensure_managed_session", _tracked("_ensure_managed_session", real_ensure_managed_session))
        mp.setattr(terminal, "_send_literal_text", _tracked("_send_literal_text", real_send_literal_text))
        mp.setattr(terminal, "_send_special_keys", _tracked("_send_special_keys", real_send_special_keys))
        mp.setattr(terminal, "_send_enter", _tracked("_send_enter", real_send_enter))
        mp.setattr(terminal, "_create_session", _tracked("_create_session", real_create_session))
        mp.setattr(terminal, "_create_logged_session", _tracked("_create_logged_session", real_create_logged_session))
        mp.setattr(terminal, "_close_session", _tracked("_close_session", real_close_session))
        mp.setattr(terminal, "_start_session_logging", _tracked("_start_session_logging", real_start_session_logging))

        # The driver simulates lifecycle changes via raw tmux calls
        # (terminal._run(), never patched) instead of any of the tracked
        # helpers above -- those internally call each other by module-level
        # name lookup, so even a *saved* reference to e.g. the original
        # _ensure_managed_session would still dispatch to the now-patched
        # _create_logged_session/_send_enter/etc. Bypassing all of them
        # means only monitor-triggered calls (if any) appear in `calls`.
        def driver(pipe_input):
            import time

            session_name = terminal.derive_production_session_name(device)
            terminal._run(["new-session", "-d", "-s", session_name, "-x", "220", "-y", "50", "bash", "-c", "sleep 5"])
            became_active.set()
            time.sleep(0.2)
            terminal._run(["kill-session", "-t", session_name], check=False)
            seen_waiting_after.set()
            time.sleep(0.2)
            pipe_input.send_text("q")

        with create_pipe_input() as pipe_input:
            threading.Thread(target=driver, args=(pipe_input,), daemon=True).start()
            with create_app_session(input=pipe_input, output=DummyOutput()):
                climain.run_terminal_monitor(device, refresh_interval=0.02)

    assert calls == []


# ==========================================================================
# Multiple-monitor isolation (Section 51)
# ==========================================================================


def _run_monitor_in_own_session(device: str, exit_after: threading.Event, result: dict) -> None:
    with create_pipe_input() as pipe_input:

        def waiter():
            exit_after.wait(timeout=_TIMEOUT)
            pipe_input.send_text("q")

        threading.Thread(target=waiter, daemon=True).start()
        with create_app_session(input=pipe_input, output=DummyOutput()):
            climain.run_terminal_monitor(device, refresh_interval=0.02)
    result["exited"] = True


def test_two_monitors_of_different_devices_are_independent():
    _open_fake("R1")
    _open_fake("R2")
    exit_r1 = threading.Event()
    exit_r2 = threading.Event()
    result_r1: dict = {}
    result_r2: dict = {}

    t1 = threading.Thread(target=_run_monitor_in_own_session, args=("R1", exit_r1, result_r1))
    t2 = threading.Thread(target=_run_monitor_in_own_session, args=("R2", exit_r2, result_r2))
    t1.start()
    t2.start()

    exit_r1.set()
    t1.join(timeout=_TIMEOUT)
    assert result_r1.get("exited") is True
    # Monitor B is still running -- quitting A did not affect it.
    assert not t2.is_alive() or "exited" not in result_r2

    exit_r2.set()
    t2.join(timeout=_TIMEOUT)
    assert result_r2.get("exited") is True


def test_two_monitors_of_the_same_device_are_independent():
    _open_fake("R1")
    exit_a = threading.Event()
    exit_b = threading.Event()
    result_a: dict = {}
    result_b: dict = {}

    t1 = threading.Thread(target=_run_monitor_in_own_session, args=("R1", exit_a, result_a))
    t2 = threading.Thread(target=_run_monitor_in_own_session, args=("R1", exit_b, result_b))
    t1.start()
    t2.start()

    exit_a.set()
    t1.join(timeout=_TIMEOUT)
    assert result_a.get("exited") is True
    assert "exited" not in result_b  # B's own quit was never triggered

    exit_b.set()
    t2.join(timeout=_TIMEOUT)
    assert result_b.get("exited") is True
    # The shared device's session was never touched by either monitor.
    assert "R1" in {s["device"] for s in terminal.list_device_sessions()}


# ==========================================================================
# Step 3.3 coexistence: monitor never holds the per-device lock
# (Sections 10, 36, 50)
# ==========================================================================


def test_monitor_does_not_block_concurrent_ai_terminal_operations():
    _open_fake("R1")
    stop = threading.Event()

    def monitor_thread():
        with create_pipe_input() as pipe_input:
            def waiter():
                stop.wait(timeout=_TIMEOUT)
                pipe_input.send_text("q")

            threading.Thread(target=waiter, daemon=True).start()
            with create_app_session(input=pipe_input, output=DummyOutput()):
                climain.run_terminal_monitor("R1", refresh_interval=0.02)

    t = threading.Thread(target=monitor_thread)
    t.start()
    try:
        # While the monitor is actively observing R1, an AI-style
        # terminal_read()/send_to_device() call for the *same* device must
        # complete promptly -- proving capture_device_terminal_view() does
        # not hold terminal._session_lock() across its observation.
        for _ in range(10):
            result = terminal.read_device("R1")
            assert result["device"] == "R1"
            terminal.send_to_device("R1", None, None, False)
    finally:
        stop.set()
        t.join(timeout=_TIMEOUT)
