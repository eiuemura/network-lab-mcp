# Network Lab MCP

Network Lab MCP provides a lightweight "AI Network Engineer Layer" designed to
maximize the reasoning capabilities of AI in network lab environments.

It is not a fixed test-automation tool. It is a thin foundation that lets
Claude Code (or any other MCP client) understand a lab's topology, follow
common operating principles, understand the current task, draw on reusable
reference knowledge, and reach lab devices through a real terminal — while
Claude Code itself does all of the network engineering reasoning.

This repository includes **Step 1** (the MCP server and terminal
foundation), **Step 2** (an IOS XR-compatible human-facing CLI, launched via
`./run_cli.sh`, for creating/editing lab definitions through a
candidate/commit model), and **Step 2.5** (separating private device access
from safe topology data, and reworking the CLI's configuration model around
that separation — see [Configuration model](#configuration-model) below).
Topology discovery (CDP/LLDP) and a `discover topology` command are still
not implemented — that is Step 3. See
[Current limitations](#current-limitations) and [Future steps](#future-steps).

## Configuration model

Five kinds of lab data are deliberately kept separate:

| Concept | Role | Exposed to Claude? |
|---------|------|---------------------|
| **running-config** | *WHICH* topology/scenario/references MCP currently uses — a **selection**, stored in `lab/settings.yaml` | Indirectly (drives which topology/scenario/references are read) |
| **access-info** | *HOW TO ACCESS* devices — private connection data (address/transport/port/username/password), `lab/access-info/*.yaml` | **Never** |
| **topology** | *WHAT EXISTS / HOW IT IS CONNECTED* — safe logical devices, device type, links, `lab/topologies/*.yaml` | Yes, via `get_active_topology()` |
| **scenario** | *WHAT TO DO* for the current task, `lab/scenarios/*.yaml` | Yes, via `get_execution_instructions()` |
| **reference** | Reusable, validated knowledge, `lab/references/*.yaml` | Yes, via `get_execution_instructions()` |

Concretely:

```
access-info  = HOW TO ACCESS DEVICES     = private, never sent to Claude
topology     = WHAT EXISTS / CONNECTIVITY = safe, sent to Claude
scenario     = WHAT TO DO                 = sent to Claude
reference    = REUSABLE KNOWLEDGE         = sent to Claude
running-config = WHICH topology/scenario/references MCP uses right now
```

`running-config` is a **selection**, not a definition: it never contains
device data itself, only the names of the topology/scenario/references
currently in effect. Topology/access-info/scenario/reference are
**definitions**: each one is a named, independently authored/edited YAML
document. Editing a definition (e.g. `topology lab1` in the CLI) never
changes which definition MCP currently uses; only committing a
`running-config` change does that. This split replaced an earlier Step 2
model where `topology <name>`/`scenario <name>`/`reference <name>` directly
changed the active selection — that old selector semantics no longer
exists anywhere in this CLI.

Also see [Responsibility model](#responsibility-model), which places these
five alongside Principles, Terminal, Claude Code, and Workspace.

## Responsibility model

| Concept        | Role                    | Meaning |
|----------------|-------------------------|---------|
| **running-config** | SELECTION | Which topology/scenario/references MCP currently uses (`lab/settings.yaml`) |
| **Topology**   | WHERE / WHAT EXISTS     | Safe logical devices, device type, and links — no private access data |
| **access-info**| PRIVATE DEVICE ACCESS   | Address/transport/port/username/password — never exposed to Claude |
| **Principles** | HOW TO BEHAVE           | Common operating rules that apply to every scenario |
| **Scenario**   | WHAT TO DO              | What must be accomplished for the current task |
| **References** | REUSABLE KNOWLEDGE      | Reusable validated guidance, operational knowledge, known values, and lab-specific know-how |
| **Terminal**   | ACTION INTERFACE        | How Claude interacts with lab devices |
| **Claude Code**| REASONING               | Determines how to accomplish the task |
| **Workspace**  | TASK ARTIFACT AREA      | Where Claude stores evidence, analysis, configurations, validation results, designs, and reports |

Network Lab MCP deliberately keeps network-engineering judgment out of the MCP
server itself. It exposes lab knowledge and terminal access; Claude Code
decides what to do with them. The Step 2 human CLI adds a further role,
**Human Configuration / Control Interface**: it creates/edits lab
definitions and the running-config selection through a candidate/commit
model, but it is not itself a network-engineering reasoning engine either.
See [docs/architecture.md](docs/architecture.md) for more detail, including
the device-access resolution flow.

## Step 1 capabilities

- A stdio MCP server (`network-lab-mcp`) exposing exactly seven tools:
  `get_active_topology`, `get_execution_instructions`, `terminal_open`,
  `terminal_send`, `terminal_read`, `terminal_list`, `terminal_close`.
- Lab data (running-config, topology, access-info, principles, scenario,
  references) loaded from YAML and re-read from disk on every relevant tool
  call — no server restart needed after a CLI `commit`.
- A dedicated tmux environment (separate socket) used as the source of truth
  for terminal session lifetime, structurally separating production device
  sessions from local validation-only sessions.
- SSH and Telnet access to lab devices via the operating system's own `ssh`
  and `telnet` binaries, driven the way a human would: read the terminal,
  decide what to send, send it.

## Step 2 / 2.5 capabilities: the Human Configuration CLI

`./run_cli.sh` launches an interactive, IOS XR-compatible CLI for
creating/editing lab **definitions** (topology, access-info, scenario,
reference) and the **running-config selection**, all through a
**candidate -> commit** model — the same responsibility split IOS XR uses
for its own configuration mode. It is a human configuration/control plane,
not a network-engineering reasoning engine, and it never talks the MCP
stdio protocol.

- **Modes**: EXEC (`network-lab#`), global configuration
  (`network-lab(config)#`), running-config selection
  (`network-lab(config-running)#`), topology definition
  (`network-lab(config-topology-<name>)#` / nested
  `network-lab(config-device-<name>)#` for safe device metadata),
  access-info definition (`network-lab(config-access-info-<name>)#` /
  nested `network-lab(config-access-device-<name>)#` for private connection
  fields), scenario definition (`network-lab(config-scenario-<name>)#`),
  and reference definition (`network-lab(config-reference-<name>)#`). See
  [docs/cli_reference.md](docs/cli_reference.md) for the full mode/command
  reference.
- **`topology`/`access-info`/`scenario`/`reference <name>` create or edit a
  definition** — an existing name loads it as a candidate, a new name
  starts a fresh one; neither ever changes what MCP currently uses. Only
  `running-config` mode's `topology`/`scenario`/`reference <name>` (and `no
  reference <name>`) change the running-config candidate's selection.
- **Candidate configuration**: entering `configure` snapshots committed
  running-config into a settings candidate; opening a definition
  (topology/access-info/scenario/reference) loads (or creates) a definition
  candidate. At most one definition is open at a time — opening a different
  one while the current one is dirty is blocked, mirroring the topology
  case-only collision safeguard's caution around implicit creation. Nothing
  is written to disk until `commit`.
- **`clear` replaces `abort`**: discards every uncommitted change in the
  current configure session — the running-config candidate and the open
  definition candidate together — restoring committed state, without
  returning to EXEC. If the definition being cleared was brand new (never
  committed), it is discarded entirely rather than reset to empty, and the
  CLI steps back to the nearest still-valid parent mode if the current
  submode's target no longer exists after the revert.
- **External YAML editor** (`edit`, in topology/scenario/reference
  definition mode): opens the candidate in `$VISUAL`, then `$EDITOR`, then a
  `vim` fallback, via a secure temporary `.yaml` file — never the committed
  file directly. A non-zero editor exit or invalid YAML leaves the
  candidate untouched; `commit` is still required to persist the edit.
  access-info uses structured CLI editing only in this phase (no `edit`).
- **IOS XR-style interaction**, all driven by one command grammar (the single
  source of truth in `cli/grammar.py`, see
  [docs/cli_reference.md](docs/cli_reference.md) for the full reference):
  unique fixed-keyword abbreviation, Tab/Ctrl-I completion, context-sensitive
  `?` (bare, partial-token, and next-token forms, including "select or
  create" identifiers that list existing names alongside a creation hint,
  and a `<cr>` marker), ambiguous/incomplete/invalid-input detection with an
  IOS XR-style caret, in-process command history (never written to disk,
  and a password-setting command is never retained in it even in memory),
  IOS XR-style line editing, and safe Ctrl-C (cancels only the current input
  line) / Ctrl-D (EOF; blocked while uncommitted changes exist) behavior.
  Repeated `?` leaves a `prompt + buffer + ?` transcript line in scrollback
  before each help block, matching a real terminal.
- **Fixed CLI keywords are case-insensitive**; **object identifiers —
  topology, scenario, reference, access-info, and device names — are
  case-sensitive** and are never silently case-folded, including in dynamic
  completion.
- **Case-only topology-name collision safeguard**: if `topology <name>` does
  not exactly match an existing topology but differs from one only by
  letter case, the CLI asks for explicit confirmation before creating a
  distinct topology, rather than silently opening the existing one or
  silently creating a look-alike. This is a narrow safety check, not fuzzy
  name matching.
- **Step 1 validator reuse**: topology/access-info/device.type validation on
  commit reuses `network_lab_mcp.lab.validate_topology_data()` /
  `validate_access_info_data()` / `normalize_device_type()` — the CLI does
  not maintain a duplicate set of validation rules.
- **Minimal, targeted writes**: `commit` writes only the definition file(s)
  that actually changed semantically, then `lab/settings.yaml` last (since a
  running-config selection may point at a definition just created in the
  same commit); an unchanged definition that was merely opened is never
  rewritten, and a no-op commit writes nothing at all.
- **Password safety**: passwords are stored in access-info YAML in plain
  text (this is a lab tool, not a secret manager) but are never shown by
  `show configuration`/`show running-config`, never offered as a completion
  candidate, and never retained in this process's in-memory history.
- **Committed-state boundary**: the MCP server only ever reads committed
  `lab/settings.yaml`, `lab/topologies/*.yaml`, and (indirectly, for
  terminal access) `lab/access-info/*.yaml`; candidate configuration is
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
ssh / telnet                              |  (running-config, topology,
    |                                     |   access-info, scenario, reference)
Lab Devices                          (read by Network Lab MCP above)
```

See [docs/architecture.md](docs/architecture.md) for the full picture,
including how running-config, access-info, topology, Principles, Scenario,
References, Terminal, Claude Code, and Workspace relate to each other, the
device-access resolution flow `terminal_open()` follows, how the production
and validation terminal session namespaces are kept structurally separate,
and how the CLI's candidate/commit model relates to the committed-state
boundary the MCP server reads from.

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
│       ├── lab.py             # running-config/topology/access-info/scenario/reference loading
│       ├── terminal.py        # tmux session management, ssh/telnet launch
│       │
│       └── cli/                       # Step 2 human-facing CLI
│           ├── __init__.py
│           ├── main.py                # REPL, prompt rendering, key bindings, dispatch
│           ├── config.py              # candidate configuration, dirty state, commit/clear
│           ├── grammar.py             # command grammar single source of truth
│           └── editor.py              # external ($VISUAL/$EDITOR/vim) YAML editor support
│
├── lab/
│   ├── settings.example.yaml  # tracked template (running-config)
│   ├── settings.yaml          # local only, gitignored (running-config)
│   ├── principles.yaml
│   │
│   ├── access-info/
│   │   └── sample_lab.yaml    # tracked; fictional sample only
│   │
│   ├── topologies/
│   │   └── sample_lab.yaml    # tracked; safe logical data, documentation-only addresses
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

`lab/access-info/sample_lab.yaml` and `lab/topologies/sample_lab.yaml`
share a basename purely as a sample convenience — see
["Access-info and topology filenames are not linked"](#access-info-and-topology-filenames-are-not-linked).

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
multiple topologies, access-info definitions, scenarios, and references;
`lab/settings.yaml` (running-config) selects which topology/scenario/
references are currently in effect.

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

- `lab/settings.example.yaml` is the tracked template for running-config.
- `lab/settings.yaml` is your local, machine-specific running-config
  selection. It is gitignored. Create it once:

  ```bash
  cp lab/settings.example.yaml lab/settings.yaml
  ```

- Changing which topology/scenario/references MCP uses, or
  creating/editing topology/access-info/scenario/reference definitions, can
  be done either by directly editing the corresponding YAML files, or
  through the Step 2 human CLI (`./run_cli.sh`) described below and in
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
`lab/settings.yaml` (running-config) and topology/access-info/scenario/
reference definition YAML directly through a candidate/commit model — see
[Step 2 / 2.5 capabilities](#step-2--25-capabilities-the-human-configuration-cli)
above and [docs/cli_reference.md](docs/cli_reference.md) for the full
command reference. It is a separate process from `network-lab-mcp`; you can
run the CLI to change lab configuration and the MCP server (if already
running for Claude Code) will pick up a successful `commit` on its next
tool call.

### Sample topology and access-info

`lab/topologies/sample_lab.yaml` and `lab/access-info/sample_lab.yaml` are
tracked in git. The topology holds only safe logical data (devices, device
type, links); the access-info definition holds the matching fictional
connection data, using only documentation-only addresses from the RFC 5737
`192.0.2.0/24` range. **These addresses are not reachable and must not be
used as real connectivity targets.** The samples exist to validate YAML
loading, MCP structured output, device-access resolution, and
device-name/session-name mapping — not to be a real lab.

### Access-info and topology filenames are not linked

`lab/access-info/sample_lab.yaml` and `lab/topologies/sample_lab.yaml`
sharing a basename is a sample convenience, **not** an association
mechanism. Network Lab MCP never infers "this access-info file belongs to
this topology" from matching filenames — see
["Temporary limitation: global device-ID uniqueness"](#temporary-limitation-global-device-id-uniqueness)
below for how a device's access information is actually resolved in this
phase, and why that lookup is deliberately not yet scoped by topology.

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

### Topology holds only safe logical data

Topology YAML may contain `name`, `description`, `devices` (each with an
optional `type`), and `links` — nothing else. `address`, `transport`,
`port`, `username`, and `password` are rejected outright by
`lab.validate_topology_data()` if present, whether the file was written by
the CLI or edited by hand: those fields belong in an access-info
definition instead, because topology is exposed to Claude via
`get_active_topology()` and access-info never is.

### Device type enum

A device's optional `type` field, when present (in either topology or
access-info), must be one of:

- `iosxr` — Cisco IOS XR
- `iosxe` — Cisco IOS XE
- `nxos` — Cisco NX-OS
- `host` — Generic host / endpoint

`lab.normalize_device_type()` is the single validation primitive for this
enum, applied by `lab.py` at topology and access-info load/write time (so a
manually edited YAML file with an unsupported `type` is rejected in either
place) and by the Step 2 CLI's `type` argument under both the topology
device submode and the access-info device submode (so `type ?`/Tab only
ever offer these four values, and an unambiguous abbreviation like `type
nx` normalizes to `nxos`, or `type h` to `host`).

This enum exists because Step 3 topology discovery will dispatch
platform-specific CDP/LLDP commands and parsers based on `device.type`:
`iosxr`/`iosxe`/`nxos` are discovery-capable, while `host` is a normal
registered topology node for which CDP/LLDP discovery is intentionally
skipped — it is not an "unsupported type" error, just a node that Step 3's
discovery pass will pass over.

Topology and access-info store `type` independently (they are separate
files, possibly authored at different times); terminal access validates
them against each other only when both are present — see
["Topology/access-info device.type consistency"](#topologyaccess-info-devicetype-consistency)
below.

### Sample scenario and references

`lab/scenarios/sample.yaml` and `lab/references/sample.yaml` are minimal
tracked examples used to validate that `get_execution_instructions()` can
load and combine principles, an active scenario, and active references. The
scenario format is intentionally not finalized in Step 1 — see
[docs/scenario_format.md](docs/scenario_format.md). Both can be
authored/edited from the CLI via `scenario <name>` / `reference <name>` and
`edit`.

## Git safety design

This repository is meant to be shared publicly, but real access-info YAML
contains device names, management addresses, usernames, passwords, and
other private lab information. Rather than relying on documentation alone,
`.gitignore` provides a default technical guard:

- `lab/settings.yaml` (your local running-config selection) is gitignored.
- `lab/settings.example.yaml` is tracked.
- `lab/topologies/sample_lab.yaml` is tracked (safe logical data only).
- `lab/access-info/sample_lab.yaml` is tracked (fictional data only).
- Every other file under `lab/topologies/*.yaml` and `lab/access-info/*.yaml`
  is gitignored by default.

Concretely:

```gitignore
lab/settings.yaml
lab/topologies/*.yaml
!lab/topologies/sample_lab.yaml
lab/access-info/*.yaml
!lab/access-info/sample_lab.yaml
```

This was validated by creating `lab/access-info/private_lab.yaml` and
`lab/topologies/private_lab.yaml` and confirming that a plain `git add .`
does not stage either, while `lab/access-info/sample_lab.yaml`,
`lab/topologies/sample_lab.yaml`, and `lab/settings.example.yaml` do get
staged normally.

This is **not** a complete security boundary — it is a default that lowers
the chance of accidentally committing real credentials or private lab data
to a public repository. Treat any access-info file that leaves this
repository as sensitive regardless of what git tracks. Topology YAML no
longer carries private access fields at all, so it is a much lower-risk
file even before considering `.gitignore`.

### Credential handling

Real access-info YAML stores device usernames and passwords directly (this
is a lab tool; it does not introduce a separate secret store). Credentials
are used only to drive interactive terminal login. Network Lab MCP:

- never logs passwords or `terminal_send()` input text;
- never echoes `terminal_send()` input text back in tool responses;
- never includes credentials in generated documentation or error messages
  (including the device-access-ambiguity and type-mismatch errors below);
- never persists credentials into any separate runtime database (there isn't
  one — tmux is the only session state);
- never returns access-info from any MCP tool: `get_active_topology()`
  returns only the safe topology, and `get_execution_instructions()` never
  includes it either.

The Step 2 human CLI applies the same principle to lab configuration
editing: a device `password` (entered in access-info's device submode) is
stored in plain text in access-info YAML (as in Step 1 — this is a lab
tool, not a secret manager), but is never shown by `show
configuration`/`show running-config` (both render `********` in its
place), never offered as a Tab/`?` completion candidate, and never retained
in the CLI's own in-memory command history. External-editor support
(`edit`) is not offered for access-info in this phase, precisely to keep
password entry on the structured, masking-aware path. See
[docs/cli_reference.md](docs/cli_reference.md) for details.

## Device access resolution (temporary, Step 2.5)

`terminal_open(device)` receives only a logical device name from Claude —
never an address, username, or password. Network Lab MCP resolves the
private connection details itself:

1. Verify the device exists in the active topology.
2. Search every committed `lab/access-info/*.yaml` definition for an exact
   device-ID match.
3. Exactly one match -> continue; zero or multiple matches -> **fail
   closed** (see below) before any connection is attempted.
4. If both the topology and the resolved access-info specify `type`,
   normalize both through the shared `DEVICE_TYPES` enum and compare —
   **fail closed** on a mismatch.
5. Only then does the existing tmux/ssh/telnet path run.

### Temporary limitation: global device-ID uniqueness

In this phase, device identifiers must be unique across all
`lab/access-info/*.yaml` files, because access-info lookup is not yet
scoped by topology. Reusing a device identifier across multiple access-info
definitions causes `terminal_open()` to fail closed with an ambiguity
error, **regardless of which topology is active** — the active topology
never breaks the tie, and neither does a matching filename, edit recency,
or alphabetical order. Concretely:

```
lab/access-info/lab_a.yaml   R1
lab/access-info/lab_b.yaml   R1
```

`terminal_open("R1")` fails closed with `% Access information for device
'R1' is ambiguous.` no matter which topology is active. This is a
deliberate, temporary constraint — not an oversight — until topology-scoped
access-info association is designed in Step 3 (see
[Future steps](#future-steps)). No filename-based association,
topology-to-access-info mapping, access-profile framework, or fuzzy
matching has been introduced to work around it in this phase.

### Topology/access-info device.type consistency

Topology and access-info are independent, separately authored definitions,
so nothing stops them from disagreeing about the same device's platform.
`terminal_open()` performs a lightweight runtime check when resolving a
device — not a general cross-file consistency framework — and fails closed
if both sides specify `type` and, once normalized through the shared
`DEVICE_TYPES` enum, they disagree:

```
% Device type mismatch for 'R1' between topology and access information.
```

If either side leaves `type` unset (already allowed), there is nothing to
compare and resolution proceeds normally — this check does not make `type`
mandatory anywhere it previously was not. Credential values are never
included in this error.

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

- No topology discovery (CDP/LLDP) and no `discover topology`/topology
  deletion from the CLI (`no topology <name>` is not implemented; both are
  Step 3).
- Login to a device is interactive (via `terminal_read()`/`terminal_send()`),
  not automated.
- Non-editable/wheel installation is not supported.
- Telnet transport mechanics (binary detection, command construction, launch
  inside the managed tmux path, and output visibility) were validated against
  a local port with no listening Telnet service; a live Telnet device
  interaction was not validated in this environment.
- Device access resolution is **not yet scoped by topology** — see
  ["Temporary limitation: global device-ID uniqueness"](#temporary-limitation-global-device-id-uniqueness).
  This is the main Step 2.5 limitation Step 3 is expected to resolve.
- access-info has no external-editor support in this phase (structured CLI
  editing only), unlike topology/scenario/reference.
- The case-only collision safeguard (`topology <name>`) is mandatory and
  implemented; the equivalent lightweight safeguard for `device <name>` (or
  for access-info/scenario/reference names) is not implemented (identifiers
  remain fully case-sensitive regardless).
- Scenario/reference schema is intentionally not fixed yet — only "valid
  YAML, root is a mapping" is enforced (see
  [docs/scenario_format.md](docs/scenario_format.md)).

## Future steps

- **Step 3**: topology discovery (CDP/LLDP), `discover topology`, and
  topology-scoped access-info association (removing the temporary global
  device-ID-uniqueness limitation above). Conceptually:

  ```
  access-info -> device access -> CDP / LLDP -> type-specific parser
      -> normalized observations -> topology candidate
  ```

  Topology will then have three paths to the same candidate/model: the
  structured CLI, an external YAML editor, and discovery. This repository
  implements only the first two so far.

## MCP SDK

Network Lab MCP uses the official
[MCP Python SDK](https://pypi.org/project/mcp/), pinned as `mcp>=2.2,<3` in
`pyproject.toml` (the current stable major version at implementation time).
It uses the SDK's `MCPServer` class (the mcp 2.x name for what was `FastMCP`
in mcp 1.x) over the `stdio` transport. See
[docs/mcp_tools.md](docs/mcp_tools.md) for the tool reference.
