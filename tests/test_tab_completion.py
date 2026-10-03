"""Tab/`?` completion regression tests for the `tab` key binding's
multiple-candidate branch (`_make_key_bindings()` in cli/main.py).

Root cause investigated: `reference pa?` / `scenario m?` do NOT actually
duplicate-render on this HEAD (verified independently via a headless
prompt_toolkit session and a real PTY run through pyte) -- the real,
reproduced bug is that Tab with more than one candidate never extended the
buffer to the longest common prefix at all (e.g. "reference pa<TAB>" left
the buffer completely unchanged at "reference pa" instead of advancing to
"reference pagent_"), even though the ambiguous candidate list was always
printed correctly. These tests cover the two pure functions factored out
of the `tab` key binding to fix that (`_longest_common_prefix()`,
`_tab_multi_candidate_extension()`), plus the full grammar.complete() ->
extension-decision pipeline for the two commands named in the report, and
a handful of other dynamic-argument/literal commands to confirm the fix is
generic rather than reference/scenario-specific.

Deliberately does not touch the actual prompt_toolkit Buffer/KeyBindings
machinery (which the project doesn't otherwise unit test -- see
test_monitor_terminal.py's module docstring for the one place that does,
via a real Application) -- the key binding itself is a thin, two-line
wrapper around the decision functions tested here."""

from __future__ import annotations

from network_lab_mcp.cli import grammar
from network_lab_mcp.cli import main as climain


def make_ctx(**kwargs) -> grammar.CliContext:
    return grammar.CliContext(**kwargs)


# ==========================================================================
# `_longest_common_prefix` -- pure
# ==========================================================================


def test_lcp_partial_divergence():
    assert climain._longest_common_prefix(["pagent_pkts", "pagent_tgn"]) == "pagent_"


def test_lcp_one_candidate_is_prefix_of_another():
    assert (
        climain._longest_common_prefix(
            ["multi_flow_path_validation", "multi_flow_path_validation_failure_test"]
        )
        == "multi_flow_path_validation"
    )


def test_lcp_no_common_prefix():
    assert climain._longest_common_prefix(["R1", "SW1"]) == ""


def test_lcp_single_candidate_is_itself():
    assert climain._longest_common_prefix(["pagent_pkts"]) == "pagent_pkts"


def test_lcp_empty_list():
    assert climain._longest_common_prefix([]) == ""


def test_lcp_three_candidates_narrows_to_shared_prefix():
    assert climain._longest_common_prefix(["R1", "R2", "R9_DISCOVERY_ONLY"]) == "R"


# ==========================================================================
# `_tab_multi_candidate_extension` -- pure, combines LCP with replace_prefix
# ==========================================================================


def test_extension_advances_past_typed_prefix():
    result = grammar.CompletionResult(candidates=["pagent_pkts", "pagent_tgn"], replace_prefix="pa")
    assert climain._tab_multi_candidate_extension(result) == "pagent_"


def test_extension_none_when_already_at_common_prefix():
    # Already typed exactly the full common prefix -- nothing further to
    # extend to (must not be reported as an extension, or Tab would loop
    # inserting the same text it already has).
    result = grammar.CompletionResult(candidates=["pagent_pkts", "pagent_tgn"], replace_prefix="pagent_")
    assert climain._tab_multi_candidate_extension(result) is None


def test_extension_to_a_candidate_that_is_itself_a_prefix_of_another():
    result = grammar.CompletionResult(
        candidates=["multi_flow_path_validation", "multi_flow_path_validation_failure_test"],
        replace_prefix="m",
    )
    extension = climain._tab_multi_candidate_extension(result)
    assert extension == "multi_flow_path_validation"
    # Never a trailing space -- an ambiguous match (one candidate is still
    # a strict prefix of another) is never marked syntactically complete.
    assert not extension.endswith(" ")


def test_extension_none_when_candidates_diverge_immediately():
    result = grammar.CompletionResult(candidates=["R1", "SW1"], replace_prefix="")
    assert climain._tab_multi_candidate_extension(result) is None


# ==========================================================================
# Full pipeline: grammar.complete() -> extension decision, for the exact
# commands named in the bug report plus other dynamic-argument/literal
# commands, to confirm the fix is generic (never reference/scenario-only).
# ==========================================================================


def test_reference_pa_tab_extends_to_pagent_underscore():
    ctx = make_ctx(reference_names=("pagent_pkts", "pagent_tgn", "sample"))
    result = grammar.complete("global", "reference pa", ctx)
    assert set(result.candidates) == {"pagent_pkts", "pagent_tgn"}
    assert climain._tab_multi_candidate_extension(result) == "pagent_"


