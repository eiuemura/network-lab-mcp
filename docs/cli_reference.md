# CLI Reference

## Step 1 status

There is no human-facing CLI in Step 1. `run_cli.sh` exists only as a
placeholder that prints a short message and exits; it is not registered with
Claude Code and does not speak the MCP stdio protocol.

In Step 1, switching the active topology, scenario, or references is done by
directly editing `lab/settings.yaml` — see the "Lab directory concepts and
setup" section of [README.md](../README.md).

## Planned for Step 2

Step 2 is expected to implement an IOS XR-style human-facing CLI, launched
via `run_cli.sh`, covering (at a high level, not finalized):

- topology configuration and device registration
- active topology selection
- active scenario selection
- active reference selection
- candidate configuration
- commit / abort

None of this is implemented yet. Do not rely on any specific command syntax
until Step 2 ships.
