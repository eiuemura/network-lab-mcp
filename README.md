# Network Lab MCP

Network Lab MCP provides a lightweight "AI Network Engineer Layer" designed to
maximize the reasoning capabilities of AI in network lab environments.

It is not a fixed test-automation tool. It is a thin foundation that lets
Claude Code (or any other MCP client) understand a lab's topology, follow
common operating principles, understand the current task, draw on reusable
reference knowledge, and reach lab devices through a real terminal — while
Claude Code itself does all of the network engineering reasoning.

This repository now includes both **Step 1** (the MCP server and terminal
foundation) and **Step 2** (an IOS XR-compatible human-facing CLI, launched
via `./run_cli.sh`, for editing lab configuration through a candidate/commit
model). Topology discovery (CDP/LLDP) and a `show topology`/`discover
topology`/`write topology`/`delete topology` command family are still not
implemented — that is Step 3. See [Current limitations](#current-limitations)
and [Future steps](#future-steps).

## Responsibility model

| Concept        | Role                    | Meaning |
|----------------|-------------------------|---------|
| **Topology**   | WHERE / WHAT EXISTS     | Where the work is performed and what devices and links exist |
| **Principles** | HOW TO BEHAVE           | Common operating rules that apply to every scenario |
| **Scenario**   | WHAT TO DO              | What must be accomplished for the current task |
| **References** | REUSABLE KNOWLEDGE      | Reusable validated guidance, operational knowledge, known values, and lab-specific know-how |
| **Terminal**   | ACTION INTERFACE        | How Claude interacts with lab devices |
| **Claude Code**| REASONING               | Determines how to accomplish the task |
| **Workspace**  | TASK ARTIFACT AREA      | Where Claude stores evidence, analysis, configurations, validation results, designs, and reports |

Network Lab MCP deliberately keeps network-engineering judgment out of the MCP
server itself. It exposes lab knowledge and terminal access; Claude Code
decides what to do with them. The Step 2 human CLI adds a fourth role,
**Human Configuration / Control Interface**: it edits lab configuration
through a candidate/commit model, but it is not itself a network-engineering
reasoning engine either. See [docs/architecture.md](docs/architecture.md)
for more detail.

## Step 1 capabilities

- A stdio MCP server (`network-lab-mcp`) exposing exactly seven tools:
  `get_active_topology`, `get_execution_instructions`, `terminal_open`,
  `terminal_send`, `terminal_read`, `terminal_list`, `terminal_close`.
- Lab data (topology, principles, scenario, references) loaded from YAML and
  re-read from disk on every relevant tool call — no server restart needed
  after editing `lab/settings.yaml`.
- A dedicated tmux environment (separate socket) used as the source of truth
  for terminal session lifetime, structurally separating production device
  sessions from local validation-only sessions.
- SSH and Telnet access to lab devices via the operating system's own `ssh`
  and `telnet` binaries, driven the way a human would: read the terminal,
  decide what to send, send it.

## Step 2 capabilities: the Human Configuration CLI

`./run_cli.sh` launches an interactive, IOS XR-compatible CLI for editing
`lab/settings.yaml` and `lab/topologies/*.yaml` through a **candidate ->
commit** model — the same responsibility split IOS XR uses for its own
configuration mode, applied here to Network Lab MCP's own lab data instead
of a network device. It is a human configuration/control plane, not a
network-engineering reasoning engine, and it never talks the MCP stdio
protocol.

- **Modes**: EXEC (`network-lab#`), global configuration
  (`network-lab(config)#`), topology configuration
  (`network-lab(config-topology-<name>)#`), and device configuration
  (`network-lab(config-device-<name>)#`).
- **Candidate configuration**: entering `configure` snapshots committed
  settings into a settings candidate; selecting a topology loads (or creates)
  a topology candidate. Nothing is written to disk until `commit`.
- **Scoped dirty state**: a settings-only change (scenario/reference
  selection) does not block switching to a different topology; an in-progress
  topology edit does. `exit`/`end`/Ctrl-D refuse to silently discard
  uncommitted changes.
- **IOS XR-style interaction**, all driven by one command grammar (the single
  source of truth in `cli/grammar.py`, see
  [docs/cli_reference.md](docs/cli_reference.md) for the full reference):
  unique fixed-keyword abbreviation (`conf`, `top <name>`, `dev <name>`,
  `tra ssh`), Tab/Ctrl-I completion, context-sensitive `?` (bare, partial-token,
  and next-token forms, including a `<cr>` marker), ambiguous/incomplete/
  invalid-input detection with an IOS XR-style caret, in-process command
  history (never written to disk, and a password-setting command is never
  retained in it even in memory), IOS XR-style line editing, and safe
  Ctrl-C (cancels only the current input line) / Ctrl-D (EOF; blocked while
  uncommitted changes exist) behavior.
- **Fixed CLI keywords are case-insensitive** (`configure`/`CONFIGURE`/
  `Configure` are equivalent); **object identifiers — topology, scenario,
  reference, and device names — are case-sensitive** and are never silently
  case-folded, including in dynamic completion.
- **Case-only topology-name collision safeguard**: if `topology <name>` does
  not exactly match an existing topology but differs from one only by
  letter case (e.g. entering `SRv6_Lab` when `srv6_lab` already exists), the
  CLI asks for explicit confirmation before creating a distinct topology,
  rather than silently opening the existing one or silently creating a
  look-alike. This is a narrow safety check, not fuzzy name matching.
- **Step 1 validator reuse**: topology/device validation on commit reuses
  `network_lab_mcp.lab.validate_topology_data()` (built on Step 1's own
  `validate_topology_device_names()`) — the CLI does not maintain a
  duplicate set of validation rules.
- **Minimal, targeted writes**: commit only writes the YAML files that
  actually changed semantically; an unchanged topology that was merely
  selected is never rewritten, and a no-op commit writes nothing at all.
- **Password safety**: passwords are stored in lab YAML in plain text (as in
  Step 1 — this is a lab tool, not a secret manager) but are never shown by
  `show configuration`/`show running-config`, never offered as a completion
  candidate, and never retained in this process's in-memory history.
- **Committed-state boundary**: the MCP server only ever reads committed
  `lab/settings.yaml` and `lab/topologies/*.yaml`; candidate configuration is
  memory-only and invisible to Claude Code until `commit` succeeds, at which
  point it becomes visible on the very next MCP tool call — no MCP server
  restart is needed.

## Architecture overview

```
Claude Code                          Human Operator
    |                                     |
Network Lab MCP (this repository)   ./run_cli.sh (this repository)
    |                                     |
dedicated tmux environment           Candidate configuration -> commit
(socket: network-lab-mcp)                 |
    |                                Committed lab YAML
ssh / telnet                              |
    |                              (read by Network Lab MCP above)
Lab Devices
```

See [docs/architecture.md](docs/architecture.md) for the full picture,
including how Topology, Principles, Scenario, References, Terminal, Claude
Code, and Workspace relate to each other, how the production and validation
terminal session namespaces are kept structurally separate, and how the
Step 2 CLI's candidate/commit model relates to the committed-state boundary
the MCP server reads from.

## Directory structure

```
network-lab-mcp/
├── pyproject.toml
├── README.md
├── .gitignore
├── run_cli.sh                 # Step 2 human CLI launcher
│
├── src/
│   └── network_lab_mcp/
│       ├── __init__.py
│       ├── mcp_server.py      # stdio MCP server, defines the 7 tools
│       ├── lab.py             # settings/topology/principles/scenario/reference loading
│       ├── terminal.py        # tmux session management, ssh/telnet launch
│       │
│       └── cli/                       # Step 2 human-facing CLI
│           ├── __init__.py
│           ├── main.py                # REPL, prompt rendering, key bindings, dispatch
│           ├── config.py              # candidate configuration, dirty state, commit/abort
│           └── grammar.py             # command grammar single source of truth
│
├── lab/
│   ├── settings.example.yaml  # tracked template
│   ├── settings.yaml          # local only, gitignored
│   ├── principles.yaml
│   │
│   ├── topologies/
│   │   └── sample_lab.yaml    # tracked; documentation-only addresses
│   │
│   ├── scenarios/
│   │   └── sample.yaml
│   │
│   └── references/
│       └── sample.yaml
│
└── docs/
    ├── architecture.md
    ├── mcp_tools.md
    ├── cli_reference.md
    └── scenario_format.md
```

## Installation model

### Supported: local editable installation

Step 1 supports exactly one deployment model: a local repository checkout,
installed with `pip install -e .`. The repository checkout owns the `lab/`
directory, and the MCP server resolves it relative to its own source
location — never relative to the current working directory of whatever
process launched it.

```bash
cd ~/work/network-lab-mcp

python3 -m venv .venv
source .venv/bin/activate

pip install -e .
```

Claude Code itself is normally started from a separate task workspace, e.g.:

```bash
cd ~/work/customer-lab-investigation
claude --permission-mode bypassPermissions
```

Network Lab MCP does not depend on that working directory. One Network Lab
MCP checkout owns exactly one lab root, and that lab root can contain
multiple topologies, scenarios, and references; `lab/settings.yaml` selects
which ones are active.

### Not supported in Step 1: non-editable / wheel installation

`pip install .`, installing from a built wheel, or `pip install
network-lab-mcp` from a package index are **not supported** in Step 1.
`lab/` is repository-local operational data, not a Python package resource,
so a non-editable install has no lab directory to find. Making that work
would require packaging changes (e.g. `importlib.resources`, an external
writable config directory, environment-variable-based lab-root overrides)
that are explicitly out of scope for Step 1. This may be reconsidered in a
later step.

## The `network-lab-mcp` command

`pyproject.toml` defines a console script:

```toml
[project.scripts]
network-lab-mcp = "network_lab_mcp.mcp_server:main"
```

Running `network-lab-mcp` starts the stdio MCP server. It takes no arguments
and does not read the current working directory to find lab data.

## Registering with Claude Code

Register the server once, from anywhere, after installing it into an
activated environment:

```bash
claude mcp add --scope user --transport stdio network-lab -- network-lab-mcp
```

(Verified against Claude Code CLI 2.1.277's `claude mcp add --help`; if a
newer Claude Code version changes this syntax, follow its own `--help`
output instead of this snippet.)

`--scope user` registers the server for the current user across all
projects. `network-lab-mcp` must be resolvable on `PATH` at the time Claude
Code launches it — for example, by installing it into an environment that is
active in your shell, or by using that environment's absolute path in place
of `network-lab-mcp`.

## Lab directory concepts and setup

- `lab/settings.example.yaml` is the tracked template.
- `lab/settings.yaml` is your local, machine-specific active selection. It is
  gitignored. Create it once:

  ```bash
  cp lab/settings.example.yaml lab/settings.yaml
  ```

- Switching the active topology, scenario, or references can be done either
  by directly editing `lab/settings.yaml`, or through the Step 2 human CLI
  (`./run_cli.sh`) described below and in
  [docs/cli_reference.md](docs/cli_reference.md). Either way, changes take
  effect on the next MCP tool call; the MCP server does not need to be
  restarted, because lab YAML is re-read from disk on every relevant call.

## Running the Human Configuration CLI

```bash
./run_cli.sh
network-lab#
```

This launches the Step 2 IOS XR-compatible CLI in the same activated
environment used for `pip install -e .` (there is no separate console
script for it; it is run as a module by `run_cli.sh`). It edits
`lab/settings.yaml` and `lab/topologies/*.yaml` directly through a
candidate/commit model — see
[Step 2 capabilities](#step-2-capabilities-the-human-configuration-cli) above
and [docs/cli_reference.md](docs/cli_reference.md) for the full command
reference. It is a separate process from `network-lab-mcp`; you can run the
CLI to change lab configuration and the MCP server (if already running for
Claude Code) will pick up a successful `commit` on its next tool call.

### Sample topology

`lab/topologies/sample_lab.yaml` is tracked in git and uses only fictional,
documentation-only addresses from the RFC 5737 `192.0.2.0/24` range. **These
addresses are not reachable and must not be used as real connectivity
targets.** The sample topology exists to validate YAML loading, MCP
structured output, and device-name/session-name mapping — not to be a real
lab.

### Topology device-name validation

Device names in a topology must map safely and unambiguously to an internal
terminal session name. `lab.py` validates, at load time, that:

- each device name is a non-empty string using a safe character set
  (letters, digits, `-`, `_`);
- no two device names can resolve to the same terminal session name.

This validation does **not** reject device names merely because they begin
with `validation-` (e.g. `validation-router` is a perfectly legitimate
device name) — see
["Structurally separate session namespaces"](#structurally-separate-session-namespaces)
below for why that is safe.

### Device type enum

A device's optional `type` field, when present, must be one of:

- `iosxr` — Cisco IOS XR
- `iosxe` — Cisco IOS XE
- `nxos` — Cisco NX-OS
- `host` — Generic host / endpoint

`lab.normalize_device_type()` is the single validation primitive for this
enum, applied both by `lab.py` at topology load/write time (so a manually
edited YAML file with an unsupported `type` is rejected) and by the Step 2
CLI's `type` argument (so `type ?`/Tab only ever offer these four values,
and an unambiguous abbreviation like `type nx` normalizes to `nxos`, or
`type h` to `host`).

This enum exists because Step 3 topology discovery will dispatch
platform-specific CDP/LLDP commands and parsers based on `device.type`:
`iosxr`/`iosxe`/`nxos` are discovery-capable, while `host` is a normal
registered topology node for which CDP/LLDP discovery is intentionally
skipped — it is not an "unsupported type" error, just a node that Step 3's
discovery pass will pass over.

### Sample scenario and references

`lab/scenarios/sample.yaml` and `lab/references/sample.yaml` are minimal
tracked examples used to validate that `get_execution_instructions()` can
load and combine principles, an active scenario, and active references. The
scenario format is intentionally not finalized in Step 1 — see
[docs/scenario_format.md](docs/scenario_format.md).

## Git safety design

This repository is meant to be shared publicly, but real topology YAML can
contain device names, management addresses, usernames, passwords, and other
private lab information. Rather than relying on documentation alone, `.gitignore`
provides a default technical guard:

- `lab/settings.yaml` (your local active selection) is gitignored.
- `lab/settings.example.yaml` is tracked.
- `lab/topologies/sample_lab.yaml` is tracked (fictional data only).
- Every other file under `lab/topologies/*.yaml` is gitignored by default.

Concretely:

```gitignore
lab/settings.yaml
lab/topologies/*.yaml
!lab/topologies/sample_lab.yaml
```

This was validated by creating `lab/topologies/private_lab.yaml` and
confirming that a plain `git add .` does not stage it, while
`lab/topologies/sample_lab.yaml` and `lab/settings.example.yaml` do get
staged normally.

This is **not** a complete security boundary — it is a default that lowers
the chance of accidentally committing real credentials or private topology
data to a public repository. Treat any topology file that leaves this
repository as sensitive regardless of what git tracks.

### Credential handling

Real topology YAML can store device usernames and passwords directly (this
is a lab tool; it does not introduce a separate secret store). Credentials
are used only to drive interactive terminal login. Network Lab MCP:

- never logs passwords or `terminal_send()` input text;
- never echoes `terminal_send()` input text back in tool responses;
- never includes credentials in generated documentation or error messages;
- never persists credentials into any separate runtime database (there isn't
  one — tmux is the only session state).

The Step 2 human CLI applies the same principle to lab configuration
editing: a device `password` is stored in plain text in topology YAML (as in
Step 1 — this is a lab tool, not a secret manager), but is never shown by
`show configuration`/`show running-config` (both render `********` in its
place), never offered as a Tab/`?` completion candidate, and never retained
in the CLI's own in-memory command history. See
[docs/cli_reference.md](docs/cli_reference.md) for details.

## Dedicated tmux environment

All Network Lab MCP terminal sessions run on a dedicated tmux server, reached
with `tmux -L network-lab-mcp ...`, separate from any tmux environment you
use interactively. This keeps `terminal_list()`/`terminal_close()` safe from
interfering with unrelated sessions, and lets Network Lab MCP configure
tmux options (like scrollback history) without touching your own tmux
setup.

### Structurally separate session namespaces

Production topology-device sessions and local validation-only sessions live
in structurally distinct namespaces, distinguished by a fixed prefix — never
by searching for the substring `validation` inside a name:

```
network-lab-device-<device-id>        production topology device sessions
network-lab-validation-<validation-id> local validation-only sessions
```

So a topology device literally named `validation-router` maps to the
production session `network-lab-device-validation-router`, which is *not*
classified as a validation session; it is exposed by `terminal_list()` like
any other device, and `terminal_close(device="validation-router")` can only
ever target that production session, never anything in the
`network-lab-validation-*` namespace.

### tmux persistence

Terminal sessions live in tmux, independent of the MCP server process. The
server does not create a session registry of its own, and it never destroys
a tmux session on exit. A restarted MCP server rediscovers and reuses any
existing managed session instead of creating a duplicate.

## stdio / stdout rule

The MCP server communicates over stdio, and stdout is reserved for MCP
protocol traffic. It never uses `print()`, prints no startup banner, and
sends all diagnostic logging to stderr.

## Local terminal validation policy

The sample topology's addresses are documentation-only and not connectivity
targets, so device-independent terminal mechanics (tmux session creation and
reuse, `terminal_send()` ordering, scrollback capture, special-key handling,
session persistence) are validated locally against a safe local process
(e.g. `cat` running in a `network-lab-validation-*` session), reusing the
exact same internal tmux session-management primitives that
`terminal_open()`/`terminal_send()`/`terminal_read()`/`terminal_list()`/
`terminal_close()` use in production. This local validation harness is
internal only: it does not add a public "shell" transport, and it does not
add a new public MCP tool.

## Current limitations

- No topology discovery (CDP/LLDP) and no topology write/delete from the CLI
  (`no topology <name>` is not implemented; topology deletion is Step 3).
- Login to a device is interactive (via `terminal_read()`/`terminal_send()`),
  not automated.
- Non-editable/wheel installation is not supported.
- Telnet transport mechanics (binary detection, command construction, launch
  inside the managed tmux path, and output visibility) were validated against
  a local port with no listening Telnet service; a live Telnet device
  interaction was not validated in this environment.
- The Step 2 CLI edits topology/settings selection and device connection
  fields only; scenario, reference, and principles *content* remain
  file-based and are not editable from the CLI (selection only).
- The case-only collision safeguard (`topology <name>`) is mandatory and
  implemented; the equivalent lightweight safeguard for `device <name>` is
  not implemented in Step 2 (device identifiers remain fully case-sensitive
  regardless).

## Future steps

- **Step 3**: topology discovery (CDP/LLDP), `show topology`, `discover
  topology`, `write topology`, `delete topology`.

## MCP SDK

Network Lab MCP uses the official
[MCP Python SDK](https://pypi.org/project/mcp/), pinned as `mcp>=2.2,<3` in
`pyproject.toml` (the current stable major version at implementation time).
It uses the SDK's `MCPServer` class (the mcp 2.x name for what was `FastMCP`
in mcp 1.x) over the `stdio` transport. See
[docs/mcp_tools.md](docs/mcp_tools.md) for the tool reference.
