"""Gated real-lab acceptance tests for Step 3 IOS XR + LLDP Discovery.

Skipped by default -- `pytest` alone never touches real hardware. Run
explicitly with:

    NETWORK_LAB_REAL_TESTS=1 pytest tests/test_real_lab_iosxr.py -v

These tests use the real, committed `lab/settings.yaml` /
`lab/access-info/<selected>.yaml` exactly as-is (never write to either) and
open real bootstrap SSH sessions against whatever IOS XR devices the
currently selected access-info defines. They never print full
`show running-config` output or any credential value."""

from __future__ import annotations

import os

import pytest

from network_lab_mcp import discovery, lab, terminal

pytestmark = pytest.mark.real_lab

if os.environ.get("NETWORK_LAB_REAL_TESTS") != "1":
    pytest.skip(
        "set NETWORK_LAB_REAL_TESTS=1 to run real IOS XR lab acceptance tests",
        allow_module_level=True,
    )


def _real_iosxr_targets() -> dict[str, dict]:
    settings = lab.read_settings()
    access_info_name = lab.get_active_access_info_name(settings)
    if not access_info_name:
        pytest.fail("Real lab prerequisite: no access-info is selected in running-config.")
    if not lab.access_info_exists(access_info_name):
        pytest.fail(f"Real lab prerequisite: selected access-info '{access_info_name}' does not exist.")
    access_data = lab.load_access_info(access_info_name)
    return discovery._select_iosxr_targets(access_data)


@pytest.fixture(scope="module")
def iosxr_targets() -> dict[str, dict]:
    targets = _real_iosxr_targets()
    if not targets:
        pytest.fail("Real lab prerequisite: no IOS XR targets in the selected access-info.")
    return targets


@pytest.mark.parametrize("device_id", ["R1", "R2", "R3", "R4"])
def test_real_device_login_and_required_commands(device_id, iosxr_targets):
    if device_id not in iosxr_targets:
        pytest.fail(f"Real lab prerequisite: '{device_id}' is missing from the selected access-info.")
    device_config = iosxr_targets[device_id]

    try:
        info = discovery._bootstrap_collect(device_id, device_config)
    finally:
        terminal.close_bootstrap_terminal(device_id)

    assert info["hostname"], f"{device_id}: no hostname resolved from the IOS XR prompt"
    assert info["show_version"].strip(), f"{device_id}: 'show version' returned empty output"
    assert info["show_running_config"].strip(), f"{device_id}: 'show running-config' returned empty output"
    # 'show lldp neighbors' may legitimately report zero neighbors; the
    # command having executed without raising (above) is what's asserted.

    logs = terminal.list_device_logs(device_id)
    assert logs, f"{device_id}: no terminal log file was created"


def test_real_end_to_end_discovery_run():
    result = discovery.discover_topology()

    assert result.connected_count == result.iosxr_target_count
    assert result.observation_count >= 0
    assert all(device["type"] == "iosxr" for device in result.devices.values())
    for device_id in result.devices:
        assert terminal.list_device_logs(device_id), f"{device_id}: no terminal log after discovery"

    # Never commits or otherwise touches the real running-config/committed
    # topology -- this call only ever returns an in-memory DiscoveryResult.
