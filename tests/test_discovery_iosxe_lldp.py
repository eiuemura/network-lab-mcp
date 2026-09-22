"""Step 3.7: IOS XE LLDP collection (added on top of Step 3.6's CDP-only
IOS XE support) -- `% LLDP is not enabled` must mean zero LLDP
observations, never a device failure, with CDP staying fully usable
either way; an enabled IOS XE LLDP response parses correctly reusing
parse_lldp_neighbors() unchanged (verified format-compatible against
Cisco's own IOS XE Carrier Ethernet Command Reference -- see the final
report / docs/architecture.md); LLDP+CDP dedup/conflict on IOS XE reuses
the exact same reconcile_links() Step 3.6 already relies on, not a
second implementation."""

from __future__ import annotations

from network_lab_mcp import discovery, lab, terminal
import pytest

_CDP_HEADER = "Device ID        Local Intrfce     Holdtme    Capability  Platform    Port ID"
# IOS XE's documented `show lldp neighbors` format (Cisco Carrier Ethernet
# Command Reference, Catalyst 3850 / IOS XE): identical column shape and
# capability-code legend to IOS XR's, which is why parse_lldp_neighbors()
# needs zero changes to accept it.
_LLDP_HEADER = "Device ID       Local Intf                      Hold-time  Capability      Port ID"


@pytest.fixture(autouse=True)
def _patch_lab_root(lab_root, monkeypatch):
    monkeypatch.setattr(lab, "find_lab_root", lambda: lab_root)


@pytest.fixture(autouse=True)
def _track_bootstrap_close(monkeypatch):
    monkeypatch.setattr(terminal, "close_bootstrap_terminal", lambda device_id: None)


def _write_access_info(lab_root, devices):
    lab.write_access_info("sample_lab", {"name": "sample_lab", "devices": devices}, lab_root)


def test_iosxe_lldp_disabled_yields_zero_observations_cdp_still_usable(lab_root, monkeypatch):
    def fake_collect(device_id, cfg):
        row = f"{'PAGENT':<17}{'Gi0/0/0/10':<18}{'144':<11}{'R':<12}{'Cisco 720':<12}Gig0/0"
        return {
            "hostname": "SW1",
            "show_version": "x",
            "show_lldp_neighbors": "% LLDP is not enabled\n",
            "show_cdp_neighbors": f"{_CDP_HEADER}\n{row}\n",
        }

    monkeypatch.setattr(discovery, "_bootstrap_collect_iosxe", fake_collect)
    _write_access_info(lab_root, {"SW1": {"type": "iosxe", "address": "192.0.2.31", "transport": "ssh"}})

    result = discovery.discover_topology(lab_root)

    assert result.observation_count == 0  # LLDP: disabled -> zero, not a failure
    assert result.cdp_observation_count == 1
    assert {u.remote_device_id_raw for u in result.unresolved} == {"PAGENT"}


def test_iosxe_enabled_lldp_parses_via_the_unchanged_iosxr_parser(lab_root, monkeypatch):
    """Documented IOS XE format example (Cisco Carrier Ethernet Command
    Reference): "Device2 Et0/0 150 R Et0/0" -- 5 whitespace tokens,
    identical shape to IOS XR's own rows."""

    def fake_collect(device_id, cfg):
        return {
            "hostname": "SW1",
            "show_version": "x",
            "show_lldp_neighbors": f"{_LLDP_HEADER}\nSW2 Et0/0 150 R Et0/0\nTotal entries displayed: 1\n",
            "show_cdp_neighbors": "% CDP is not enabled\n",
        }

    monkeypatch.setattr(discovery, "_bootstrap_collect_iosxe", fake_collect)
    _write_access_info(lab_root, {"SW1": {"type": "iosxe", "address": "192.0.2.31", "transport": "ssh"}})

    result = discovery.discover_topology(lab_root)

    assert result.observation_count == 1
    assert {u.remote_device_id_raw for u in result.unresolved} == {"SW2"}


def test_iosxe_lldp_and_cdp_agree_dedupe_to_one_link(lab_root, monkeypatch):
    def fake_sw1(device_id, cfg):
        row = f"{'SW2':<17}{'Et0/0':<18}{'150':<11}{'R':<12}{'Cat':<12}Et0/0"
        return {
            "hostname": "SW1",
            "show_version": "x",
            "show_lldp_neighbors": f"{_LLDP_HEADER}\nSW2 Et0/0 150 R Et0/0\nTotal entries displayed: 1\n",
            "show_cdp_neighbors": f"{_CDP_HEADER}\n{row}\n",
        }

    def fake_sw2(device_id, cfg):
        row = f"{'SW1':<17}{'Et0/0':<18}{'150':<11}{'R':<12}{'Cat':<12}Et0/0"
        return {
            "hostname": "SW2",
            "show_version": "x",
            "show_lldp_neighbors": f"{_LLDP_HEADER}\nSW1 Et0/0 150 R Et0/0\nTotal entries displayed: 1\n",
            "show_cdp_neighbors": f"{_CDP_HEADER}\n{row}\n",
        }

    def dispatch(device_id, cfg):
        return fake_sw1(device_id, cfg) if device_id == "SW1" else fake_sw2(device_id, cfg)

    monkeypatch.setattr(discovery, "_bootstrap_collect_iosxe", dispatch)
    _write_access_info(
        lab_root,
        {
            "SW1": {"type": "iosxe", "address": "192.0.2.31", "transport": "ssh"},
            "SW2": {"type": "iosxe", "address": "192.0.2.32", "transport": "ssh"},
        },
    )

    result = discovery.discover_topology(lab_root)

    assert result.conflicts == []
    assert len(result.managed_links) == 1


def test_iosxe_lldp_and_cdp_disagree_on_same_local_interface_is_a_conflict(lab_root, monkeypatch):
    def fake_sw1(device_id, cfg):
        cdp_row = f"{'SW3':<17}{'Et0/0':<18}{'150':<11}{'R':<12}{'Cat':<12}Et0/0"
        return {
            "hostname": "SW1",
            "show_version": "x",
            "show_lldp_neighbors": f"{_LLDP_HEADER}\nSW2 Et0/0 150 R Et0/0\nTotal entries displayed: 1\n",
            "show_cdp_neighbors": f"{_CDP_HEADER}\n{cdp_row}\n",
        }

    def fake_other(device_id, cfg):
        return {
            "hostname": device_id,
            "show_version": "x",
            "show_lldp_neighbors": "% LLDP is not enabled\n",
            "show_cdp_neighbors": "% CDP is not enabled\n",
        }

    def dispatch(device_id, cfg):
        return fake_sw1(device_id, cfg) if device_id == "SW1" else fake_other(device_id, cfg)

    monkeypatch.setattr(discovery, "_bootstrap_collect_iosxe", dispatch)
    _write_access_info(
        lab_root,
        {
            "SW1": {"type": "iosxe", "address": "192.0.2.31", "transport": "ssh"},
            "SW2": {"type": "iosxe", "address": "192.0.2.32", "transport": "ssh"},
            "SW3": {"type": "iosxe", "address": "192.0.2.33", "transport": "ssh"},
        },
    )

    result = discovery.discover_topology(lab_root)

    assert len(result.conflicts) == 1
    conflict = result.conflicts[0]
    assert conflict.endpoint_a == ("SW1", "Et0/0")
    assert {conflict.observation_a.source, conflict.observation_b.source} == {"lldp", "cdp"}
    # No link at all for the conflicting endpoint -- fail closed, not a guess.
    assert result.managed_links == []
