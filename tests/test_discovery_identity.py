"""Identity resolution: mapping an LLDP remote Device ID to a logical
managed device ID (resolve_remote_identity()). Deliberately bounded --
exact/case-normalized/short-name-alias matching only, fails closed
(returns None) on anything ambiguous or unknown. No substring search, no
fuzzy matching, no inference from the logical device ID itself."""

from __future__ import annotations

from network_lab_mcp.discovery import resolve_remote_identity

IDENTITY_MAP = {
    "R1": "APJC_JP_OSK_R1",
    "R2": "APJC_JP_OSK_R2",
    "R3": "APJC_JP_OSK_R3",
    "R4": "APJC_JP_OSK_R4",
}


def test_exact_hostname_match():
    assert resolve_remote_identity("APJC_JP_OSK_R1", IDENTITY_MAP) == "R1"


def test_fqdn_style_alias_resolves_uniquely():
    assert resolve_remote_identity("APJC_JP_OSK_R2.cisco", IDENTITY_MAP) == "R2"


def test_case_insensitive_exact_match():
    assert resolve_remote_identity("apjc_jp_osk_r3", IDENTITY_MAP) == "R3"


def test_case_insensitive_alias_match():
    assert resolve_remote_identity("apjc_jp_osk_r4.CISCO", IDENTITY_MAP) == "R4"


def test_unknown_hostname_is_unresolved():
    assert resolve_remote_identity("ASR9001_R1.cisco.com", IDENTITY_MAP) is None


def test_ambiguous_hostname_is_unresolved():
    ambiguous_map = {"R1": "SHARED", "R2": "SHARED"}
    assert resolve_remote_identity("SHARED", ambiguous_map) is None


def test_ambiguous_alias_is_unresolved():
    ambiguous_map = {"R1": "DUP", "R2": "DUP"}
    assert resolve_remote_identity("DUP.cisco", ambiguous_map) is None


def test_does_not_use_substring_matching():
    # "R2" appearing inside a logical ID string must never cause a match --
    # only comparisons against the *hostname* value are allowed.
    tricky_map = {"R2": "totally-unrelated-hostname"}
    assert resolve_remote_identity("something-R2-something", tricky_map) is None


def test_empty_identity_map_is_unresolved():
    assert resolve_remote_identity("APJC_JP_OSK_R1", {}) is None
