"""IOS XR `show lldp neighbors` parser (parse_lldp_neighbors).

Uses the sanitized real-lab-derived fixtures under tests/fixtures/lldp/ --
no access-info, credentials, or private addresses. The parser only ever
produces raw normalized observations; it never resolves a remote Device ID
to a logical managed device (see test_discovery_identity.py for that
separate stage)."""

from __future__ import annotations

from pathlib import Path

from network_lab_mcp.discovery import parse_lldp_neighbors

FIXTURES = Path(__file__).parent / "fixtures" / "lldp"


def _read(name: str) -> str:
    return (FIXTURES / name).read_text()


def test_r1_fixture_parses_five_observations():
    text = _read("r1_show_lldp_neighbors.txt")
    observations = parse_lldp_neighbors(text, "R1")
    assert len(observations) == 5
    assert observations[0].local_device_id == "R1"
    assert observations[0].local_interface == "GigabitEthernet0/0/0/2"
    assert observations[0].remote_device_id_raw == "APJC_JP_OSK_R2.cisco"
    assert observations[0].remote_port_id == "GigabitEthernet0/0/0/2"
    assert observations[0].capabilities == ("router",)
    assert observations[0].source == "lldp"


def test_r2_fixture_parses_five_observations():
    text = _read("r2_show_lldp_neighbors.txt")
    observations = parse_lldp_neighbors(text, "R2")
    assert len(observations) == 5
    remote_ids = {o.remote_device_id_raw for o in observations}
    assert remote_ids == {"APJC_JP_OSK_R1.cisco", "APJC_JP_OSK_R4.cisco", "ASR9001_R1.cisco.com"}


def test_asr9001_observation_preserves_raw_evidence():
    text = _read("r1_show_lldp_neighbors.txt")
    observations = parse_lldp_neighbors(text, "R1")
    asr = next(o for o in observations if o.remote_device_id_raw == "ASR9001_R1.cisco.com")
    assert asr.local_device_id == "R1"
    assert asr.local_interface == "GigabitEthernet0/0/0/10"
    assert asr.remote_port_id == "GigabitEthernet0/0/0/0"
    assert asr.capabilities == ("router",)


# ---- robustness (section 66) ----


_HEADER = "Device ID       Local Intf                      Hold-time  Capability      Port ID"


def test_parser_works_without_timestamp_line():
    text = f"{_HEADER}\nNEIGH.cisco Gi0/0/0/1 120 R Gi0/0/0/1\n\nTotal entries displayed: 1\n"
    observations = parse_lldp_neighbors(text, "R1")
    assert len(observations) == 1
    assert observations[0].remote_device_id_raw == "NEIGH.cisco"


def test_parser_works_with_timestamp_and_legend():
    text = (
        "RP/0/RP0/CPU0:R1#show lldp neighbors\n"
        "Sun Sep 20 19:07:12.895 JST\n"
        "Capability codes:\n"
        "        (R) Router, (B) Bridge, (T) Telephone, (C) DOCSIS Cable Device\n"
        "        (W) WLAN Access Point, (P) Repeater, (S) Station, (O) Other\n\n"
        f"{_HEADER}\n"
        "NEIGH.cisco Gi0/0/0/1 120 R Gi0/0/0/1\n\n"
        "Total entries displayed: 1\n\n"
        "RP/0/RP0/CPU0:R1#"
    )
    observations = parse_lldp_neighbors(text, "R1")
    assert len(observations) == 1


def test_parser_handles_multiple_neighbors():
    text = f"{_HEADER}\n" + "\n".join(f"N{i}.cisco Gi0/0/0/{i} 120 R Gi0/0/0/{i}" for i in range(1, 6))
    observations = parse_lldp_neighbors(text, "R1")
    assert len(observations) == 5


def test_parser_stops_at_trailing_prompt_with_no_blank_line_before_it():
    text = f"{_HEADER}\nNEIGH.cisco Gi0/0/0/1 120 R Gi0/0/0/1\nRP/0/RP0/CPU0:R1#"
    observations = parse_lldp_neighbors(text, "R1")
    assert len(observations) == 1


def test_parser_stops_at_total_entries_displayed():
    text = f"{_HEADER}\nNEIGH.cisco Gi0/0/0/1 120 R Gi0/0/0/1\nTotal entries displayed: 1\nNOTALINE.cisco Gi0/0/0/2 120 R Gi0/0/0/2"
    observations = parse_lldp_neighbors(text, "R1")
    assert len(observations) == 1


def test_parser_handles_fqdn_like_device_ids():
    text = f"{_HEADER}\nASR9001_R1.cisco.com Gi0/0/0/10 120 R Gi0/0/0/0\n"
    observations = parse_lldp_neighbors(text, "R1")
    assert observations[0].remote_device_id_raw == "ASR9001_R1.cisco.com"


def test_parser_handles_zero_neighbors():
    text = f"{_HEADER}\n\nTotal entries displayed: 0\n"
    observations = parse_lldp_neighbors(text, "R1")
    assert observations == []


def test_parser_tolerates_spacing_variation():
    text = f"{_HEADER}\nNEIGH.cisco    Gi0/0/0/1     120   R   Gi0/0/0/1\n"
    observations = parse_lldp_neighbors(text, "R1")
    assert len(observations) == 1
    assert observations[0].local_interface == "Gi0/0/0/1"


def test_parser_returns_empty_list_for_missing_header():
    text = "% Invalid input detected at '^' marker.\n"
    assert parse_lldp_neighbors(text, "R1") == []
