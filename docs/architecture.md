# Architecture

Network Lab MCP provides a lightweight "AI Network Engineer Layer" designed to
maximize the reasoning capabilities of AI in network lab environments.

Network Lab MCP is **not** a network engineering reasoning engine. It is a
thin layer that gives Claude Code (or any other MCP client) two things:

1. **Lab knowledge** — where the work happens, how to behave, what to
   accomplish, and what reusable knowledge already exists.
2. **Terminal access** — a real, general-purpose way to interact with lab
   devices, close to how a human operator would use a terminal.

All actual network engineering judgment — what command to run next, how to
interpret output, when a task is done — is left to Claude Code.

## Responsibility model

```
Topology
    -> WHERE / WHAT EXISTS
    -> Where the work is performed and what devices and links exist

Principles
    -> HOW TO BEHAVE
    -> Common operating rules that apply to every scenario

Scenario
    -> WHAT TO DO
    -> What must be accomplished for the current task

References
    -> REUSABLE KNOWLEDGE
    -> Reusable validated guidance, operational knowledge,
       known values, and lab-specific know-how

Terminal
    -> ACTION INTERFACE
    -> How Claude interacts with lab devices

Claude Code
    -> REASONING
    -> Determines how to accomplish the task

Workspace
    -> TASK ARTIFACT AREA
    -> Where Claude stores evidence, analysis, configurations,
       validation results, designs, and reports
```

`get_active_topology()` answers "where should I work?" `get_execution_instructions()`
answers "what rules must I follow, what must I accomplish, and what reusable
knowledge is available?" combining principles, the active scenario, and active
references into one structured result. Neither tool tells Claude Code *how*
to solve the problem — that is Claude Code's own job.

The Workspace is deliberately **not** managed by the MCP server. Claude Code
uses its own current working directory (the task workspace it was started
from) to store evidence, configurations, and reports, organized however the
task requires. Network Lab MCP has no `runtime/` directory and keeps no
records of what Claude Code produces.

## Request path

```
Claude Code
    |
Network Lab MCP
    |  get_active_topology()
    |  get_execution_instructions()
    |  terminal_open() / terminal_send() / terminal_read()
    |  terminal_list() / terminal_close()
    v
dedicated tmux environment (socket: network-lab-mcp)
    v
ssh / telnet
    v
Lab Devices
```

The MCP server itself is a thin translation layer: lab tools read YAML from
disk and return it as structured data; terminal tools translate a logical
device name into tmux commands. It holds essentially no state of its own —
tmux is the single source of truth for terminal session lifetime, and lab
YAML on disk is the single source of truth for topology/principles/scenario/
reference content.

## Installation model and lab root ownership

Step 1 supports exactly one installation model: a local repository checkout
installed with `pip install -e .`. The repository checkout **owns** the
`lab/` directory. `network_lab_mcp.lab.find_lab_root()` resolves this
directory relative to the installed package's own source location (its
`__file__`), never relative to the current working directory of the process
that launched the MCP server — Claude Code is normally started from an
unrelated task workspace, so depending on its working directory would be
incorrect.

Non-editable or wheel installation (`pip install .`, a built wheel, or a
package-index install) is **not a supported configuration in Step 1**: such
an install has no `lab/` directory to find, since lab data is repository-local
operational data rather than a packaged resource. Supporting that would
require packaging work (e.g. `importlib.resources`, an external writable
config directory, or an environment-variable-based lab-root override) that is
explicitly deferred to a later step.

## Lab YAML reload policy

None of `get_active_topology()`, `get_execution_instructions()`, or
`terminal_open()` cache lab YAML at server startup. Each reads
`lab/settings.yaml` and whatever it references fresh from disk on every call.
This means editing `lab/settings.yaml` to switch the active topology,
scenario, or references takes effect on the very next tool call, without
restarting the MCP server.

## Terminal architecture

Terminal sessions are managed with tmux, using a dedicated named socket
(`tmux -L network-lab-mcp ...`) so that Network Lab MCP never interferes with
a user's own interactive tmux sessions. tmux itself — not any structure
inside the MCP server — is the source of truth for which sessions exist and
whether they are still running. The MCP server does not keep a session
database; a restarted server rediscovers existing sessions by asking tmux.

SSH and Telnet are launched as the operating system's own `ssh`/`telnet`
binaries, inside a managed tmux pane. Network Lab MCP does not use a Python
SSH library and does not use `pexpect` as its core mechanism, and it does not
attempt to parse device prompts (IOS XR or otherwise). Instead, Claude Code
reads the pane with `terminal_read()`, decides what a prompt means, and
responds with `terminal_send()` — the same loop a human operator would use.

### Structurally separate session namespaces

Two structurally distinct namespaces exist, chosen by the type of caller
(production tool vs. internal validation), never by pattern-matching on a
device's name:

```
Production:  network-lab-device-<device-id>
Validation:  network-lab-validation-<validation-id>
```

`derive_production_session_name()` and `derive_validation_session_name()` are
the only two functions that produce these names, and each only ever produces
a name in its own namespace. Because the two prefixes are fixed and distinct
strings, a session name can belong to at most one namespace — there is no
ambiguity to resolve at runtime. Concretely, a topology device literally
named `validation-router` maps to the production session
`network-lab-device-validation-router`; it is not, and cannot become, a
validation-namespace session such as `network-lab-validation-terminal-io`.

The public tools (`terminal_open`, `terminal_send`, `terminal_read`,
`terminal_list`, `terminal_close`) only ever derive and act on production
session names. A small set of internal validation helpers
(`open_validation_session`, `send_to_validation`, `read_validation`,
`list_validation_sessions`, `close_validation_session` in `terminal.py`) only
ever derive and act on validation session names. Both sets of callers share
the same underlying session-management primitives (session creation/reuse,
send text/keys, capture pane, list, close) — local validation exercises the
real production code path, just against a safe local command (e.g. `cat`)
instead of `ssh`/`telnet`.

## Not implemented in Step 1

To keep the MCP layer thin and the scope tight, Step 1 deliberately excludes:

- Configuration mode, candidate configuration, and commit/abort.
- Topology discovery (CDP/LLDP), and topology write/delete.
- A human-facing CLI (placeholder only — see `run_cli.sh` and
  `src/network_lab_mcp/cli/`).
- An HTTP MCP server, containerization, or an MCP-owned runtime/session
  database.
- A public local-shell transport (local processes are used only inside the
  internal validation helpers described above).

These are candidates for later steps, not for this repository's current
scope.
