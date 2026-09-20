"""Tests for the command grammar single source of truth: parsing, unique
abbreviation, ambiguity/incomplete/invalid-input detection, fixed-keyword
case-insensitivity, object-identifier case-sensitivity, and context-sensitive
completion/help (including exact-case dynamic candidates)."""

from __future__ import annotations

from network_lab_mcp.cli import grammar


def make_ctx(**kwargs) -> grammar.CliContext:
    return grammar.CliContext(**kwargs)


# ---- parsing / abbreviation ----


def test_unique_abbreviation_resolves():
    result = grammar.parse("exec", "conf")
    assert result.ok
    assert result.action == "exec.configure"


def test_abbreviation_is_context_sensitive():
    result = grammar.parse("global", "top srv6_lab")
    assert result.ok
    assert result.action == "global.topology"
    assert result.args == {"name": "srv6_lab"}

    result = grammar.parse("device", "tra ssh")
    assert result.ok
    assert result.action == "device.set_transport"
    assert result.args == {"value": "ssh"}


def test_ambiguous_abbreviation_rejected():
    # global root has both "scenario" and "show" -> "s" is ambiguous.
    result = grammar.parse("global", "s")
    assert not result.ok
    assert result.error.kind == "ambiguous"
    assert result.error.token == "s"


def test_ambiguous_end_exit():
    # global root has both "end" and "exit" -> "e" is ambiguous.
    result = grammar.parse("global", "e")
    assert not result.ok
    assert result.error.kind == "ambiguous"


def test_incomplete_command():
    result = grammar.parse("device", "transport")
    assert not result.ok
    assert result.error.kind == "incomplete"
    assert result.error.span is None


def test_unknown_command_at_first_token():
    result = grammar.parse("exec", "foo")
    assert not result.ok
    assert result.error.kind == "unknown"
    assert result.error.token == "foo"


def test_invalid_transport_value_has_caret_span():
    result = grammar.parse("device", "transport invalid")
    assert not result.ok
    assert result.error.kind == "invalid"
    assert result.error.token == "invalid"
    assert result.error.span == (10, 17)
    assert "ssh" in result.error.detail


def test_device_type_accepts_exact_and_case_insensitive_values():
    for line, expected in (
        ("type iosxr", "iosxr"),
        ("type IOSXE", "iosxe"),
        ("type NxOs", "nxos"),
        ("type HOST", "host"),
        ("type Host", "host"),
    ):
        result = grammar.parse("device", line)
        assert result.ok, line
        assert result.args == {"value": expected}


def test_device_type_accepts_unambiguous_abbreviation():
    result = grammar.parse("device", "type nx")
    assert result.ok
    assert result.args == {"value": "nxos"}

    result_host = grammar.parse("device", "type h")
    assert result_host.ok
    assert result_host.args == {"value": "host"}


def test_device_type_rejects_ambiguous_abbreviation():
    result = grammar.parse("device", "type ios")
    assert not result.ok
    assert result.error.kind == "invalid"
    assert "Ambiguous" in result.error.detail
    assert "iosxr" in result.error.detail and "iosxe" in result.error.detail


def test_device_type_rejects_unknown_value():
    result = grammar.parse("device", "type junos")
    assert not result.ok
    assert result.error.kind == "invalid"
    assert "Invalid device type" in result.error.detail


def test_invalid_port_value():
    result = grammar.parse("device", "port 99999")
    assert not result.ok
    assert result.error.kind == "invalid"
    assert "65535" in result.error.detail

    ok_result = grammar.parse("device", "port 22")
    assert ok_result.ok
    assert ok_result.args == {"value": "22"}


def test_configure_terminal_alias():
    result = grammar.parse("exec", "configure terminal")
    assert result.ok
    assert result.action == "exec.configure"


def test_no_reference_command():
    result = grammar.parse("global", "no reference srv6")
    assert result.ok
    assert result.action == "global.reference_remove"
    assert result.args == {"name": "srv6"}


def test_description_is_rest_of_line():
    result = grammar.parse("topology", "description SRv6 lab with two PEs")
    assert result.ok
    assert result.args == {"text": "SRv6 lab with two PEs"}


# ---- fixed-keyword case-insensitivity ----


def test_fixed_keywords_case_insensitive():
    for line in ("configure", "CONFIGURE", "Configure", "CoNfIgUrE"):
        result = grammar.parse("exec", line)
        assert result.ok
        assert result.action == "exec.configure"


def test_nested_fixed_keyword_case_insensitive():
    result = grammar.parse("device", "TRANSPORT SSH")
    assert result.ok
    assert result.args == {"value": "ssh"}


# ---- object-identifier case sensitivity (grammar does not fold case) ----


def test_object_identifier_case_preserved_by_parser():
    result = grammar.parse("global", "topology SRv6_Lab")
    assert result.ok
    # The grammar itself never case-folds or validates existence; it hands
    # the exact entered case to the caller (cli/config.py) unchanged.
    assert result.args == {"name": "SRv6_Lab"}


