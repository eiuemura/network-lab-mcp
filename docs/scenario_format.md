# Scenario Format

The scenario format is intentionally minimal and experimental. It is not a
fixed schema, and this document describes the current, deliberately loose
conventions rather than a contract the loader enforces field-by-field.

## What a scenario is

- **Scenario** defines what should be accomplished for the current task —
  returned by `get_execution_instructions()` as part of the tool result
  Claude reads at the start of a task.
- **Principles** (`lab/principles.yaml`) define scenario-independent
  operating rules that apply regardless of which scenario is active.
- **References** (`lab/references/`) provide reusable, validated knowledge
  that scenarios can draw on instead of repeating the same operational
  know-how in every scenario file.
- **Topology** defines where the task is performed — which devices and links
  are available to work with, returned separately by `get_active_topology()`.

`lab/scenarios/sample.yaml` is a simple example used to validate that
`get_execution_instructions()` can load a scenario and return it alongside
principles and references. It is not a template to copy literally for every
future use case.

## Validation

Only "valid YAML, root is a mapping" is enforced by
`lab.validate_scenario_data()` / `lab.validate_reference_data()`. No
detailed business schema (required fields, objective structure, etc.) is
checked — a scenario or reference file with any mapping content at all will
load successfully.

## Conventional fields

There is no fixed schema, but the sample scenario and the CLI's `edit`
workflow follow this loose convention:

```yaml
name: sample

description: >
  Inspect the selected lab topology and summarize the current
  state of the available devices.

objectives:
  - obtain the active topology
  - access available devices when appropriate
  - inspect their current state
  - summarize the findings
```

- `name`: a human-readable label (not necessarily the same as the file's own
  definition name used for selection).
- `description`: free text describing the task.
- `objectives`: an ordered list of what should be accomplished.

A reference file follows the same loose shape (`name`, `description`, plus
whatever guidance content makes sense for that reference):

```yaml
name: sample

description: >
  Example reusable reference information used to validate
  Network Lab MCP reference loading.

guidance:
  - This reference is intentionally simple; a real reference file can hold
    whatever structured or free-form operational knowledge is useful.
```

Nothing about these fields is enforced by the loader — a scenario or
reference author is free to add, omit, or restructure fields as long as the
document's root is a YAML mapping.

## Prohibited actions and operating principles

`lab/principles.yaml` (not editable through the CLI) is the place for
scenario-independent operating rules — the things that should always (or
never) happen regardless of which scenario is active, such as a list of
prohibited actions. `get_execution_instructions()` returns its content
alongside the active scenario and active references, combined into one
structured result:

```json
{
  "principles": { "workspace_principles": [...], "general_operating_principles": [...], "prohibited_actions": [...] },
  "scenario": { "name": "sample", "content": { "...": "..." } },
  "references": [ { "name": "sample", "content": { "...": "..." } } ]
}
```

## Authoring and selecting scenarios/references

The human CLI (`./run_cli.sh`) separates *authoring* a scenario/reference
from *selecting* which one MCP currently uses — see
[cli_reference.md](cli_reference.md) for the full command reference:

- **Authoring**: global mode's `scenario <name>` / `reference <name>`
  create or edit a scenario/reference *definition* — an existing name loads
  it as a candidate, a new name starts a fresh minimal one. This enters
  `network-lab(config-scenario-<name>)#` / `network-lab(config-reference-<name>)#`,
  where `edit` opens the candidate in an external YAML editor
  (`$VISUAL`/`$EDITOR`/`vim`); there is no structured field-by-field editor,
  consistent with the format not being fixed.
- **Selection**: `running-config` mode's `scenario <name>` / `reference
  <name>` / `no reference <name>` change which scenario/references MCP
  uses, validating that the name exists as an exact, case-sensitive match
  against a file under `lab/scenarios/`/`lab/references/`, or is the
  definition currently being authored in the same configure session.

## Why the schema is not fixed

Scenarios are expected to represent very different kinds of work — for
example environment builds, network configuration, troubleshooting,
failover or feature validation, migration validation, design creation,
configuration review, or documentation creation. Committing to a rigid
schema before enough of those shapes have been used in practice would risk
over-constraining scenario authors. A firmer scenario schema may be designed
once real usage patterns are clearer.
