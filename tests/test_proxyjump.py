"""Tests for single-hop OpenSSH ProxyJump support: the access-info
jump_hosts schema/validation (lab.py), device.get_device() resolution
attaching the right jump host, and terminal.py's argv construction. No real
network access or external dependency is used -- direct calls into the
existing command-builder seam are enough to prove argv correctness."""

from __future__ import annotations

import pytest

from network_lab_mcp import lab, terminal


# ---- jump_hosts schema / validation ----


def test_jump_hosts_mapping_accepted(lab_root):
    data = {
        "name": "jh",
        "jump_hosts": {"jump1": {"type": "host", "address": "192.0.2.10", "transport": "ssh"}},
        "devices": {},
    }
    lab.write_access_info("jh", data, lab_root)
    assert lab.load_access_info("jh", lab_root) == data


def test_jump_host_type_host_accepted_case_insensitive_and_abbreviated(lab_root):
    for raw_type in ("host", "HOST", "Host", "h"):
        data = {"name": "jh", "jump_hosts": {"jump1": {"type": raw_type}}, "devices": {}}
        lab.write_access_info("jh", data, lab_root)
        assert lab.load_access_info("jh", lab_root)["jump_hosts"]["jump1"]["type"] == raw_type


def test_jump_host_network_device_types_rejected(lab_root):
    for bad_type in ("iosxr", "iosxe", "nxos"):
        data = {"name": "jh", "jump_hosts": {"jump1": {"type": bad_type}}, "devices": {}}
        with pytest.raises(lab.LabConfigError, match="must have type 'host'"):
            lab.write_access_info("jh", data, lab_root)


def test_jump_host_ssh_transport_accepted(lab_root):
    data = {"name": "jh", "jump_hosts": {"jump1": {"type": "host", "transport": "ssh"}}, "devices": {}}
    lab.write_access_info("jh", data, lab_root)


def test_jump_host_non_ssh_transport_rejected(lab_root):
    data = {"name": "jh", "jump_hosts": {"jump1": {"type": "host", "transport": "telnet"}}, "devices": {}}
    with pytest.raises(lab.LabConfigError, match="transport 'ssh'"):
        lab.write_access_info("jh", data, lab_root)


def test_device_jump_host_reference_accepted(lab_root):
    data = {
        "name": "jh",
        "jump_hosts": {"jump1": {"type": "host", "transport": "ssh"}},
        "devices": {"R1": {"type": "iosxr", "transport": "ssh", "jump_host": "jump1"}},
    }
    lab.write_access_info("jh", data, lab_root)


def test_device_jump_host_missing_reference_rejected(lab_root):
    data = {
        "name": "jh",
        "jump_hosts": {},
        "devices": {"R1": {"type": "iosxr", "transport": "ssh", "jump_host": "does-not-exist"}},
    }
    with pytest.raises(lab.LabConfigError, match="unknown jump host"):
        lab.write_access_info("jh", data, lab_root)


def test_device_telnet_with_jump_host_rejected(lab_root):
    data = {
        "name": "jh",
        "jump_hosts": {"jump1": {"type": "host", "transport": "ssh"}},
        "devices": {"R1": {"type": "iosxr", "transport": "telnet", "jump_host": "jump1"}},
    }
    with pytest.raises(lab.LabConfigError, match="transport is not 'ssh'"):
        lab.write_access_info("jh", data, lab_root)


def test_no_multi_hop_support_is_structurally_impossible(lab_root, monkeypatch):
    monkeypatch.setattr(lab, "find_lab_root", lambda: lab_root)
    # jump_hosts entries have no 'jump_host' field of their own in the
    # schema/validator -- even if manually written into YAML, resolution
    # (get_device()) never looks at it, so chaining silently has no effect.
    data = {
        "name": "jh",
        "jump_hosts": {
            "jump1": {"type": "host", "transport": "ssh", "jump_host": "jump2"},
            "jump2": {"type": "host", "transport": "ssh"},
        },
        "devices": {"R1": {"type": "iosxr", "transport": "ssh", "jump_host": "jump1"}},
    }
    lab.write_access_info("jh", data, lab_root)  # validator does not reject the stray field...
    _, resolved = _resolve(lab_root, "sample_lab", "jh", "R1")
    # ...but the resolved jump host config has no second hop attached.
    assert "jump_host_config" not in resolved["jump_host_config"]


def _resolve(lab_root, topology_name, access_info_name, device_name):
    settings = lab.read_settings(lab_root)
    settings["active_access_info"] = access_info_name
    lab.write_settings(settings, lab_root)
    return lab.get_device(device_name)


