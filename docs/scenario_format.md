# Scenario Format

The scenario format is intentionally minimal and experimental in Step 1. The
detailed scenario schema is not finalized, and Step 1 does not attempt to fix
one.

## What a scenario is

- **Scenario** defines what should be accomplished for the current task.
- **Principles** (`lab/principles.yaml`) define scenario-independent
  operating rules that apply regardless of which scenario is active.
- **References** (`lab/references/`) provide reusable, validated knowledge
  that scenarios can draw on instead of repeating the same operational
  know-how in every scenario file.
- **Topology** defines where the task is performed — which devices and links
  are available to work with.

`lab/scenarios/sample.yaml` is a simple example used to validate that
`get_execution_instructions()` can load a scenario and return it alongside
principles and references. It is not a template to copy literally for every
future use case.

## The Step 2 CLI is selection-only

The Step 2 human CLI (`./run_cli.sh`) can select which scenario and which
references are active (`scenario <name>`, `reference <name>`, `no reference
<name>`), and validates that the selected name exists as an exact,
case-sensitive match against a file under `lab/scenarios/` or
`lab/references/`. It does not edit scenario or reference *content* — there
is no `network-lab(config-scenario)#` or `network-lab(config-reference)#`
submode. Authoring scenario/reference YAML content remains a manual,
file-based activity, consistent with the format not being finalized yet
(see below). `lab/principles.yaml` is not edited by the CLI at all.

## Why the schema is not fixed yet

Future scenarios are expected to represent very different kinds of work —
for example environment builds, network configuration, troubleshooting,
failover or feature validation, migration validation, design creation,
configuration review, or documentation creation. Committing to a rigid
schema in Step 1, before any of those shapes have been used in practice,
would risk over-constraining later steps. A firmer scenario schema may be
designed once real usage patterns are clearer.