# ---- completion ----


def test_unique_literal_completion():
    ctx = make_ctx()
    result = grammar.complete("exec", "conf", ctx)
    assert result.candidates == ["configure"]


def test_ambiguous_literal_completion_lists_candidates():
    ctx = make_ctx()
    result = grammar.complete("global", "s", ctx)
    assert set(result.candidates) == {"scenario", "show"}


def test_zero_candidate_completion():
    ctx = make_ctx()
    result = grammar.complete("exec", "zzz", ctx)
    assert result.candidates == []


def test_dynamic_completion_is_case_sensitive_and_preserves_case():
    ctx = make_ctx(topology_candidate_device_names=("R1", "R2", "PE1"))
    result = grammar.complete("topology", "device R", ctx)
    assert set(result.candidates) == {"R1", "R2"}

    result_lower = grammar.complete("topology", "device r", ctx)
    assert result_lower.candidates == []  # must not case-fold to match R1/R2


def test_topology_name_completion_case_sensitive():
    ctx = make_ctx(topology_names=("srv6_lab",))
    assert grammar.complete("global", "topology s", ctx).candidates == ["srv6_lab"]
    assert grammar.complete("global", "topology S", ctx).candidates == []


def test_password_never_completes():
    ctx = make_ctx()
    assert grammar.complete("device", "password ", ctx).candidates == []
    assert grammar.complete("device", "password some-text", ctx).candidates == []


def test_no_reference_completion_uses_candidate_references():
    ctx = make_ctx(candidate_reference_names=("srv6", "iosxr_basics"))
    result = grammar.complete("global", "no reference ", ctx)
    assert set(result.candidates) == {"srv6", "iosxr_basics"}


def test_device_type_tab_completion_exposes_only_fixed_enum():
    ctx = make_ctx()
    result = grammar.complete("device", "type ", ctx)
    assert set(result.candidates) == {"iosxr", "iosxe", "nxos", "host"}

    result_prefix = grammar.complete("device", "type n", ctx)
    assert result_prefix.candidates == ["nxos"]

    result_host = grammar.complete("device", "type h", ctx)
    assert result_host.candidates == ["host"]


# ---- context-sensitive help ----


def test_bare_help_lists_all_root_commands():
    ctx = make_ctx()
    result = grammar.help("exec", "", ctx)
    tokens = [line.token for line in result.lines]
    assert tokens == ["configure", "show", "help", "exit", "quit"]
    assert result.show_cr is False


def test_partial_token_help():
    ctx = make_ctx()
    result = grammar.help("exec", "con", ctx)
    assert [line.token for line in result.lines] == ["configure"]
    assert result.show_cr is False


def test_next_token_help_for_enum_argument():
    ctx = make_ctx()
    result = grammar.help("device", "transport ", ctx)
    values = {line.token: line.description for line in result.lines}
    assert values == {"ssh": "Use SSH transport", "telnet": "Use Telnet transport"}


def test_device_type_help_lists_fixed_enum_not_a_placeholder():
    ctx = make_ctx()
    result = grammar.help("device", "type ", ctx)
    values = {line.token: line.description for line in result.lines}
    assert values == {
        "iosxr": "Cisco IOS XR",
        "iosxe": "Cisco IOS XE",
        "nxos": "Cisco NX-OS",
        "host": "Generic host / endpoint",
    }
    assert result.show_cr is False
    # Never falls back to a generic "<value>" placeholder for this enum.
    assert "<value>" not in [line.token for line in result.lines]


def test_device_type_cr_marker_when_value_already_supplied():
    ctx = make_ctx()
    result = grammar.help("device", "type iosxr ", ctx)
    assert result.lines == []
    assert result.show_cr is True


def test_next_token_help_for_identifier_argument_shows_generic_hint():
    ctx = make_ctx(topology_names=("srv6_lab",))
    result = grammar.help("global", "topology ", ctx)
    assert [line.token for line in result.lines] == ["<name>"]


def test_partial_identifier_help_shows_case_sensitive_candidates():
    ctx = make_ctx(topology_names=("srv6_lab",))
    result = grammar.help("global", "topology s", ctx)
    assert [line.token for line in result.lines] == ["srv6_lab"]

    result_upper = grammar.help("global", "topology S", ctx)
    assert result_upper.lines == []


def test_password_help_shows_hint_never_value():
    ctx = make_ctx()
    result = grammar.help("device", "password ", ctx)
    assert [line.token for line in result.lines] == ["<password>"]
    result_partial = grammar.help("device", "password secret", ctx)
    assert [line.token for line in result_partial.lines] == ["<password>"]


def test_cr_marker_when_command_complete():
    ctx = make_ctx()
    result = grammar.help("global", "commit ", ctx)
    assert result.lines == []
    assert result.show_cr is True