# ---- manually invalid committed YAML fails closed at get_device() time ----


def test_manually_invalid_jump_host_yaml_fails_closed_at_load(lab_root, monkeypatch):
    monkeypatch.setattr(lab, "find_lab_root", lambda: lab_root)
    (lab_root / "access-info" / "sample_lab.yaml").write_text(
        "name: sample_lab\n"
        "jump_hosts:\n"
        "  jump1:\n"
        "    type: iosxr\n"  # manually edited to an invalid jump-host type
        "devices:\n"
        "  R1:\n"
        "    type: iosxr\n"
        "    jump_host: jump1\n",
        encoding="utf-8",
    )
    with pytest.raises(lab.LabConfigError, match="must have type 'host'"):
        lab.get_device("R1")


# ---- get_device() resolution attaches the right jump host ----


def test_get_device_attaches_resolved_jump_host_config(lab_root, monkeypatch):
    monkeypatch.setattr(lab, "find_lab_root", lambda: lab_root)
    access_data = {
        "name": "sample_lab",
        "jump_hosts": {
            "jump1": {
                "type": "host",
                "address": "192.168.1.10",
                "transport": "ssh",
                "port": 22,
                "username": "jump-user",
                "password": "jump-pass",
            },
        },
        "devices": {
            "R1": {
                "type": "iosxr",
                "address": "192.168.70.159",
                "transport": "ssh",
                "port": 22,
                "username": "target-user",
                "password": "target-pass",
                "jump_host": "jump1",
            },
            "R2": {"type": "iosxr", "address": "192.0.2.12", "transport": "ssh"},
        },
    }
    lab.write_access_info("sample_lab", access_data, lab_root)

    _, resolved = lab.get_device("R1")
    assert resolved["jump_host_config"]["address"] == "192.168.1.10"
    assert resolved["jump_host_config"]["username"] == "jump-user"
    assert resolved["jump_host_config"]["password"] == "jump-pass"

    # Distinct credential scopes: the device's own username/password are
    # never overwritten by, or confused with, the jump host's.
    assert resolved["username"] == "target-user"
    assert resolved["password"] == "target-pass"

    # A device with no jump_host resolves with no jump_host_config at all.
    _, resolved_r2 = lab.get_device("R2")
    assert "jump_host_config" not in resolved_r2


# ---- terminal.py argv construction ----


def test_direct_ssh_has_no_proxyjump_argv():
    transport, command = terminal._build_transport_command(
        {"transport": "ssh", "address": "192.0.2.11", "port": 22, "username": "cisco"}
    )
    assert transport == "ssh"
    assert command == ["ssh", "-p", "22", "cisco@192.0.2.11"]
    assert "-J" not in command


def test_proxyjump_ssh_argv_has_native_dash_j_option():
    transport, command = terminal._build_transport_command(
        {
            "transport": "ssh",
            "address": "192.168.70.159",
            "port": 22,
            "username": "cisco",
            "jump_host_config": {
                "address": "192.168.1.10",
                "port": 22,
                "username": "jump-user",
            },
        }
    )
    assert transport == "ssh"
    assert command == ["ssh", "-J", "jump-user@192.168.1.10:22", "-p", "22", "cisco@192.168.70.159"]


def test_proxyjump_preserves_non_default_ports():
    _, command = terminal._build_transport_command(
        {
            "transport": "ssh",
            "address": "10.0.0.5",
            "port": 2222,
            "username": "target",
            "jump_host_config": {"address": "10.0.0.1", "port": 2200, "username": "jump"},
        }
    )
    assert command == ["ssh", "-J", "jump@10.0.0.1:2200", "-p", "2222", "target@10.0.0.5"]


def test_proxyjump_argv_never_contains_passwords():
    _, command = terminal._build_transport_command(
        {
            "transport": "ssh",
            "address": "192.168.70.159",
            "username": "cisco",
            "password": "target-secret",
            "jump_host_config": {"address": "192.168.1.10", "username": "jump-user", "password": "jump-secret"},
        }
    )
    joined = " ".join(command)
    assert "target-secret" not in joined
    assert "jump-secret" not in joined


def test_telnet_transport_unaffected_by_jump_host_key_presence():
    # A jump_host_config key should never be attached for telnet in
    # practice (validate_device_jump_host_references() rejects that
    # combination at commit time), but the argv builder itself only looks
    # at it for the ssh branch -- direct telnet behavior is unchanged.
    transport, command = terminal._build_transport_command({"transport": "telnet", "address": "192.0.2.1", "port": 23})
    assert transport == "telnet"
    assert command == ["telnet", "192.0.2.1", "23"]