def test_reference_pagent_p_tab_is_unique_full_completion():
    ctx = make_ctx(reference_names=("pagent_pkts", "pagent_tgn", "sample"))
    result = grammar.complete("global", "reference pagent_p", ctx)
    assert result.candidates == ["pagent_pkts"]
    # Unique-candidate completion is the *other* branch of the tab key
    # binding (len(candidates) == 1), not this extension helper -- assert
    # the contract it relies on instead: the one candidate, complete with
    # no trailing space of its own.
    assert result.candidates[0] == "pagent_pkts"


def test_scenario_m_tab_extends_without_premature_trailing_space():
    ctx = make_ctx(scenario_names=("multi_flow_path_validation", "multi_flow_path_validation_failure_test"))
    result = grammar.complete("global", "scenario m", ctx)
    assert set(result.candidates) == {
        "multi_flow_path_validation",
        "multi_flow_path_validation_failure_test",
    }
    extension = climain._tab_multi_candidate_extension(result)
    assert extension == "multi_flow_path_validation"
    assert not extension.endswith(" ")


def test_topology_tab_extension_is_generic_not_reference_scenario_only():
    ctx = make_ctx(topology_names=("lab_abc", "lab_abd"))
    result = grammar.complete("global", "topology lab_a", ctx)
    assert set(result.candidates) == {"lab_abc", "lab_abd"}
    assert climain._tab_multi_candidate_extension(result) == "lab_ab"


def test_access_info_tab_extension_is_generic():
    ctx = make_ctx(access_info_names=("site_1", "site_2"))
    result = grammar.complete("global", "access-info site_", ctx)
    assert set(result.candidates) == {"site_1", "site_2"}
    assert climain._tab_multi_candidate_extension(result) is None  # already at common prefix


def test_terminal_monitor_tab_extension_is_generic():
    ctx = make_ctx(monitor_terminal_device_ids=("R1", "R2", "R9_DISCOVERY_ONLY"))
    result = grammar.complete("exec", "terminal monitor R", ctx)
    assert set(result.candidates) == {"R1", "R2", "R9_DISCOVERY_ONLY"}
    assert climain._tab_multi_candidate_extension(result) is None  # "R" already fully typed


def test_literal_single_candidate_unaffected_by_extension_helper():
    # A single literal-keyword match (e.g. "d" -> "delete" at EXEC root)
    # goes through the len(candidates) == 1 branch of the tab key binding,
    # never this helper -- confirm grammar.complete() itself still returns
    # exactly one candidate, unaffected by this change.
    ctx = make_ctx()
    result = grammar.complete("exec", "d", ctx)
    assert result.candidates == ["delete"]


# ==========================================================================
# Inline `?` regression: candidates render exactly once, descriptions kept.
# No duplicate-rendering bug was reproduced on this HEAD (see module
# docstring), but these pin the exact expected (non-duplicated) shape so
# a future regression here would be caught.
# ==========================================================================


def test_reference_prefix_help_has_no_duplicate_rendering():
    ctx = make_ctx(reference_names=("pagent_pkts", "pagent_tgn", "sample"))
    result = grammar.help("global", "reference pa", ctx)
    tokens = [line.token for line in result.lines]
    assert tokens == sorted(tokens)
    assert tokens.count("pagent_pkts") == 1
    assert tokens.count("pagent_tgn") == 1
    assert all(line.description == "Existing reference definition" for line in result.lines)


def test_scenario_prefix_help_has_no_duplicate_rendering():
    ctx = make_ctx(scenario_names=("multi_flow_path_validation", "multi_flow_path_validation_failure_test"))
    result = grammar.help("global", "scenario m", ctx)
    tokens = [line.token for line in result.lines]
    assert tokens == sorted(tokens)
    assert tokens.count("multi_flow_path_validation") == 1
    assert tokens.count("multi_flow_path_validation_failure_test") == 1
    assert all(line.description == "Existing scenario definition" for line in result.lines)


# ==========================================================================
# Candidate ordering is unchanged by this fix -- grammar.complete() already
# sorts dynamic-argument candidates; this fix never re-sorts or reorders.
# ==========================================================================


def test_candidate_ordering_unchanged_for_reference():
    ctx = make_ctx(reference_names=("pagent_tgn", "pagent_pkts", "sample"))
    result = grammar.complete("global", "reference pa", ctx)
    assert result.candidates == sorted(result.candidates) == ["pagent_pkts", "pagent_tgn"]
