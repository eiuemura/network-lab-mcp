"""Step 3.7: build_topology_devices_and_links()'s per-interface L3 merge
(Section 40/41) -- unlike every other device field, `interfaces` is never
replaced wholesale: an interface not re-observed this run is preserved, an
interface re-observed with a new value is updated, and an interface
explicitly observed as removed (unassigned, or excluded as a management
address) is dropped via a `None` sentinel -- without disturbing any other
interface, device, or link."""

from __future__ import annotations

from network_lab_mcp.discovery import DiscoveryResult, ManagedLink, build_topology_devices_and_links


def _result(devices, managed_links=None):
    return DiscoveryResult(
        access_info_name="sample_lab",
        default_topology_name="sample_lab",
        iosxr_target_count=len(devices),
        connected_count=len(devices),
        observation_count=0,
        devices=devices,
        managed_links=managed_links or [],
    )


def test_new_device_interfaces_are_added():
    result = _result({"R1": {"type": "iosxr", "interfaces": {"Gi0/0": {"ipv4_address": "10.0.0.1", "vrf": "default"}}}})
    devices, _links = build_topology_devices_and_links(result, {})
    assert devices["R1"]["interfaces"] == {"Gi0/0": {"ipv4_address": "10.0.0.1", "vrf": "default"}}


def test_reobserved_interface_updates_only_that_interface_not_the_whole_dict():
    existing = {
        "devices": {
            "R1": {
                "type": "iosxr",
                "interfaces": {
                    "Gi0/0": {"ipv4_address": "10.0.0.1", "vrf": "default"},
                    "Gi0/1": {"ipv4_address": "10.0.0.5", "vrf": "default"},
                },
            }
        }
    }
    result = _result({"R1": {"type": "iosxr", "interfaces": {"Gi0/0": {"ipv4_address": "10.0.0.9", "vrf": "VRF_A"}}}})
    devices, _links = build_topology_devices_and_links(result, existing)
    assert devices["R1"]["interfaces"]["Gi0/0"] == {"ipv4_address": "10.0.0.9", "vrf": "VRF_A"}
    # Gi0/1 was NOT re-observed this run -- must survive untouched.
    assert devices["R1"]["interfaces"]["Gi0/1"] == {"ipv4_address": "10.0.0.5", "vrf": "default"}


def test_device_with_no_l3_result_this_run_keeps_all_prior_interfaces():
    """A device whose L3 enrichment failed/was skipped this run has no
    'interfaces' key at all in its DiscoveryResult fields -- its existing
    candidate interfaces must be left completely untouched (Section 40)."""
    existing = {
        "devices": {
            "R1": {"type": "iosxr", "interfaces": {"Gi0/0": {"ipv4_address": "10.0.0.1", "vrf": "default"}}}
        }
    }
    result = _result({"R1": {"type": "iosxr"}})  # no "interfaces" key -- L3 skipped this run
    devices, _links = build_topology_devices_and_links(result, existing)
    assert devices["R1"]["interfaces"] == {"Gi0/0": {"ipv4_address": "10.0.0.1", "vrf": "default"}}


def test_none_sentinel_removes_only_that_interface():
    existing = {
        "devices": {
            "R1": {
                "type": "iosxr",
                "interfaces": {
                    "Gi0/0": {"ipv4_address": "10.0.0.1", "vrf": "default"},
                    "Gi0/1": {"ipv4_address": "10.0.0.5", "vrf": "default"},
                },
            }
        }
    }
    # This run explicitly re-observed Gi0/0 as unassigned/excluded.
    result = _result({"R1": {"type": "iosxr", "interfaces": {"Gi0/0": None}}})
    devices, _links = build_topology_devices_and_links(result, existing)
    assert "Gi0/0" not in devices["R1"]["interfaces"]
    assert devices["R1"]["interfaces"]["Gi0/1"] == {"ipv4_address": "10.0.0.5", "vrf": "default"}


def test_none_sentinel_for_interface_never_present_is_a_safe_no_op():
    result = _result({"R1": {"type": "iosxr", "interfaces": {"Gi0/9": None}}})
    devices, _links = build_topology_devices_and_links(result, {})
    assert devices["R1"].get("interfaces") == {}


def test_other_device_fields_still_merge_with_plain_update():
    existing = {"devices": {"R1": {"type": "iosxr", "interfaces": {"Gi0/0": {"ipv4_address": "10.0.0.1", "vrf": "default"}}}}}
    result = _result({"R1": {"type": "iosxr"}})
    devices, _links = build_topology_devices_and_links(result, existing)
    assert devices["R1"]["type"] == "iosxr"


def test_links_are_unaffected_by_interfaces_merge():
    existing = {"devices": {"R1": {"type": "iosxr"}, "R2": {"type": "iosxr"}}, "links": []}
    result = _result(
        {
            "R1": {"type": "iosxr", "interfaces": {"Gi0/0": {"ipv4_address": "10.0.0.1", "vrf": "default"}}},
            "R2": {"type": "iosxr"},
        },
        managed_links=[ManagedLink("R1", "Gi0/0", "R2", "Gi0/0")],
    )
    devices, links = build_topology_devices_and_links(result, existing)
    assert len(links) == 1
    assert devices["R1"]["interfaces"]["Gi0/0"]["ipv4_address"] == "10.0.0.1"
