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

## The Step 2 / 2.5 CLI: definitions vs. selection

The Step 2 / 2.5 human CLI (`./run_cli.sh`) separates *authoring* a
scenario/reference from *selecting* which one MCP currently uses:

- **Authoring**: global mode's `scenario <name>` / `reference <name>`
  create or edit a scenario/reference *definition* — an existing name loads
  it as a candidate, a new name starts a fresh minimal one. This enters
  `network-lab(config-scenario-<name>)#` / `network-lab(config-reference-<name>)#`,
  where `edit` opens the candidate in an external YAML editor
  ($VISUAL/$EDITOR/vim); there is no structured field-by-field editor,
  consistent with the format not being finalized yet (see below).
- **Selection**: `running-config` mode's `scenario <name>` / `reference
  <name>` / `no reference <name>` change which scenario/references MCP
  uses, validating that the name exists as an exact, case-sensitive match
  against a file under `lab/scenarios/`/`lab/references/` *or* is the
  definition currently being authored in the same configure session.

`lab/principles.yaml` is not edited by the CLI at all, and only "valid
YAML, root is a mapping" is enforced for scenario/reference content — no
detailed business schema is checked, consistent with the format not being
finalized yet.

## Why the schema is not fixed yet

Future scenarios are expected to represent very different kinds of work —
for example environment builds, network configuration, troubleshooting,
failover or feature validation, migration validation, design creation,
configuration review, or documentation creation. Committing to a rigid
schema in Step 1, before any of those shapes have been used in practice,
would risk over-constraining later steps. A firmer scenario schema may be
designed once real usage patterns are clearer.
